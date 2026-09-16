"""The reminder sent to accounts that registered but never finished setting up.

One definition, three consumers: the admin console's one-click button, the
`tools/notify_stalled.py` CLI, and the tests. They must agree, because the thing
being decided is *which sentence a person needs* -- and sending the wrong one is
worse than sending nothing: telling somebody who already configured a mailbox to
"go turn on IMAP" reads as "everything you did was pointless".

Two groups
----------
* ``never``   -- no usable mailbox at all. They need to know the four steps exist
  and are short.
* ``refused`` -- a mailbox is configured but the mail server keeps rejecting it.
  They need to know that 授权码 is not their mailbox login password.

The provider-specific steps are read from :mod:`pilot_app.mailpresets`, the same
source the in-app wizard renders, so this mail cannot drift from the product. A
reminder that sends someone to a menu that no longer exists is a support ticket.

Why this is not just the sentinel
---------------------------------
`setup_gap` already makes a stalled account *visible to the operator*, and the
sentinel already reports `setup_stalled`. Visible is not fixed: **the one person
who cannot see the problem is the person it belongs to.** They receive nothing,
nothing fails, and nothing tells them anything was expected of them.

Sending is deliberately awkward
-------------------------------
* **Never twice by accident.** Each delivery is recorded in `app_settings` under
  ``setup_reminder:<user_id>``; a second run skips anyone already written to.
* **Written after the send, not before.** The other order would mark somebody as
  told when the send had actually failed -- the one way to lose a person.
* **A brand-new account is left alone** (see :data:`MIN_AGE_HOURS`): somebody who
  registered ten minutes ago is not stuck, they are busy.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
from typing import Any

from . import mailpresets
from .alerting import send_as_operator
from .database import Database, parse_utc, utc_now
from .security import SecretBox

REMINDER_KEY = "setup_reminder:"
# Do not pounce on someone who is halfway through the wizard right now.
MIN_AGE_HOURS = 6.0
# How many letters one press of the button may send.
#
# nginx allows a 330s response (it was raised for the slow model calls), so the
# batch is bounded by what a *pathological* SMTP server can do inside that:
# `mailio.send_report` sets 30s per socket operation, so ten letters is the point
# where even a host that hangs on every step still answers before the proxy gives
# up. A 504 while mail is quietly going out is the worst outcome here -- the
# operator would press again, and "again" is exactly what the bookkeeping makes
# safe, but they would not know that.
#
# The idempotent bookkeeping is what makes a cap harmless: the next press
# continues with whoever is left instead of starting over.
BATCH_LIMIT = 10

GAP_NEVER = "never"
GAP_REFUSED = "refused"


def app_url() -> str:
    origin = os.environ.get("INFE_PILOT_ORIGIN", "").rstrip("/")
    return f"{origin}/app" if origin else "/app"


def contact_wechat() -> str:
    """The operator's WeChat id, if this instance publishes one.

    Read from the environment rather than written into the code for the same
    reason `INFE_PILOT_SOURCE_URL` is: this repository is public, and a
    self-hosted copy must not tell its users to contact somebody else's personal
    account. Unset means the line is simply absent.
    """
    return os.environ.get("INFE_PILOT_CONTACT_WECHAT", "").strip()


def _contact_lines() -> str:
    lines = ["- 每一步都写清了在哪里点；也可以直接回这封邮件问我。"]
    wechat = contact_wechat()
    if wechat:
        lines.append(f"- 还是搞不定可以直接找我：微信 {wechat}（说明你用的是哪个邮箱就行）。")
    return "\n".join(lines)


# The wording is editable from the console. What is *not* editable is the shape
# of the message: the placeholders below are substituted at send time, and a
# template naming anything else is refused rather than sent with a literal
# "{linkk}" in it -- a typo that ships to a real person's inbox is not something
# they can report back to us.
TEMPLATE_KEYS = {GAP_NEVER: "reminder_template:never", GAP_REFUSED: "reminder_template:refused"}
PLACEHOLDERS = ("{link}", "{wechat}", "{steps}", "{mailbox}")
TEMPLATE_MAX = 4000


def _never_default() -> str:
    """For somebody who never filled in a private mailbox."""
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
        "- 打开设置向导：{link}\n"
        "{wechat}\n\n"
        "内测期间免费，模型调用默认用管理员提供的 key（费用由管理员承担），"
        "你也可以在「AI 模型」里换成自己的。\n\n"
        "如果暂时不打算用了，回一句「不用了」就行，我不会再打扰你。"
    )


def _refused_default() -> str:
    """For somebody whose mailbox exists but keeps refusing our login."""
    return (
        "你好，\n\n"
        "你的账号已经配好了私人邮箱，但那个邮箱一直拒绝我们登录"
        "（邮箱服务器返回的是「登录名或密码错误」）——"
        "所以到现在为止，一封信的摘要都没有发给你。\n\n"
        "最常见的原因是授权码填成了邮箱的登录密码，这两者不是一回事：\n\n"
        "授权码是专门发给程序用的另一套密码，要单独生成，"
        "而且随时可以在邮箱设置里作废重发。\n\n"
        "{steps}\n"
        "- 拿到新的授权码后，打开 {link} 的第 3 步重新填一次并保存。\n"
        "- 保存后页面顶部的四个格子会告诉你有没有接通。\n"
        "{wechat}\n\n"
        "如果你确认授权码没错，也可能是这个邮箱还没开启 IMAP 服务，"
        "页面上第 3 步有对应的说明。"
    )


def default_template(group: str) -> str:
    return _refused_default() if group == GAP_REFUSED else _never_default()


def template_for(db: Database | None, group: str) -> str:
    """The template actually used: the operator's text, or ours."""
    key = TEMPLATE_KEYS.get(group)
    if db is not None and key:
        stored = str(db.get_setting(key) or "").strip()
        if stored:
            return stored
    return default_template(group)


