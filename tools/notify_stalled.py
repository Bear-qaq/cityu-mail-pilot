#!/usr/bin/env python3
"""Email the accounts that registered but never finished setting up a mailbox.

    python tools/notify_stalled.py                 # dry run: who, and the exact text
    PILOT_NOTIFY_CONFIRM=yes python tools/notify_stalled.py --send

Why this exists
---------------
`setup_gap` and the sentinel already make a stalled account *visible* to the
operator. Visible is not the same as fixed: the one person who cannot see the
problem is the person it belongs to, and on 2026-09-15 three of seven registered
accounts had never configured a mailbox at all while a fourth was being refused
by its own mail server -- so they were receiving nothing and had no way to know
that anything was expected of them.

Two groups, because they need different sentences:

* **never configured** -- they need to know the four steps exist and are short;
* **configured but refused** -- they need to know that "授权码" is not their
  mailbox login password. One account sat here with a wrong code.

The provider-specific part is read from `pilot_app/mailpresets.py`, the same
source the in-app wizard uses, so this mail cannot drift from the product.

Safety
------
* **Dry run by default.** Sending needs `--send` *and* `PILOT_NOTIFY_CONFIRM=yes`,
  because these are real people's inboxes and there is no unsend.
* **Idempotent.** Each delivery is recorded in `app_settings` under
  ``setup_reminder:<user_id>``; a second run skips anyone already written to
  unless ``--repeat`` is passed. Without this, re-running after a partial
  failure would mail the people who already got one.
* **Nothing personal is printed.** Addresses are masked in the output -- the
  operator does not need the full list in a terminal scrollback to decide
  whether to send, and the tool's output is often pasted somewhere.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# The package this tool talks to must be the *installed* one. `python /tmp/x.py`
# puts the script's own directory first on `sys.path`, so a stale unpacked copy
# left in /tmp silently shadows the real code -- which is exactly what happened
# the first time this ran on the server: it imported a v0.48.0 `pilot_app` from a
# forgotten deploy directory and the permission error was the only reason it was
# noticed. Run it from the installed tree, and say out loud where it came from.
ROOT = Path(os.environ.get("INFE_PILOT_APP_DIR") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))

import pilot_app  # noqa: E402
from pilot_app import database, mailpresets  # noqa: E402
from pilot_app.alerting import send_as_operator  # noqa: E402
from pilot_app.security import SecretBox  # noqa: E402

REMINDER_KEY = "setup_reminder:"
# Do not pounce on someone who is halfway through the wizard right now.
MIN_AGE_HOURS = 6.0


def mask(address: str) -> str:
    address = str(address or "")
    if "@" not in address:
        return "(none)"
    local, _, domain = address.partition("@")
    return f"{local[:2]}…@{domain}"


def app_url() -> str:
    origin = os.environ.get("INFE_PILOT_ORIGIN", "").rstrip("/")
    return f"{origin}/app" if origin else "/app"


def never_configured_body() -> str:
    return (
        "你好，\n\n"
        "你在 CityU Mail Pilot 注册了账号，但还没有填「私人邮箱」——"
        "所以到现在为止，一封信的摘要都没有发给你。\n\n"
        "整件事只有四步，大概 3 分钟：\n"
        "1. 在 CityU Outlook 里设一条转发规则，把学校邮箱转到一个你自己常用的私人邮箱"
        "（QQ / 163 / Gmail 都行）；\n"
        "2. 在那个私人邮箱里开启 IMAP/SMTP 服务，生成一个「授权码」——"
        "注意它不是你的邮箱登录密码；\n"
        "3. 打开设置页，填上私人邮箱和授权码，保存；\n"
        "4. 页面顶部有四个格子，灰着的那格就是还没完成的那一步。\n\n"
        "- 打开设置向导：{url}\n"
        "- 每一步都写清了在哪里点，卡住了直接回这封邮件问我。\n\n"
        "内测期间免费，模型调用默认用管理员提供的 key（费用由管理员承担），"
        "你也可以在「AI 模型」里换成自己的。\n\n"
        "如果暂时不打算用了，回一句「不用了」就行，我不会再打扰你。"
    ).format(url=app_url())


def refused_login_body(mailbox_email: str) -> str:
    preset_id = mailpresets.preset_id_for_email(mailbox_email)
    preset = mailpresets.PRESETS_BY_ID.get(preset_id) or {}
    steps = preset.get("steps") or []
    if steps:
        provider = f"以{preset.get('label', '这个邮箱')}为例：\n" + "".join(
            f"{index}. {step}\n" for index, step in enumerate(steps, start=1))
    else:
        provider = (
            "在你邮箱网页版的「设置」里找到 IMAP/SMTP 服务，开启它，"
            "然后按提示生成一个「客户端授权码」。\n")
    return (
        "你好，\n\n"
        "你的账号已经配好了私人邮箱，但那个邮箱一直拒绝我们登录"
        "（邮箱服务器返回的是「登录名或密码错误」）——"
        "所以到现在为止，一封信的摘要都没有发给你。\n\n"
        "最常见的原因是授权码填成了邮箱的登录密码，这两者不是一回事：\n\n"
        "授权码是专门发给程序用的另一套密码，要单独生成，"
        "而且随时可以在邮箱设置里作废重发。\n\n"
        f"{provider}\n"
        "- 拿到新的授权码后，打开 {url} 的第 3 步重新填一次并保存。\n"
        "- 保存后页面顶部的四个格子会告诉你有没有接通；也可以直接回这封邮件问我。\n\n"
        "如果你确认授权码没错，也可能是这个邮箱还没开启 IMAP 服务，"
        "页面上第 3 步有对应的说明。"
    ).format(url=app_url())


def group_for(db: database.Database, row: dict) -> str:
    """'never' | 'refused' | '' -- which sentence this account needs."""
    if db.setup_gap(row) == "no_mailbox":
        return "never"
    lights = [light for light in db.verification_lights(row) if light.get("key") == "mailbox"]
    if lights and not lights[0].get("ok"):
        return "refused"
    return ""


def collect(db: database.Database, now) -> list[dict]:
    out = []
    for row in db.list_users_overview():
        if str(row.get("status") or "") not in ("active", "paused"):
            continue
        group = group_for(db, row)
        if not group:
            continue
        registered = database.parse_utc(row.get("created_at"))
        if registered is None:
            continue
        age_hours = (now - registered).total_seconds() / 3600
        if age_hours < MIN_AGE_HOURS:
            continue
        out.append({**row, "group": group, "age_hours": age_hours})
    return out


def message_for(row: dict) -> tuple[str, str]:
    """(subject, body) for one account -- the only place the wording is chosen."""
    if row["group"] == "never":
        return "你的 CityU Mail Pilot 还差一步：把私人邮箱接上", never_configured_body()
    return ("你的 CityU Mail Pilot 收不到信：邮箱登录被拒绝了",
            refused_login_body(str(row.get("mailbox_email") or "")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--send", action="store_true",
                        help="真的发信；还需要 PILOT_NOTIFY_CONFIRM=yes")
    parser.add_argument("--repeat", action="store_true",
                        help="给已经收到过提醒的账号再发一次")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多发几封（0 = 不限）")
    args = parser.parse_args(argv)

    db = database.Database(os.environ.get("INFE_PILOT_DB", "/var/lib/cityu-mail-pilot/pilot.sqlite3"))
    now = database.parse_utc(database.utc_now())
    targets = collect(db, now)

    if args.send and os.environ.get("PILOT_NOTIFY_CONFIRM") != "yes":
        print("要真发信，请设置 PILOT_NOTIFY_CONFIRM=yes（先不加 --send 看一遍 dry run）。", file=sys.stderr)
        return 2

    rows = []
    for row in targets:
        already = db.get_setting(REMINDER_KEY + row["id"])
        if already and not args.repeat:
            continue
        rows.append(row)
    if args.limit:
        rows = rows[:args.limit]

    print(f"共 {len(targets)} 个账号需要提醒，其中 {len(rows)} 个还没提醒过。")
    print(f"（用的是 {Path(pilot_app.__file__).parent}）")
    print()
    for row in rows:
        subject, body = message_for(row)
        print(f"--- {row['id'][:12]}  {mask(row.get('email'))}  "
              f"group={row['group']}  age={row['age_hours']:.1f}h")
        print(f"    主题   {subject}")
        print("    " + body.replace("\n", "\n    "))
        print()

    if not args.send:
        print(f"这是 dry run。要发这 {len(rows)} 封：PILOT_NOTIFY_CONFIRM=yes … --send")
        return 0

    secrets = SecretBox.from_environment()
    sent = failed = 0
    for row in rows:
        subject, body = message_for(row)
        try:
            receipt = send_as_operator(db, secrets, row["email"], subject, body)
        except Exception as exc:  # noqa: BLE001 -- one failure must not stop the rest
            failed += 1
            print(f"  失败 {row['id'][:12]} {mask(row.get('email'))}: {type(exc).__name__}: {exc}")
            continue
        if receipt.get("refused"):
            failed += 1
            print(f"  被拒 {row['id'][:12]} {mask(row.get('email'))}: {receipt['refused']}")
            continue
        db.set_setting(REMINDER_KEY + row["id"],
                       f"{database.utc_now()}|{row['group']}", actor="notify_stalled")
        sent += 1
        print(f"  已发 {row['id'][:12]} {mask(row.get('email'))} "
              f"id={receipt.get('message_id', '')}")
    print()
    print(f"发出 {sent} 封，失败 {failed} 封。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
