"""「没收到邀请码」的 B 计划（v0.63.72）：自助重发 + 自动重试。

用户原话：**「帮我做一个 planb 可以自动解决一下用户没有收到邀请码的方案」**。

这个文件盯的是**接口层与 worker 那一层**，因为这条路最有价值也最容易出错的
地方不是「能不能发出去」，而是**谁能让它发**：

* 自主重发是**第三个未认证写入**，而且是唯一一个**能间接产生凭据**的 —— 它能让
  一张已经被人批准过的邀请码再走一次邮件。所以每一条边界都有断言：批准过才发、
  没批准不发、婉拒不发、已经注册过的不发、**回执逐字节相同**（否则它就是一个
  「这个邮箱申请过没有」的查询接口）。
* 自动重试必须**自己会停**：没有次数上限，worker 会对着一个拒收我们的地址一分钟
  重开一张码，直到永远。

用户可见的那一半（落地页那个收起的表单）由浏览器套件 `landing_check` 验。
细节见 `docs/invite-plan-b-2026-09-17.md`。
"""

import datetime as dt
import http.cookiejar
import json
import os
import pathlib
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/invite-resend.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import invites  # noqa: E402
from pilot_app import web  # noqa: E402
from pilot_app.security import hash_password, token_hash  # noqa: E402
from pilot_app.web import db  # noqa: E402

RESEND = "/api/invite/resend"
PASSWORD = "a-long-enough-password"


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method: str, path: str, payload=None, *, raw: bytes = None,
                content_type: str = "application/json"):
        data = raw if raw is not None else (
            json.dumps(payload).encode("utf-8") if payload is not None else None)
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", content_type)
        try:
            with self.opener.open(request, timeout=20) as response:
                return response.status, json.loads(response.read().decode("utf-8") or "{}"), response
        except urllib.error.HTTPError as exc:
            raw_body = exc.read().decode("utf-8")
            try:
                return exc.code, json.loads(raw_body or "{}"), exc
            except json.JSONDecodeError:
                return exc.code, raw_body, exc

    def post(self, path: str, payload=None, **kw):
        return self.request("POST", path, payload, **kw)

    def get(self, path: str):
        return self.request("GET", path)


