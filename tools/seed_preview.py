"""Seed a throwaway preview database with an account, mail and reports.

    .venv-pilot/bin/python tools/seed_preview.py /tmp/preview.sqlite3 [--base URL]

Why this exists: several browser checks need a logged-in account that already
has reports, and more than one person has lost time to a check that timed out on
"#dashboard" because the account simply did not exist. Writing the same ad-hoc
script by hand each time also got the invite, the profile and the report
encoding subtly different every run.

It creates, idempotently:

* one invite and one account (``$PILOT_ADMIN``, default ``boss@example.com``)
* a mailbox with a verified-looking state
* three reports: two for today, one for yesterday
* one task already handled *yesterday*, so the "look back by day" view has a
  second day to show

Everything is encrypted with the same master key the preview server runs with,
so the reports render exactly as production reports do.

Never point this at a real database: it writes an account with a known password.
It refuses to run unless INFE_PILOT_PREVIEW=1 is set, so a stray path argument
cannot quietly seed something that matters.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pilot_app import database as database_mod  # noqa: E402
from pilot_app import pricing as pricing_mod  # noqa: E402
from pilot_app import reports as reports_mod  # noqa: E402
from pilot_app.security import SecretBox, token_hash  # noqa: E402

PASSWORD = "a-long-enough-password"
INVITE = "seed-invite"

TODAY_MAIL = [
    ("作业截止提醒", "t1@cityu.edu.hk", """## 1. 重要程度与一句话结论
- 等级：高
- 结论：本周五 23:59 前必须提交作业。

## 2. 必须采取的行动与截止时间
- 提交 CS3101 作业到 Canvas（截止：2026-09-18 23:59）
- 预习第六章并整理笔记

## 3. 邮件内容总结
- 老师提醒作业提交时间。
"""),
    ("图书馆逾期通知", "lib@cityu.edu.hk", """## 1. 重要程度与一句话结论
- 等级：中
- 结论：有两本书即将到期。

## 2. 必须采取的行动与截止时间
- 归还《计算机网络》与《算法导论》（截止：2026-09-20 18:00）

## 3. 邮件内容总结
- 图书馆催还。
"""),
]

def _filler(index: int) -> tuple[str, str, str]:
    """Extra reports so list behaviour is reachable in a preview.

    Paging, collapsing and "show the next N" only exercise themselves past the
    first page. A preview with three reports quietly cannot test the code path
    those features exist for, and the check that covered it failed as though the
    feature were broken.
    """
    return (
        f"课程通知 {index}",
        f"course{index}@cityu.edu.hk",
        f"""## 1. 重要程度与一句话结论
- 等级：{'高' if index % 3 == 0 else '中'}
- 结论：这是第 {index} 份用于演示的课程通知。

## 2. 必须采取的行动与截止时间
- 阅读第 {index} 章并整理笔记（截止：2026-10-{index:02d} 23:59）

## 3. 邮件内容总结
- 老师发布了新的课程材料。
""",
    )


YESTERDAY_MAIL = ("选课系统开放", "reg@cityu.edu.hk", """## 1. 重要程度与一句话结论
- 等级：中
- 结论：下学期选课已开放。

## 2. 必须采取的行动与截止时间
- 在选课系统里确认下学期的三门课

