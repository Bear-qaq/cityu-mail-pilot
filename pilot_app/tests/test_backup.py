"""Tests for backup retention, the offsite push, and the freshness alerts.

The retention tests exist because of a bug that had already fired in production:
retention used to be "the newest seven", and the pre-upgrade backup runs through
the same function, so one afternoon of deploys rotated out every daily copy. The
directory really did hold seven backups from the same afternoon.

The offsite tests talk to a real HTTP server on a loopback port rather than
mocking the request. Signing and encoding a PUT is the part that goes wrong, and
a mock would agree with whatever the code does.
"""

import base64
import datetime as dt
import gzip
import http.server
import io
import json
import os
import pathlib
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from pilot_app import alerting, backup


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.now = dt.datetime(2026, 9, 14, 12, 0, tzinfo=dt.timezone.utc)

    def make(self, name: str, *, days_old: float):
        path = self.dir / name
        path.write_bytes(b"x")
        moment = (self.now - dt.timedelta(days=days_old)).timestamp()
        os.utime(path, (moment, moment))
        return path

    def test_copies_inside_the_window_are_kept(self):
        for index in range(3):
            self.make(f"pilot-2026091{index}T030000Z.sqlite3", days_old=index)
        removed = backup.prune(self.dir, now=self.now)
        self.assertEqual(removed, [])
        self.assertEqual(len(list(self.dir.glob("pilot-*.sqlite3"))), 3)

    def test_copies_older_than_the_window_are_removed(self):
        """Needs more than the floor's worth of fresh copies, or nothing is old."""
        for index in range(backup.BACKUP_KEEP_MIN + 1):
            self.make(f"pilot-202609{10 + index:02d}T030000Z.sqlite3", days_old=index)
        old = self.make("pilot-20260801T030000Z.sqlite3", days_old=44)
        backup.prune(self.dir, now=self.now)
        self.assertFalse(old.exists())
        self.assertEqual(len(list(self.dir.glob("pilot-*.sqlite3"))), backup.BACKUP_KEEP_MIN + 1)

    def test_a_run_of_deploys_cannot_evict_the_daily_chain(self):
        """The exact production incident: same afternoon, seven copies, no dailies.

        Age-based retention keeps the older daily even when newer copies exist,
        which is the whole point of the change.
        """
        daily = self.make("pilot-20260913T032000Z.sqlite3", days_old=1)
        for minute in range(7):
            self.make(f"pilot-20260914T15{minute}00Z.sqlite3", days_old=minute / 1440)
        backup.prune(self.dir, now=self.now)
        self.assertTrue(daily.exists(), "一整天的部署不该把前一天的日报备份挤掉")

    def test_a_few_old_copies_are_never_all_deleted(self):
        """The floor is a floor, not a target: it cannot invent the missing ones.

        A machine that was off for a month must not come back, run one backup and
        delete its only remaining copies for being old.
        """
        for index in range(3):
            self.make(f"pilot-2026010{index}T030000Z.sqlite3", days_old=100 + index)
        removed = backup.prune(self.dir, now=self.now)
        self.assertEqual(removed, [])
        self.assertEqual(len(list(self.dir.glob("pilot-*.sqlite3"))), 3)

    def test_the_ceiling_caps_a_large_history(self):
        for index in range(backup.BACKUP_KEEP_MAX + 5):
            self.make(f"pilot-20260914T{index:06d}Z.sqlite3", days_old=0)
        backup.prune(self.dir, now=self.now)
        self.assertLessEqual(len(list(self.dir.glob("pilot-*.sqlite3"))), backup.BACKUP_KEEP_MAX)

    def test_a_fresh_backup_is_a_readable_database(self):
        source = self.dir / "pilot.sqlite3"
        connection = sqlite3.connect(source)
        connection.execute("CREATE TABLE t(x)")
        connection.execute("INSERT INTO t VALUES(42)")
        connection.commit()
        connection.close()
        destination = backup.create_backup(source, self.dir / "copies")
        copied = sqlite3.connect(destination)
        self.assertEqual(copied.execute("SELECT x FROM t").fetchone()[0], 42)
        copied.close()
        mode = destination.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, oct(mode))


