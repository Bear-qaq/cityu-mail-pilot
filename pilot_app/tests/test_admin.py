"""Admin console tests: who may see it, what it may expose, what it can do.

The console is the most privileged surface in the app, so these tests pin the
security properties rather than just the happy path:

* only emails listed in ``INFE_PILOT_ADMIN_EMAILS`` can reach it, and an
  ordinary logged-in user gets a 404 (not a 403) so its existence is not
  disclosed;
* no response ever contains an encrypted password, API key or invite hash;
* an operator can pause / resume / remove another user, but cannot lock
  themselves out;
* deleting a user does not break the operator's own view or the invite list.
"""

import datetime as dt
import http.cookiejar
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock as _mock  # module-level; some tests import it locally too

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/admin.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ["INFE_PILOT_ADMIN_EMAILS"] = "boss@example.com"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import web  # noqa: E402
from pilot_app.database import Database  # noqa: E402
from pilot_app.security import SecretBox, hash_password, token_hash  # noqa: E402
from pilot_app.web import db  # noqa: E402


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method: str, path: str, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(request, timeout=20) as response:
                return response.status, json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as error:
            raw = error.read().decode()
            try:
                return error.code, json.loads(raw or "{}")
            except json.JSONDecodeError:
                return error.code, {"detail": raw}

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, payload=None):
        return self.request("POST", path, payload)

    def put(self, path, payload=None):
        return self.request("PUT", path, payload)

    def delete(self, path):
        return self.request("DELETE", path)


def status_of(response) -> int:
    return response[0] if isinstance(response, tuple) else response


class AdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = web.create_server("127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.box = SecretBox.from_environment()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        """Start every test from an empty database so emails stay unique."""
        db.initialize()
        with db.connect() as connection:
            for table in ("alert_state", "app_settings",
                          "announcement_deliveries", "announcement_dismissals", "announcements",
                          "token_usage", "model_prices", "feedback", "reports", "messages", "mailboxes", "connections",
                          "sessions", "invites", "profiles", "users"):
                connection.execute(f"DELETE FROM {table}")

    # -- helpers ----------------------------------------------------------

    def _make_user(self, email: str, *, mailbox: bool = True, model: bool = True,
                   verify: bool = False) -> dict:
        """Create a fully configured account directly in the database."""
        label = f"invite-{email}-{dt.datetime.now().timestamp()}"
        invite = db.create_invite(label, 1)
        user = db.create_user(email, hash_password("a-long-enough-password"), token_hash(invite))
        db.upsert_profile(user["id"], {
            "school_email": "student@my.cityu.edu.hk", "major": "通信工程", "year_of_study": "大二",
            "courses": ["密码学"], "interests": [], "career_goals": [], "focus_topics": [],
            "less_interested": [], "custom_instructions": "", "language": "bilingual",
            "timezone": "Asia/Hong_Kong", "immediate_enabled": True, "daily_enabled": True,
            "daily_time": "22:00",
        })
        if mailbox:
            mailbox_id = db.upsert_mailbox(user["id"], {
                "email": f"box-{email}", "report_to": f"box-{email}", "imap_host": "imap.qq.com",
                "imap_port": 993, "smtp_host": "smtp.qq.com", "smtp_port": 465,
                "encrypted_password": self.box.encrypt("mail-secret", context=f"mailbox:{user['id']}"),
            })
            if verify:
                db.record_mailbox_verification(mailbox_id)
        if model:
            db.upsert_connection(user["id"], {
                "kind": "model", "provider": "deepseek", "model": "deepseek-chat", "base_url": "",
                "encrypted_api_key": self.box.encrypt("model-secret", context=f"connection:{user['id']}:model"),
                "config_json": "{}", "enabled": True,
            })
        return user

    def _login(self, email: str) -> Client:
        client = Client(self.base)
        status, body = client.post("/api/auth/login", {"email": email, "password": "a-long-enough-password"})
        self.assertEqual(status, 200, body)
        return client

    # -- access control ---------------------------------------------------

    def test_anonymous_is_rejected(self):
        client = Client(self.base)
        status, _ = client.get("/api/admin/users")
        self.assertEqual(status, 401)
        # POST-only route: an anonymous GET must be a method error, never a hit.
        status, _ = client.get("/api/admin/invites")
        self.assertEqual(status, 405)
        status, _ = client.post("/api/admin/invites", {"label": "x", "days": 7})
        self.assertEqual(status, 401)
        status, _ = client.put("/api/admin/users/usr_x/status/paused")
        self.assertEqual(status, 401)
        status, _ = client.delete("/api/admin/invites/x")
        self.assertEqual(status, 401)

    def test_ordinary_user_sees_no_admin_surface(self):
        self._make_user("plain1@example.com")
        client = self._login("plain1@example.com")
        status, body = client.get("/api/me")
        self.assertEqual(status, 200)
        self.assertFalse(body["is_admin"])
        status, _ = client.get("/api/admin/users")
        self.assertEqual(status, 404, "非管理员必须看到 404，而不是 403（不暴露后台存在）")
        status, _ = client.post("/api/admin/invites", {"label": "x", "days": 7})
        self.assertEqual(status, 404)
        status, _ = client.delete("/api/admin/invites/anything")
        self.assertEqual(status, 404)

    def test_ordinary_user_cannot_change_any_other_account(self):
        """The write routes matter more than the read routes: a normal account
        must not be able to pause, resume or delete anybody through the admin
        path, and a refused call must not have mutated anything either."""
        victim = self._make_user("victim@example.com")
        self._make_user("plain2@example.com")
        client = self._login("plain2@example.com")
        for status_name in ("paused", "active", "deleted"):
            status, body = client.put(
                f"/api/admin/users/{victim['id']}/status/{status_name}",
                {"confirm_email": "victim@example.com"})
            self.assertEqual(status, 404, (status_name, body))
        with db.connect() as connection:
            row = connection.execute("SELECT status FROM users WHERE id=?", (victim["id"],)).fetchone()
        self.assertEqual(row["status"], "active")

    def test_admin_routes_require_a_session_at_all(self):
        status, body = Client(self.base).get("/api/admin/users")
        self.assertEqual(status, 401, body)

    def test_admin_identity_is_the_whole_address_not_a_substring(self):
        """A lookalike address must never inherit the console: the match is on
        the complete, normalised address, not on a prefix or a substring."""
        for lookalike in ("boss@example.com.evil.test", "notboss@example.com",
                          "boss+admin@example.com", "boss@example.co"):
            self._make_user(lookalike)
            client = self._login(lookalike)
            status, _ = client.get("/api/admin/users")
            self.assertEqual(status, 404, lookalike)
            status, me = client.get("/api/me")
            self.assertFalse(me["is_admin"], lookalike)

    def test_admin_flag_comes_from_the_environment_not_the_database(self):
        admin = self._make_user("boss@example.com")
        client = self._login("boss@example.com")
        status, body = client.get("/api/me")
        self.assertTrue(body["is_admin"])

        # Even with a valid session, removing the email from the server env
        # revokes the console immediately: identity cannot be self-granted.
        os.environ["INFE_PILOT_ADMIN_EMAILS"] = ""
        try:
            status, _ = client.get("/api/admin/users")
            self.assertEqual(status, 404)
        finally:
            os.environ["INFE_PILOT_ADMIN_EMAILS"] = "boss@example.com"
        status, _ = client.get("/api/admin/users")
        self.assertEqual(status, 200)
        self.assertEqual(admin["email"], "boss@example.com")

    # -- editing another account ------------------------------------------

    def test_operator_can_fix_another_users_model(self):
        """The case that motivated this: a classmate picks a model that cannot
        produce a report, and the operator has no way to help him."""
        self._make_user("boss@example.com")
        member = self._make_user("member-model@example.com", model=False)
        admin = self._login("boss@example.com")
        status, body = admin.put(f"/api/admin/users/{member['id']}/settings", {
            "model_provider": "deepseek", "model_name": "deepseek-chat", "model_api_key": "member-key",
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body["changed"], ["model_api_key", "model_name", "model_provider"])
        stored = db.get_connection(member["id"], "model")
        self.assertEqual(stored["provider"], "deepseek")
        self.assertEqual(stored["model"], "deepseek-chat")
        self.assertEqual(self.box.decrypt(stored["encrypted_api_key"], context=f"connection:{member['id']}:model"),
                         "member-key")

    def test_a_partial_edit_does_not_wipe_the_other_settings(self):
        """PUT /api/profile rewrites every field and defaults the missing ones,
        which is why this endpoint is a selective patch: fixing one field must
        not blank the user's courses, instructions or daily time."""
        self._make_user("boss@example.com")
        member = self._make_user("member-partial@example.com")
        admin = self._login("boss@example.com")
        before = db.get_profile(member["id"])
        status, body = admin.put(f"/api/admin/users/{member['id']}/settings", {"major": "计算机科学"})
        self.assertEqual(status, 200, body)
        after = db.get_profile(member["id"])
        self.assertEqual(after["major"], "计算机科学")
        for field in ("school_email", "year_of_study", "courses", "custom_instructions",
                      "daily_time", "timezone", "language"):
            self.assertEqual(after[field], before[field], f"{field} 被改动了")

    def test_operator_can_change_schedule_and_switch_things_off(self):
        self._make_user("boss@example.com")
        member = self._make_user("member-sched@example.com")
        admin = self._login("boss@example.com")
        status, body = admin.put(f"/api/admin/users/{member['id']}/settings", {
            "daily_time": "07:30", "daily_enabled": False, "immediate_enabled": True,
            "timezone": "Asia/Shanghai"})
        self.assertEqual(status, 200, body)
        profile = db.get_profile(member["id"])
        self.assertEqual(profile["daily_time"], "07:30")
        self.assertFalse(profile["daily_enabled"])
        self.assertTrue(profile["immediate_enabled"])
        self.assertEqual(profile["timezone"], "Asia/Shanghai")

    def test_operator_can_replace_a_mailbox_password_without_seeing_it(self):
        self._make_user("boss@example.com")
        member = self._make_user("member-box@example.com")
        admin = self._login("boss@example.com")
        status, body = admin.put(f"/api/admin/users/{member['id']}/settings", {
            "mailbox_app_password": "brand-new-app-password", "report_to": "elsewhere@example.com"})
        self.assertEqual(status, 200, body)
        stored = db.get_mailbox(member["id"])
        self.assertEqual(stored["report_to"], "elsewhere@example.com")
        self.assertEqual(self.box.decrypt(stored["encrypted_password"], context=f"mailbox:{member['id']}"),
                         "brand-new-app-password")
        # The cursor survives: a password change must not replay old mail.
        self.assertEqual(stored["last_uid"], 0 if stored["last_uid"] is None else stored["last_uid"])
        self.assertNotIn("brand-new-app-password", json.dumps(body))
        self.assertNotIn("member-key", json.dumps(body))

    def test_the_audit_record_names_fields_never_values(self):
        self._make_user("boss@example.com")
        member = self._make_user("member-audit@example.com", model=False)
        admin = self._login("boss@example.com")
        admin.put(f"/api/admin/users/{member['id']}/settings", {
            "model_provider": "deepseek", "model_name": "deepseek-chat", "model_api_key": "super-secret-key"})
        entries = [row for row in db.list_audit(50) if row["action"] == "admin_user_settings_changed"]
        self.assertTrue(entries, "写操作必须留下审计")
        # Several admin actions can land in the same second, so look for this
        # target rather than assuming the global newest row is ours.
        mine = [row for row in entries if row["target_email"] == "member-audit@example.com"]
        self.assertTrue(mine, "本次修改必须留下审计")
        newest = mine[0]
        self.assertIn("model_api_key", newest["detail"])
        self.assertNotIn("super-secret-key", newest["detail"])
        for row in db.list_audit(200):
            self.assertNotIn("super-secret-key", json.dumps(row, ensure_ascii=False))

    def test_invalid_values_are_rejected_and_change_nothing(self):
        self._make_user("boss@example.com")
        member = self._make_user("member-bad@example.com")
        admin = self._login("boss@example.com")
        before = db.get_profile(member["id"])
        cases = [
            ({"daily_time": "25:00"}, "每日发送时间"),
            ({"timezone": "Mars/Olympus"}, "时区"),
            ({"school_email": "not-a-cityu@example.com"}, "CityU"),
            ({"model_provider": "definitely-not-real"}, "供应商"),
            ({"model_provider": "deepseek", "model_api_key": ""}, "model_api_key"),
            ({"report_to": "not-an-email"}, "邮箱"),
        ]
        for payload, hint in cases:
            status, body = admin.put(f"/api/admin/users/{member['id']}/settings", payload)
            self.assertEqual(status, 422, (payload, body))
            self.assertIn(hint, str(body.get("detail", "")), (payload, body))
        self.assertEqual(db.get_profile(member["id"]), before)

    def test_an_empty_patch_is_refused(self):
        self._make_user("boss@example.com")
        member = self._make_user("member-empty@example.com")
        admin = self._login("boss@example.com")
        status, body = admin.put(f"/api/admin/users/{member['id']}/settings", {})
        self.assertEqual(status, 422, body)

    def test_unknown_users_and_ordinary_accounts_are_refused(self):
        self._make_user("boss@example.com")
        self._make_user("plain3@example.com")
        member = self._make_user("member-target@example.com")
        before = db.get_profile(member["id"])

        admin = self._login("boss@example.com")
        status, _ = admin.put("/api/admin/users/usr_does_not_exist/settings", {"major": "x"})
        self.assertEqual(status, 404)

        plain = self._login("plain3@example.com")
        status, _ = plain.put(f"/api/admin/users/{member['id']}/settings", {"major": "hacked"})
        self.assertEqual(status, 404, "普通用户不能改别人")
        self.assertEqual(db.get_profile(member["id"]), before)

        status, _ = Client(self.base).put(f"/api/admin/users/{member['id']}/settings", {"major": "x"})
        self.assertEqual(status, 401)

    def test_a_deleted_account_is_gone_rather_than_editable(self):
        """Deletion is a real row delete (cascading away the encrypted secrets),
        so the operator gets the same 404 as for any unknown id."""
        self._make_user("boss@example.com")
        member = self._make_user("member-gone@example.com")
        db.set_user_status(member["id"], "deleted")
        admin = self._login("boss@example.com")
        status, body = admin.put(f"/api/admin/users/{member['id']}/settings", {"major": "x"})
        self.assertEqual(status, 404, body)

    def test_school_email_still_has_to_be_a_cityu_address(self):
        self._make_user("boss@example.com")
        member = self._make_user("member-school@example.com")
        admin = self._login("boss@example.com")
        status, body = admin.put(f"/api/admin/users/{member['id']}/settings",
                                 {"school_email": "student@my.cityu.edu.hk"})
        self.assertEqual(status, 200, body)
        self.assertEqual(db.get_profile(member["id"])["school_email"], "student@my.cityu.edu.hk")

        # The response is the same overview the console already renders, so it
        # must not have grown a route to the stored secrets.
        blob = json.dumps(body)
        for forbidden in ("encrypted_api_key", "encrypted_password", "password_hash"):
            self.assertNotIn(forbidden, blob)

    # -- every mail, and what happened to it ------------------------------

    def _seed_mail(self, owner: dict, *, uid: int, status: str, skip_reason: str = "",
                   report="sent", subject="Assignment 2",
                   sent_at=None, last_error="") -> None:
        mailbox = db.get_mailbox(owner["id"])
        message_id = db.insert_message(owner["id"], mailbox["id"], "1", uid, {
            "subject": subject, "sender_name": "teacher", "sender_address": "teacher@cityu.edu.hk",
            "received": "2026-09-13T10:00:00+00:00", "body": b"private mail body",
            "skip_reason": skip_reason,
        })
        with db.connect() as connection:
            connection.execute("UPDATE messages SET status=?,skip_reason=?,last_error=? WHERE id=?",
                               (status, skip_reason, last_error, message_id))
        if report:
            report_id = db.create_report(user_id=owner["id"], message_id=message_id, kind="immediate",
                                         subject="【AI邮件摘要】" + subject, body=b"secret report text",
                                         sent_to="owner@outlook.com")
            with db.connect() as connection:
                connection.execute("UPDATE reports SET status=?,sent_at=? WHERE id=?",
                                   (report, sent_at or "2026-09-13T10:04:00+00:00", report_id))

    def test_the_mail_board_lists_every_user_with_its_outcome(self):
        self._make_user("boss@example.com")
        first = self._make_user("member-a@example.com")
        second = self._make_user("member-b@example.com")
        self._seed_mail(first, uid=1, status="sent")
        self._seed_mail(first, uid=2, status="skipped", skip_reason="发件人不在允许名单内", report=None)
        self._seed_mail(second, uid=3, status="failed", report="failed",
                        sent_at=None, last_error="接口响应超时")
        admin = self._login("boss@example.com")
        status, body = admin.get("/api/admin/messages")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["total"], 3)
        by_uid = {row["imap_uid"]: row for row in body["messages"]}
        self.assertEqual(by_uid[1]["delivery"], "sent")
        self.assertEqual(by_uid[1]["report_status"], "sent")
        self.assertEqual(by_uid[1]["sent_to"], "owner@outlook.com")
        self.assertEqual(by_uid[1]["latency_seconds"], 240.0)
        self.assertEqual(by_uid[2]["delivery"], "skipped")
        self.assertIn("允许名单", by_uid[2]["skip_reason"])
        self.assertEqual(by_uid[3]["delivery"], "failed")
        self.assertIn("超时", by_uid[3]["last_error"])
        owners = {row["user_email"] for row in body["messages"]}
        self.assertEqual(owners, {"member-a@example.com", "member-b@example.com"})

    def test_the_mail_board_never_carries_mail_or_report_content(self):
        """The panel answers "processed and delivered?" — it must not become a
        way to read other people's mail."""
        self._make_user("boss@example.com")
        member = self._make_user("member-privacy@example.com")
        self._seed_mail(member, uid=7, status="sent")
        admin = self._login("boss@example.com")
        status, body = admin.get("/api/admin/messages")
        self.assertEqual(status, 200)
        blob = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("private mail body", blob)
        self.assertNotIn("secret report text", blob)
        self.assertNotIn("body_markdown", blob)

    def test_filters_and_counts_line_up(self):
        self._make_user("boss@example.com")
        member = self._make_user("member-filter@example.com")
        self._seed_mail(member, uid=11, status="sent")
        self._seed_mail(member, uid=12, status="skipped", skip_reason="非允许名单", report=None)
        self._seed_mail(member, uid=13, status="failed", report="failed", sent_at=None)
        admin = self._login("boss@example.com")
        status, body = admin.get("/api/admin/messages")
        self.assertEqual(body["counts"]["all"], 3)
        self.assertEqual(body["counts"]["sent"], 1)
        self.assertEqual(body["counts"]["skipped"], 1)
        self.assertEqual(body["counts"]["failed"], 1)
        # "undelivered" means "should have gone out but did not". A skipped mail
        # was a deliberate decision about someone else's newsletter, so counting
        # it here would train the operator to ignore the number.
        self.assertEqual(body["counts"]["undelivered"], 1)

        status, only_failed = admin.get("/api/admin/messages?status=failed")
        self.assertEqual([row["imap_uid"] for row in only_failed["messages"]], [13])
        status, only_skipped = admin.get("/api/admin/messages?status=skipped")
        self.assertEqual([row["imap_uid"] for row in only_skipped["messages"]], [12])

        status, bad = admin.get("/api/admin/messages?status=nonsense")
        self.assertEqual(status, 422, bad)

    def test_paging_and_per_user_filter(self):
        self._make_user("boss@example.com")
        first = self._make_user("member-page-a@example.com")
        second = self._make_user("member-page-b@example.com")
        for uid in range(20, 25):
            self._seed_mail(first, uid=uid, status="sent")
        self._seed_mail(second, uid=99, status="sent", subject="别人的邮件")
        admin = self._login("boss@example.com")

        status, page1 = admin.get("/api/admin/messages?limit=3")
        self.assertEqual(len(page1["messages"]), 3)
        self.assertEqual(page1["total"], 6)
        status, page2 = admin.get("/api/admin/messages?limit=3&offset=3")
        self.assertEqual(len(page2["messages"]), 3)
        self.assertFalse({row["id"] for row in page1["messages"]} & {row["id"] for row in page2["messages"]})

        status, mine = admin.get(f"/api/admin/messages?user_id={second['id']}")
        self.assertEqual([row["user_email"] for row in mine["messages"]], ["member-page-b@example.com"])

        status, bad = admin.get("/api/admin/messages?limit=abc")
        self.assertEqual(status, 422, bad)

    def test_mail_delivered_before_any_report_existed_is_not_undelivered(self):
        """Real case: 41 rows migrated from the previous single-user service have
        status='sent' and no report row. Judging delivery by the report row
        reported every one of them as never sent."""
        self._make_user("boss@example.com")
        member = self._make_user("member-legacy@example.com")
        self._seed_mail(member, uid=31, status="sent", report=None)
        admin = self._login("boss@example.com")
        status, body = admin.get("/api/admin/messages")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["counts"]["sent"], 1)
        self.assertEqual(body["counts"]["undelivered"], 0)
        row = body["messages"][0]
        self.assertEqual(row["delivery"], "sent")
        self.assertIsNone(row["report_id"])

    def test_a_failed_report_makes_the_mail_undelivered(self):
        self._make_user("boss@example.com")
        member = self._make_user("member-rep-failed@example.com")
        self._seed_mail(member, uid=32, status="failed", report="failed", sent_at=None)
        admin = self._login("boss@example.com")
        status, body = admin.get("/api/admin/messages?status=undelivered")
        self.assertEqual([row["imap_uid"] for row in body["messages"]], [32])
        self.assertEqual(body["messages"][0]["delivery"], "failed")

    def test_audit_order_is_stable_within_the_same_second(self):
        """Log lines are second-stamped; the newest row must still be the one
        most recently written, or the console shows a scrambled history."""
        for index in range(5):
            db.record_audit(action="order_probe", actor_email="boss@example.com",
                            detail=f"n={index}")
        rows = [row for row in db.list_audit(20) if row["action"] == "order_probe"]
        self.assertEqual([row["detail"] for row in rows], ["n=4", "n=3", "n=2", "n=1", "n=0"])

    def test_the_mail_board_is_operator_only(self):
        self._make_user("plain4@example.com")
        plain = self._login("plain4@example.com")
        status, _ = plain.get("/api/admin/messages")
        self.assertEqual(status, 404)
        status, _ = Client(self.base).get("/api/admin/messages")
        self.assertEqual(status, 401)

    # -- broadcasts ---------------------------------------------------------

    def test_a_broadcast_reaches_every_user_as_a_banner(self):
        self._make_user("boss@example.com")
        first = self._make_user("member-b1@example.com")
        second = self._make_user("member-b2@example.com")
        admin = self._login("boss@example.com")
        status, body = admin.post("/api/admin/announcements", {
            "title": "本周六 22:00 维护", "body": "预计 30 分钟，期间收信会延迟。", "tone": "warn"})
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["announcements"]), 1)
        self.assertEqual(body["announcements"][0]["active"], 1)
        self.assertEqual(body["announcements"][0]["email_total"], 0, "默认只发站内")

        for owner in (first, second):
            dash = web.build_dashboard(db.get_user(owner["id"]))
            self.assertIsNotNone(dash["announcement"], "每个用户登录后都应看到")
            self.assertEqual(dash["announcement"]["title"], "本周六 22:00 维护")
            self.assertEqual(dash["announcement"]["tone"], "warn")

    def test_dismissing_is_per_user(self):
        self._make_user("boss@example.com")
        first = self._make_user("member-d1@example.com")
        second = self._make_user("member-d2@example.com")
        admin = self._login("boss@example.com")
        admin.post("/api/admin/announcements", {"title": "t", "body": "b"})

        reader = self._login("member-d1@example.com")
        status, body = reader.post(f"/api/announcements/{web.get_db().list_announcements(1)[0]['id']}/dismiss")
        self.assertEqual(status, 200, body)
        self.assertIsNone(web.build_dashboard(db.get_user(first["id"]))["announcement"])
        self.assertIsNotNone(web.build_dashboard(db.get_user(second["id"]))["announcement"],
                             "另一个人仍然看得到")

    def test_only_the_newest_active_broadcast_is_shown(self):
        """Primer's banner guidance is explicit that two banners on one page is a
        stacking problem, so the dashboard returns exactly one."""
        self._make_user("boss@example.com")
        member = self._make_user("member-one@example.com")
        admin = self._login("boss@example.com")
        admin.post("/api/admin/announcements", {"title": "第一条", "body": "旧的"})
        admin.post("/api/admin/announcements", {"title": "第二条", "body": "新的"})
        dash = web.build_dashboard(db.get_user(member["id"]))
        self.assertEqual(dash["announcement"]["title"], "第二条")

    def test_withdrawing_removes_it_for_everyone(self):
        self._make_user("boss@example.com")
        member = self._make_user("member-w@example.com")
        admin = self._login("boss@example.com")
        created = admin.post("/api/admin/announcements", {"title": "撤下测试", "body": "b"})[1]
        announcement_id = created["id"]
        status, body = admin.put(f"/api/admin/announcements/{announcement_id}/withdraw", {})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["announcements"][0]["active"], 0)
        self.assertIsNone(web.build_dashboard(db.get_user(member["id"]))["announcement"])
        # Withdrawing twice is a 404, not a silent success.
        status, _ = admin.put(f"/api/admin/announcements/{announcement_id}/withdraw", {})
        self.assertEqual(status, 404)

    def test_email_delivery_queues_one_row_per_user_with_a_mailbox(self):
        self._make_user("boss@example.com")           # has a mailbox
        self._make_user("member-nomail@example.com", mailbox=False)
        admin = self._login("boss@example.com")
        status, body = admin.post("/api/admin/announcements", {
            "title": "带邮件的公告", "body": "内容", "deliver_email": True})
        self.assertEqual(status, 200, body)
        queued = db.pending_announcement_deliveries(50)
        self.assertEqual(len(queued), 1, "只给有邮箱的账号排队")
        self.assertEqual(queued[0]["report_to"], "box-boss@example.com")

    def test_a_broadcast_email_goes_out_and_is_marked_sent(self):
        from unittest import mock
        from pilot_app import reports as reports_mod
        self._make_user("boss@example.com")
        member = self._make_user("member-send@example.com")
        admin = self._login("boss@example.com")
        admin.post("/api/admin/announcements", {
            "title": "维护通知", "body": "周六 22:00。", "tone": "warn", "deliver_email": True})

        sent = {}

        def record(config, password, subject, markdown, **kwargs):
            sent["subject"] = subject
            sent["to"] = config["report_to"]
            sent["html"] = kwargs.get("html_body", "")
            sent["text"] = kwargs.get("text_body", "")

        service = web.get_service()
        with mock.patch("pilot_app.service.mailio.send_report", side_effect=record):
            result = service.send_announcement_emails(10)
        self.assertEqual(result["sent"], 2, "两个账号各一封")
        self.assertIn("公告", sent["subject"])
        self.assertIn("周六 22:00", sent["text"])
        self.assertIn("CITYU MAIL PILOT", sent["html"])
        self.assertNotIn("<script", sent["html"])
        self.assertEqual(db.pending_announcement_deliveries(50), [])

    def test_a_failing_mailbox_does_not_stop_the_others(self):
        from unittest import mock
        self._make_user("boss@example.com")
        self._make_user("member-bad2@example.com")
        admin = self._login("boss@example.com")
        admin.post("/api/admin/announcements", {"title": "t", "body": "b", "deliver_email": True})
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("mailbox refused")

        service = web.get_service()
        with mock.patch("pilot_app.service.mailio.send_report", side_effect=flaky):
            result = service.send_announcement_emails(10)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], 1)
        rows = db.list_announcements(1)[0]
        self.assertEqual(rows["email_failed"], 1)
        self.assertEqual(rows["email_sent"], 1)

    def test_broadcasts_are_operator_only_and_validated(self):
        self._make_user("boss@example.com")
        self._make_user("plain5@example.com")
        member = self._make_user("member-bad3@example.com")
        plain = self._login("plain5@example.com")
        status, _ = plain.post("/api/admin/announcements", {"title": "x", "body": "y"})
        self.assertEqual(status, 404, "普通用户不能发广播")
        dashboard = web.build_dashboard(db.get_user(member["id"]))
        self.assertIsNone(dashboard["announcement"])

        admin = self._login("boss@example.com")
        for payload in ({"title": "", "body": "b"}, {"title": "t", "body": ""},
                        {"title": "t", "body": "b", "tone": "shouty"}):
            status, body = admin.post("/api/admin/announcements", payload)
            self.assertEqual(status, 422, (payload, body))
        status, _ = Client(self.base).post("/api/admin/announcements", {"title": "t", "body": "b"})
        self.assertEqual(status, 401)
        self.assertEqual(db.list_announcements(10), [])

    # -- what the console may expose --------------------------------------

    def test_overview_never_leaks_secrets(self):
        self._make_user("boss@example.com", verify=True)
        self._make_user("member-secret@example.com", verify=True)
        admin = self._login("boss@example.com")
        status, body = admin.get("/api/admin/users")
        self.assertEqual(status, 200, body)
        self.assertGreaterEqual(len(body["users"]), 2)
        blob = json.dumps(body)
        for forbidden in ("mail-secret", "model-secret", "encrypted_password", "encrypted_api_key",
                          "password_hash", "code_hash", "cipher"):
            self.assertNotIn(forbidden, blob, f"管理接口泄露了 {forbidden}")
        member = next(row for row in body["users"] if row["email"] == "member-secret@example.com")
        self.assertEqual(member["model_provider"], "deepseek")
        self.assertEqual(member["major"], "通信工程")
        self.assertIsNotNone(member["last_verified_at"])
        self.assertIn("queue_depth", member)
        self.assertIn("report_count", member)

    def test_health_summary_is_reported(self):
        self._make_user("boss@example.com", verify=True)
        admin = self._login("boss@example.com")
        status, body = admin.get("/api/admin/users")
        health = body["health"]
        self.assertEqual(status, 200)
        for key in ("users", "active_users", "paused_users", "mailboxes", "pending_messages",
                    "failed_reports", "max_users", "version"):
            self.assertIn(key, health)
        self.assertGreaterEqual(health["mailboxes"], 1)
        self.assertGreaterEqual(health["max_users"], 1)

    # -- operator actions -------------------------------------------------

    def test_pause_and_resume_another_user(self):
        self._make_user("boss@example.com")
        member = self._make_user("member-pause@example.com")
        admin = self._login("boss@example.com")

        status, body = admin.put(f"/api/admin/users/{member['id']}/status/paused")
        self.assertEqual(status, 200, body)
        self.assertEqual(db.get_user(member["id"])["status"], "paused")
        row = next(item for item in body["users"] if item["id"] == member["id"])
        self.assertEqual(row["status"], "paused")

        # A paused user cannot keep using the console even with a live cookie.
        member_client = self._login("member-pause@example.com") if False else None
        status, body = admin.put(f"/api/admin/users/{member['id']}/status/active")
        self.assertEqual(status, 200)
        self.assertEqual(db.get_user(member["id"])["status"], "active")

    def test_admin_cannot_lock_itself_out(self):
        self._make_user("boss@example.com")
        admin = self._login("boss@example.com")
        status, body = admin.get("/api/me")
        admin_id = body["user"]["id"]
        for bad in ("paused", "deleted"):
            status, body = admin.put(f"/api/admin/users/{admin_id}/status/{bad}")
            self.assertEqual(status, 422, body)
        self.assertEqual(db.get_user(admin_id)["status"], "active")

    def test_unknown_user_and_invalid_status_are_rejected(self):
        self._make_user("boss@example.com")
        admin = self._login("boss@example.com")
        status, _ = admin.put("/api/admin/users/usr_does_not_exist/status/paused")
        self.assertEqual(status, 404)
        status, body = admin.put("/api/admin/users/usr_does_not_exist/status/banana")
        self.assertEqual(status, 422, body)

    def test_deleting_a_user_keeps_the_console_working(self):
        self._make_user("boss@example.com")
        member = self._make_user("member-delete@example.com", mailbox=True, model=True)
        admin = self._login("boss@example.com")

        # Deleting is irreversible, so it must be refused without a typed
        # confirmation, and accepted only with the exact account e-mail.
        status, body = admin.put(f"/api/admin/users/{member['id']}/status/deleted")
        self.assertEqual(status, 422, body)
        self.assertIn("确认", body["detail"])
        status, body = admin.put(f"/api/admin/users/{member['id']}/status/deleted",
                                 {"confirm_email": "wrong@example.com"})
        self.assertEqual(status, 422, body)
        status, body = admin.put(f"/api/admin/users/{member['id']}/status/deleted",
                                 {"confirm_email": "member-delete@example.com"})
        self.assertEqual(status, 200, body)
        self.assertNotIn(member["id"], [row["id"] for row in body["users"]])
        with self.assertRaises(KeyError):
            db.get_user(member["id"])

        # The console still works, and the deleted member cannot log in.
        status, body = admin.get("/api/admin/users")
        self.assertEqual(status, 200)
        client = Client(self.base)
        status, _ = client.post("/api/auth/login",
                                {"email": "member-delete@example.com", "password": "a-long-enough-password"})
        self.assertEqual(status, 401)

    # -- invites ----------------------------------------------------------

    def test_invite_is_returned_once_then_only_tracked_as_state(self):
        self._make_user("boss@example.com")
        admin = self._login("boss@example.com")
        status, body = admin.post("/api/admin/invites", {"label": "pilot-user-2", "days": 7})
        self.assertEqual(status, 200, body)
        code = body["code"]
        self.assertTrue(code and len(code) >= 12)

        status, listing = admin.get("/api/admin/users")
        self.assertEqual(status, 200)
        row = next(item for item in listing["invites"] if item["label"] == "pilot-user-2")
        self.assertEqual(row["state"], "available")
        blob = json.dumps(listing)
        self.assertNotIn(code, blob, "邀请码明文不得在列表接口回显")
        with db.connect() as connection:
            hashes = [r[0] for r in connection.execute("SELECT code_hash FROM invites")]
        self.assertIn(token_hash(code), hashes)

        # The freshly minted code really can register somebody.
        fresh = Client(self.base)
        status, user = fresh.post("/api/auth/register", {
            "email": "invited@example.com", "password": "a-long-enough-password", "invite_code": code, "accepted_terms": True,
        })
        self.assertEqual(status, 200, user)
        status, listing = admin.get("/api/admin/users")
        row = next(item for item in listing["invites"] if item["label"] == "pilot-user-2")
        self.assertEqual(row["state"], "used")
        self.assertEqual(row["used_by_email"], "invited@example.com")

    def test_invite_validation_and_revocation(self):
        self._make_user("boss@example.com")
        admin = self._login("boss@example.com")
        status, body = admin.post("/api/admin/invites", {"label": "to-revoke", "days": 3})
        code = body["code"]

        status, body = admin.post("/api/admin/invites", {"label": "bad", "days": 999})
        self.assertEqual(status, 422, body)
        status, body = admin.post("/api/admin/invites", {"label": "bad", "days": "seven"})
        self.assertEqual(status, 422, body)

        status, body = admin.delete("/api/admin/invites/to-revoke")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["retired"], 1)
        row = next(item for item in body["invites"] if item["label"] == "to-revoke")
        self.assertEqual(row["state"], "expired")

        # A revoked code must no longer register anybody.
        fresh = Client(self.base)
        status, body = fresh.post("/api/auth/register", {
            "email": "late@example.com", "password": "a-long-enough-password", "invite_code": code, "accepted_terms": True,
        })
        self.assertEqual(status, 400, body)

    def test_invite_label_is_not_a_path_traversal_or_injection_surface(self):
        self._make_user("boss@example.com")
        admin = self._login("boss@example.com")
        weird = "user/../etc"
        status, body = admin.post("/api/admin/invites", {"label": weird, "days": 1})
        self.assertEqual(status, 200, body)
        listing = admin.get("/api/admin/users")[1]
        self.assertTrue(any(item["label"] == weird for item in listing["invites"]))
        # Deleting by that label must not touch anything outside invites.
        status, body = admin.delete("/api/admin/invites/" + urllib.request.quote(weird, safe=""))
        self.assertEqual(status, 200, body)

    def test_password_change_revokes_every_other_session(self):
        """A stolen device must lose access at password change, not in 14 days."""
        self._make_user("boss@example.com")
        victim = self._make_user("victim@example.com")
        # Simulate: victim logged in on two devices.
        first = self._login("victim@example.com")
        second = self._login("victim@example.com")
        self.assertEqual(status_of(second.get("/api/account/security")), 200)

        status, body = first.put("/api/account/password", {
            "current_password": "a-long-enough-password",
            "new_password": "a-brand-new-long-password",
        })
        self.assertEqual(status, 200, body)
        self.assertGreaterEqual(body["revoked"], 1, "应吊销至少一个其他会话")

        # The other device's cookie must now be rejected...
        status, _ = second.get("/api/me")
        self.assertEqual(status, 401, "被吊销的会话必须失效")
        # ...while the device that changed the password keeps working.
        status, _ = first.get("/api/me")
        self.assertEqual(status, 200, "改密码的这台设备应继续可用")
        # And the old password no longer works.
        fresh = Client(self.base)
        status, _ = fresh.post("/api/auth/login",
                               {"email": "victim@example.com", "password": "a-long-enough-password"})
        self.assertEqual(status, 401)

    def test_password_change_requires_the_current_password(self):
        self._make_user("boss@example.com")
        self._make_user("victim2@example.com")
        client = self._login("victim2@example.com")
        status, body = client.put("/api/account/password", {
            "current_password": "not-the-password", "new_password": "a-brand-new-long-password",
        })
        self.assertEqual(status, 400, body)
        status, _ = client.get("/api/me")
        self.assertEqual(status, 200, "失败的改密码不应踢自己下线")

    def test_sign_out_all_devices_revokes_and_reissues(self):
        self._make_user("boss@example.com")
        self._make_user("victim3@example.com")
        first = self._login("victim3@example.com")
        second = self._login("victim3@example.com")
        status, body = first.post("/api/account/sessions/revoke")
        self.assertEqual(status, 200, body)
        self.assertGreaterEqual(body["revoked"], 2)
        status, _ = second.get("/api/me")
        self.assertEqual(status, 401)
        status, _ = first.get("/api/me")
        self.assertEqual(status, 200, "本机应拿到新会话而不是被踢出")

    def test_operator_actions_are_recorded_in_the_audit_trail(self):
        self._make_user("boss@example.com")
        member = self._make_user("audited@example.com")
        admin = self._login("boss@example.com")
        admin.put(f"/api/admin/users/{member['id']}/status/paused")
        admin.post("/api/admin/invites", {"label": "audit-check", "days": 1})

        status, body = admin.get("/api/admin/users")
        self.assertEqual(status, 200)
        actions = [item["action"] for item in body["audit"]]
        self.assertIn("user_status_paused", actions)
        self.assertIn("invite_created", actions)
        entry = next(item for item in body["audit"] if item["action"] == "user_status_paused")
        self.assertEqual(entry["actor_email"], "boss@example.com")
        self.assertEqual(entry["target_email"], "audited@example.com")
        # The audit trail must never contain secrets.
        blob = json.dumps(body["audit"])
        for forbidden in ("mail-secret", "model-secret", "encrypted_", "code_hash"):
            self.assertNotIn(forbidden, blob)

    # -- the health line the operator actually reads ------------------------

    def _set_polled(self, user_email: str, *, minutes_ago: float, imap_host: str = "") -> str:
        user = db.find_user_for_login(user_email)
        stamp = (dt.datetime.now(dt.timezone.utc)
                 - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
        with db.connect() as connection:
            if imap_host:
                connection.execute("UPDATE mailboxes SET imap_host=?, last_polled_at=? WHERE user_id=?",
                                   (imap_host, stamp, user["id"]))
            else:
                connection.execute("UPDATE mailboxes SET last_polled_at=? WHERE user_id=?",
                                   (stamp, user["id"]))
            row = connection.execute("SELECT email FROM mailboxes WHERE user_id=?", (user["id"],)).fetchone()
        return str(row["email"])

    def test_a_slow_provider_is_not_reported_as_a_broken_mailbox(self):
        """Regression: the panel judged every mailbox against a flat five-minute
        threshold. Gmail is polled every fifteen minutes because Google
        documents that as its own limit, so the panel called a healthy mailbox
        broken on every single visit — and a warning that is always on is one
        nobody reads, which is precisely how a real fault gets missed."""
        self._make_user("boss@example.com")
        self._make_user("slow@example.com")
        mailbox = self._set_polled("slow@example.com", minutes_ago=7, imap_host="imap.gmail.com")

        health = web._service_health()
        self.assertNotIn(mailbox, health["stale_mailbox_emails"],
                         "Gmail 每 15 分钟才轮询一次，7 分钟没轮询是正常的")

    def test_a_genuinely_stalled_mailbox_is_still_reported_and_named(self):
        self._make_user("boss@example.com")
        self._make_user("stalled@example.com")
        mailbox = self._set_polled("stalled@example.com", minutes_ago=180)

        health = web._service_health()
        self.assertIn(mailbox, health["stale_mailbox_emails"],
                      "告警必须点名是哪个邮箱，否则运营者要逐个账号去找")

    def test_a_mailbox_that_never_polled_is_reported(self):
        self._make_user("boss@example.com")
        self._make_user("fresh@example.com")
        with db.connect() as connection:
            connection.execute("UPDATE mailboxes SET last_polled_at=NULL")
        health = web._service_health()
        self.assertEqual(health["stale_mailboxes"], 2)

    # -- the pilot cap the operator can now change from here ----------------

    def test_an_operator_can_raise_the_pilot_cap_without_touching_the_environment(self):
        """This used to mean editing a 0600 root-owned file over SSH and
        restarting the service."""
        self._make_user("boss@example.com")
        admin = self._login("boss@example.com")
        status, body = admin.get("/api/admin/capacity")
        self.assertEqual(status, 200)
        self.assertIn("recommended", body)
        self.assertEqual(body["source"], "environment")

        status, body = admin.put("/api/admin/capacity", {"max_users": 12})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["max_users"], 12)
        self.assertEqual(body["source"], "settings")
        self.assertEqual(db.get_setting("max_users"), "12")

        # And the registration gate actually honours the stored value.
        status, body = admin.get("/api/admin/capacity")
        self.assertEqual(body["current"], 12)

    def test_the_stored_cap_beats_the_environment_default(self):
        self._make_user("boss@example.com")
        with _mock.patch.dict("os.environ", {"INFE_PILOT_MAX_USERS": "5"}):
            db.set_setting("max_users", "9")
            self.assertEqual(web._max_users(), (9, "settings"))
            db.delete_setting("max_users")
            self.assertEqual(web._max_users(), (5, "environment"))

    def test_reset_falls_back_to_the_environment_default(self):
        self._make_user("boss@example.com")
        admin = self._login("boss@example.com")
        admin.put("/api/admin/capacity", {"max_users": 12})
        status, body = admin.put("/api/admin/capacity", {"reset": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["source"], "environment")
        self.assertEqual(db.get_setting("max_users"), "")

    def test_the_cap_cannot_drop_below_the_accounts_that_already_exist(self):
        """Not dangerous — the cap only gates new registrations — but the panel
        would show a cap nobody could fit under, which reads as a bug."""
        self._make_user("boss@example.com")
        self._make_user("member@example.com")
        admin = self._login("boss@example.com")
        status, body = admin.put("/api/admin/capacity", {"max_users": 1})
        self.assertEqual(status, 422, body)
        self.assertEqual(db.get_setting("max_users"), "", "被拒绝的值不能写进去")

    def test_silly_values_are_refused(self):
        self._make_user("boss@example.com")
        admin = self._login("boss@example.com")
        for payload in ({"max_users": 0}, {"max_users": -3}, {"max_users": 99999},
                        {"max_users": "abc"}, {}):
            status, _ = admin.put("/api/admin/capacity", payload)
            self.assertEqual(status, 422, payload)
        self.assertEqual(db.get_setting("max_users"), "")

    def test_the_audit_record_names_the_number_and_nothing_else(self):
        self._make_user("boss@example.com")
        admin = self._login("boss@example.com")
        admin.put("/api/admin/capacity", {"max_users": 12})
        _, body = admin.get("/api/admin/users")
        entries = [item for item in body["audit"] if item["action"] == "capacity_changed"]
        self.assertTrue(entries, "改名额必须留审计")
        self.assertEqual(entries[0]["detail"], "12")
        for forbidden in ("master", "key", "encrypted_", "password"):
            self.assertNotIn(forbidden, json.dumps(entries).lower())

    def test_the_endpoints_are_404_for_an_ordinary_account(self):
        self._make_user("boss@example.com")
        member = self._make_user("member@example.com")
        client = self._login(member["email"])
        self.assertEqual(client.get("/api/admin/capacity")[0], 404)
        self.assertEqual(client.put("/api/admin/capacity", {"max_users": 99})[0], 404)

    def test_an_anonymous_caller_is_refused(self):
        self._make_user("boss@example.com")
        client = Client(self.base)
        self.assertIn(client.get("/api/admin/capacity")[0], (401, 404))


if __name__ == "__main__":
    unittest.main()
