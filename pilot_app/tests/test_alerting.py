"""Tests for the operator alert sentinel.

The properties that matter, and why:

* a healthy pilot must be **silent** — an alerting system that cries wolf gets
  ignored, which is the same as not having one;
* a broken mailbox, a stalled poller, a backlog, a filling disk and an expiring
  certificate must each be **reported**, and each must keep being reported at
  most once per silence window rather than every loop;
* clearing a condition must produce exactly one recovery notice;
* a **failure to send** must leave the state untouched, so the next pass tries
  again instead of assuming the operator was told;
* nothing that leaves the machine may contain a credential.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import tempfile
import unittest
from unittest import mock

from pilot_app import alerting
from pilot_app.database import Database, utc_now
from pilot_app.security import SecretBox, hash_password, token_hash

NOW = dt.datetime(2026, 9, 14, 12, 0, tzinfo=dt.timezone.utc)


class AlertingTestCase(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.work.name) / "pilot.sqlite3")
        self.db.initialize()
        self.secrets = SecretBox(b"7" * 32)
        # "Now" must be the real clock, not a constant: the mailbox stamps the
        # sentinel compares against are written by the database itself. A frozen
        # clock in the future makes every healthy mailbox look hours stale, and
        # a frozen clock in the past hides every fresh one.
        self.now = dt.datetime.now(dt.timezone.utc)
        self.env = mock.patch.dict("os.environ", {
            "INFE_PILOT_ADMIN_EMAILS": "boss@example.com",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.work.cleanup()

    def _user(self, email: str = "boss@example.com", *, index: int = 1,
              with_mailbox: bool = True, enabled: bool = True) -> dict:
        invite = self.db.create_invite(f"u{index}", 1)
        user = self.db.create_user(email, hash_password("a-long-enough-password"),
                                   token_hash(invite))
        mailbox_id = ""
        if with_mailbox:
            mailbox_id = self.db.upsert_mailbox(user["id"], {
                "email": f"box{index}@qq.com", "report_to": email,
                "imap_host": "imap.qq.com", "imap_port": 993,
                "smtp_host": "smtp.qq.com", "smtp_port": 465,
                "enabled": enabled,
                "encrypted_password": self.secrets.encrypt("pw", context=f"mailbox:{user['id']}"),
            })
        return {"user": user, "mailbox_id": mailbox_id}

    def _polled(self, mailbox_id: str, *, seconds_ago: int = 0, error: str = "") -> None:
        """Stamp a poll result, backdating last_polled_at when asked."""
        self.db.update_mailbox_poll(mailbox_id, last_uid=1, uid_validity="1", error=error)
        if seconds_ago:
            stamp = (dt.datetime.now(dt.timezone.utc)
                     - dt.timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")
            with self.db.connect() as connection:
                connection.execute("UPDATE mailboxes SET last_polled_at=? WHERE id=?",
                                   (stamp, mailbox_id))

    def _queue(self, owner: dict, count: int) -> None:
        for uid in range(count):
            self.db.insert_message(owner["user"]["id"], owner["mailbox_id"], "1", uid + 1,
                                   {"subject": f"s{uid}", "message_key": f"k{uid}"})

    def _failed_reports(self, owner: dict, count: int) -> None:
        for index in range(count):
            report_id = self.db.create_report(
                user_id=owner["user"]["id"], message_id=None, kind="immediate",
                subject=f"r{index}", body="x", sent_to="a@b.c")
            self.db.fail_report(report_id, "boom")

    def _keys(self, findings: list[dict]) -> set:
        return {item["key"] for item in findings}


class EvaluateTests(AlertingTestCase):
    def test_a_healthy_pilot_produces_no_findings(self):
        owner = self._user()
        self._polled(owner["mailbox_id"])
        self.assertEqual(
            alerting.evaluate(self.db, now=self.now, disk_percent=10.0, certificate_days=80.0), [])

    def test_a_failed_poll_is_reported_even_though_it_stamps_last_polled_at(self):
        """A failing poll still updates last_polled_at, so staleness alone would
        never see it. Only the stored error exposes this state."""
        owner = self._user()
        self._polled(owner["mailbox_id"], error="IMAP 认证失败")
        findings = alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=90.0)
        self.assertEqual(self._keys(findings), {f"mailbox_error:{owner['user']['id']}"})
        self.assertEqual(findings[0]["severity"], "critical")
        self.assertIn("IMAP 认证失败", findings[0]["detail"])

    def test_a_stalled_poller_is_reported(self):
        owner = self._user()
        self._polled(owner["mailbox_id"], seconds_ago=3600)
        findings = alerting.evaluate(self.db, now=dt.datetime.now(dt.timezone.utc),
                                     disk_percent=1.0, certificate_days=90.0)
        self.assertEqual(self._keys(findings), {f"mailbox_stale:{owner['user']['id']}"})

    def test_a_mailbox_never_polled_is_reported(self):
        owner = self._user()
        findings = alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=90.0)
        self.assertIn(f"mailbox_stale:{owner['user']['id']}", self._keys(findings))

    def test_a_failed_mailbox_is_one_problem_not_two(self):
        """A failed poll is the cause; "no poll for N minutes" is the consequence.

        Reporting both counts one incident twice, and on 2026-09-15 the pair sent
        the operator about seventy mails in a day. The stale check now runs only
        when there is no error to report -- so a genuinely *silent* stall (the
        poller thread died with no error anywhere) is still caught.
        """
        owner = self._user()
        self._polled(owner["mailbox_id"], error="IMAP 认证失败")
        keys = self._keys(alerting.evaluate(self.db, now=self.now, disk_percent=1.0,
                                            certificate_days=90.0))
        self.assertIn(f"mailbox_error:{owner['user']['id']}", keys)
        self.assertNotIn(f"mailbox_stale:{owner['user']['id']}", keys,
                         "已经报了原因，就不要再报它的后果")

    def test_a_silent_stall_keeps_the_same_detail_between_passes(self):
        """The detail must not carry a live counter.

        `_should_send` re-mails a finding whose detail *changed*, so a detail
        like "已有 16 分钟"复-mailed on every pass -- the dedupe rule became a
        metronome. The threshold in the detail is stable; the exact age is in the
        console instead.
        """
        owner = self._user()
        self._polled(owner["mailbox_id"], seconds_ago=40 * 60)
        first = {item["key"]: item for item in alerting.evaluate(
            self.db, now=self.now, disk_percent=1.0, certificate_days=90.0)}
        later = {item["key"]: item for item in alerting.evaluate(
            self.db, now=self.now + dt.timedelta(minutes=5), disk_percent=1.0,
            certificate_days=90.0)}
        key = f"mailbox_stale:{owner['user']['id']}"
        self.assertIn(key, first)
        self.assertEqual(first[key]["detail"], later[key]["detail"],
                         "详情里不能有会随时间自己变化的数字，否则每轮都会重发")

    def test_a_paused_account_is_left_alone(self):
        """Pausing is an operator decision; alerting on it would train them to
        ignore the alerts."""
        owner = self._user()
        self._polled(owner["mailbox_id"], error="IMAP 认证失败")
        self.db.set_user_status(owner["user"]["id"], "paused")
        self.assertEqual(
            alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=90.0), [])

    def test_a_disabled_mailbox_is_left_alone(self):
        owner = self._user(enabled=False)
        self.assertEqual(
            alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=90.0), [])

    def test_a_backlog_is_reported_only_above_the_threshold(self):
        owner = self._user()
        self._polled(owner["mailbox_id"])
        self._queue(owner, alerting.ALERT_QUEUE_DEPTH)
        self.assertNotIn("queue_backlog", self._keys(
            alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=90.0)))
        self._queue(owner, alerting.ALERT_QUEUE_DEPTH + 1)
        # The second call re-inserts distinct message keys, so the depth grows.
        self.assertIn("queue_backlog", self._keys(
            alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=90.0)))

    def test_failed_reports_are_reported_only_above_the_threshold(self):
        owner = self._user()
        self._polled(owner["mailbox_id"])
        self._failed_reports(owner, alerting.ALERT_FAILED_REPORTS - 1)
        self.assertNotIn("failed_reports", self._keys(
            alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=90.0)))
        self._failed_reports(owner, 2)
        self.assertIn("failed_reports", self._keys(
            alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=90.0)))

    def test_disk_is_reported_at_the_threshold(self):
        owner = self._user()
        self._polled(owner["mailbox_id"])
        below = alerting.evaluate(self.db, now=self.now,
                                  disk_percent=alerting.ALERT_DISK_PERCENT - 0.1,
                                  certificate_days=90.0)
        self.assertNotIn("disk", self._keys(below))
        at = alerting.evaluate(self.db, now=self.now, disk_percent=float(alerting.ALERT_DISK_PERCENT),
                               certificate_days=90.0)
        self.assertIn("disk", self._keys(at))

    def test_certificate_is_reported_when_close_and_when_expired(self):
        owner = self._user()
        self._polled(owner["mailbox_id"])
        fresh = alerting.evaluate(self.db, now=self.now, disk_percent=1.0,
                                  certificate_days=float(alerting.ALERT_CERT_DAYS))
        self.assertNotIn("tls_cert", self._keys(fresh))
        soon = alerting.evaluate(self.db, now=self.now, disk_percent=1.0,
                                 certificate_days=float(alerting.ALERT_CERT_DAYS) - 1)
        self.assertIn("tls_cert", self._keys(soon))
        expired = alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=-3.0)
        self.assertIn("已过期", expired[0]["detail"])

    def test_unreadable_readings_do_not_invent_findings(self):
        """The macOS dev box cannot read /proc; None must mean "unknown", not
        "fine" and certainly not "broken"."""
        owner = self._user()
        self._polled(owner["mailbox_id"])
        self.assertEqual(
            alerting.evaluate(self.db, now=self.now, disk_percent=None, certificate_days=None), [])


class DeDuplicationTests(AlertingTestCase):
    def test_a_new_finding_is_sent(self):
        self.assertTrue(alerting._should_send(None, {"detail": "d"}, NOW))

    def test_a_repeat_inside_the_window_is_suppressed(self):
        previous = {"open": 1, "detail": "d",
                    "last_sent_at": (NOW - dt.timedelta(minutes=5)).isoformat()}
        self.assertFalse(alerting._should_send(previous, {"detail": "d"}, NOW))

    def test_a_repeat_after_the_window_is_sent(self):
        previous = {"open": 1, "detail": "d",
                    "last_sent_at": (NOW - dt.timedelta(
                        seconds=alerting.ALERT_REPEAT_SECONDS + 1)).isoformat()}
        self.assertTrue(alerting._should_send(previous, {"detail": "d"}, NOW))

    def test_a_changed_detail_is_sent_immediately(self):
        previous = {"open": 1, "detail": "old",
                    "last_sent_at": (NOW - dt.timedelta(seconds=5)).isoformat()}
        self.assertTrue(alerting._should_send(previous, {"detail": "new"}, NOW))

    def test_a_cleared_then_returning_finding_is_sent_again(self):
        previous = {"open": 0, "detail": "d",
                    "last_sent_at": (NOW - dt.timedelta(seconds=5)).isoformat()}
        self.assertTrue(alerting._should_send(previous, {"detail": "d"}, NOW))


class RunChecksTests(AlertingTestCase):
    def _healthy(self) -> dict:
        owner = self._user()
        self._polled(owner["mailbox_id"])
        return owner

    def test_a_healthy_system_sends_nothing(self):
        self._healthy()
        sent: list = []
        result = alerting.run_checks(self.db, self.secrets, now=self.now, disk=1.0,
                                     certificate_days=90.0,
                                     sender=lambda *args: sent.append(args) or ["boss@example.com"])
        self.assertEqual(result["sent"], 0)
        self.assertEqual(sent, [])

    def test_the_same_problem_is_mailed_once_then_suppressed(self):
        owner = self._user()
        self._polled(owner["mailbox_id"], error="IMAP 认证失败")
        sent: list = []

        def sender(*args):
            sent.append(args)
            return ["boss@example.com"]

        first = alerting.run_checks(self.db, self.secrets, now=self.now, disk=1.0,
                                    certificate_days=90.0, sender=sender)
        self.assertEqual(first["sent"], 1)
        second = alerting.run_checks(self.db, self.secrets,
                                     now=self.now + dt.timedelta(minutes=1), disk=1.0,
                                     certificate_days=90.0, sender=sender)
        self.assertEqual(second["sent"], 0)
        self.assertEqual(len(sent), 1)

    def test_a_persistent_problem_re_mails_after_the_window(self):
        owner = self._user()
        self._polled(owner["mailbox_id"], error="IMAP 认证失败")
        sent: list = []
        sender = lambda *args: sent.append(args) or ["boss@example.com"]

        alerting.run_checks(self.db, self.secrets, now=self.now, disk=1.0,
                            certificate_days=90.0, sender=sender)
        later = self.now + dt.timedelta(seconds=alerting.ALERT_REPEAT_SECONDS + 60)
        alerting.run_checks(self.db, self.secrets, now=later, disk=1.0,
                            certificate_days=90.0, sender=sender)
        self.assertEqual(len(sent), 2)

    def test_clearing_a_problem_sends_exactly_one_recovery(self):
        owner = self._user()
        self._polled(owner["mailbox_id"], error="IMAP 认证失败")
        sent: list = []
        sender = lambda *args: sent.append(args) or ["boss@example.com"]

        alerting.run_checks(self.db, self.secrets, now=self.now, disk=1.0,
                            certificate_days=90.0, sender=sender)
        self._polled(owner["mailbox_id"])
        recovery = alerting.run_checks(self.db, self.secrets, now=self.now + dt.timedelta(minutes=1),
                                       disk=1.0, certificate_days=90.0, sender=sender)
        self.assertEqual(recovery["sent"], 1)
        self.assertIn("已恢复", sent[-1][2])
        # And it is not repeated once the state is closed.
        again = alerting.run_checks(self.db, self.secrets, now=self.now + dt.timedelta(minutes=2),
                                    disk=1.0, certificate_days=90.0, sender=sender)
        self.assertEqual(again["sent"], 0)
        self.assertEqual(len(sent), 2)

    def test_a_send_failure_leaves_the_state_untouched_so_it_retries(self):
        owner = self._user()
        self._polled(owner["mailbox_id"], error="IMAP 认证失败")

        def broken(*args):
            raise RuntimeError("SMTP 挂了")

        result = alerting.run_checks(self.db, self.secrets, now=self.now, disk=1.0,
                                     certificate_days=90.0, sender=broken)
        self.assertEqual(result["sent"], 0)
        self.assertTrue(result["errors"])
        # No state was recorded, so the very next pass tries again.
        self.assertEqual(self.db.list_alert_states(), [])
        sent: list = []
        retry = alerting.run_checks(self.db, self.secrets, now=self.now + dt.timedelta(minutes=1),
                                    disk=1.0, certificate_days=90.0,
                                    sender=lambda *a: sent.append(a) or ["boss@example.com"])
        self.assertEqual(retry["sent"], 1)

    def test_state_survives_a_process_restart(self):
        """The whole reason state lives in SQLite: a redeploy must not replay
        every standing alert."""
        owner = self._user()
        self._polled(owner["mailbox_id"], error="IMAP 认证失败")
        alerting.run_checks(self.db, self.secrets, now=self.now, disk=1.0, certificate_days=90.0,
                            sender=lambda *a: ["boss@example.com"])
        reopened = Database(pathlib.Path(self.work.name) / "pilot.sqlite3")
        reopened.initialize()
        sent: list = []
        result = alerting.run_checks(reopened, self.secrets, now=self.now + dt.timedelta(minutes=1),
                                     disk=1.0, certificate_days=90.0,
                                     sender=lambda *a: sent.append(a) or ["boss@example.com"])
        self.assertEqual(result["sent"], 0)
        self.assertEqual(sent, [])

    def test_the_kill_switch_silences_everything(self):
        owner = self._user()
        self._polled(owner["mailbox_id"], error="IMAP 认证失败")
        sent: list = []
        with mock.patch.object(alerting, "ALERTS_ENABLED", False):
            result = alerting.run_checks(self.db, self.secrets, now=self.now, disk=1.0,
                                         certificate_days=90.0,
                                         sender=lambda *a: sent.append(a) or ["x@y.z"])
        self.assertFalse(result["enabled"])
        self.assertEqual(sent, [])

    def test_an_evaluation_crash_is_contained(self):
        with mock.patch.object(alerting, "evaluate", side_effect=RuntimeError("db gone")):
            result = alerting.run_checks(self.db, self.secrets, now=self.now, disk=1.0,
                                         certificate_days=90.0,
                                         sender=lambda *a: ["x@y.z"])
        self.assertEqual(result["sent"], 0)
        self.assertTrue(result["errors"])


class RedactionTests(unittest.TestCase):
    def test_known_credential_values_are_removed(self):
        self.assertEqual(
            alerting.redact("key is hunter2hunter2 ok", {"hunter2hunter2"}),
            "key is *** ok")

    def test_inline_secret_assignments_are_removed(self):
        text = "INFE_PILOT_MASTER_KEY=abc123\nINFE_PILOT_DB=/var/lib/x.sqlite3\n"
        cleaned = alerting.redact(text)
        self.assertNotIn("abc123", cleaned)
        self.assertIn("INFE_PILOT_DB=/var/lib/x.sqlite3", cleaned)

    def test_short_values_are_not_treated_as_secrets(self):
        """Replacing a 3-character "secret" would corrupt unrelated text."""
        self.assertEqual(alerting.redact("a short value", {"abc"}), "a short value")

    def test_collect_secret_values_reads_only_secret_named_keys(self):
        with tempfile.TemporaryDirectory() as work:
            path = pathlib.Path(work) / "pilot.env"
            path.write_text(
                "INFE_PILOT_DB=/var/lib/cityu-mail-pilot/pilot.sqlite3\n"
                "INFE_PILOT_MASTER_KEY=topsecretvalue\n"
                "# comment\n"
                "INFE_PILOT_ORIGIN=https://example.test\n"
                "SOME_PASSWORD='quotedsecret'\n",
                encoding="utf-8")
            values = alerting.collect_secret_values(str(path))
        self.assertIn("topsecretvalue", values)
        self.assertIn("quotedsecret", values)
        self.assertNotIn("/var/lib/cityu-mail-pilot/pilot.sqlite3", values)
        self.assertNotIn("https://example.test", values)

    def test_collect_secret_values_tolerates_a_missing_file(self):
        self.assertEqual(alerting.collect_secret_values("/nonexistent/pilot.env"), set())


class RenderingTests(AlertingTestCase):
    def test_the_alert_mail_obeys_the_email_html_rules(self):
        """Invariant 5: 600 px single-column table, inline CSS, no script, no
        <style>, no @media, and no links other than https."""
        due = [{"key": "k", "severity": "critical", "title": "标题", "detail": "详情"}]
        recovered = [{"key": "r", "title": "已恢复项"}]
        rendered = alerting._render_html(due, recovered)
        self.assertIn('width="600"', rendered)
        self.assertNotIn("<script", rendered.lower())
        self.assertNotIn("<style", rendered.lower())
        self.assertNotIn("@media", rendered.lower())
        self.assertNotIn("<img", rendered.lower())
        self.assertNotIn("http://", rendered.lower())

    def test_html_escapes_operator_facing_text(self):
        due = [{"key": "k", "severity": "critical",
                "title": "<script>alert(1)</script>", "detail": "&<>"}]
        rendered = alerting._render_html(due, [])
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)


class SendAdminMailTests(AlertingTestCase):
    def test_it_sends_from_the_admins_own_mailbox(self):
        self._user("boss@example.com")
        captured: dict = {}

        def fake_send(config, password, subject, markdown, **kwargs):
            captured.update(config)
            captured["password"] = password
            captured["subject"] = subject

        with mock.patch.object(alerting.mailio, "send_report", fake_send):
            delivered = alerting.send_admin_mail(self.db, self.secrets, "主题", "正文")
        self.assertEqual(delivered, ["boss@example.com"])
        self.assertEqual(captured["email"], "box1@qq.com")
        self.assertEqual(captured["report_to"], "boss@example.com")
        self.assertEqual(captured["smtp_host"], "smtp.qq.com")
        self.assertEqual(captured["password"], "pw")

    def test_it_raises_rather_than_silently_dropping_when_nobody_can_send(self):
        self._user("boss@example.com", with_mailbox=False)
        with self.assertRaises(RuntimeError):
            alerting.send_admin_mail(self.db, self.secrets, "主题", "正文")

    def test_it_raises_when_no_admin_is_configured(self):
        with mock.patch.dict("os.environ", {"INFE_PILOT_ADMIN_EMAILS": ""}):
            with self.assertRaises(RuntimeError):
                alerting.send_admin_mail(self.db, self.secrets, "主题", "正文")


class AdminIdentityTests(unittest.TestCase):
    def test_admin_emails_are_lower_cased_and_trimmed(self):
        with mock.patch.dict("os.environ", {"INFE_PILOT_ADMIN_EMAILS": " A@B.com , c@d.com "}):
            self.assertEqual(alerting.admin_emails(), {"a@b.com", "c@d.com"})

    def test_the_web_surface_and_the_mailer_share_one_definition(self):
        from pilot_app import web
        with mock.patch.dict("os.environ", {"INFE_PILOT_ADMIN_EMAILS": "X@Y.com"}):
            self.assertEqual(web._admin_emails(), alerting.admin_emails())


class CertificateTests(unittest.TestCase):
    def _handshake(self, *, certificate=None, error=None):
        """Patch out the socket so the parsing and the two failure modes can be
        driven without a network or a real certificate."""
        tls = mock.MagicMock()
        tls.getpeercert.return_value = certificate
        tls.__enter__ = lambda self: tls
        tls.__exit__ = lambda *args: False
        context = mock.MagicMock()
        if error is not None:
            context.wrap_socket.side_effect = error
        else:
            context.wrap_socket.return_value = tls
        raw = mock.MagicMock()
        raw.__enter__ = lambda self: raw
        raw.__exit__ = lambda *args: False
        return mock.MagicMock(return_value=raw), context

    def _days(self, certificate=None, error=None, **kwargs):
        connection, context = self._handshake(certificate=certificate, error=error)
        with mock.patch.object(alerting.socket, "create_connection", connection), \
                mock.patch.object(alerting.ssl, "create_default_context", lambda *a, **k: context):
            return alerting.certificate_days_remaining(**kwargs)

    def test_a_valid_certificate_yields_days_remaining(self):
        """The bug this replaces: verification was disabled to read notAfter,
        but getpeercert() returns an empty dict for an unvalidated peer, so the
        reading was always None and the check could never fire."""
        not_after = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=88))
        days = self._days(certificate={"notAfter": not_after.strftime("%b %d %H:%M:%S %Y GMT")},
                          host="mail.example.test")
        self.assertIsNotNone(days)
        self.assertAlmostEqual(days, 88, delta=1)

    def test_an_expired_certificate_is_reported_as_negative(self):
        not_after = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2))
        days = self._days(certificate={"notAfter": not_after.strftime("%b %d %H:%M:%S %Y GMT")},
                          host="mail.example.test")
        self.assertLess(days, 0)

    def test_a_failed_verification_is_reported_not_swallowed(self):
        error = alerting.ssl.SSLCertVerificationError("certificate has expired")
        days = self._days(error=error, host="mail.example.test")
        self.assertIsNotNone(days)
        self.assertLess(days, 0)

    def test_a_certificate_without_notafter_is_unknown(self):
        self.assertIsNone(self._days(certificate={}, host="mail.example.test"))

    def test_an_unreachable_host_reports_unknown_rather_than_expiring(self):
        days = alerting.certificate_days_remaining(host="127.0.0.1", port=1, timeout=2.0)
        self.assertIsNone(days)

    def test_no_configured_host_means_no_check(self):
        self.assertIsNone(alerting.certificate_days_remaining(host=""))

    def test_the_host_falls_back_to_the_configured_origin(self):
        with mock.patch.dict("os.environ", {"INFE_PILOT_ORIGIN": "https://mail.example.test/",
                                            "INFE_PILOT_TLS_HOST": ""}):
            self.assertEqual(alerting._tls_host_from_environment(), "mail.example.test")
        with mock.patch.dict("os.environ", {"INFE_PILOT_ORIGIN": "https://mail.example.test/",
                                            "INFE_PILOT_TLS_HOST": "other.test"}):
            self.assertEqual(alerting._tls_host_from_environment(), "other.test")


if __name__ == "__main__":
    unittest.main()
