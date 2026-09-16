# -*- coding: utf-8 -*-
"""Tests for the operator tools in ``pilot_app/manage.py``.

These commands are what an operator runs *while something is wrong*, which makes
two kinds of bug expensive: one that prints a credential into a terminal or an
e-mail, and one that answers confidently when it could not actually check
anything. The tests below are therefore about properties, not line coverage:

* the masking helper shows at most the first two local characters;
* the env and credential readers cannot overwrite a running deployment;
* a command that cannot run says so in text instead of raising;
* unit-failure mail is scrubbed before it leaves the machine, and a send that
  fails still exits non-zero without raising;
* the two commands documented to work on a host with no database still do.
"""

import base64
import contextlib
import io
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from pilot_app import manage

_TMP = tempfile.mkdtemp()
_TEST_KEY = base64.urlsafe_b64encode(b"\x00" * 32).decode()


def main(*args):
    """Run ``manage.main`` the way the shell does, capturing both streams."""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("sys.argv", ["manage.py", *args]), \
         contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = manage.main()
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def fake_environment_key():
    """Give ``SecretBox.from_environment`` a valid key without a real secret."""
    with mock.patch("pilot_app.security.SecretBox.from_environment",
                    staticmethod(lambda: manage.SecretBox.from_base64(_TEST_KEY))):
        yield


class MaskTests(unittest.TestCase):
    def test_at_most_two_local_characters_are_shown(self):
        # Deliberately *not* a school address, and deliberately not written out
        # here either. The published tree scrubs real school addresses out of
        # every file it ships, rewriting the local part -- but it cannot rewrite
        # the *expected* string next to it, because a masked local part contains
        # a `*` and the pattern will not match it. So the two halves of an
        # assertion using a real school domain stop describing the same address,
        # it passes on this machine and fails only in CI, on the published tree.
        # That is what the first run of the pipeline found. The domain has
        # nothing to do with what `_mask` does, so it is a neutral one.
        for address, expected in (
            ("someone@example.com", "so***@example.com"),
            ("ab@example.org", "ab***@example.org"),
            ("a@b.com", "a***@b.com"),
        ):
            self.assertEqual(manage._mask(address), expected)

    def test_a_longer_local_part_cannot_be_read_back(self):
        self.assertNotIn("verylongname", manage._mask("verylongname@example.com"))

    def test_something_without_a_domain_is_stars(self):
        for value in ("", "not-an-address", None):
            self.assertEqual(manage._mask(value), "***")


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self.path = pathlib.Path(_TMP) / "pilot.env"
        self.saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def test_it_reads_infe_values_and_strips_quotes(self):
        self.path.write_text('INFE_PILOT_A=1\nINFE_PILOT_B="two words"\n', encoding="utf-8")
        os.environ.pop("INFE_PILOT_A", None)
        os.environ.pop("INFE_PILOT_B", None)
        manage._load_env_file(str(self.path))
        self.assertEqual(os.environ["INFE_PILOT_A"], "1")
        self.assertEqual(os.environ["INFE_PILOT_B"], "two words")

    def test_it_ignores_comments_blank_lines_and_foreign_keys(self):
        self.path.write_text("\n# INFE_PILOT_SKIP=nope\nOTHER=1\nbroken line\nINFE_PILOT_OK=yes\n",
                             encoding="utf-8")
        os.environ.pop("INFE_PILOT_SKIP", None)
        os.environ.pop("INFE_PILOT_OK", None)
        manage._load_env_file(str(self.path))
        self.assertNotIn("INFE_PILOT_SKIP", os.environ)
        self.assertNotIn("OTHER", os.environ)
        self.assertEqual(os.environ.get("INFE_PILOT_OK"), "yes")

    def test_it_never_overwrites_a_value_that_is_already_set(self):
        """The file supplies *defaults*. If it could overwrite, running a
        diagnostic with the wrong file would silently point the tool at the
        wrong database or the wrong key -- and say nothing."""
        self.path.write_text("INFE_PILOT_A=from-file\n", encoding="utf-8")
        os.environ["INFE_PILOT_A"] = "from-environment"
        manage._load_env_file(str(self.path))
        self.assertEqual(os.environ["INFE_PILOT_A"], "from-environment")

    def test_a_missing_file_is_reported_not_raised(self):
        with contextlib.redirect_stdout(io.StringIO()) as out_buffer:
            manage._load_env_file(str(pathlib.Path(_TMP) / "nope.env"))
        self.assertIn("读取环境文件失败", out_buffer.getvalue())


