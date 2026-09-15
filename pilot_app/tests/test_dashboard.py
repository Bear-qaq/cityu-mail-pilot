"""Tests for the "clear action" dashboard status API."""

import datetime as dt
import os
import unittest
from unittest import mock

_TMP = os.environ.get("INFE_PILOT_DB", "/tmp/pilot-dashboard.sqlite3")
os.environ["INFE_PILOT_DB"] = _TMP
os.environ.setdefault("INFE_PILOT_MASTER_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"

from pilot_app import reports as reports_mod  # noqa: E402
from pilot_app import web  # noqa: E402
from pilot_app.security import SecretBox, hash_password, token_hash  # noqa: E402


REPORT = """## 1. 重要程度与一句话结论
- 等级：高
- 结论：本周五 23:59 前必须提交作业。

## 2. 必须采取的行动与截止时间
- 周五 23:59 前提交（截止：2026-09-18 23:59）

## 3. 邮件内容总结
- 老师更新了提交时间。

## 5. 联网搜索后的建议与来源
来源：Canvas 指南 https://community.canvaslms.com/x
"""


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.db = web.db
        self.db.initialize()
        with self.db.connect() as connection:
            connection.execute("DELETE FROM feedback")
            connection.execute("DELETE FROM reports")
            connection.execute("DELETE FROM messages")
            connection.execute("DELETE FROM mailboxes")
            connection.execute("DELETE FROM connections")
            connection.execute("DELETE FROM sessions")
            connection.execute("DELETE FROM profiles")
            connection.execute("DELETE FROM users")
        self.box = SecretBox.from_environment()
        stamp = dt.datetime.now().timestamp()
        invite = f"dash-invite-{stamp}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat(timespec="seconds")
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO invites(code_hash,expires_at) VALUES(?,?)", (token_hash(invite), expiry))
        self.user = self.db.create_user(
            f"dash-{stamp}@example.com",
            hash_password("a-long-enough-password"), token_hash(invite),
        )

    def _user_row(self):
        return self.db.get_user(self.user["id"])

    def _complete_profile(self, school_email="student@my.cityu.edu.hk"):
        self.db.upsert_profile(self.user["id"], {
            "school_email": school_email, "major": "通信工程", "year_of_study": "大二",
            "courses": ["密码学"], "interests": [], "career_goals": [], "focus_topics": [],
            "less_interested": [], "custom_instructions": "", "language": "bilingual",
            "timezone": "Asia/Hong_Kong", "immediate_enabled": True, "daily_enabled": True,
            "daily_time": "22:00",
        })

    def _add_mailbox(self, **overrides):
        values = {
            "email": "pilot@qq.com", "report_to": "pilot@qq.com", "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465,
            "encrypted_password": self.box.encrypt("pw", context=f"mailbox:{self.user['id']}"),
        }
        values.update(overrides)
        return self.db.upsert_mailbox(self.user["id"], values)

    def _add_model(self, provider="deepseek", last_error=""):
        return self.db.upsert_connection(self.user["id"], {
            "kind": "model", "provider": provider, "model": "m", "base_url": "",
            "encrypted_api_key": self.box.encrypt("key", context=f"connection:{self.user['id']}:model"),
            "config_json": "{}", "enabled": True, "last_error": last_error,
        })

    def test_dashboard_start_asks_for_profile_first(self):
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["next_step"]["kind"], "profile")
        self.assertEqual(body["channels"]["mailbox"]["state"], "missing")
        self.assertEqual(body["today"]["messages"], 0)

    def test_dashboard_verification_state_reflects_recorded_result(self):
        self._complete_profile()
        mailbox_id = self._add_mailbox()
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["next_step"]["kind"], "verify")
        self.assertEqual(body["channels"]["mailbox"]["state"], "unknown")

        self.db.record_mailbox_verification(mailbox_id)
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["channels"]["mailbox"]["state"], "ok")
        self.assertIn("检查成功", body["channels"]["mailbox"]["detail"])

        self.db.record_mailbox_verification(mailbox_id, error="授权码不正确")
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["channels"]["mailbox"]["state"], "error")
        self.assertIn("授权码不正确", body["channels"]["mailbox"]["detail"])

    # -- "nothing has arrived yet" ------------------------------------------

    def _ready(self, hours_ago: float = 5.0) -> None:
        """Profile + verified mailbox + model, registered `hours_ago` hours ago."""
        self._complete_profile()
        mailbox_id = self._add_mailbox()
        self.db.record_mailbox_verification(mailbox_id)
        self._add_model()
        # Both clocks are backdated: the warning counts from the later of
        # "registered" and "mailbox last checked", so a test that only moved
        # created_at would still be inside the cold-start window.
        with self.db.connect() as connection:
            stamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)).isoformat(timespec="seconds")
            connection.execute("UPDATE users SET created_at=? WHERE id=?", (stamp, self.user["id"]))
            connection.execute("UPDATE mailboxes SET last_verified_at=? WHERE user_id=?", (stamp, self.user["id"]))

    def _add_message(self, status: str, skip_reason: str = "") -> None:
        mailbox = self.db.get_mailbox(self.user["id"])
        self.db.insert_message(self.user["id"], mailbox["id"], "1", 500, {
            "subject": "s", "sender_name": "t", "sender_address": "teacher@cityu.edu.hk",
            "received": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "body": b"body", "skip_reason": skip_reason,
        })
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE messages SET status=?, skip_reason=? WHERE user_id=?",
                (status, skip_reason, self.user["id"]))

    def test_setup_done_but_nothing_received_asks_to_check_forwarding(self):
        """The one step we cannot verify from our side is the school's forwarding
        rule, so a silent inbox must not be reported as "everything is ready"."""
        self._ready()
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["next_step"]["kind"], "mailbox")
        self.assertEqual(body["next_step"].get("tone"), "warn")
        self.assertIn("CityU 邮件", body["next_step"]["title"])

    def test_no_warning_during_the_first_two_hours(self):
        self._ready(hours_ago=0.2)
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["next_step"]["kind"], "done")

    def test_a_single_cityu_mail_clears_the_warning(self):
        self._ready()
        self._add_message("sent")
        body = web.build_dashboard(self._user_row())
        self.assertNotEqual(body["next_step"]["kind"], "mailbox")
        self.assertNotEqual(body["next_step"].get("tone"), "warn")

    def test_skipped_mail_does_not_count_as_a_working_forward(self):
        """Someone else's newsletter landing in the same inbox proves nothing."""
        self._ready()
        self._add_message("skipped", "发件人不在允许名单内")
        self.assertEqual(self.db.count_analysed_messages(self.user["id"]), 0)
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["next_step"].get("tone"), "warn")

    def test_no_warning_when_immediate_reports_are_switched_off(self):
        """With immediate reports off the mailbox is not polled at all, so an
        empty inbox is expected rather than suspicious."""
        self._ready()
        self.db.upsert_profile(self.user["id"], {"immediate_enabled": False})
        body = web.build_dashboard(self._user_row())
        self.assertNotEqual(body["next_step"].get("tone"), "warn")

    def test_verification_does_not_touch_the_uid_cursor(self):
        self._complete_profile()
        mailbox_id = self._add_mailbox()
        self.db.update_mailbox_poll(mailbox_id, last_uid=500, uid_validity="123")
        self.db.record_mailbox_verification(mailbox_id)
        with self.db.connect() as connection:
            row = connection.execute("SELECT last_uid,uid_validity FROM mailboxes WHERE id=?", (mailbox_id,)).fetchone()
        self.assertEqual(row["last_uid"], 500)
        self.assertEqual(row["uid_validity"], "123")

    def test_stale_verification_asks_to_recheck(self):
        self._complete_profile()
        mailbox_id = self._add_mailbox()
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat(timespec="seconds")
        with self.db.connect() as connection:
            connection.execute("UPDATE mailboxes SET last_verified_at=? WHERE id=?", (old, mailbox_id))
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["channels"]["mailbox"]["state"], "stale")
        self.assertEqual(body["next_step"]["kind"], "verify")

    def test_today_tasks_are_extracted_with_deadlines(self):
        self._complete_profile()
        mailbox_id = self._add_mailbox()
        self.db.record_mailbox_verification(mailbox_id)
        self._add_model()
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        message_id = self.db.insert_message(
            self.user["id"], mailbox_id, "1", 1,
            {"subject": "作业截止", "sender_name": "老师", "sender_address": "t@x.hk",
             "received": now, "importance": "normal",
             "body": self.box.encrypt("body", context=f"message:{self.user['id']}")},
        )
        with self.db.connect() as connection:
            connection.execute("UPDATE messages SET received_at=?,status='sent' WHERE id=?", (now, message_id))
        self.db.create_report(
            user_id=self.user["id"], message_id=message_id, kind="immediate", subject="【AI邮件摘要】作业截止",
            body=self.box.encrypt(REPORT, context=f"report:{self.user['id']}"), sent_to="pilot@qq.com",
        )
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["next_step"]["kind"], "task")
        self.assertGreaterEqual(len(body["tasks"]), 1)
        self.assertEqual(body["tasks"][0]["deadline"], "2026/9/18 23:59")
        self.assertEqual(body["today"]["tasks"], len(body["tasks"]))
        self.assertIn("作业截止", body["recent"][0]["subject"])
        self.assertEqual(body["channels"]["model"]["state"], "ok")

    def test_native_search_marks_step_four_skippable(self):
        self._complete_profile()
        self._add_mailbox()
        self._add_model(provider="openai")
        body = web.build_dashboard(self._user_row())
        self.assertTrue(body["channels"]["search"]["native"])
        self.assertEqual(body["channels"]["search"]["state"], "ok")

    def test_search_error_is_surfaced_not_hidden(self):
        self._complete_profile()
        self._add_mailbox()
        self._add_model(provider="deepseek")
        self.db.upsert_connection(self.user["id"], {
            "kind": "search", "provider": "tavily", "model": "", "base_url": "",
            "encrypted_api_key": self.box.encrypt("k", context=f"connection:{self.user['id']}:search"),
            "config_json": "{}", "enabled": True,
        })
        self.db.record_connection_result(self.user["id"], "search", error="quota exhausted")
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["channels"]["search"]["state"], "error")
        self.assertIn("quota exhausted", body["channels"]["search"]["detail"])

    def test_model_error_is_surfaced_not_hidden(self):
        self._complete_profile()
        self._add_mailbox()
        self._add_model(provider="deepseek")
        self.db.record_connection_result(self.user["id"], "model", error="401 unauthorized")
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["channels"]["model"]["state"], "error")
        self.assertIn("401 unauthorized", body["channels"]["model"]["detail"])

    def test_setup_checklist_starts_with_everything_missing(self):
        """The four things a new user must do, and the honest starting state.

        Four of the seven production accounts stalled here and the setup page had
        no way to say what was missing -- so the checklist is the fix, and its
        *empty* state is the one that matters most.
        """
        setup = self.db.setup_progress(self.user["id"])
        self.assertEqual([key for key in setup],
                         ["emails", "mailbox", "forwarding", "report"])
        self.assertFalse(setup["emails"]["ok"])
        self.assertIn("还没填写", setup["emails"]["detail"])
        self.assertFalse(setup["forwarding"]["ok"])
        # `verification_lights` states both reds differently on purpose:
        # "never tried" and "tried and failed" need different next actions.
        self.assertEqual(setup["mailbox"]["state"], "untested")
        self.assertEqual(setup["report"]["state"], "untested")

    def test_setup_checklist_names_the_one_missing_field(self):
        """Saying "还差私人转发邮箱" is the whole point; a generic "未完成" is not."""
        self.db.upsert_mailbox(self.user["id"], {
            "email": "me@qq.com", "report_to": "me@qq.com",
            "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465,
            "encrypted_password": b"x"})
        setup = self.db.setup_progress(self.user["id"])
        self.assertFalse(setup["emails"]["ok"])
        self.assertIn("CityU 学校邮箱", setup["emails"]["detail"])

    def test_a_wrong_password_shows_the_error_not_a_tick(self):
        """Asked for by the real case: an account that pasted the QQ login
        password got a red light, and the message has to say what it was."""
        mailbox_id = self.db.upsert_mailbox(self.user["id"], {
            "email": "me@qq.com", "report_to": "me@qq.com",
            "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465,
            "encrypted_password": b"x"})
        self.db.update_mailbox_poll(mailbox_id, last_uid=1, uid_validity="1",
                                    error="IMAP 连接失败：b'LOGIN Login error or password error'")
        setup = self.db.setup_progress(self.user["id"])
        self.assertFalse(setup["mailbox"]["ok"])
        self.assertEqual(setup["mailbox"]["state"], "failed")
        self.assertIn("LOGIN", setup["mailbox"]["detail"])

    def test_a_successful_poll_is_proof_even_without_pressing_the_button(self):
        """The worker polls every minute; a user should not have to press
        「只读连接测试」 to be told their mailbox works."""
        mailbox_id = self.db.upsert_mailbox(self.user["id"], {
            "email": "me@qq.com", "report_to": "me@qq.com",
            "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465,
            "encrypted_password": b"x"})
        self.db.update_mailbox_poll(mailbox_id, last_uid=5, uid_validity="1", error="")
        setup = self.db.setup_progress(self.user["id"])
        self.assertTrue(setup["mailbox"]["ok"], setup["mailbox"])

    def test_forwarding_is_proven_only_by_mail_actually_arriving(self):
        """The one step we cannot perform and cannot test from our side. A
        configured mailbox is not evidence that the school rule exists."""
        mailbox_id = self.db.upsert_mailbox(self.user["id"], {
            "email": "me@qq.com", "report_to": "me@qq.com",
            "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465,
            "encrypted_password": b"x"})
        self.db.update_mailbox_poll(mailbox_id, last_uid=1, uid_validity="1", error="")
        self.assertFalse(self.db.setup_progress(self.user["id"])["forwarding"]["ok"])
        # A skipped message is somebody else's newsletter: it proves the mailbox
        # is reachable, not that CityU is forwarding.
        self.db.insert_message(self.user["id"], mailbox_id, "1", 2,
                               {"subject": "newsletter", "sender_address": "a@b.example.com"})
        skipped = self.db.due_messages()[0]["id"]
        self.db.mark_message_skipped(skipped, "非本校发件域")
        self.assertFalse(self.db.setup_progress(self.user["id"])["forwarding"]["ok"])

    def test_the_checklist_travels_in_the_dashboard(self):
        payload = web.build_dashboard(self._user_row())
        self.assertIn("setup", payload)
        self.assertEqual(set(payload["setup"]),
                         {"emails", "mailbox", "forwarding", "report"})

    def test_verify_endpoint_requires_a_saved_mailbox(self):
        import http.cookiejar
        import json
        import threading
        import urllib.error
        import urllib.request
        from pilot_app.security import new_token, token_hash

        session = new_token()
        expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat(timespec="seconds")
        self.db.create_session(self.user["id"], token_hash(session), expires)

        server = web.create_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:%d" % server.server_address[1]
            jar = http.cookiejar.CookieJar()
            jar.set_cookie(http.cookiejar.Cookie(
                version=0, name="cityu_mail_session", value=session, port=None, port_specified=False,
                domain="127.0.0.1", domain_specified=True, domain_initial_dot=False, path="/",
                path_specified=True, secure=False, expires=None, discard=False, comment=None,
                comment_url=None, rest={},
            ))
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

            def call(method, path, payload=None):
                data = None if payload is None else json.dumps(payload).encode()
                request = urllib.request.Request(base + path, data=data, method=method)
                request.add_header("Content-Type", "application/json")
                try:
                    with opener.open(request, timeout=20) as response:
                        return response.status, json.loads(response.read().decode())
                except urllib.error.HTTPError as error:
                    return error.code, json.loads(error.read().decode())

            status, body = call("GET", "/api/dashboard")
            self.assertEqual(status, 200, body)
            self.assertIn("next_step", body)
            self.assertEqual(body["next_step"]["kind"], "profile")
            status, body = call("POST", "/api/mailbox/verify")
            self.assertEqual(status, 422)
            self.assertIn("私人转发邮箱", body["detail"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
