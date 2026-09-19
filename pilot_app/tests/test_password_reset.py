# -*- coding: utf-8 -*-
"""运营者重设密码：`manage reset-password`。

为什么这条命令必须存在，而不是「让用户自己找回」：产品里**没有**自助找回这条路，
而且不是漏做——我们没有第二个能验证「你是你」的渠道。往那个私人邮箱发重置链接，
等于把**只读**的邮箱变成登录凭据；学校邮箱又不是我们能写的。所以登录页上那句
「忘了密码…请他帮你重设」如果背后没有一条命令，就是一句做不到的承诺。

这个文件钉的是那句话与机制之间的一致性，以及这条命令的边界：

* 只有能登服务器的人跑得出来（后台没有这个按钮），每次留一条审计；
* 默认只预演，`--apply` 才写；
* 明文密码只在 stdout 出现一次：不进审计、不进邮件、不接受 argv 传入；
* 写入之后**真的能登进去**（不是「哈希换了」就算完）、旧密码立刻失效、全部会话撤销；
* 字符表里没有 0/O/1/l/I —— 它要被人念出来、在手机上敲一遍。
"""

import contextlib
import datetime as dt
import http.cookiejar
import io
import json
import os
import re
import secrets
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/password-reset.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import manage  # noqa: E402
from pilot_app import web  # noqa: E402
from pilot_app.database import Database  # noqa: E402
from pilot_app.security import hash_password, token_hash, verify_password  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static")

OLD_PASSWORD = "the-password-they-lost-1234"
# 命令行输出里的字段行形如「临时密码      : xxxxxxxxxxxxxxxx」。
PASSWORD_LINE = re.compile(r"^临时密码\s*:\s*(\S+)$", re.MULTILINE)


def run_manage(*args):
    """Run ``manage.main`` the way a shell does, capturing both streams."""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("sys.argv", ["manage.py", *args]), \
         contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = manage.main()
    return code, out.getvalue(), err.getvalue()