class TemplateError(ValueError):
    """Refused before it can reach anybody: an unknown or missing placeholder."""


def check_template(text: str) -> str:
    """Validate an operator's template, or raise with the reason."""
    body = str(text or "")
    if not body.strip():
        raise TemplateError("正文不能是空的。")
    if len(body) > TEMPLATE_MAX:
        raise TemplateError(f"正文太长了（{len(body)} 字，上限 {TEMPLATE_MAX}）。")
    for name in re.findall(r"\{[a-z_]*\}", body):
        if name not in PLACEHOLDERS:
            raise TemplateError(
                f"不认识的占位符 {name}；可用的只有 {'、'.join(PLACEHOLDERS)}。")
    if "{link}" not in body:
        raise TemplateError("正文里必须保留 {link}，否则收信人不知道该去哪里设置。")
    return body


def set_template(db: Database, group: str, text: str, *, actor: str = "console") -> str:
    """Save (or with an empty string, reset) one template. Returns what is stored."""
    if group not in TEMPLATE_KEYS:
        raise TemplateError("未知的模板。")
    body = str(text or "").strip()
    if body:
        check_template(body)
        db.set_setting(TEMPLATE_KEYS[group], body, actor=actor)
        return body
    db.delete_setting(TEMPLATE_KEYS[group])
    return default_template(group)


def _provider_steps(mailbox_email: str) -> str:
    preset_id = mailpresets.preset_id_for_email(mailbox_email)
    preset = mailpresets.PRESETS_BY_ID.get(preset_id) or {}
    steps = preset.get("steps") or []
    if steps:
        return f"以{preset.get('label', '这个邮箱')}为例：\n" + "".join(
            f"{index}. {step}\n" for index, step in enumerate(steps, start=1))
    return ("在你邮箱网页版的「设置」里找到 IMAP/SMTP 服务，开启它，"
            "然后按提示生成一个「客户端授权码」。\n")


def render_body(db: Database | None, group: str, mailbox_email: str = "") -> str:
    """One message body, with the operator's text and our values filled in."""
    text = template_for(db, group)
    return (text.replace("{link}", app_url())
                .replace("{wechat}", _contact_lines())
                .replace("{steps}", _provider_steps(mailbox_email))
                .replace("{mailbox}", mailbox_email or "你的私人邮箱"))


def never_configured_body(db: Database | None = None) -> str:
    return render_body(db, GAP_NEVER)


def refused_login_body(mailbox_email: str, db: Database | None = None) -> str:
    return render_body(db, GAP_REFUSED, mailbox_email)


def message_for(row: dict[str, Any], db: Database | None = None) -> tuple[str, str]:
    """(subject, body) for one account -- the only place the wording is chosen."""
    if row["group"] == GAP_NEVER:
        return ("你的 CityU Mail Pilot 还差一步：把私人邮箱接上",
                render_body(db, GAP_NEVER, str(row.get("mailbox_email") or "")))
    return ("你的 CityU Mail Pilot 收不到信：邮箱登录被拒绝了",
            render_body(db, GAP_REFUSED, str(row.get("mailbox_email") or "")))


def group_for(db: Database, row: dict[str, Any]) -> str:
    """``never`` | ``refused`` | ``""`` -- which sentence this account needs.

    Two judgements, not one, and the second is easy to miss: an account whose
    auth code is wrong has a `last_polled_at` (the stamp is written on failure
    too), so its `setup_gap` is **empty** and `stalled_setups` never lists it.
    Only the receive light can see it.
    """
    if db.setup_gap(row) == "no_mailbox":
        return GAP_NEVER
    lights = [light for light in db.verification_lights(row) if light.get("key") == "mailbox"]
    if lights and not lights[0].get("ok"):
        return GAP_REFUSED
    return ""