class ResendTests(unittest.TestCase):
    """接口层 + worker 那一层。

    **一个进程里所有测试文件共用一个库**（`web.get_db()` 是进程级单例，第一个 import
    的模块决定了路径 —— `test_admin`）。所以这里的断言一律**只针对自己的那几个地址**：
    「没有人被发过信」这种话在全量跑里是假的（别的文件留下的失败记录会被 worker 重试），
    而单跑这个文件时它又恰好成立 —— 那种测试绿得没有意义。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = web.create_server("127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.stamp = str(int(dt.datetime.now().timestamp()))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        self.anon = Client(self.base)
        self.admin = Client(self.base)
        self.email = f"resend-{self.stamp}-{id(self)}@example.com"
        # Each test starts from a fresh rate-limit budget. The limiters are keyed
        # by client address *in memory*, and every test in this file is the same
        # address (127.0.0.1) -- without this, the fifth test would be refused by
        # the application form's own 5-per-hour budget and fail for a reason that
        # has nothing to do with what it is testing.
        web._signup_attempts.clear()
        web._resend_attempts.clear()
        web._guestbook_attempts.clear()
        # 管理员身份来自环境变量，而**别的测试文件也在改它**（各自 save/restore，
        # 全量跑时顺序一变就串味）。所以这里每个用例自己设一次并还原：一个用例红成
        # 「批准失败」，而单跑这个文件是绿的，就是这种串味。
        self._saved_admin_emails = os.environ.get("INFE_PILOT_ADMIN_EMAILS")
        os.environ["INFE_PILOT_ADMIN_EMAILS"] = "boss@example.com"
        # 这个文件共用一个库（一次进程、一次 initialize），而队列表是**工作队列**：
        # 上一个用例排进去的行会被下一个用例的 worker 一起处理掉。每个用例从空队列
        # 开始，断言才说得清是哪一件事。
        with db.connect() as connection:
            connection.execute("DELETE FROM invite_resends")

    def tearDown(self) -> None:
        if self._saved_admin_emails is None:
            os.environ.pop("INFE_PILOT_ADMIN_EMAILS", None)
        else:
            os.environ["INFE_PILOT_ADMIN_EMAILS"] = self._saved_admin_emails

    # -- helpers ---------------------------------------------------------

    def _admin_session(self) -> None:
        if db.find_user_for_login("boss@example.com"):
            status, body, _ = self.admin.post("/api/auth/login",
                                              {"email": "boss@example.com", "password": PASSWORD})
        else:
            code = db.create_invite(f"admin-{self.stamp}", 7)
            status, body, _ = self.admin.post("/api/auth/register", {
                "email": "boss@example.com", "password": PASSWORD,
                "invite_code": code, "accepted_terms": True,
            })
        self.assertEqual(status, 200, body)

    def _apply(self, email: str) -> str:
        status, body, _ = self.anon.post("/api/signup", {"email": email, "note": "测试"})
        self.assertEqual(status, 200, body)
        row = db.approved_signup_for(email)
        return row["id"] if row else self._request_id(email)

    def _request_id(self, email: str) -> str:
        for row in db.list_signup_requests(200):
            if row["email"] == email:
                return row["id"]
        raise AssertionError(f"没有这条申请：{email}")

    def _approve(self, email: str, *, emailed: bool = True) -> str:
        """Approve one application the way the operator does. Returns the id."""
        self._admin_session()
        request_id = self._apply(email)
        receipt = {"from": "operator@example.com", "message_id": "<m@example.com>", "refused": {}}
        # 失败必须由**真的那条路**产生（抛异常的 SMTP），不是事后再补一条记录：
        # 补一条会把 `invite_attempts` 多加一次，于是自动重试的退避从错误的那一次
        # 起算 —— 测试会因为「还没到时间」而绿得莫名其妙。
        failure = RuntimeError("smtp 挂了")
        with mock.patch.object(web.alerting, "send_as_operator",
                               **({"side_effect": failure} if not emailed
                                  else {"return_value": receipt})):
            status, body, _ = self.admin.post(f"/api/admin/signups/{request_id}",
                                              {"status": "invited"})
        self.assertEqual(status, 200, body)
        return request_id

    def _queue(self) -> list[dict]:
        return db.open_invite_resends(50)

    # -- 谁能让它发 -------------------------------------------------------

    def test_an_approved_application_is_queued(self):
        self._approve(self.email)
        status, body, _ = self.anon.post(RESEND, {"email": self.email, "elapsed_ms": 9000})
        self.assertEqual(status, 200, body)
        self.assertEqual(len(self._queue()), 1, "批准过的申请应当入队")
        self.assertEqual(self._queue()[0]["email"], self.email)

    def test_the_reply_never_says_whether_we_sent_anything(self):
        """**回执恒定**是这个接口的性质，不是措辞。

        三种情形（批准过 / 从没申请过 / 已经注册过）的响应体必须**逐字节相同** ——
        只要有一个字节不同，它就成了「这个邮箱申请过没有、批准了没有」的查询接口，
        而那是我们承诺不提供的。
        """
        approved = f"approved-{self.stamp}@example.com"
        self._approve(approved)
        bodies = []
        for address in (approved, f"stranger-{self.stamp}@example.com", approved):
            # 每次都清一次限流预算：三次请求本身是同一件事的三个面，被 429 挡掉
            # 的话测的就不是「回执一样」了。
            web._resend_attempts.clear()
            status, raw = self._raw(address)
            self.assertEqual(status, 200)
            bodies.append(raw)
        self.assertEqual(len(set(bodies)), 1, f"三种情形的回执必须一模一样：{bodies}")

    def _raw(self, address: str):
        payload = json.dumps({"email": address, "elapsed_ms": 9000}).encode()
        request = urllib.request.Request(self.base + RESEND, data=payload, method="POST")
        request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()

    def test_an_address_that_never_applied_is_never_queued(self):
        status, _, _ = self.anon.post(RESEND, {"email": f"nobody-{self.stamp}@example.com",
                                               "elapsed_ms": 9000})
        self.assertEqual(status, 200)
        self.assertEqual(self._queue(), [], "从没申请过的邮箱不该在队列里留下任何东西")

    def test_a_pending_application_is_not_queued(self):
        """还在等运营者决定的人，等的是决定，不是重发。"""
        email = f"pending-{self.stamp}@example.com"
        self.anon.post("/api/signup", {"email": email})
        status, _, _ = self.anon.post(RESEND, {"email": email, "elapsed_ms": 9000})
        self.assertEqual(status, 200)
        self.assertEqual(self._queue(), [])

    def test_a_declined_application_is_not_queued(self):
        email = f"declined-{self.stamp}@example.com"
        self._admin_session()
        request_id = self._apply(email)
        status, body, _ = self.admin.post(f"/api/admin/signups/{request_id}",
                                          {"status": "declined"})
        self.assertEqual(status, 200, body)
        self.anon.post(RESEND, {"email": email, "elapsed_ms": 9000})
        self.assertEqual(self._queue(), [])

    def test_somebody_who_already_registered_is_not_sent_another_code(self):
        """他已经进来了 —— 再发一张活码是没人需要的凭据。"""
        email = f"registered-{self.stamp}@example.com"
        self._approve(email)
        code = db.create_invite(f"manual-{self.stamp}", 7)
        db.create_user(email, hash_password(PASSWORD), token_hash(code))
        self.anon.post(RESEND, {"email": email, "elapsed_ms": 9000})
        self.assertEqual(self._queue(), [])

    # -- 反滥用 -----------------------------------------------------------

    def test_the_honeypot_is_answered_but_does_nothing(self):
        self._approve(self.email)
        status, body, _ = self.anon.post(RESEND, {"email": self.email, "website": "http://spam",
                                                  "elapsed_ms": 9000})
        self.assertEqual(status, 200, body)
        self.assertEqual(self._queue(), [], "蜜罐填了就当机器人：回执照给，事不做")

    def test_submitting_too_fast_is_refused(self):
        self._approve(self.email)
        status, body, _ = self.anon.post(RESEND, {"email": self.email, "elapsed_ms": 200})
        self.assertEqual(status, 422, body)
        self.assertEqual(self._queue(), [])

    def test_the_client_rate_limit_stops_a_flood(self):
        self._approve(self.email)
        for _ in range(web.INVITE_RESEND_RATE_LIMIT):
            self.anon.post(RESEND, {"email": self.email, "elapsed_ms": 9000})
        status, body, _ = self.anon.post(RESEND, {"email": self.email, "elapsed_ms": 9000})
        self.assertEqual(status, 429, body)

    def test_one_address_cannot_be_resent_forever(self):
        """IP 限流挡不住换设备的人，所以邮箱这一侧也要有一条。"""
        self._approve(self.email)
        for _ in range(web.INVITE_RESEND_PER_EMAIL):
            db.queue_invite_resend(request_id=self._request_id(self.email), email=self.email)
        web._resend_attempts.clear()
        status, body, _ = self.anon.post(RESEND, {"email": self.email, "elapsed_ms": 9000})
        self.assertEqual(status, 200, body)
        self.assertEqual(len(self._queue()), web.INVITE_RESEND_PER_EMAIL,
                         "同一个邮箱 24 小时内不该超过上限")

    def test_a_malformed_address_is_refused(self):
        status, body, _ = self.anon.post(RESEND, {"email": "not-an-address", "elapsed_ms": 9000})
        self.assertEqual(status, 422, body)

    # -- worker 那一半 ----------------------------------------------------

    def test_the_worker_sends_the_queued_request(self):
        self._approve(self.email)
        self.anon.post(RESEND, {"email": self.email, "elapsed_ms": 9000})
        sent = []
        receipt = {"from": "operator@example.com", "message_id": "<b@example.com>", "refused": {}}
        with mock.patch.object(invites.alerting, "send_as_operator",
                               side_effect=lambda *a, **kw: sent.append(a) or receipt):
            result = invites.delivery_pass(db, web.get_service())
        self.assertEqual(result["queued_sent"], 1, result)
        mine = [call for call in sent if call[2] == self.email]
        self.assertEqual(len(mine), 1, "必须发到申请人自己留的邮箱")
        self.assertEqual(self._queue(), [], "处理过的行不该留在队列里")
        row = next(item for item in db.list_signup_requests(200) if item["email"] == self.email)
        self.assertTrue(row["invite_sent_at"], "重发成功要落在申请行上")
        self.assertEqual(row["resend_count"], 1, "自助重发次数要能被面板读到")

    def test_a_queued_request_for_somebody_who_registered_meanwhile_sends_nothing(self):
        """点按钮和处理之间世界会变：他可能已经用一张后来才到的码注册了。"""
        self._approve(self.email)
        self.anon.post(RESEND, {"email": self.email, "elapsed_ms": 9000})
        code = db.create_invite(f"late-{self.stamp}", 7)
        db.create_user(self.email, hash_password(PASSWORD), token_hash(code))
        with mock.patch.object(invites.alerting, "send_as_operator") as sender:
            result = invites.delivery_pass(db, web.get_service())
        self.assertNotIn(self.email, [call.args[2] for call in sender.call_args_list])
        self.assertGreaterEqual(result["queued_skipped"], 1, result)

    def test_a_declined_application_in_the_queue_sends_nothing(self):
        self._approve(self.email)
        request_id = self._request_id(self.email)
        self.anon.post(RESEND, {"email": self.email, "elapsed_ms": 9000})
        self._admin_session()
        self.admin.post(f"/api/admin/signups/{request_id}", {"status": "declined"})
        with mock.patch.object(invites.alerting, "send_as_operator") as sender:
            result = invites.delivery_pass(db, web.get_service())
        self.assertNotIn(self.email, [call.args[2] for call in sender.call_args_list])
        self.assertGreaterEqual(result["queued_skipped"], 1, result)

    def test_a_failed_send_is_retried_and_then_recovers(self):
        """B 计划的另一半：没人做任何事，它自己再试一次。"""
        email = f"retry-{self.stamp}@example.com"
        request_id = self._approve(email, emailed=False)
        with mock.patch.object(invites.alerting, "send_as_operator",
                               return_value={"from": "o@example.com", "message_id": "<r@example.com>",
                                             "refused": {}}) as sender:
            result = invites.retry_failed_sends(db, web.get_service(),
                                                now=dt.datetime.now(dt.timezone.utc)
                                                + dt.timedelta(minutes=30))
        self.assertGreaterEqual(result["retried"], 1, result)
        self.assertIn(email, [call.args[2] for call in sender.call_args_list],
                      "这一趟必须真的为这个地址发了一封")
        row = db.get_signup_request(request_id)
        self.assertTrue(row["invite_sent_at"], "重试成功后要清掉失败状态")
        self.assertEqual(row["invite_send_error"], "")
        self.assertEqual(int(row["invite_attempts"]), 2, "第一次失败 + 这次重试")

    def test_the_retry_waits_before_trying_again(self):
        """刚失败就立刻重试，撞的往往是同一个瞬时故障。"""
        email = f"backoff-{self.stamp}@example.com"
        self._approve(email, emailed=False)
        with mock.patch.object(invites.alerting, "send_as_operator") as sender:
            invites.retry_failed_sends(db, web.get_service())
        self.assertNotIn(self.email, [call.args[2] for call in sender.call_args_list])

    def test_the_retry_gives_up_and_leaves_it_to_the_sentinel(self):
        """一个拒收我们的地址不是瞬时故障；第四次静默重试只会变成噪音。"""
        email = f"giveup-{self.stamp}@example.com"
        request_id = self._approve(email, emailed=False)
        with db.connect() as connection:
            connection.execute(
                "UPDATE signup_requests SET invite_attempts=? WHERE id=?",
                (invites.MAX_AUTO_ATTEMPTS, request_id))
        with mock.patch.object(invites.alerting, "send_as_operator") as sender:
            invites.retry_failed_sends(db, web.get_service(),
                                       now=dt.datetime.now(dt.timezone.utc)
                                       + dt.timedelta(days=1))
        self.assertNotIn(self.email, [call.args[2] for call in sender.call_args_list],
                         "放弃之后不该再为这个地址发信")
        self.assertTrue(db.invite_send_failed(db.get_signup_request(request_id)),
                        "放弃之后仍然要留在哨兵看得见的那张表里")

    def test_a_retry_never_touches_somebody_who_registered(self):
        email = f"retryused-{self.stamp}@example.com"
        self._approve(email, emailed=False)
        code = db.create_invite(f"used-{self.stamp}", 7)
        db.create_user(email, hash_password(PASSWORD), token_hash(code))
        with mock.patch.object(invites.alerting, "send_as_operator") as sender:
            invites.retry_failed_sends(db, web.get_service(),
                                       now=dt.datetime.now(dt.timezone.utc)
                                       + dt.timedelta(days=1))
        self.assertNotIn(self.email, [call.args[2] for call in sender.call_args_list])

    # -- 三条路共用同一份实现 ----------------------------------------------

    def test_every_path_mints_through_one_function(self):
        """批准、自助重发、自动重试：三个调用者，一份「开码并发出去」。"""
        source = pathlib.Path(invites.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("def issue_and_send("), 1)
        for caller in ("web.py", "invites.py"):
            text = pathlib.Path(web.__file__).parent.joinpath(caller).read_text(encoding="utf-8")
            self.assertIn("issue_and_send(", text, caller)
        # 而真正的发送动作只有一个地方 —— 那是我唯一会写日志/处理拒收的地方。
        self.assertEqual(source.count("alerting.send_as_operator("), 1)

    def test_the_new_table_and_columns_exist_on_an_old_database(self):
        """老库升级：新表 + 两列必须幂等补上。

        少了列 → `SELECT s.*` 之后的 `invite_attempts` 当场 no such column，整个
        内测申请面板 500；少了表 → 自助重发那一步报错。两件事都发生在**升级**这条
        路上，而升级只在生产上跑一次，所以只能在这里钉住。
        """
        import sqlite3
        from pilot_app.database import Database
        older = pathlib.Path(_TMP) / f"older-{id(self)}.sqlite3"
        connection = sqlite3.connect(older)
        connection.executescript(
            """
            CREATE TABLE signup_requests (
                id TEXT PRIMARY KEY, email TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending', invite_label TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, decided_at TEXT, client TEXT NOT NULL DEFAULT '');
            INSERT INTO signup_requests(id,email,created_at)
                VALUES('sgn_old','old@example.com','2026-01-01T00:00:00+00:00');
            """)
        connection.commit()
        connection.close()
        upgraded = Database(older)
        upgraded.initialize()
        upgraded.initialize()  # 幂等：第二次不能再 ALTER 一遍
        with upgraded.connect() as connection:
            columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(signup_requests)")}
        self.assertIn("invite_attempts", columns)
        self.assertIn("invite_last_attempt_at", columns)
        row = upgraded.get_signup_request("sgn_old")
        self.assertEqual(int(row["invite_attempts"]), 0)
        self.assertEqual(upgraded.open_invite_resends(5), [], "新表建出来了，而且是空的")


class PageTests(unittest.TestCase):
    """页面那一半：门要看得见，而且要在**卡住的人站着的地方**。

    这两条都是「加了功能但没人找得到」的典型：自助重发藏在落他找不到的地方，
    等于只有运营者知道它存在 —— 而这个功能的全部价值就是**不需要运营者**。
    """

    @staticmethod
    def _read(name: str) -> str:
        return (pathlib.Path(web.__file__).resolve().parent / "static" / name).read_text(
            encoding="utf-8")

    def test_the_landing_page_has_the_door(self):
        page = self._read("landing.html")
        self.assertIn('id="resend"', page)
        self.assertIn('id="resend-form"', page)
        self.assertIn('action="/api/invite/resend"', page)
        self.assertIn("没收到邀请码？", page)
        # 蜜罐与「停留时长」是这一页第三次复用留言板那套门槛，字段名必须一致
        # （服务端按 `website` 读）。
        self.assertIn('id="resend-website"', page)
        # 收起是刻意的：它不该跟「申请内测」抢注意力。
        self.assertIn('<details class="resend" id="resend">', page)

    def test_the_script_posts_to_the_endpoint_and_shows_the_server_sentence(self):
        script = self._read("landing.js")
        self.assertIn("/api/invite/resend", script)
        self.assertIn("elapsed_ms", script, "少了它服务端会当成机器人提交")
        # 服务端那句话要**原样**显示：客户端自己判断「已发送」会把「其实什么都没发生」
        # 说成发生了，而这个接口的全部设计就是不让客户端知道是哪种。
        self.assertIn("result.body && result.body.detail", script)

    def test_the_register_screen_points_at_the_door(self):
        """等了很多天没收到的人，停在注册页的「邀请码」那一栏前面。"""
        page = self._read("index.html")
        row = page[page.index('id="invite-row"'):page.index('id="consent-row"')]
        self.assertIn('href="/#resend"', row)
        self.assertIn("没收到邀请码？", row)
        # 空码提示也要说出这条路（那是他唯一会看到的答案）。
        script = self._read("app.js")
        self.assertIn("没收到邀请码？", script)

    def test_the_panel_shows_how_often_he_asked(self):
        """运营者看不到垃圾邮件箱，但看得到「他点过几次」。"""
        script = self._read("app.js")
        self.assertIn("row.resend_count", script)
        self.assertIn("row.registered_at", script,
                      "重发会让 invite_label 指向最新那张码，判「注册了没有」要看账号")


if __name__ == "__main__":
    unittest.main()
