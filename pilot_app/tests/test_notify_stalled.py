"""Tests for the setup reminder (tools/notify_stalled.py).

This tool does the one thing in the project that cannot be undone: it writes to
real people's inboxes. Everything worth pinning follows from that.

* **Two sentences, not one.** "You never configured a mailbox" and "your mailbox
  is refusing your password" need different instructions; sending the wrong one
  is worse than sending nothing.
* **It cannot drift from the app.** The provider steps come from
  `pilot_app/mailpresets.py`, the same source the wizard renders. A reminder that
  tells someone to click a menu that no longer exists is a support ticket.
* **Sending twice is a bug.** Each delivery is recorded, so a re-run after a
  partial failure cannot mail the people who already got one.
* **The output is masked.** The operator decides from *who is stuck and why*,
  not from a scrollback full of addresses.
"""

import contextlib
import datetime as dt
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "notify_stalled_tool", ROOT / "tools" / "notify_stalled.py")
notify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notify)

import pilot_app  # noqa: E402
from pilot_app import mailpresets  # noqa: E402
from pilot_app.database import Database, utc_now  # noqa: E402


def hours_ago(hours: float) -> str:
    moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    return moment.isoformat(timespec="seconds")


class NotifyStalledTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "notify.sqlite3"
        self.db = Database(self.path)
        self.db.initialize()
        self._env = {}
        for key, value in (
            ("INFE_PILOT_DB", str(self.path)),
            ("INFE_PILOT_ORIGIN", "https://example.test"),
            ("INFE_PILOT_MASTER_KEY", "A" * 43 + "="),
            ("PILOT_NOTIFY_CONFIRM", "yes"),
        ):
            self._env[key] = os.environ.get(key)
            os.environ[key] = value
        self.sent: list[tuple[str, str, str]] = []
        self._real_send = notify.send_as_operator
        notify.send_as_operator = self._fake_send

    def tearDown(self):
        notify.send_as_operator = self._real_send
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.folder.cleanup()

    def _fake_send(self, db, secrets, to, subject, body, html_body=None):
        self.sent.append((to, subject, body))
        return {"from": "operator@example.com", "message_id": f"<{len(self.sent)}@example.com>",
                "refused": {}}

    # -- fixtures ----------------------------------------------------------

    def account(self, email, *, age_hours=20, mailbox=None, status="active",
                last_verified_at=None, last_polled_at=None, mailbox_error="",
                verify_error=""):
        user_id = f"usr_{email.split('@')[0]}"
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO users(id,email,password_hash,status,created_at) VALUES(?,?,?,?,?)",
                (user_id, email, "x", status, hours_ago(age_hours)))
            if mailbox:
                connection.execute(
                    """INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                           smtp_host,smtp_port,encrypted_password,enabled,last_verified_at,
                           last_polled_at,last_error,last_verify_error,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (f"mbx_{user_id}", user_id, mailbox, mailbox, "imap.example.com", 993,
                     "smtp.example.com", 465, b"cipher", 1, last_verified_at, last_polled_at,
                     mailbox_error, verify_error, utc_now()))
        return user_id

    def run_tool(self, *argv) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = notify.main(list(argv))
        return code, buffer.getvalue()

    # -- the two sentences -------------------------------------------------

    def test_an_account_with_no_mailbox_gets_the_four_steps(self):
        self.account("bare@example.com", mailbox=None)
        code, out = self.run_tool()
        self.assertEqual(code, 0)
        self.assertIn("还差一步", out)
        self.assertIn("1. 在 CityU Outlook", out)
        self.assertIn("https://example.test/app", out)

    def test_an_account_whose_mailbox_is_refused_gets_the_auth_code_sentence(self):
        self.account("refused@example.com", mailbox="refused@example.com",
                     last_verified_at=hours_ago(30), last_polled_at=hours_ago(0.1),
                     mailbox_error="IMAP 连接失败：b'LOGIN Login error or password error'",
                     verify_error="IMAP 连接失败：b'LOGIN Login error or password error'")
        code, out = self.run_tool()
        self.assertEqual(code, 0)
        self.assertIn("登录被拒绝", out)
        self.assertNotIn("1. 在 CityU Outlook", out, "这两句话不能混")

    def test_a_never_reached_mailbox_counts_as_refused(self):
        """`unreachable` is the same user problem as a rejected login: configured,
        and never once answered."""
        self.account("never@example.com", mailbox="never@example.com")
        rows = notify.collect(self.db, dt.datetime.now(dt.timezone.utc))
        self.assertEqual([row["group"] for row in rows], ["refused"])

    def test_a_healthy_account_is_left_alone(self):
        self.account("fine@example.com", mailbox="fine@example.com",
                     last_verified_at=hours_ago(1), last_polled_at=hours_ago(0.1))
        code, out = self.run_tool()
        self.assertEqual(code, 0)
        self.assertIn("共 0 个账号需要提醒", out)

    def test_a_brand_new_account_is_not_pounced_on(self):
        self.account("fresh@example.com", age_hours=0.2)
        code, out = self.run_tool()
        self.assertIn("共 0 个账号需要提醒", out)

    def test_a_deleted_account_is_never_reminded(self):
        self.account("gone@example.com", status="deleted")
        rows = notify.collect(self.db, dt.datetime.now(dt.timezone.utc))
        self.assertEqual(rows, [])

    # -- cannot drift from the app ----------------------------------------

    def test_the_provider_steps_are_the_ones_the_wizard_shows(self):
        # The address is built from the preset's own domain rather than written
        # out: the privacy gate that guards the public export cannot tell a
        # fixture from a real person's address, and it is right not to try. What
        # this asserts is the *coupling* -- that the mail's steps are the
        # wizard's steps -- so taking the domain from the wizard's data is also
        # the more honest fixture.
        preset = mailpresets.PRESETS_BY_ID["163"]
        address = "steps@" + preset["domains"][0]
        self.account("steps@example.com", mailbox=address)
        body = notify.refused_login_body(address)
        for step in preset["steps"]:
            self.assertIn(step, body, "邮件里的步骤必须与向导同源，否则迟早对不上")
        self.assertIn(preset["label"], body)

    def test_an_unknown_provider_still_gets_usable_advice(self):
        body = notify.refused_login_body("someone@self-hosted.invalid")
        self.assertIn("IMAP", body)
        self.assertIn("SMTP", body)
        self.assertIn("https://example.test/app", body)

    # -- the renderer quirk ------------------------------------------------

    def test_the_link_is_in_a_bullet_so_the_mail_renders_it_as_a_link(self):
        """`mailio.markdown_to_html` only auto-links `https://` inside bullets.
        In a paragraph the URL arrives as dead text -- the one thing this mail
        needs to be is clickable."""
        for body in (notify.never_configured_body(), notify.refused_login_body("x@example.com")):
            linked = [line for line in body.splitlines()
                      if line.startswith("- ") and "https://example.test/app" in line]
            self.assertTrue(linked, "行动链接必须放在项目符号里，否则渲染出来点不动")

    # -- the guardrails ----------------------------------------------------

    def test_the_dry_run_prints_no_full_address(self):
        self.account("private.person@example.com")
        code, out = self.run_tool()
        self.assertNotIn("private.person@example.com", out)
        self.assertIn("pr…@example.com", out)

    def test_it_says_which_package_it_imported(self):
        """A stale `pilot_app` left in /tmp shadows the installed one, because a
        script's own directory comes first on `sys.path`. That is how the first
        server run of this tool read v0.48.0 while production was v0.60.0 -- the
        wrong version would have produced a wrong list of who is stuck."""
        _, out = self.run_tool()
        self.assertIn("用的是", out)
        self.assertIn(str(Path(pilot_app.__file__).parent), out)

    def test_sending_needs_an_explicit_confirmation(self):
        os.environ.pop("PILOT_NOTIFY_CONFIRM", None)
        self.account("guarded@example.com")
        code, _ = self.run_tool("--send")
        self.assertEqual(code, 2)
        self.assertEqual(self.sent, [])

    def test_a_second_run_does_not_mail_the_same_person_again(self):
        self.account("once@example.com")
        first, _ = self.run_tool("--send")
        self.assertEqual(first, 0)
        self.assertEqual(len(self.sent), 1)
        second_code, second = self.run_tool("--send")
        self.assertEqual(second_code, 0)
        self.assertEqual(len(self.sent), 1, "同一个人不该收到第二封")
        self.assertIn("其中 0 个还没提醒过", second)

    def test_a_failed_send_is_not_recorded_as_delivered(self):
        """Recording before the send would be the one way to lose a person: the
        record would say they were told, and they never were."""
        self.account("flaky@example.com")

        def boom(*args, **kwargs):
            raise RuntimeError("SMTP 发送失败：connection refused")

        notify.send_as_operator = boom
        code, out = self.run_tool("--send")
        self.assertEqual(code, 1)
        self.assertIn("失败 1 封", out)
        self.assertEqual(self.db.get_setting("setup_reminder:usr_flaky"), "")

    def test_only_the_intended_account_is_mailed(self):
        self.account("stuck@example.com")
        self.account("fine@example.com", mailbox="fine@example.com",
                     last_verified_at=hours_ago(1), last_polled_at=hours_ago(0.1))
        self.run_tool("--send")
        self.assertEqual([to for to, _, _ in self.sent], ["stuck@example.com"])


if __name__ == "__main__":
    unittest.main()