def collect(db: Database, now: dt.datetime | None = None, *,
            include_recent: bool = False) -> list[dict[str, Any]]:
    """Every account that needs a reminder, oldest first.

    ``include_recent`` drops the ``MIN_AGE_HOURS`` filter, so an account that
    registered ten minutes ago is included too. The filter exists so the
    automatic first nudge does not land while somebody is still typing; the
    operator asking for "everyone who has not finished" means everyone, and
    without this the console could not reach a person who signed up today --
    which was the complaint.
    """
    now = now or parse_utc(utc_now())
    out: list[dict[str, Any]] = []
    for row in db.list_users_overview():
        if str(row.get("status") or "") not in ("active", "paused"):
            continue
        group = group_for(db, row)
        if not group:
            continue
        registered = parse_utc(row.get("created_at"))
        if registered is None:
            continue
        age_hours = (now - registered).total_seconds() / 3600
        if age_hours < MIN_AGE_HOURS and not include_recent:
            continue
        out.append({**row, "group": group, "age_hours": age_hours,
                    "notified_at": db.get_setting(REMINDER_KEY + row["id"])})
    out.sort(key=lambda item: item["created_at"])
    return out


def panel_rows(db: Database, now: dt.datetime | None = None, *,
               include_recent: bool = False) -> list[dict[str, Any]]:
    """What the admin console draws. Full addresses -- this is the operator."""
    rows = []
    for row in collect(db, now, include_recent=include_recent):
        notified_at, _, group = str(row.get("notified_at") or "").partition("|")
        rows.append({
            "user_id": row["id"], "email": row.get("email"), "status": row.get("status"),
            "group": row["group"], "age_hours": round(row["age_hours"], 1),
            "mailbox_email": row.get("mailbox_email"),
            "notified_at": notified_at or "", "notified_group": group or "",
            "too_new": row["age_hours"] < MIN_AGE_HOURS,
            "body": message_for(row, db)[1],
        })
    return rows


def preview(db: Database | None = None) -> dict[str, str]:
    """The two messages exactly as they would be sent.

    The console shows this before the button is pressed. "Send mail to real
    people" is not an action anybody should take on a label alone -- and the
    operator is the one who has to live with the wording.
    """
    never_subject, never_body = message_for({"group": GAP_NEVER}, db)
    refused_subject, refused_body = message_for(
        {"group": GAP_REFUSED, "mailbox_email": "someone@example.com"})
    return {"never": f"{never_subject}\n\n{never_body}",
            "refused": f"{refused_subject}\n\n{refused_body}",
            "wechat": contact_wechat()}


def send_pending(db: Database, secrets: SecretBox, *, include_notified: bool = False,
                 include_recent: bool = False, limit: int = 0,
                 actor: str = "console") -> dict[str, Any]:
    """Mail everyone who still needs it. Never raises; one failure cannot stop the rest.

    ``include_notified`` re-sends to people who already got one, which is only
    ever right when the wording changed or the first one clearly did not arrive.
    """
    rows = collect(db, include_recent=include_recent)
    pending = [row for row in rows if include_notified or not row["notified_at"]]
    cap = limit or BATCH_LIMIT
    remaining = max(0, len(pending) - cap)
    pending = pending[:cap]

    sent: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for row in pending:
        subject, body = message_for(row, db)
        try:
            receipt = send_as_operator(db, secrets, row["email"], subject, body)
        except Exception as exc:  # noqa: BLE001 -- one bad address must not stop the rest
            logging.warning("setup reminder to %s failed: %s", row["id"], exc)
            failed.append({"user_id": row["id"], "email": row["email"],
                           "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        if receipt.get("refused"):
            # A partial refusal comes back as a map instead of an exception, and
            # recording it as delivered is the one unrecoverable mistake here.
            logging.warning("setup reminder to %s refused: %s", row["id"], receipt["refused"])
            failed.append({"user_id": row["id"], "email": row["email"],
                           "error": f"收件人被拒绝：{receipt['refused']}"[:200]})
            continue
        db.set_setting(REMINDER_KEY + row["id"], f"{utc_now()}|{row['group']}", actor=actor)
        sent.append({"user_id": row["id"], "email": row["email"], "group": row["group"],
                     "message_id": str(receipt.get("message_id") or "")})
        logging.info("setup reminder sent to %s (%s)", row["id"], row["group"])
    return {"considered": len(rows), "attempted": len(pending), "remaining": remaining,
            "sent": sent, "failed": failed}


def whats_left(db: Database) -> dict[str, int]:
    """Counts for the console's summary line, computed the same way as the send."""
    rows = collect(db)
    everything = collect(db, include_recent=True)
    notified = [row for row in rows if row["notified_at"]]
    return {"stalled": len(rows), "pending": len(rows) - len(notified),
            "notified": len(notified),
            # "所有人" 那一档：含还没满 MIN_AGE_HOURS 的新账号。
            "all": len(everything),
            "recent": len(everything) - len(rows),
            "never": len([row for row in rows if row["group"] == GAP_NEVER]),
            "refused": len([row for row in rows if row["group"] == GAP_REFUSED])}


__all__ = ["REMINDER_KEY", "MIN_AGE_HOURS", "BATCH_LIMIT", "GAP_NEVER", "GAP_REFUSED", "app_url",
           "contact_wechat", "never_configured_body", "refused_login_body", "message_for",
           "group_for", "collect", "panel_rows", "preview", "send_pending", "whats_left",
           "TEMPLATE_KEYS", "PLACEHOLDERS", "TemplateError", "check_template", "default_template",
           "template_for", "set_template", "render_body"]