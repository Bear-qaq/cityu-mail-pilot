"""并发竞态：一张邀请码只能开一个号，一个邮箱也只能有一个号。

审查报告（`handoff/REVIEW-2026-09-22.md`）的 C 段把「注册与邀请码并发」列成扩容前要补的证据。
这里查的是 `Database.create_user` 那条路——它是**先 SELECT 检查、再 UPDATE 消费**的形状，
而两个并发请求完全可以在对方提交之前双双读到「这张码没用过」：

    T1: SELECT invite（没用过）→ SELECT email（没人用）→ INSERT user1 → UPDATE invite
    T2: SELECT invite（也没用过，因为 T1 还没提交）→ INSERT user2 → UPDATE invite
    → 两个账号，一张码

修法是把「认领」变成**原子**的：`UPDATE invites ... WHERE code_hash=? AND used_by IS NULL`，
抢不到（`rowcount != 1`）就抛，抛出去把整个事务回滚（包括刚插进去的用户行）。

**这条测试必须用真线程**：单线程里「先查后改」这个形状永远是对的，测不出任何东西。
"""

import os
import sqlite3
import tempfile
import threading
import unittest

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/race-env.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import database as database_mod  # noqa: E402
from pilot_app.security import hash_password, token_hash  # noqa: E402

PASSWORD_HASH = "x" * 60


def _db(name: str) -> database_mod.Database:
    """**一个用例一个库文件**：同一个文件里跑几个用例时，前一个用例留下的账号会被
    后一个的 `COUNT(*)` 算进去——第一版就是这么误报「一张码开出两个号」的。"""
    database = database_mod.Database(os.path.join(_TMP, f"race-{name}.sqlite3"))
    database.initialize()
    return database


def _invite(database: database_mod.Database, code: str, *, expires: str = "2099-01-01T00:00:00+00:00") -> str:
    with database.connect() as connection:
        connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                           (token_hash(code), expires))
    return token_hash(code)


def _race(workers: list) -> tuple[list, list]:
    """让所有线程尽量同时起跑，返回（成功, 异常）。"""
    results: list = []
    errors: list = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(workers))

    def run(index: int):
        barrier.wait()
        try:
            value = workers[index]()
        except Exception as exc:               # noqa: BLE001 - 测试要看的就是异常类型
            with lock:
                errors.append(exc)
            return
        with lock:
            results.append(value)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(len(workers))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results, errors


class InviteRaceTests(unittest.TestCase):
    def setUp(self):
        self.database = _db(self.id().rsplit(".", 1)[-1])

    def test_one_code_cannot_open_two_accounts(self):
        code = "race-code-0001"
        invite_hash = _invite(self.database, code)
        count = 8
        emails = [f"race-{index}@example.com" for index in range(count)]

        def worker(index: int):
            return self.database.create_user(emails[index], PASSWORD_HASH, invite_hash)

        successes, errors = _race([lambda index=index: worker(index) for index in range(count)])

        self.assertEqual(len(successes), 1, f"一张码只该开一个号，实际开了 {len(successes)} 个")
        self.assertTrue(all(isinstance(exc, ValueError) for exc in errors),
                        f"失败的必须是「邀请码无效」这种可读错误，实际：{[type(e).__name__ for e in errors]}")
        self.assertEqual(len(errors), count - 1)

        with self.database.connect() as connection:
            users = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            used_by = connection.execute("SELECT used_by FROM invites WHERE code_hash=?",
                                         (invite_hash,)).fetchone()[0]
        self.assertEqual(users, 1, "抢不到码的那些请求不该留下账号（整个事务要回滚）")
        self.assertEqual(used_by, successes[0]["id"], "码要记在真正抢到的那个人名下")

    def test_one_email_cannot_register_twice_at_once(self):
        """同一个邮箱同时注册两次：一个成功，另一个拿到人能读的错，而不是 500。"""
        codes = [_invite(self.database, f"race-email-{index}") for index in range(6)]

        def worker(index: int):
            return self.database.create_user("same@example.com", PASSWORD_HASH, codes[index])

        successes, errors = _race([lambda index=index: worker(index) for index in range(6)])
        self.assertEqual(len(successes), 1)
        self.assertTrue(all(isinstance(exc, ValueError) for exc in errors),
                        f"重复邮箱必须是 ValueError（web 层翻成 400），实际：{[type(e).__name__ for e in errors]}")
        with self.database.connect() as connection:
            users = connection.execute("SELECT COUNT(*) FROM users WHERE email='same@example.com'").fetchone()[0]
        self.assertEqual(users, 1)

    def test_an_expired_code_is_refused_even_when_racing(self):
        code = "race-expired"
        invite_hash = _invite(self.database, code, expires="2020-01-01T00:00:00+00:00")

        def worker(index: int):
            return self.database.create_user(f"late-{index}@example.com", PASSWORD_HASH, invite_hash)

        successes, errors = _race([lambda index=index: worker(index) for index in range(4)])
        self.assertEqual(successes, [])
        self.assertEqual(len(errors), 4)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)

    def test_no_locked_database_errors_escape(self):
        """并发下最可能冒出来的是 `database is locked`——它不该逃到用户面前（会变成 500）。"""
        invite_hash = _invite(self.database, "race-locked")
        outcomes: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def worker(index: int):
            barrier.wait()
            try:
                self.database.create_user(f"locked-{index}@example.com", PASSWORD_HASH, invite_hash)
                kind = "ok"
            except sqlite3.OperationalError:
                kind = "OperationalError"
            except ValueError:
                kind = "ValueError"
            except Exception as exc:           # noqa: BLE001
                kind = type(exc).__name__
            with lock:
                outcomes.append(kind)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertNotIn("OperationalError", outcomes,
                         "并发写同一个库时不该把 SQLITE_BUSY 直接抛出去（busy_timeout 该兜住它）")
        self.assertEqual(outcomes.count("ok"), 1)