class CredentialFileTests(unittest.TestCase):
    def setUp(self):
        self.path = pathlib.Path(_TMP) / "cred.txt"

    def _write(self, text: str):
        self.path.write_text(text, encoding="utf-8")
        return str(self.path)

    def test_the_email_and_password_shape(self):
        self.assertEqual(manage.read_credential_file(self._write("email=me@example.com\npassword=app-pass\n")),
                         ("me@example.com", "app-pass"))

    def test_the_aliases_the_help_text_promises(self):
        self.assertEqual(manage.read_credential_file(self._write("user=me@example.com\ncode=app-pass\n")),
                         ("me@example.com", "app-pass"))

    def test_a_lone_password_line(self):
        """Some providers hand you only a code; the file may hold just that."""
        self.assertEqual(manage.read_credential_file(self._write("# 注释\n\napp-pass\n")),
                         ("", "app-pass"))

    def test_the_last_lone_line_wins(self):
        self.assertEqual(manage.read_credential_file(self._write("first\nsecond\n")), ("", "second"))

    def test_a_missing_file_returns_nothing_rather_than_raising(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = manage.read_credential_file(str(pathlib.Path(_TMP) / "nope.txt"))
        self.assertEqual(result, ("", ""))
        self.assertIn("读取凭据文件失败", out.getvalue())


class DiagnoseForwardingArgumentTests(unittest.TestCase):
    """The argument pre-flight, which runs before anything touches a network."""

    def test_a_missing_address_is_refused(self):
        with mock.patch.object(manage, "diagnose_forwarding") as diag:
            code, out, _ = main("diagnose-forwarding")
        self.assertEqual(code, 2)
        self.assertIn("--email", out)
        diag.assert_not_called()

    def test_an_empty_password_is_refused(self):
        with mock.patch("getpass.getpass", return_value=""), \
             mock.patch.object(manage, "diagnose_forwarding") as diag:
            code, out, _ = main("diagnose-forwarding", "--email", "me@example.com",
                                "--password-env", "INFE_DIAG_PASSWORD_ABSENT")
        self.assertEqual(code, 2)
        self.assertIn("没有提供授权码", out)
        diag.assert_not_called()

    def test_the_credential_file_fills_in_both_fields(self):
        path = pathlib.Path(_TMP) / "diag.txt"
        path.write_text("user=me@example.com\ncode=app-pass\n", encoding="utf-8")
        with mock.patch.object(manage, "diagnose_forwarding", return_value=0) as diag, \
             fake_environment_key():
            code, out, _ = main("diagnose-forwarding", "--password-file", str(path))
        self.assertEqual(code, 0)
        args, kwargs = diag.call_args
        self.assertEqual(args[0], "me@example.com")
        self.assertEqual(args[1], "app-pass")
        # Invariant 2: the file may hold a credential, the terminal output may not.
        self.assertNotIn("app-pass", out)

    def test_the_env_file_is_read_before_the_password_env(self):
        env = pathlib.Path(_TMP) / "diag.env"
        env.write_text("INFE_DIAG_FROM_FILE=from-file\n", encoding="utf-8")
        saved = os.environ.pop("INFE_DIAG_FROM_FILE", None)
        try:
            with mock.patch.object(manage, "diagnose_forwarding", return_value=0) as diag, \
                 fake_environment_key():
                main("diagnose-forwarding", "--email", "me@example.com",
                     "--env-file", str(env), "--password-env", "INFE_DIAG_FROM_FILE")
            self.assertEqual(diag.call_args[0][1], "from-file")
        finally:
            os.environ.pop("INFE_DIAG_FROM_FILE", None)
            if saved is not None:
                os.environ["INFE_DIAG_FROM_FILE"] = saved


def _headers(message_id: str, subject: str = "Hello", to: str = "me@example.com",
             delivered_to: str = "me@example.com") -> bytes:
    return (f"Message-ID: <{message_id}>\r\nSubject: {subject}\r\n"
            f"Date: Thu, 11 Sep 2026 10:00:00 +0800\r\nTo: {to}\r\n"
            f"Delivered-To: {delivered_to}\r\n\r\n").encode("utf-8")


class FakeIMAP:
    """The smallest IMAP server that can answer this one diagnosis."""

    def __init__(self, messages: dict[str, bytes], fail_login: bool = False,
                 fail_search: bool = False):
        self.messages = messages
        self.fail_login = fail_login
        self.fail_search = fail_search
        self.commands: list[tuple] = []
        self.closed = False
        self.logged_out = False

    def login(self, address, password):
        self.commands.append(("login", address, password))
        if self.fail_login:
            raise RuntimeError("login failed: authenticationfailed")
        return "OK", [b"1"]

    def select(self, folder, readonly=False):
        self.commands.append(("select", folder, readonly))
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.commands.append(("uid", command, args))
        if command == "search":
            if self.fail_search:
                raise RuntimeError("search blew up")
            return "OK", [b" ".join(uid.encode() for uid in self.messages)]
        return "OK", [(b"x", self.messages[args[0]])]

    def close(self):
        self.closed = True
        return "OK", []

    def logout(self):
        self.logged_out = True
        return "BYE", []


class DiagnoseForwardingTests(unittest.TestCase):
    """Invariant 2 and the read-only rule, on the one tool that opens a real
    user's mailbox by hand."""

    def setUp(self):
        self.server = None

    def _install(self, messages, **kwargs):
        self.server = FakeIMAP(messages, **kwargs)
        patcher = mock.patch("imaplib.IMAP4_SSL", return_value=self.server)
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.server

    def _run(self, messages, **kwargs):
        self._install(messages, **kwargs)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = manage.diagnose_forwarding("someone@example.com", "app-pass")
        return code, out.getvalue()

    def test_the_folder_is_opened_read_only(self):
        """The whole tool exists to look; a write here would mark a user's real
        mail as read."""
        self._run({"1": _headers("a@b")})
        selects = [c for c in self.server.commands if c[0] == "select"]
        self.assertEqual(selects, [("select", "INBOX", True)])

    def test_only_headers_are_fetched_and_never_a_body(self):
        self._run({"1": _headers("a@b")})
        fetches = [c for c in self.server.commands if c[0] == "uid" and c[1] == "fetch"]
        self.assertTrue(fetches)
        for _, _, args in fetches:
            spec = args[1]
            self.assertIn("BODY.PEEK[HEADER.FIELDS", spec)
            self.assertNotIn("BODY[", spec)
            self.assertNotIn("TEXT", spec)

    def test_the_password_never_reaches_the_output(self):
        code, out = self._run({"1": _headers("a@b")})
        self.assertEqual(code, 0)
        self.assertNotIn("app-pass", out)

    def test_the_whole_address_is_masked(self):
        code, out = self._run({"1": _headers("a@b")})
        self.assertNotIn("someone@example.com", out)
        self.assertIn("som***@example.com", out)

    def test_two_copies_of_one_message_are_the_duplicate_verdict(self):
        code, out = self._run({
            "1": _headers("same@id", delivered_to="me@example.com"),
            "2": _headers("same@id", delivered_to="me2@example.com"),
        })
        self.assertEqual(code, 0)
        self.assertIn("重复 Message-ID：1 组", out)
        self.assertIn("同一封邮件被投递了两遍", out)

    def test_different_message_ids_are_not_a_duplicate(self):
        code, out = self._run({"1": _headers("one@id"), "2": _headers("two@id")})
        self.assertEqual(code, 0)
        self.assertIn("重复 Message-ID：0 组", out)
        self.assertIn("未发现相同 Message-ID", out)

    def test_a_message_without_an_id_is_not_grouped_with_another(self):
        """Two forwards can legitimately arrive with no Message-ID; calling them
        a duplicate would send the operator chasing a rule that is fine."""
        code, out = self._run({"1": b"Subject: A\r\n\r\n", "2": b"Subject: B\r\n\r\n"})
        self.assertEqual(code, 0)
        self.assertIn("重复 Message-ID：0 组", out)

    def test_only_the_most_recent_messages_are_checked(self):
        self._install({str(n): _headers(f"{n}@id") for n in range(1, 11)})
        with contextlib.redirect_stdout(io.StringIO()):
            manage.diagnose_forwarding("someone@example.com", "app-pass", limit=3)
        fetched = [c[2][0] for c in self.server.commands if c[0] == "uid" and c[1] == "fetch"]
        self.assertEqual(fetched, ["8", "9", "10"])

    def test_a_login_failure_is_reported_and_the_connection_is_dropped(self):
        self._install({}, fail_login=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = manage.diagnose_forwarding("someone@example.com", "app-pass")
        self.assertEqual(code, 1)
        self.assertIn("登录失败", out.getvalue())
        self.assertIn("授权码", out.getvalue())

    def test_the_connection_is_dropped_even_when_the_search_raises(self):
        """A leaked connection keeps the mailbox locked for everyone else."""
        self._install({}, fail_search=True)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                manage.diagnose_forwarding("someone@example.com", "app-pass")
        self.assertTrue(self.server.closed)
        self.assertTrue(self.server.logged_out)


class RunTests(unittest.TestCase):
    def test_a_missing_command_is_text_not_an_exception(self):
        """``_run`` feeds text that gets e-mailed; a traceback here would replace
        the unit's real failure with ours."""
        self.assertIn("无法运行", manage._run(["definitely-not-a-real-command-xyz"]))

    def test_output_is_returned(self):
        self.assertIn("ok", manage._run(["echo", "ok"]))


class NotifyUnitFailureTests(unittest.TestCase):
    """The ``OnFailure=`` path, which has to work while other things are broken."""

    def setUp(self):
        self.db = mock.MagicMock()
        self.sent: list[tuple] = []
        # This handler is dispatched *after* the database is opened, because it
        # borrows a real mailbox to send. It is the one command whose whole job
        # is to work when something else has already broken, so the storage it
        # needs is worth naming: it is not optional.
        patcher = mock.patch.object(manage, "Database", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _send(self, db, box, subject, text):
        self.sent.append((subject, text))
        return ["ops@example.com"]

    def test_an_empty_unit_is_refused_without_mailing(self):
        with mock.patch("pilot_app.alerting.send_admin_mail", side_effect=self._send):
            code, out, _ = main("notify-unit-failure", "--unit", "   ")
        self.assertEqual(code, 2)
        self.assertIn("缺少 --unit", out)
        self.assertEqual(self.sent, [])

    def test_the_mail_carries_no_credential_from_the_environment(self):
        """Invariant 2, with a message that literally embeds command output."""
        secret = "SUPER-SECRET-APP-PASSWORD-0123456789"
        with mock.patch.object(manage, "_run", return_value=f"Environment=CODE={secret}\nboom"), \
             mock.patch("pilot_app.alerting.collect_secret_values", return_value={secret}), \
             mock.patch("pilot_app.alerting.send_admin_mail", side_effect=self._send), \
             fake_environment_key():
            code, out, _ = main("notify-unit-failure", "--unit", "cityu-mail-pilot-worker")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.sent), 1)
        subject, text = self.sent[0]
        self.assertNotIn(secret, text)
        self.assertIn("cityu-mail-pilot-worker", subject)

    def test_the_mail_says_how_to_look_further(self):
        with mock.patch.object(manage, "_run", return_value="status"), \
             mock.patch("pilot_app.alerting.collect_secret_values", return_value=set()), \
             mock.patch("pilot_app.alerting.send_admin_mail", side_effect=self._send), \
             fake_environment_key():
            main("notify-unit-failure", "--unit", "cityu-mail-pilot-web")
        self.assertIn("systemctl status --full cityu-mail-pilot-web", self.sent[0][1])

    def test_the_journal_tail_is_capped(self):
        captured: dict = {}

        def fake_run(command):
            captured["command"] = command
            return "x"

        with mock.patch.object(manage, "_run", side_effect=fake_run), \
             mock.patch("pilot_app.alerting.collect_secret_values", return_value=set()), \
             mock.patch("pilot_app.alerting.send_admin_mail", side_effect=self._send), \
             fake_environment_key():
            main("notify-unit-failure", "--unit", "cityu-mail-pilot-worker", "--lines", "9999")
        self.assertIn("200", captured["command"])
        self.assertNotIn("9999", captured["command"])

    def test_a_send_failure_exits_non_zero_and_does_not_raise(self):
        """A handler that raises would obscure the original failure, and
        ``OnFailure=`` must not recurse."""
        with mock.patch.object(manage, "_run", return_value="status"), \
             mock.patch("pilot_app.alerting.collect_secret_values", return_value=set()), \
             mock.patch("pilot_app.security.SecretBox.from_environment",
                        side_effect=RuntimeError("no master key")):
            code, out, err = main("notify-unit-failure", "--unit", "cityu-mail-pilot-backup")
        self.assertEqual(code, 1)
        self.assertIn("无法发出单元失败告警", err)


class CheckAlertsTests(unittest.TestCase):
    def test_dry_run_prints_and_sends_nothing(self):
        findings = [{"severity": "warning", "key": "mailbox_error:usr_1",
                     "title": "收信失败", "detail": "授权码被拒"}]
        with mock.patch.object(manage, "Database"), \
             mock.patch("pilot_app.alerting.evaluate", return_value=findings), \
             mock.patch("pilot_app.alerting.run_checks") as ran:
            code, out, _ = main("check-alerts", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("mailbox_error:usr_1", out)
        self.assertIn("DRY RUN", out)
        ran.assert_not_called()

    def test_a_real_run_reports_failure_through_the_exit_code(self):
        """``OnFailure=`` and cron read the exit code, not the prose."""
        with mock.patch.object(manage, "Database"), fake_environment_key(), \
             mock.patch("pilot_app.alerting.run_checks",
                        return_value={"errors": ["boom"], "sent": 0}):
            code, out, _ = main("check-alerts")
        self.assertEqual(code, 1)
        self.assertIn("哨兵结果", out)

    def test_a_clean_run_exits_zero(self):
        with mock.patch.object(manage, "Database"), fake_environment_key(), \
             mock.patch("pilot_app.alerting.run_checks",
                        return_value={"errors": [], "sent": 1}):
            code, _, _ = main("check-alerts")
        self.assertEqual(code, 0)


class DigTests(unittest.TestCase):
    def test_it_walks_a_nested_path(self):
        self.assertEqual(manage._dig({"a": {"b": {"c": 1}}}, "a.b.c"), 1)

    def test_a_missing_key_is_none_not_an_error(self):
        self.assertIsNone(manage._dig({"a": {}}, "a.b.c"))

    def test_a_non_dict_in_the_middle_is_none(self):
        self.assertIsNone(manage._dig({"a": [1, 2]}, "a.b"))

    def test_a_top_level_scalar_is_none(self):
        self.assertIsNone(manage._dig({}, "a"))


class NoDatabaseNeededTests(unittest.TestCase):
    """Two commands are documented to work on a host with no database.

    ``check-model`` and ``check-search`` are what someone runs *while setting a
    key up*, which is exactly when the app may not be installed yet. Opening a
    database there made them fail for a reason that had nothing to do with the
    key. That is a property worth pinning down, not a comment worth trusting.
    """

    def test_check_model_never_opens_the_database(self):
        with mock.patch.object(manage, "Database") as database, \
             mock.patch.object(manage, "check_model", return_value=0) as called:
            code, _, _ = main("check-model")
        self.assertEqual(code, 0)
        called.assert_called_once()
        database.assert_not_called()

    def test_check_search_never_opens_the_database(self):
        with mock.patch.object(manage, "Database") as database, \
             mock.patch.object(manage, "check_search", return_value=0):
            main("check-search")
        database.assert_not_called()

    def test_generate_master_key_prints_one_usable_key_and_touches_nothing(self):
        with mock.patch.object(manage, "Database") as database:
            code, out, _ = main("generate-master-key")
        self.assertEqual(code, 0)
        database.assert_not_called()
        value = out.strip()
        self.assertEqual(len(value), 44, "32 字节的 base64 应该正好 44 个字符")
        self.assertEqual(len(base64.urlsafe_b64decode(value)), 32)

    def test_a_command_that_needs_storage_does_open_it(self):
        """The other half: the exceptions above must not have swallowed the rule."""
        db = mock.MagicMock()
        with mock.patch.object(manage, "Database", return_value=db) as database, \
             mock.patch.object(manage, "check_metrics", return_value=0):
            main("check-metrics")
        database.assert_called_once()
        db.initialize.assert_called_once()


class CreateInviteTests(unittest.TestCase):
    def test_it_prints_a_code_and_stores_only_its_hash(self):
        """The code is the credential. If the plaintext ever reached the table,
        a database leak would hand out working invitations."""
        db = mock.MagicMock()
        connection = db.connect.return_value.__enter__.return_value
        with mock.patch.object(manage, "Database", return_value=db):
            code, out, _ = main("create-invite", "--label", "pilot", "--days", "7")
        self.assertEqual(code, 0)
        value = out.strip()
        self.assertGreaterEqual(len(value), 20)
        statement, params = connection.execute.call_args[0]
        self.assertIn("INSERT INTO invites", statement)
        self.assertNotIn(value, params)
        self.assertEqual(params[1], "pilot")


class VerifyE2ETests(unittest.TestCase):
    def test_an_unknown_account_is_reported_masked(self):
        db = mock.MagicMock()
        db.find_user_for_login.return_value = None
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = manage.verify_e2e(db, "nobody@example.com", 5, False, False)
        self.assertEqual(code, 2)
        self.assertIn("找不到试点用户", out.getvalue())
        self.assertNotIn("nobody@example.com", out.getvalue())

    def test_a_deleted_account_is_refused(self):
        db = mock.MagicMock()
        db.find_user_for_login.return_value = {"id": "usr_1", "email": "gone@example.com",
                                              "status": "deleted"}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = manage.verify_e2e(db, "gone@example.com", 5, False, False)
        self.assertEqual(code, 2)
        self.assertIn("已删除", out.getvalue())

    def test_an_account_without_a_mailbox_is_told_so(self):
        db = mock.MagicMock()
        db.find_user_for_login.return_value = {"id": "usr_1", "email": "new@example.com",
                                              "status": "active"}
        db.get_mailbox.return_value = None
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = manage.verify_e2e(db, "new@example.com", 5, False, False)
        self.assertEqual(code, 2)
        self.assertIn("还没有配置邮箱", out.getvalue())


if __name__ == "__main__":
    unittest.main()