def help_output(*args) -> tuple[str, int]:
    """``manage <args> --help``: argparse prints and exits, so catch that."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch("sys.argv", ["manage.py", *args, "--help"]), \
         contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            manage.main()
        except SystemExit as stopped:
            code = stopped.code if isinstance(stopped.code, int) else 0
    return out.getvalue() + err.getvalue(), code


class Client:
    """A cookie-keeping HTTP client, so a session is a real session."""

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
                return response.status, _decode(response.read())
        except urllib.error.HTTPError as error:
            return error.code, _decode(error.read())

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, payload=None):
        return self.request("POST", path, payload=payload)


def _decode(raw: bytes):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return {"raw": raw.decode("utf-8", "replace")}


class PasswordResetTests(unittest.TestCase):
    """One user per test, and every assertion goes through the real login path.

    **A database of its own, on purpose.** The whole suite shares one process and
    the web layer resolves its database through ``web.get_db()``, a process-wide
    singleton whose path was fixed by whichever module imported it first
    (``test_admin``). Registering fourteen accounts in *that* database is how the
    first version of this file broke `test_signup` and `test_signup_notice` in a
    full run only: the shared database crossed the ``INFE_PILOT_MAX_USERS`` cap
    those suites register against, so their registrations started coming back
    「名额已满」. So here the HTTP layer is pointed at this module's own file for
    the duration of each test -- which also means these tests cannot be broken by
    whatever ran before them.
    """

    @classmethod
    def setUpClass(cls):
        cls.database = Database(os.path.join(_TMP, "password-reset.sqlite3"))
        cls.database.initialize()
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
        web._login_attempts.clear()
        self.db = self.database
        # `manage` 从环境变量取库路径，所以测试期间把它指向本模块自己的文件；
        # 跑完必须放回去——同进程里别的套件在测试进行时也会读这个变量。
        self.saved_db_path = os.environ.get("INFE_PILOT_DB")
        os.environ["INFE_PILOT_DB"] = self.db.path
        self.addCleanup(self._restore_db_path)
        # 登录/`/api/me` 都走 `web.get_db()`，把它指向同一个文件，这一条才算走的是
        # 真的 HTTP 路径（而不是「测了一个我们以为一样的库」）。
        patcher = mock.patch.object(web, "get_db", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _restore_db_path(self):
        if self.saved_db_path is None:
            os.environ.pop("INFE_PILOT_DB", None)
        else:
            os.environ["INFE_PILOT_DB"] = self.saved_db_path

    def _make_user(self, password: str = OLD_PASSWORD, status: str = "") -> tuple[str, dict]:
        # 随机后缀，而不是时间戳：同一秒里跑的两个用例不能撞同一个邀请码/邮箱。
        tag = secrets.token_hex(8)
        email = f"reset-{tag}@example.com"
        # 注册要一张真邀请码（`create_user` 会核对），所以每个账号造一张自己的。
        code = f"reset-code-{tag}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with self.db.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash(code), expiry))
        user = self.db.create_user(email, hash_password(password), token_hash(code))
        if status and status != "active":
            self.db.set_user_status(user["id"], status)
        return email, user

    def _login(self, email: str, password: str):
        return Client(self.base).post("/api/auth/login", {"email": email, "password": password})

    def _apply(self, email: str, *extra):
        code, out, err = run_manage("reset-password", "--user-email", email, "--apply", *extra)
        self.assertEqual(code, 0, f"重设失败了：{out}{err}")
        match = PASSWORD_LINE.search(out)
        self.assertIsNotNone(match, f"没有打印临时密码：\n{out}")
        return match.group(1), out

    def _audit_rows(self, email: str):
        """这一条账号的重设记录。整个类是共用一个库的，所以必须按目标过滤。"""
        return [row for row in self.db.list_audit(limit=200)
                if row["action"] == "password_reset_by_operator"
                and row["target_email"] == email]

    # ---------------------------------------------------------------- 预演

    def test_the_preview_changes_nothing_and_prints_no_password(self):
        email, user = self._make_user()
        before = self.db.find_user_for_login(email)["password_hash"]
        code, out, _ = run_manage("reset-password", "--user-email", email)
        self.assertEqual(code, 0, out)
        self.assertIn("预演", out, "必须明确说这一遍什么都没写")
        self.assertIsNone(PASSWORD_LINE.search(out), "预演不许生成、更不许打印密码")
        self.assertEqual(self.db.find_user_for_login(email)["password_hash"], before)
        self.assertEqual(self._audit_rows(email), [], "预演不该留下审计")
        self.assertEqual(self._login(email, OLD_PASSWORD)[0], 200, "预演不该动到旧密码")

    # ------------------------------------------------------- 真的能登进去

    def test_the_new_password_is_the_one_that_actually_logs_in(self):
        email, _ = self._make_user()
        password, out = self._apply(email)
        self.assertNotEqual(password, OLD_PASSWORD)
        self.assertEqual(self._login(email, OLD_PASSWORD)[0], 401, "旧密码必须立刻失效")
        status, body = self._login(email, password)
        self.assertEqual(status, 200, f"新密码登不进去就等于没修：{body}")

    def test_every_existing_session_dies_in_the_same_run(self):
        """"重设了但旧手机还开着" 是这条命令唯一不能有的结果。"""
        email, user = self._make_user()
        first, second = Client(self.base), Client(self.base)
        for client in (first, second):
            self.assertEqual(client.post("/api/auth/login",
                                         {"email": email, "password": OLD_PASSWORD})[0], 200)
        self.assertEqual(self.db.count_sessions(user["id"]), 2)
        password, out = self._apply(email)
        self.assertEqual(self.db.count_sessions(user["id"]), 0, "必须全部撤销")
        self.assertIn("已撤销 2 个", out)
        self.assertEqual(first.get("/api/me")[0], 401, "旧 cookie 必须当场失效")
        self.assertEqual(second.get("/api/me")[0], 401)

    def test_a_second_reset_invalidates_the_first_temporary_password(self):
        email, _ = self._make_user()
        first, _ = self._apply(email)
        second, _ = self._apply(email)
        self.assertNotEqual(first, second, "两次必须不一样——常量密码等于没有密码")
        self.assertEqual(self._login(email, first)[0], 401)
        self.assertEqual(self._login(email, second)[0], 200)

    # ------------------------------------------------------------ 说不的时候

    def test_an_unknown_address_is_refused_and_changes_nothing(self):
        missing = f"nobody-{secrets.token_hex(6)}@example.com"
        code, out, _ = run_manage("reset-password", "--user-email", missing, "--apply")
        self.assertEqual(code, 2, "找不到人必须非零退出（脚本里靠得住）")
        self.assertIn("没有用", out)
        self.assertIn("***", out, "回显只给打码后的地址")
        self.assertIsNone(PASSWORD_LINE.search(out))
        self.assertEqual(self._audit_rows(missing), [])

    def test_a_deleted_account_is_not_brought_back(self):
        email, _ = self._make_user()
        self.db.set_user_status(self.db.find_user_for_login(email)["id"], "deleted")
        code, out, _ = run_manage("reset-password", "--user-email", email, "--apply")
        self.assertEqual(code, 2, out)
        self.assertIn("没有用", out)

    def test_the_address_is_matched_case_insensitively(self):
        """`users.email` 是 NOCASE；重设这条路也必须当同一个邮箱。"""
        email, _ = self._make_user()
        password, _ = self._apply(email.upper())
        self.assertEqual(self._login(email, password)[0], 200)

    def test_a_password_cannot_be_passed_in(self):
        """不许用 `--password` 传口令：那会留在 shell history 和进程表里。"""
        email, _ = self._make_user()
        with self.assertRaises(SystemExit):
            run_manage("reset-password", "--user-email", email, "--password", "chosen-by-hand")
        with self.assertRaises(SystemExit):
            run_manage("reset-password")  # --user-email 不能省
        self.assertEqual(self._login(email, "chosen-by-hand")[0], 401)

    # --------------------------------------------------------------- 审计

    def test_the_audit_row_names_the_shell_and_never_the_password(self):
        email, user = self._make_user()
        password, out = self._apply(email, "--note", "微信上说登不上")
        rows = self._audit_rows(email)
        self.assertEqual(len(rows), 1, f"每次重设都要留痕：{rows}")
        row = rows[0]
        self.assertEqual(row["target_email"], email)
        self.assertIn("命令行", row["actor_email"], "命令行写入要说清是哪个 shell，别编一个管理员")
        self.assertIn("revoked=", row["detail"])
        self.assertIn("微信上说登不上", row["detail"])
        blob = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn(password, blob, "明文密码绝不许进审计")
        self.assertNotIn("pbkdf2", out, "更不能把哈希打出来")
        stored = self.db.find_user_for_login(email)["password_hash"]
        self.assertTrue(stored.startswith("pbkdf2_sha256$"), stored)
        self.assertNotIn(password, stored, "库里存的必须是哈希，不是明文")

    # ------------------------------------------------------- 输出不许进日志

    def test_it_refuses_to_print_a_password_into_the_journal(self):
        """不带 --pipe 跑 = 输出进 journal；密码会活得比终端久。这时候必须拒绝写。

        判据是**stdout 这个文件描述符**，不是那个环境变量（见下一条）：systemd 把
        journal 连接写成 ``设备:inode``，而 fd1 正是 inode 相同的那个 socket。
        生产服务器上量到的原文：fd1 = `socket:[15215159]`，`JOURNAL_STREAM=10:15215159`。
        """
        email, _ = self._make_user()
        before = self.db.find_user_for_login(email)["password_hash"]
        with mock.patch.dict(os.environ, {"JOURNAL_STREAM": "9:12345"}), \
             mock.patch.object(manage.os, "readlink", return_value="socket:[12345]"):
            code, out, _ = run_manage("reset-password", "--user-email", email, "--apply")
        self.assertEqual(code, 2, "必须非零退出，不能只是警告一句")
        self.assertIn("--pipe", out, "要给出能照抄的正确命令")
        self.assertIn("--apply", out)
        self.assertIsNone(PASSWORD_LINE.search(out), "拒绝时也不许生成/打印密码行")
        self.assertEqual(self.db.find_user_for_login(email)["password_hash"], before,
                         "拒绝了就不能改库——否则账号会变成谁也进不去")
        self.assertEqual(self._audit_rows(email), [])
        # 不带 --pipe 时的**预演**是安全的：它不打印任何密码。
        with mock.patch.dict(os.environ, {"JOURNAL_STREAM": "9:12345"}), \
             mock.patch.object(manage.os, "readlink", return_value="socket:[12345]"):
            code, out, _ = run_manage("reset-password", "--user-email", email)
        self.assertEqual(code, 0, out)

    def test_an_inherited_variable_alone_does_not_refuse(self):
        """**变量会被继承**：GitHub Actions 的 runner 自己就是 systemd 服务，
        它把 `JOURNAL_STREAM` 带进了每一步，而那一步的 stdout 其实是普通管道。

        只看变量存在就拒绝，会把 CI 上**每一次正常写入**都拒掉——2026-09-19 真的发生了：
        三个作业全红、八条测试失败，而同一棵树在本机与宿舍机全绿。所以这条测试钉住
        反面：变量在、fd1 是管道时，必须照常写入。
        """
        email, _ = self._make_user()
        with mock.patch.dict(os.environ, {"JOURNAL_STREAM": "9:12345"}), \
             mock.patch.object(manage.os, "readlink", return_value="pipe:[999]"):
            password, out = self._apply(email)
        self.assertEqual(len(password), manage.RESET_PASSWORD_LENGTH)
        self.assertEqual(self._login(email, password)[0], 200, "这种环境本来就该正常写入")

    def test_the_journal_check_reads_the_descriptor_not_the_environment(self):
        """把判据本身钉死：同一个变量，fd1 不同，结论必须不同。"""
        with mock.patch.dict(os.environ, {"JOURNAL_STREAM": "10:15215159"}):
            with mock.patch.object(manage.os, "readlink", return_value="socket:[15215159]"):
                self.assertTrue(manage._stdout_is_a_journal())
            for other in ("pipe:[15215159]", "socket:[1]", "/dev/pts/0", "socket:[15215159]x"):
                with mock.patch.object(manage.os, "readlink", return_value=other):
                    self.assertFalse(manage._stdout_is_a_journal(), other)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JOURNAL_STREAM", None)
            self.assertFalse(manage._stdout_is_a_journal(), "没有这个变量就不是 journal")

    # ------------------------------------------------------- 给人念的密码

    def test_the_password_can_be_read_aloud_and_retyped(self):
        """字符表里不能有 0/O/1/l/I —— 它们是这条通道上最常见的假故障。"""
        for _ in range(50):
            password = manage.generate_reset_password()
            self.assertGreaterEqual(len(password), 12)
            self.assertEqual(len(password), manage.RESET_PASSWORD_LENGTH)
            for confusable in "0O1lI":
                self.assertNotIn(confusable, password)
            self.assertTrue(set(password) <= set(manage.RESET_PASSWORD_ALPHABET))
            self.assertTrue(verify_password(password, hash_password(password)),
                            "自己生成的密码必须能被自己的校验接受")
        self.assertEqual(len({manage.generate_reset_password() for _ in range(50)}), 50)

    # ------------------------------------------------------ 句子与机制一致

    def test_the_login_page_promise_has_this_command_behind_it(self):
        """登录页写着「请他帮你重设」——那这句话背后就必须真有一条命令。"""
        with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as handle:
            page = handle.read()
        self.assertIn("忘了密码", page)
        self.assertIn("重设", page)
        self.assertIn("/privacy", page, "要给出找到运营者的路径")
        listed, code = help_output()
        self.assertEqual(code, 0)
        self.assertIn("reset-password", listed, "命令必须出现在 manage.py --help 里")
        self.assertIn("服务器", listed, "要说明这条命令的代价是服务器权限")
        self.assertIn("预演", listed)
        text, code = help_output("reset-password")
        self.assertEqual(code, 0)
        self.assertIn("--apply", text, "默认预演这件事必须写在帮助里")

    def test_the_privacy_policy_discloses_the_operator_reset(self):
        """新能力必须写进隐私政策——政策与代码行为不一致比没有政策更糟。"""
        with open(os.path.join(STATIC, "privacy.html"), encoding="utf-8") as handle:
            policy = handle.read()
        self.assertIn("忘记密码没有自助找回", policy)
        self.assertIn("临时密码", policy)
        self.assertIn("看不到你的旧密码", policy)
        self.assertIn("版本 1.2", policy)

    # ------------------------------------------------------------ 暂停账号

    def test_a_paused_account_is_reset_but_flagged(self):
        email, _ = self._make_user(status="paused")
        password, out = self._apply(email)
        self.assertIn("已暂停", out)
        self.assertIn("恢复", out, "要告诉运营者这个账号还有第二道门")
        # 暂停只停收信发信，不停登录；所以密码本身照样要能用。
        self.assertEqual(self._login(email, password)[0], 200)