class StubWebDAV:
    """A real HTTP server that accepts PUT, so the request is actually encoded."""

    def __init__(self, *, status=201):
        self.status = status
        self.received: dict[str, bytes] = {}
        self.headers: list[dict[str, str]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_PUT(self):  # noqa: N802 - http.server's naming
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                outer.received[self.path] = body
                outer.headers.append({k.lower(): v for k, v in self.headers.items()})
                self.send_response(outer.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):  # keep the test output readable
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/dav"
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class OffsitePushTests(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.database = self.dir / "pilot-20260914T030000Z.sqlite3"
        self.database.write_bytes(b"SQLite format 3\x00" + b"payload" * 100)
        self.saved = {name: os.environ.get(name) for name in (
            backup.WEBDAV_URL_ENV, backup.WEBDAV_USER_ENV, backup.WEBDAV_PASSWORD_ENV)}
        for name in self.saved:
            os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_without_a_target_nothing_is_attempted(self):
        result = backup.push_offsite(self.database)
        self.assertFalse(result["configured"])
        self.assertFalse(result["ok"])

    def test_a_configured_target_receives_both_rolling_names(self):
        with StubWebDAV() as stub:
            os.environ[backup.WEBDAV_URL_ENV] = stub.url
            result = backup.push_offsite(self.database,
                                         now=dt.datetime(2026, 9, 14, tzinfo=dt.timezone.utc))
        self.assertTrue(result["ok"], result)
        self.assertIn("/dav/pilot-14.sqlite3.gz", stub.received)
        self.assertIn("/dav/pilot-latest.sqlite3.gz", stub.received)

    def test_what_arrives_is_the_database(self):
        with StubWebDAV() as stub:
            os.environ[backup.WEBDAV_URL_ENV] = stub.url
            backup.push_offsite(self.database)
        uploaded = gzip.decompress(stub.received["/dav/pilot-latest.sqlite3.gz"])
        self.assertEqual(uploaded, self.database.read_bytes())

    def test_credentials_travel_in_the_authorization_header(self):
        with StubWebDAV() as stub:
            os.environ[backup.WEBDAV_URL_ENV] = stub.url
            os.environ[backup.WEBDAV_USER_ENV] = "operator@example.com"
            os.environ[backup.WEBDAV_PASSWORD_ENV] = "app-password-123456"
            backup.push_offsite(self.database)
        header = stub.headers[0]["authorization"]
        self.assertTrue(header.startswith("Basic "))
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
        self.assertEqual(decoded, "operator@example.com:app-password-123456")

    def test_a_refused_upload_is_reported_not_raised(self):
        with StubWebDAV(status=507) as stub:
            os.environ[backup.WEBDAV_URL_ENV] = stub.url
            result = backup.push_offsite(self.database)
        self.assertFalse(result["ok"])
        self.assertIn("507", result["error"])

    def test_an_unreachable_host_is_reported_not_raised(self):
        os.environ[backup.WEBDAV_URL_ENV] = "http://127.0.0.1:9/dav"
        result = backup.push_offsite(self.database)
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    def test_the_password_is_never_in_the_reported_target(self):
        """Includes the credentials-in-URL form, which is a common provider shape."""
        safe = backup.safe_url("https://user:secret@dav.example.com/x?token=abc")
        self.assertNotIn("secret", safe)
        self.assertNotIn("token=abc", safe)
        self.assertIn("dav.example.com", safe)
        os.environ[backup.WEBDAV_URL_ENV] = "https://user:secret@dav.example.com/x?token=abc"
        result = backup.push_offsite(self.database, dry_run=True)
        self.assertNotIn("secret", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("token=abc", json.dumps(result, ensure_ascii=False))

    def test_credentials_embedded_in_the_url_become_a_header(self):
        """urllib refuses such a URL, so it has to be split before use."""
        os.environ[backup.WEBDAV_URL_ENV] = "https://me:app-pass-123@dav.example.com/dav"
        config = backup.webdav_config()
        self.assertEqual(config["url"], "https://dav.example.com/dav")
        self.assertEqual(config["user"], "me")
        self.assertEqual(config["password"], "app-pass-123")
        with StubWebDAV() as stub:
            os.environ[backup.WEBDAV_URL_ENV] = f"http://me:pw@127.0.0.1:{stub.server.server_address[1]}/dav"
            result = backup.push_offsite(self.database)
        self.assertTrue(result["ok"], result)
        self.assertIn("/dav/pilot-latest.sqlite3.gz", stub.received)

    def test_a_dry_run_reaches_no_server(self):
        with StubWebDAV() as stub:
            os.environ[backup.WEBDAV_URL_ENV] = stub.url
            result = backup.push_offsite(self.database, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(stub.received, {})

    def test_the_state_file_records_what_happened(self):
        with StubWebDAV() as stub:
            os.environ[backup.WEBDAV_URL_ENV] = stub.url
            backup.write_offsite_state(self.dir, backup.push_offsite(self.database))
        state = backup.read_offsite_state(self.dir)
        self.assertTrue(state["ok"])
        self.assertEqual(state["target"], stub.url)

    def test_a_missing_state_file_is_not_an_error(self):
        self.assertIsNone(backup.read_offsite_state(self.dir))


class BackupAlertTests(unittest.TestCase):
    """The sentinel notices the one failure that looks like nothing at all."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())

    def findings(self, **kwargs):
        # A real-looking empty database: `evaluate` iterates both of these, and a
        # bare MagicMock would raise instead of reporting "no users".
        database = mock.MagicMock()
        database.list_users_overview.return_value = []
        database.stalled_setups.return_value = []
        # Injected rather than set through the environment: `evaluate` reads the
        # backup directory now, and a unit test must not depend on what happens
        # to exist at /var/backups on the machine running it.
        return alerting.evaluate(database, disk_percent=1.0, certificate_days=365,
                                 backup_dir=self.dir, **kwargs)

    def make_backup(self, *, hours_old: float):
        path = self.dir / "pilot-20260914T030000Z.sqlite3"
        path.write_bytes(b"x")
        moment = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(hours=hours_old)).timestamp()
        os.utime(path, (moment, moment))
        return path

    def test_no_backups_at_all_is_critical(self):
        keys = {item["key"]: item for item in self.findings()}
        self.assertIn("backup_missing", keys)
        self.assertEqual(keys["backup_missing"]["severity"], "critical")

    def test_a_stale_backup_is_critical_and_explains_the_onfailure_gap(self):
        self.make_backup(hours_old=alerting.ALERT_BACKUP_HOURS + 5)
        keys = {item["key"]: item for item in self.findings()}
        self.assertIn("backup_stale", keys)
        self.assertEqual(keys["backup_stale"]["severity"], "critical")
        self.assertIn("根本没跑", keys["backup_stale"]["detail"])

    def test_a_fresh_backup_produces_no_finding_at_all(self):
        """A healthy check is silent. For a while this one was not.

        A fresh copy used to emit a "备份正常" finding at severity `info`, and it
        behaved exactly like an alert: the detail carried the copy's age in whole
        hours, and `_should_send` deliberately re-mails a finding whose detail
        changed -- so the operator got a cheerful e-mail every hour. The previous
        version of this test only asserted the severity, which is why it passed.
        Silence is the property worth pinning.
        """
        self.make_backup(hours_old=1)
        keys = {item["key"]: item for item in self.findings()}
        self.assertNotIn("backup_stale", keys, "备份正常不该产生一条 finding")
        self.assertNotIn("backup_missing", keys)
        self.assertEqual(keys, {}, "健康时哨兵对备份这件事应该完全沉默")

    def test_a_failed_offsite_push_is_surfaced(self):
        backup.write_offsite_state(self.dir, {
            "configured": True, "ok": False, "error": "URLError: 连接超时",
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        })
        keys = {item["key"]: item for item in self.findings()}
        self.assertIn("offsite_failed", keys)
        self.assertIn("连接超时", keys["offsite_failed"]["detail"])

    def test_an_offsite_push_that_stopped_is_surfaced(self):
        long_ago = (dt.datetime.now(dt.timezone.utc)
                    - dt.timedelta(hours=alerting.ALERT_OFFSITE_HOURS + 5))
        backup.write_offsite_state(self.dir, {
            "configured": True, "ok": True, "bytes": 100,
            "at": long_ago.isoformat(timespec="seconds"),
        })
        keys = {item["key"]: item for item in self.findings()}
        self.assertIn("offsite_stale", keys)

    def test_an_unconfigured_offsite_target_is_not_an_alert(self):
        """The self-hosted default is "no third party", and that is not a fault."""
        self.make_backup(hours_old=1)
        keys = {item["key"] for item in self.findings()}
        self.assertNotIn("offsite_failed", keys)
        self.assertNotIn("offsite_stale", keys)

    def test_an_unreadable_backup_directory_says_nothing(self):
        """`/var/backups` is root-only on macOS; the sentinel must survive it.

        Reporting "no backups" there would fire on every developer machine, and
        raising would take the whole sentinel down -- including the checks that
        have nothing to do with backups.
        """
        database = mock.MagicMock()
        database.list_users_overview.return_value = []
        database.stalled_setups.return_value = []
        findings = alerting.evaluate(database, disk_percent=1.0, certificate_days=365,
                                    backup_dir=pathlib.Path("/var/backups/does-not-exist"))
        keys = {item["key"] for item in findings}
        self.assertNotIn("backup_missing", keys)
        self.assertNotIn("backup_stale", keys)

    def test_backup_alerts_do_not_repeat_every_six_hours(self):
        self.assertEqual(alerting._repeat_for("backup_stale"),
                         alerting.ALERT_BACKUP_REPEAT_SECONDS)
        self.assertGreaterEqual(alerting.ALERT_BACKUP_REPEAT_SECONDS, 24 * 3600)


if __name__ == "__main__":
    unittest.main()


class MasterKeyFingerprintTests(unittest.TestCase):
    """Identifying the key without ever showing it.

    The point of the fingerprint is that an operator compares twelve characters
    read aloud against the copy in their password manager. That only works if one
    key has exactly one fingerprint -- the first version hashed whatever form it was
    handed, so the base64 text in `pilot.env` and the decoded bytes inside
    `SecretBox` produced *different* strings for the same key.
    """

    KEY_B64 = base64.urlsafe_b64encode(bytes(range(32))).decode()
    OTHER_B64 = base64.urlsafe_b64encode(bytes(range(1, 33))).decode()

    def test_the_same_key_has_one_fingerprint_whichever_form_it_arrives_in(self):
        from pilot_app.security import SecretBox, key_fingerprint
        from_base64 = key_fingerprint(self.KEY_B64)
        from_bytes = key_fingerprint(SecretBox.from_base64(self.KEY_B64).key)
        from_box = SecretBox.from_base64(self.KEY_B64).fingerprint()
        self.assertEqual(from_base64, from_bytes)
        self.assertEqual(from_base64, from_box)

    def test_different_keys_do_not_collide(self):
        from pilot_app.security import key_fingerprint
        self.assertNotEqual(key_fingerprint(self.KEY_B64), key_fingerprint(self.OTHER_B64))

    def test_the_format_survives_being_copied_off_paper(self):
        from pilot_app.security import key_fingerprint
        value = key_fingerprint(self.KEY_B64)
        self.assertRegex(value, r"^[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}$")
        # The digits 0, 1, 8 and 9 cannot appear in base32. That is what makes the
        # letters O and I safe to write down: the digits they are confused with
        # are absent. (O and I themselves DO appear -- the first version of this
        # test asserted otherwise and failed, which is the test doing its job.)
        for absent_digit in "0189":
            self.assertNotIn(absent_digit, value)

    def test_it_does_not_contain_the_key(self):
        from pilot_app.security import key_fingerprint
        value = key_fingerprint(self.KEY_B64)
        self.assertNotIn(self.KEY_B64, value)
        self.assertNotIn(self.KEY_B64[:12], value)

    def test_check_prints_the_fingerprint_of_the_live_key(self):
        from pilot_app.security import key_fingerprint
        directory = pathlib.Path(tempfile.mkdtemp())
        env_file = directory / "pilot.env"
        env_file.write_text(f"INFE_PILOT_MASTER_KEY={self.KEY_B64}\n", encoding="utf-8")
        saved = os.environ.pop("INFE_PILOT_MASTER_KEY", None)
        try:
            buffer = io.StringIO()
            with mock.patch("sys.stdout", buffer):
                backup.check(directory, True, env_file=env_file)
        finally:
            if saved is not None:
                os.environ["INFE_PILOT_MASTER_KEY"] = saved
        text = buffer.getvalue()
        self.assertIn(key_fingerprint(self.KEY_B64), text)
        self.assertNotIn(self.KEY_B64, text, "--check 绝不能打印密钥本身")

    def test_a_missing_or_broken_key_does_not_break_the_report(self):
        """A fingerprint must never be the reason `--check` fails."""
        directory = pathlib.Path(tempfile.mkdtemp())
        env_file = directory / "pilot.env"
        # Deliberately too short, and containing a character base64 cannot use:
        # the release scanner looks for INFE_PILOT_MASTER_KEY=<16+ key characters>
        # and flagged the longer-looking placeholder. A plainly-not-a-key fixture
        # beats exempting the whole file from the scan.
        env_file.write_text("INFE_PILOT_MASTER_KEY=broken!\n", encoding="utf-8")
        saved = os.environ.pop("INFE_PILOT_MASTER_KEY", None)
        try:
            buffer = io.StringIO()
            with mock.patch("sys.stdout", buffer):
                code = backup.check(directory, True, env_file=env_file)
        finally:
            if saved is not None:
                os.environ["INFE_PILOT_MASTER_KEY"] = saved
        self.assertIn("本地备份", buffer.getvalue())
        self.assertEqual(code, 1, "没有备份仍然是需要处理的状态")


class SetBackupTargetScriptTests(unittest.TestCase):
    """The installer, exercised as an operator runs it.

    Same contract as `set_platform_key.sh`: the secret is read with `read -s` so it
    never reaches argv, the shell history or a transcript, the env file is rewritten
    without `sed` (a password containing `/`, `&` or `\\` would break the expression),
    and every other line in the file survives.
    """

    SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "set_backup_target.sh"
    BASE_ENV = ("INFE_PILOT_DB=/var/lib/cityu-mail-pilot/pilot.sqlite3\n"
                "INFE_PILOT_MASTER_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n"
                "INFE_PILOT_ADMIN_EMAILS=boss@example.com\n")

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.env_file = self.dir / "pilot.env"
        self.env_file.write_text(self.BASE_ENV, encoding="utf-8")
        self.env_file.chmod(0o600)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_script(self, *args, stdin=""):
        environment = dict(os.environ)
        environment["INFE_PILOT_PREDEPLOY_DIR"] = str(self.dir / "backups")
        environment["LC_CTYPE"] = "C"
        done = subprocess.run(
            ["bash", str(self.SCRIPT), "--env-file", str(self.env_file),
             "--no-verify", *args],
            input=stdin.encode("utf-8"), capture_output=True, env=environment, timeout=60,
        )
        return subprocess.CompletedProcess(
            done.args, done.returncode,
            done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace"))

    def text(self):
        return self.env_file.read_text(encoding="utf-8")

    def test_the_three_variables_are_written_and_nothing_else_changes(self):
        result = self.run_script("--url", "https://app.koofr.net/dav/Koofr",
                                 "--user", "me@example.com", "--stdin",
                                 stdin="app-password-123456\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INFE_PILOT_BACKUP_WEBDAV_URL=https://app.koofr.net/dav/Koofr", self.text())
        self.assertIn("INFE_PILOT_BACKUP_WEBDAV_USER=me@example.com", self.text())
        self.assertIn("INFE_PILOT_BACKUP_WEBDAV_PASSWORD=app-password-123456", self.text())
        self.assertIn("INFE_PILOT_MASTER_KEY=", self.text(), "主密钥被弄丢了")

    def test_shell_metacharacters_in_the_password_survive_verbatim(self):
        tricky = "app-pass/with&and=eq\\ual"
        result = self.run_script("--url", "https://dav.example.com/x", "--user", "u@e.com",
                                 "--stdin", stdin=tricky + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"INFE_PILOT_BACKUP_WEBDAV_PASSWORD={tricky}", self.text())

    def test_the_password_is_never_printed(self):
        secret = "app-password-should-not-be-echoed"
        result = self.run_script("--url", "https://dav.example.com/x", "--user", "u@e.com",
                                 "--stdin", stdin=secret + "\n")
        self.assertNotIn(secret, result.stdout)
        self.assertNotIn(secret, result.stderr)
        self.assertIn(f"密码长度 {len(secret)}", result.stdout)

    def test_a_non_http_url_is_refused(self):
        result = self.run_script("--url", "ftp://dav.example.com/x", "--user", "u@e.com",
                                 "--stdin", stdin="pw-123456\n")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("WEBDAV", self.text())

    def test_an_empty_password_changes_nothing(self):
        result = self.run_script("--url", "https://dav.example.com/x", "--user", "u@e.com",
                                 "--stdin", stdin="\n")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("WEBDAV", self.text())

    def test_an_empty_user_is_refused(self):
        result = self.run_script("--url", "https://dav.example.com/x", "--user", "",
                                 "--stdin", stdin="pw-123456\n")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("WEBDAV", self.text())

    def test_setting_the_target_does_not_touch_the_model_key(self):
        """Both installers rewrite the same file; one must not evict the other."""
        self.run_script("--url", "https://dav.example.com/x", "--user", "u@e.com", "--stdin",
                        stdin="pw-123456\n")
        self.assertIn("INFE_PILOT_MASTER_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                      self.text())

    def test_remove_clears_only_the_three_variables(self):
        self.run_script("--url", "https://dav.example.com/x", "--user", "u@e.com", "--stdin",
                        stdin="pw-123456\n")
        result = self.run_script("--remove")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("WEBDAV", self.text())
        self.assertIn("INFE_PILOT_MASTER_KEY=", self.text())
        self.assertIn("INFE_PILOT_ADMIN_EMAILS=", self.text())

    def test_the_file_stays_private(self):
        self.run_script("--url", "https://dav.example.com/x", "--user", "u@e.com", "--stdin",
                        stdin="pw-123456\n")
        mode = self.env_file.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, oct(mode))

    def test_the_original_file_is_backed_up_first(self):
        self.run_script("--url", "https://dav.example.com/x", "--user", "u@e.com", "--stdin",
                        stdin="pw-123456\n")
        copies = list((self.dir / "backups").glob("pilot-*.env"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text(encoding="utf-8"), self.BASE_ENV)

    def test_verification_uses_the_real_systemd_unit(self):
        """Hand-running the module would not prove the unit can reach the network.

        The unit runs as `cityumail` under `ProtectSystem=strict` with a restricted
        `ReadWritePaths`; only starting it exercises those constraints.
        """
        source = self.SCRIPT.read_text(encoding="utf-8")
        self.assertIn("systemctl start", source)
        self.assertIn("cityu-mail-pilot-backup.service", source)
        self.assertIn("journalctl", source, "失败时要能看到 WebDAV 的报错原文")