## 3. 邮件内容总结
- 教务处通知选课开放。
""")

# The two failure shapes the operator's console is built to surface:
# (subject, sender, status, skip_reason, last_error)
ADMIN_MAIL = [
    ("学费缴纳通知", "fees@cityu.edu.hk", "failed", "", "SMTP 550 User has no permission"),
    ("限时优惠", "promo@example.com", "skipped", "非允许发件域", ""),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed a preview database for browser checks.")
    parser.add_argument("db", help="path to the preview sqlite file")
    parser.add_argument("--base", default="", help="preview server URL, to register through HTTP")
    parser.add_argument("--email", default=os.environ.get("PILOT_ADMIN", "boss@example.com"))
    parser.add_argument("--reports", type=int, default=8,
                        help="今天的报告份数；超过首页会把分页也带上（默认 8）")
    parser.add_argument("--admin-fixtures", action="store_true",
                        help="额外造一封没发出去的邮件、一封被跳过的邮件，以及 token 用量")
    args = parser.parse_args()

    if os.environ.get("INFE_PILOT_PREVIEW") != "1":
        print("拒绝执行：请设置 INFE_PILOT_PREVIEW=1 以确认这是预览库，不是生产库。", file=sys.stderr)
        return 2

    master = os.environ.get("INFE_PILOT_MASTER_KEY", "")
    if not master:
        print("缺少 INFE_PILOT_MASTER_KEY（要和预览服务用同一把）。", file=sys.stderr)
        return 2
    box = SecretBox.from_base64(master)
    db = database_mod.Database(args.db)
    db.initialize()

    expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
    with db.connect() as connection:
        connection.execute("INSERT OR IGNORE INTO invites(code_hash,expires_at) VALUES(?,?)",
                           (token_hash(INVITE), expiry))

    user_id = _register(args.base, args.email)
    if user_id is None:
        with db.connect() as connection:
            row = connection.execute("SELECT id FROM users WHERE email=?", (args.email,)).fetchone()
        if row is None:
            print(f"账户 {args.email} 不存在，且注册失败。", file=sys.stderr)
            return 1
        user_id = row["id"]

    now = dt.datetime.now(dt.timezone.utc)
    _ensure_mailbox(db, user_id, now.isoformat(timespec="seconds"))

    created = 0
    today = list(TODAY_MAIL) + [_filler(i) for i in range(1, max(0, args.reports - len(TODAY_MAIL)) + 1)]
    for index, (subject, sender, body) in enumerate(today):
        created += _add_report(db, box, user_id, subject, sender, body, now, 100 + index)
    yesterday = now - dt.timedelta(days=1)
    if _add_report(db, box, user_id, *YESTERDAY_MAIL, yesterday, 200):
        created += 1
        _handle_one_task(db, box, user_id, yesterday)

    if args.admin_fixtures:
        _add_admin_fixtures(db, user_id, now)

    print(f"seeded {args.email} ({user_id}): {created} new report(s)")
    if args.base:
        print(f"sign in at {args.base} with {args.email} / {PASSWORD}")
    return 0


def _register(base: str, email: str) -> str | None:
    if not base:
        return None
    request = urllib.request.Request(
        base.rstrip("/") + "/api/auth/register",
        data=json.dumps({"email": email, "password": PASSWORD, "invite_code": INVITE,
                         "accepted_terms": True}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())["id"]
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        # Already registered is the normal case on a second run.
        if "已被注册" in detail or "已注册" in detail:
            return None
        print(f"注册失败：{error.code} {detail}", file=sys.stderr)
        return None


def _ensure_mailbox(db: database_mod.Database, user_id: str, now: str) -> None:
    with db.connect() as connection:
        existing = connection.execute("SELECT id FROM mailboxes WHERE user_id=?", (user_id,)).fetchone()
        if existing:
            connection.execute("UPDATE mailboxes SET last_verified_at=?,last_verify_error='' WHERE user_id=?",
                               (now, user_id))
            return
        connection.execute(
            """INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,smtp_host,
               smtp_port,encrypted_password,last_verified_at,updated_at)
               VALUES('mbx_seed',?,'me@example.com','me@example.com','h',993,'h',465,?,?,?)""",
            (user_id, b"\x00", now, now))


def _add_report(db: database_mod.Database, box: SecretBox, user_id: str, subject: str, sender: str,
                body: str, moment: dt.datetime, uid: int) -> int:
    """Add one message + report, unless a message with this subject already exists."""
    with db.connect() as connection:
        already = connection.execute("SELECT 1 FROM messages WHERE user_id=? AND subject=?",
                                     (user_id, subject)).fetchone()
    if already:
        return 0
    received = moment.isoformat(timespec="seconds")
    message_id = db.insert_message(user_id, "mbx_seed", "1", uid, {
        "subject": subject, "sender_name": subject[:4], "sender_address": sender,
        "received": received, "importance": "normal",
        "body": box.encrypt("原始邮件正文", context=f"message:{user_id}"),
    })
    if message_id is None:
        return 0
    with db.connect() as connection:
        connection.execute("UPDATE messages SET received_at=?,status='sent' WHERE id=?",
                           (received, message_id))
    db.create_report(user_id=user_id, message_id=message_id, kind="immediate",
                     subject=f"【AI邮件摘要】{subject}",
                     body=box.encrypt(body, context=f"report:{user_id}"), sent_to="me@example.com")
    return 1


def _add_admin_fixtures(db: database_mod.Database, user_id: str, now: dt.datetime) -> None:
    """The states the admin console can only show when something went wrong.

    A happy-path seed makes every summary row read zero, so assertions in
    ``admin_edit_check`` had nothing to look at: the collapsed mail row colours
    itself only when a mail did NOT reach the user, the spend panel is uniformly
    "$0.000000" with no per-model or per-day rows to expand, and the "已跳过"
    filter passed *vacuously* because the filtered result was empty.

    Deliberately not part of the default seed. Other suites assert on the
    happy-path numbers ("已下发 9"), so a failed mail appearing in every preview
    would make those counts depend on whether this function had run.
    """
    with db.connect() as connection:
        already = connection.execute(
            "SELECT 1 FROM messages WHERE user_id=? AND subject=?",
            (user_id, ADMIN_MAIL[0][0])).fetchone()
    if already:
        return

    for index, (subject, sender, status, reason, error) in enumerate(ADMIN_MAIL, start=1):
        message_id = db.insert_message(user_id, "mbx_seed", "1", 300 + index, {
            "subject": subject, "sender_name": subject[:4], "sender_address": sender,
            "received": (now - dt.timedelta(minutes=5 * index)).isoformat(timespec="seconds"),
            "importance": "normal",
        })
        if not message_id:
            continue
        with db.connect() as connection:
            connection.execute(
                "UPDATE messages SET status=?,skip_reason=?,last_error=? WHERE id=?",
                (status, reason, error, message_id))

    # Two local days and two models: a one-row "按天" breakdown would satisfy the
    # assertion while proving nothing about the grouping.
    calls = [
        ("deepseek", "deepseek-chat", {"input": 12000, "cached_input": 4000,
                                       "output": 900, "reasoning": 0}, now),
        ("deepseek", "deepseek-v4-pro", {"input": 8000, "cached_input": 0,
                                         "output": 600, "reasoning": 0},
         now - dt.timedelta(days=1)),
        ("deepseek", "deepseek-chat", {"input": 3000, "cached_input": 1000,
                                       "output": 200, "reasoning": 0},
         now - dt.timedelta(days=1)),
    ]
    for provider, model, usage, moment in calls:
        usage = dict(usage, total=usage["input"] + usage["output"])
        price = pricing_mod.lookup(provider, model)
        cost = pricing_mod.estimate(usage, price, at=moment)
        row_id = db.record_usage(user_id=user_id, kind="immediate", provider=provider,
                                 model=model, usage=usage, cost=cost, price=price)
        # record_usage stamps "now"; the day grouping needs one row older, so the
        # timestamp is corrected the same way _add_report corrects received_at.
        if moment.date() != now.date():
            with db.connect() as connection:
                connection.execute("UPDATE token_usage SET created_at=? WHERE id=?",
                                   (moment.isoformat(timespec="seconds"), row_id))


def _handle_one_task(db: database_mod.Database, box: SecretBox, user_id: str,
                     moment: dt.datetime) -> None:
    """Mark yesterday's first action as handled, so the day archive has content."""
    start, end, _now, day = _window(moment)
    rows = db.today_reports(user_id, start, end)
    if not rows:
        return
    row = rows[0]
    markdown = box.decrypt(row["body_markdown"], context=f"report:{user_id}")
    tasks = reports_mod.today_tasks(
        [(row["id"], markdown, row["message_id"])],
        [{"id": row["message_id"], "subject": row["message_subject"], "sender_name": row["sender_name"],
          "sender_address": row["sender_address"], "received": row["received_at"],
          "importance": row["importance"]}],
        timezone="Asia/Hong_Kong",
    )
    if tasks:
        db.set_task_state(user_id, tasks[0]["task_key"], "done", tasks[0])


def _window(moment: dt.datetime) -> tuple[str, str, dt.datetime, str]:
    from zoneinfo import ZoneInfo
    local = moment.astimezone(ZoneInfo("Asia/Hong_Kong"))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1)
    return (start.astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
            end.astimezone(dt.timezone.utc).isoformat(timespec="seconds"), local,
            start.date().isoformat())


if __name__ == "__main__":
    raise SystemExit(main())
