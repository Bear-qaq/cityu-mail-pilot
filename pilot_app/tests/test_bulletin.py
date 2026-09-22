"""Tests for the public bulletin board on the landing page.

The board is the one place where something an operator types ends up on a page
that is *world-readable and indexable* and served from our own origin. So the
tests are mostly about the two ways that can go wrong:

* **It leaks by default.** Publishing a banner to four signed-up users and
  publishing to the open web are different acts, so the flag is a separate one
  that defaults to off, and "撤下" has to mean gone from the board too.
* **Operator text becomes markup.** A notice is plain text on a page with
  `script-src 'self'`; if it were inserted as HTML, a quote or a tag in a title
  would be enough to break the page or worse. Escaping is asserted directly.
"""

import datetime as dt
import http.cookiejar
import json
import os
import re
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/bulletin.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import database as database_mod  # noqa: E402
from pilot_app import web  # noqa: E402
from pilot_app.security import hash_password, token_hash  # noqa: E402
from pilot_app.web import db  # noqa: E402


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method: str, path: str, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(request, timeout=20) as response:
                return response.status, _decode(response.read()), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, _decode(error.read()), dict(error.headers)

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, payload=None):
        return self.request("POST", path, payload=payload)

    def put(self, path, payload=None):
        return self.request("PUT", path, payload=payload)


def _decode(raw: bytes):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", "replace")


class BulletinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = web.create_server("127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        # Saved and restored rather than popped. `test_metrics` and others set
        # `INFE_PILOT_ADMIN_EMAILS` at import time and rely on it still being
        # there when they run; this module is imported earlier than they are, so
        # an unconditional pop in tearDown quietly turned their admin into a 404.
        self._admin_env = os.environ.get("INFE_PILOT_ADMIN_EMAILS")
        self.stamp = dt.datetime.now().timestamp()
        self.client = Client(self.base)
        with db.connect() as connection:
            connection.execute("DELETE FROM announcements")
            connection.execute("DELETE FROM announcement_dismissals")

    def tearDown(self):
        if self._admin_env is None:
            os.environ.pop("INFE_PILOT_ADMIN_EMAILS", None)
        else:
            os.environ["INFE_PILOT_ADMIN_EMAILS"] = self._admin_env

    def _publish(self, *, title="系统维护", body="周六 22:00 起维护两小时。",
                 tone="info", public=False, created_by="ops@example.com"):
        return db.create_announcement(title=title, body=body, tone=tone,
                                      deliver_email=False, created_by=created_by,
                                      is_public=public)

    def _landing(self) -> str:
        status, body, _ = self.client.get("/")
        self.assertEqual(status, 200)
        return body

    def _as_admin(self) -> Client:
        code = f"bulletin-admin-{self.stamp}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with db.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash(code), expiry))
        admin = Client(self.base)
        email = f"bulletin-boss-{self.stamp}@example.com"
        os.environ["INFE_PILOT_ADMIN_EMAILS"] = email
        status, body, _ = admin.post("/api/auth/register", {
            "email": email, "password": "a-long-enough-password",
            "invite_code": code, "accepted_terms": True,
        })
        self.assertEqual(status, 200, body)
        return admin

    # -- the board renders ---------------------------------------------------

    def test_an_empty_board_is_not_on_the_page_at_all(self):
        """A heading with nothing under it reads as an abandoned page."""
        page = self._landing()
        self.assertNotIn('id="board"', page)
        self.assertNotIn("布告栏", page)

    def test_the_placeholder_never_reaches_the_browser(self):
        """`{{BULLETIN}}` is a server-side placeholder, not page content."""
        self.assertNotIn("{{BULLETIN}}", self._landing())
        self._publish(public=True)
        self.assertNotIn("{{BULLETIN}}", self._landing())

    def test_a_public_notice_is_on_the_page(self):
        self._publish(title="名额已满", body="本周的码发完了，下周再放十个。", public=True)
        page = self._landing()
        self.assertIn('id="board"', page)
        self.assertIn("名额已满", page)
        self.assertIn("本周的码发完了", page)

    def test_the_board_is_readable_without_javascript(self):
        """It is in the served bytes, not fetched by a script afterwards.

        This is the same requirement as the rest of the landing page: the board
        exists to be read by someone who has not signed up, and a client-rendered
        board would also be invisible to the crawlers the page is written for.
        """
        self._publish(title="只有服务端渲染才看得到", public=True)
        page = self._landing()
        self.assertIn("只有服务端渲染才看得到", page)
        self.assertNotIn("<script>", page)

    def test_a_notice_shown_only_in_the_app_is_not_on_the_public_board(self):
        """The flag defaults to off, and that default is the whole safety story."""
        self._publish(title="只给已登录用户看的话", public=False)
        page = self._landing()
        self.assertNotIn('id="board"', page)
        self.assertNotIn("只给已登录用户看的话", page)

    def test_a_withdrawn_notice_is_gone_from_the_board(self):
        """"撤下" has to mean everywhere, including the half nobody sees."""
        announcement_id = self._publish(title="撤回的公告", public=True)
        self.assertIn("撤回的公告", self._landing())
        db.withdraw_announcement(announcement_id)
        page = self._landing()
        self.assertNotIn("撤回的公告", page)
        self.assertNotIn('id="board"', page)

    def test_operator_text_is_escaped_not_interpreted(self):
        """A title is plain text: it must not be able to open a tag."""
        self._publish(title='<script>alert(1)</script>', body="a<b & c",
                      public=True)
        page = self._landing()
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn("a&lt;b &amp; c", page)

    def test_only_the_newest_three_notices_are_shown(self):
        for index in range(5):
            self._publish(title=f"第 {index} 条", public=True)
        page = self._landing()
        self.assertIn("第 4 条", page)
        self.assertIn("第 2 条", page)
        self.assertNotIn("第 1 条", page)
        self.assertNotIn("第 0 条", page)

    def test_the_board_carries_an_explicit_time_offset(self):
        """A bare clock time on a public page is a time in which timezone?"""
        self._publish(public=True)
        page = self._landing()
        self.assertIn("(GMT+8)", page)

    def test_a_notice_body_keeps_the_line_breaks_the_operator_typed(self):
        self._publish(body="第一行\n第二行", public=True)
        page = self._landing()
        self.assertIn("第一行\n第二行", page)

    def test_the_tone_becomes_a_class_not_a_colour_in_the_text(self):
        self._publish(tone="critical", public=True)
        page = self._landing()
        self.assertIn("notice-critical", page)

    def test_the_page_still_tells_the_truth_about_the_pilot_size(self):
        """The two templated substitutions must not shadow each other."""
        self._publish(public=True)
        page = self._landing()
        self.assertNotIn("{{PILOT_COUNT}}", page)
        self.assertIn('id="pilot-count"', page)
        # 措辞从「内测期间」改成「目前免费」（2026-09-22 收尾正式版），事实没变：仍然免费。
        self.assertIn("目前免费", page)

    def test_the_board_does_not_print_two_separators(self):
        """One hairline between each pair of blocks, never two in a row.

        The board emits its own trailing rule because it is optional (see
        render_bulletin), and every other block in the template has one *before*
        it. When the message board was added between the board and the
        screenshot, the failure this guards against came back in a new shape:
        two hairlines with nothing between them. So the check is adjacency, not
        a total count -- two rules around a real block are correct.
        """
        self._publish(public=True)
        page = self._landing()
        board_at = page.index('id="board"')
        privacy_at = page.index('id="privacy"')
        guestbook_at = page.index('id="guestbook"')
        download_at = page.index('id="download"')
        # 邻居 2026-09-20 变了：布告栏挪到「它是怎么工作的」之后，留言板挪到申请之后。
        # 判据没变——**每一对相邻板块之间恰好一条分隔线**，而且不许两条挨着。
        self.assertEqual(page[board_at:privacy_at].count('class="rule"'), 1, "布告栏与下一节之间")
        self.assertEqual(page[guestbook_at:download_at].count('class="rule"'), 1, "留言板与下载之间")
        self.assertIsNone(re.search(r'class="rule">\s*<hr', page), "两条分隔线不能挨在一起")
        # 顺序本身也是**有意的**（采纳了 PR #3 的方向，但位置比它靠前一点）：
        # 布告栏不许占陌生人落地的第二眼；留言板不许掉到页面最底。
        how_at = page.index('id="how"')
        apply_at = page.index('id="apply"')
        self.assertLess(how_at, board_at, "布告栏要在「它是怎么工作的」之后")
        self.assertLess(apply_at, guestbook_at, "留言板要在「申请邀请码」之后")
        self.assertLess(guestbook_at, download_at, "留言板别掉到安装说明后面")

    # -- storage -------------------------------------------------------------

    def test_posting_the_same_notice_twice_does_not_move_its_date(self):
        announcement_id = self._publish(title="同一条", public=True)
        first = db.public_announcements(3)[0]["public_at"]
        db.set_announcement_public(announcement_id, True)
        self.assertEqual(db.public_announcements(3)[0]["public_at"], first)

    def test_a_withdrawn_notice_cannot_be_posted_to_the_board(self):
        announcement_id = self._publish(title="已经撤下的")
        db.withdraw_announcement(announcement_id)
        with self.assertRaises(ValueError):
            db.set_announcement_public(announcement_id, True)

    def test_an_unknown_notice_is_not_found(self):
        with self.assertRaises(KeyError):
            db.set_announcement_public("ann_nope", True)

    def test_taking_a_notice_off_the_board_keeps_it_in_the_app(self):
        announcement_id = self._publish(title="撤下布告栏但在应用里还在", public=True)
        db.set_announcement_public(announcement_id, False)
        self.assertEqual(db.public_announcements(3), [])
        active = [row for row in db.list_announcements(20) if row["id"] == announcement_id]
        self.assertEqual(len(active), 1)
        self.assertTrue(active[0]["active"])

    def test_the_migration_adds_the_columns_to_an_existing_database(self):
        """The production database predates the board; the upgrade must not fail."""
        path = os.path.join(_TMP, "old-announcements.sqlite3")
        connection = sqlite3.connect(path)
        connection.execute(
            """CREATE TABLE announcements (
                   id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
                   body TEXT NOT NULL DEFAULT '', tone TEXT NOT NULL DEFAULT 'info',
                   deliver_email INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
                   created_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                   withdrawn_at TEXT)""")
        connection.execute(
            """INSERT INTO announcements(id,title,body,tone,created_at)
               VALUES('ann_old','旧公告','正文','info','2026-01-01T00:00:00+00:00')""")
        connection.commit()
        connection.close()
        fresh = database_mod.Database(path)
        fresh.initialize()
        columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(announcements)")}
        self.assertIn("is_public", columns)
        self.assertIn("public_at", columns)
        self.assertEqual(fresh.public_announcements(3), [])
        # Idempotent: a second initialize on the migrated file must not raise.
        fresh.initialize()

    # -- the console ---------------------------------------------------------

    def test_publishing_with_the_board_flag_puts_it_on_the_page(self):
        admin = self._as_admin()
        status, body, _ = admin.post("/api/admin/announcements", {
            "title": "今天限流", "body": "报告会慢一点，稍后补上。",
            "tone": "warn", "deliver_email": False, "public": True,
        })
        self.assertEqual(status, 200, body)
        page = self._landing()
        self.assertIn("今天限流", page)
        # The same notice in two places must agree, or one of them is lying.
        listed = [row for row in body["announcements"] if row["id"] == body["id"]][0]
        self.assertEqual(listed["is_public"], 1)
        self.assertTrue(listed["public_at"])

    def test_publishing_without_the_flag_leaves_the_page_alone(self):
        admin = self._as_admin()
        status, body, _ = admin.post("/api/admin/announcements", {
            "title": "只发站内", "body": "内部消息。", "deliver_email": False,
        })
        self.assertEqual(status, 200, body)
        self.assertNotIn("只发站内", self._landing())

    def test_the_publish_entry_says_which_places_it_went_to(self):
        admin = self._as_admin()
        _, body, _ = admin.post("/api/admin/announcements", {
            "title": "带板", "body": "x", "deliver_email": False, "public": True,
        })
        # Scoped to this announcement: the audit table is shared and accumulates
        # rows from the other tests in this class.
        with db.connect() as connection:
            row = connection.execute(
                "SELECT detail FROM audit_log WHERE action='announcement_published' "
                "AND detail LIKE ? ORDER BY rowid DESC", (f"%id={body['id']}%",)).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("board=1", row["detail"])
        self.assertIn("email=0", row["detail"])
        self.assertEqual(body["id"][:4], "ann_")

    def test_the_operator_can_post_an_existing_notice_to_the_board(self):
        admin = self._as_admin()
        _, created, _ = admin.post("/api/admin/announcements", {
            "title": "后来才想贴出去", "body": "正文。", "deliver_email": False,
        })
        announcement_id = created["id"]
        self.assertNotIn("后来才想贴出去", self._landing())
        status, body, _ = admin.put(f"/api/admin/announcements/{announcement_id}/board",
                                    {"public": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["is_public"], 1)
        self.assertIn("后来才想贴出去", self._landing())

    def test_the_operator_can_take_it_off_the_board_again(self):
        admin = self._as_admin()
        _, created, _ = admin.post("/api/admin/announcements", {
            "title": "先贴再撤", "body": "正文。", "deliver_email": False, "public": True,
        })
        self.assertIn("先贴再撤", self._landing())
        status, body, _ = admin.put(f"/api/admin/announcements/{created['id']}/board",
                                    {"public": False})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["is_public"], 0)
        self.assertNotIn("先贴再撤", self._landing())

    def test_withdrawing_also_takes_it_off_the_board(self):
        admin = self._as_admin()
        _, created, _ = admin.post("/api/admin/announcements", {
            "title": "撤下就走", "body": "正文。", "deliver_email": False, "public": True,
        })
        self.assertIn("撤下就走", self._landing())
        status, _, _ = admin.put(f"/api/admin/announcements/{created['id']}/withdraw", {})
        self.assertEqual(status, 200)
        self.assertNotIn("撤下就走", self._landing())

    def test_posting_a_withdrawn_notice_back_is_refused(self):
        admin = self._as_admin()
        _, created, _ = admin.post("/api/admin/announcements", {
            "title": "撤了就不能贴", "body": "正文。", "deliver_email": False,
        })
        admin.put(f"/api/admin/announcements/{created['id']}/withdraw", {})
        status, body, _ = admin.put(f"/api/admin/announcements/{created['id']}/board",
                                    {"public": True})
        self.assertEqual(status, 422, body)

    def test_a_non_admin_cannot_touch_the_board(self):
        admin = self._as_admin()
        _, created, _ = admin.post("/api/admin/announcements", {
            "title": "别人碰不到", "body": "正文。", "deliver_email": False,
        })
        os.environ.pop("INFE_PILOT_ADMIN_EMAILS", None)
        code = f"bulletin-member-{self.stamp}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with db.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash(code), expiry))
        member = Client(self.base)
        status, _, _ = member.post("/api/auth/register", {
            "email": f"bulletin-member-{self.stamp}@example.com",
            "password": "a-long-enough-password", "invite_code": code, "accepted_terms": True,
        })
        self.assertEqual(status, 200)
        # 404, not 403: an ordinary user should not learn the console exists.
        status, _, _ = member.put(f"/api/admin/announcements/{created['id']}/board",
                                  {"public": True})
        self.assertEqual(status, 404)
        self.assertNotIn("别人碰不到", self._landing())

    def test_an_anonymous_visitor_can_only_read(self):
        admin = self._as_admin()
        _, created, _ = admin.post("/api/admin/announcements", {
            "title": "匿名的只能看", "body": "正文。", "deliver_email": False,
        })
        status, _, _ = self.client.put(f"/api/admin/announcements/{created['id']}/board",
                                       {"public": True})
        self.assertIn(status, (401, 404))


class PendingCountTests(unittest.TestCase):
    """「还有几条没确认」—— 2026-09-16 那个「点了没反应」的故障。

    对话框一次只显示一条，所以确认掉一条之后紧接着弹出来的下一条，长得和刚关掉的
    那条一模一样。用户读到的是「我点过了，它没反应」。服务端把待确认条数给出来，
    按钮就能写「确认收到（还有 N 条）」。这里钉住这个数字本身与它的边界。
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = database_mod.Database(os.path.join(self.temporary.name, "pilot.sqlite3"))
        self.db.initialize()
        invite = self.db.create_invite("pending", 1)
        self.user = self.db.create_user("reader@example.com", hash_password("a-long-enough-password"),
                                        token_hash(invite))
        with self.db.connect() as connection:
            connection.execute("DELETE FROM announcements")
            connection.execute("DELETE FROM announcement_dismissals")

    def _publish(self, title: str) -> str:
        return self.db.create_announcement(title=title, body="正文", tone="info",
                                           deliver_email=False, created_by="ops@example.com")

    def test_no_announcements_means_zero(self):
        self.assertEqual(self.db.count_pending_announcements(self.user["id"]), 0)

    def test_every_active_announcement_counts_until_this_user_confirms_it(self):
        first = self._publish("第一条")
        second = self._publish("第二条")
        self.assertEqual(self.db.count_pending_announcements(self.user["id"]), 2)
        self.db.dismiss_announcement(first, self.user["id"])
        self.assertEqual(self.db.count_pending_announcements(self.user["id"]), 1)
        self.db.dismiss_announcement(second, self.user["id"])
        self.assertEqual(self.db.count_pending_announcements(self.user["id"]), 0)

    def test_another_users_confirmation_does_not_count_as_mine(self):
        # 广播是「每个人都要自己确认」：别人点过不代表我看到过。
        announcement = self._publish("只给别人确认过")
        invite = self.db.create_invite("pending2", 1)
        other = self.db.create_user("other@example.com", hash_password("a-long-enough-password"),
                                    token_hash(invite))
        self.db.dismiss_announcement(announcement, other["id"])
        self.assertEqual(self.db.count_pending_announcements(self.user["id"]), 1)

    def test_a_withdrawn_announcement_stops_counting(self):
        alive = self._publish("还在的")
        gone = self._publish("要撤下的")
        self.assertEqual(self.db.count_pending_announcements(self.user["id"]), 2)
        self.db.withdraw_announcement(gone)
        self.assertEqual(self.db.count_pending_announcements(self.user["id"]), 1)
        self.db.dismiss_announcement(alive, self.user["id"])
        self.assertEqual(self.db.count_pending_announcements(self.user["id"]), 0)


class BulletinStampTests(unittest.TestCase):
    def test_the_stamp_names_the_offset(self):
        stamp = web.bulletin_stamp("2026-09-14T14:01:00+00:00")
        self.assertIn("(GMT+8)", stamp)
        self.assertIn("22:01", stamp)

    def test_junk_produces_nothing_rather_than_a_wrong_time(self):
        self.assertEqual(web.bulletin_stamp("not a time"), "")
        self.assertEqual(web.bulletin_stamp(None), "")

    def test_the_offset_in_the_marker_is_the_one_in_the_time(self):
        """The marker must not be a literal that can drift from the clock."""
        summer = web.bulletin_stamp("2026-07-01T04:00:00+00:00")
        winter = web.bulletin_stamp("2026-01-01T04:00:00+00:00")
        self.assertTrue(summer.endswith("(GMT+8)"))
        self.assertTrue(winter.endswith("(GMT+8)"))
        self.assertIn("12:00", summer)
        self.assertIn("12:00", winter)


if __name__ == "__main__":
    unittest.main()
