"""Read-only IMAP ingestion and SMTP report delivery."""

from __future__ import annotations

import datetime as dt
import email
import email.policy
import email.utils
import html
import imaplib
import os
import re
import smtplib
import ssl
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from typing import Any


GENERATED_PREFIXES = ("【AI邮件摘要】", "【AI每日报告】", "[AI Mail Summary]", "[AI Daily Report]")

# Providers differ in how much polling they tolerate, and only Gmail publishes
# both the rule and the penalty. Its "Gmail server request limits" page states
# "When the limit is reached, the account is temporarily suspended", that a
# suspension "typically lasts an hour, but can last up to 24 hours", and that
# clients should "check for new messages less frequently. We recommend once
# every 15 minutes." That is a floor on how often we may open a Gmail mailbox,
# and it lives here rather than in the worker because the alert sentinel needs
# the same number to decide what "no poll for a while" means.
GMAIL_MIN_POLL_SECONDS = max(60, int(os.environ.get("INFE_PILOT_GMAIL_POLL_SECONDS", "900")))


def minimum_poll_seconds(config: dict[str, Any]) -> int:
    """The interval this provider asks for, or 0 when it publishes no figure.

    Zero means "no documented floor" — the caller should use its own default,
    which is deliberately the faster direction for providers that have never
    complained about us.
    """
    host = str((config or {}).get("imap_host") or "").strip().lower()
    if "gmail" in host or "googlemail" in host:
        return GMAIL_MIN_POLL_SECONDS
    return 0


class MailError(RuntimeError):
    pass


def _decode(value: str | None) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except (TypeError, ValueError):
        return value or ""


def _strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>|</p\s*>", "\n", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return re.sub(r"[ \t]+", " ", html.unescape(value).replace("\r", "")).strip()


def _part_text(part: Message) -> str:
    try:
        content = part.get_content()
        return content if isinstance(content, str) else str(content)
    except (LookupError, TypeError, UnicodeError):
        raw = part.get_payload(decode=True)
        return raw.decode(part.get_content_charset() or "utf-8", errors="replace") if isinstance(raw, bytes) else str(raw or "")


def _nested(message: Message) -> Message | None:
    for part in message.walk():
        if part.get_content_type() == "message/rfc822":
            payload = part.get_payload()
            if isinstance(payload, list) and payload and isinstance(payload[0], Message):
                return payload[0]
            if isinstance(payload, Message):
                return payload
    return None


def normalize_message(raw: bytes) -> dict[str, str]:
    outer = email.message_from_bytes(raw, policy=email.policy.default)
    message = _nested(outer) or outer
    plain: list[str] = []
    rich: list[str] = []
    attachments: list[str] = []
    for part in message.walk():
        if part.get_content_disposition() == "attachment":
            if part.get_filename():
                attachments.append(_decode(part.get_filename()))
            continue
        if part.get_content_maintype() == "multipart" or part.get_content_type() == "message/rfc822":
            continue
        if part.get_content_type() == "text/plain":
            plain.append(_part_text(part))
        elif part.get_content_type() == "text/html":
            rich.append(_part_text(part))
    body = "\n\n".join(plain) if plain else _strip_html("\n\n".join(rich) or _part_text(message))
    if attachments:
        body += "\n\n附件名称 / Attachments: " + ", ".join(attachments[:20])
    sender_name, sender_address = email.utils.parseaddr(_decode(message.get("From")))
    try:
        received = email.utils.parsedate_to_datetime(message.get("Date")).astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        received = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    priority = str(message.get("X-Priority") or message.get("Importance") or "normal").lower()
    importance = "high" if priority.startswith(("1", "high")) else "low" if priority.startswith(("5", "low")) else "normal"
    # RFC 5322 Message-ID: the identity of the *mail*, independent of how many
    # times it was forwarded. A second copy carrying the same value is a
    # duplicate delivery, not a new message.
    message_id = re.sub(r"[\s\x00-\x1f]", "", _decode(message.get("Message-ID")))[:400]
    return {
        "subject": (_decode(message.get("Subject")) or "（无主题）")[:500],
        "sender_name": sender_name[:200], "sender_address": sender_address[:320],
        "received": received, "importance": importance, "body": _strip_html(body)[:20000],
        "message_key": message_id,
    }


def fetch_new_messages(config: dict[str, Any], password: str, *, initial_lookback_hours: int = 48) -> tuple[str, list[tuple[int, dict[str, str]]], int]:
    try:
        client = imaplib.IMAP4_SSL(config["imap_host"], int(config["imap_port"]), timeout=30)
        client.login(config["email"], password)
        status, data = client.select("INBOX", readonly=True)
        if status != "OK":
            raise MailError("无法以只读方式打开 INBOX。")
        uid_validity = ""
        status, response = client.response("UIDVALIDITY")
        if status == "UIDVALIDITY" and response:
            uid_validity = response[0].decode(errors="ignore") if isinstance(response[0], bytes) else str(response[0])
        last_uid = int(config.get("last_uid") or 0) if not config.get("uid_validity") or config.get("uid_validity") == uid_validity else 0
        if last_uid:
            criteria = ("UID", f"{last_uid + 1}:*")
        else:
            since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=initial_lookback_hours)).strftime("%d-%b-%Y")
            criteria = ("SINCE", since)
        status, rows = client.uid("search", None, *criteria)
        if status != "OK":
            raise MailError("IMAP 搜索新邮件失败。")
        found: list[tuple[int, dict[str, str]]] = []
        highest_seen = last_uid
        for raw_uid in (rows[0].split() if rows and rows[0] else []):
            uid = int(raw_uid)
            if uid <= last_uid:
                continue
            highest_seen = max(highest_seen, uid)
            status, content = client.uid("fetch", str(uid), "(BODY.PEEK[])")
            if status != "OK":
                raise MailError(f"读取邮件 UID {uid} 失败。")
            raw = next((item[1] for item in content if isinstance(item, tuple) and isinstance(item[1], bytes)), None)
            if raw is None:
                raise MailError(f"邮件 UID {uid} 没有正文。")
            message = normalize_message(raw)
            if not message["subject"].startswith(GENERATED_PREFIXES):
                found.append((uid, message))
        return uid_validity, found, highest_seen
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
        raise MailError(explain_imap_failure(exc)) from exc
    finally:
        if "client" in locals():
            try:
                client.close()
            except Exception:
                pass
            try:
                client.logout()
            except Exception:
                pass


def fetch_recent_messages(config: dict[str, Any], password: str, *, count: int = 2,
                          lookback_days: int = 30) -> list[tuple[int, dict[str, str]]]:
    """Read the newest real messages from a mailbox, for verification only.

    Deliberately independent of ``last_uid``: this is how ``verify-e2e`` gets a
    real message corpus when the worker's cursor is already caught up. It is
    read-only (``BODY.PEEK[]``, ``readonly=True``) and returns at most ``count``
    messages, newest last.
    """
    try:
        client = imaplib.IMAP4_SSL(config["imap_host"], int(config["imap_port"]), timeout=30)
        client.login(config["email"], password)
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise MailError("无法以只读方式打开 INBOX。")
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max(1, lookback_days))).strftime("%d-%b-%Y")
        status, rows = client.uid("search", None, "SINCE", since)
        if status != "OK":
            raise MailError("IMAP 搜索历史邮件失败。")
        uids = [int(value) for value in (rows[0].split() if rows and rows[0] else [])]
        found: list[tuple[int, dict[str, str]]] = []
        for uid in reversed(uids):
            if len(found) >= max(1, count):
                break
            status, content = client.uid("fetch", str(uid), "(BODY.PEEK[])")
            if status != "OK":
                continue
            raw = next((item[1] for item in content if isinstance(item, tuple) and isinstance(item[1], bytes)), None)
            if raw is None:
                continue
            message = normalize_message(raw)
            if message["subject"].startswith(GENERATED_PREFIXES):
                continue
            found.append((uid, message))
        return list(reversed(found))
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
        raise MailError(explain_imap_failure(exc)) from exc
    finally:
        if "client" in locals():
            try:
                client.close()
            except Exception:
                pass
            try:
                client.logout()
            except Exception:
                pass


def probe_mailbox(config: dict[str, Any], password: str, *, lookback_hours: int = 48) -> dict[str, Any]:
    """Read-only reconnaissance for migration safety checks.

    Returns the live UIDVALIDITY, every UID currently in the mailbox, and the
    subset inside the lookback window. Fetches no bodies and never moves the
    cursor. Callers use it to verify a supplied UIDVALIDITY and to report
    "holes" (mail present in the mailbox but absent from the legacy processed
    set) before applying a migration.
    """
    client = None
    try:
        client = imaplib.IMAP4_SSL(config["imap_host"], int(config["imap_port"]), timeout=30)
        client.login(config["email"], password)
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise MailError("无法以只读方式打开 INBOX。")
        uid_validity = ""
        status, response = client.response("UIDVALIDITY")
        if status == "UIDVALIDITY" and response:
            uid_validity = response[0].decode(errors="ignore") if isinstance(response[0], bytes) else str(response[0])
        status, rows = client.uid("search", None, "ALL")
        if status != "OK":
            raise MailError("IMAP 搜索全部邮件失败。")
        present = sorted(int(value) for value in (rows[0].split() if rows and rows[0] else []))
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=max(1, lookback_hours))).strftime("%d-%b-%Y")
        status, rows = client.uid("search", None, "SINCE", since)
        if status != "OK":
            raise MailError("IMAP 搜索最近邮件失败。")
        recent = sorted(int(value) for value in (rows[0].split() if rows and rows[0] else []))
        return {"uid_validity": uid_validity, "present": present, "recent": recent}
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
        raise MailError(explain_imap_failure(exc)) from exc
    finally:
        if client is not None:
            for closer in (client.close, client.logout):
                try:
                    closer()
                except Exception:
                    pass


def explain_imap_failure(exc: Exception) -> str:
    """Turn opaque server errors into something a pilot user can act on.

    Microsoft already forces OAuth on personal Outlook/Hotmail mailboxes, so a
    correct app password still fails with "Basic authentication is disabled".
    That is not a user mistake and must not be reported as one.
    """
    text = str(exc)
    lowered = text.lower()
    if "basic authentication is disabled" in lowered or "logon is denied" in lowered:
        return (
            "这个邮箱的服务商已经停用「账号密码 / 授权码」登录（微软 Outlook、Hotmail 已强制改用 OAuth）。"
            "请换一个支持授权码的邮箱作为转发邮箱，例如 QQ 邮箱、Gmail 或 163 邮箱。"
        )
    if "unknown user" in lowered or "user is unknown" in lowered:
        return "邮箱地址不存在，请检查「你的私人邮箱」是否填对。"
    if "authenticationfailed" in lowered or "invalid credentials" in lowered or "login failed" in lowered:
        return "授权码（应用专用密码）不正确或已失效，请在邮箱设置里重新生成一个再试。"
    if "login fail" in lowered or "account is abnormal" in lowered or "service is not open" in lowered:
        return (
            "邮箱拒绝了这次登录。常见原因：① 授权码不对或已被重置；② 该邮箱还没在设置里开启 "
            "IMAP/SMTP 服务；③ 邮箱被临时限制登录（例如改过密码或异地登录）。"
            "请到邮箱网页版的「设置 → 账户 / 客户端」确认服务已开启，并重新生成授权码。"
        )
    if "login frequency limited" in lowered or "too many" in lowered:
        return "登录尝试过于频繁，已被邮箱临时限制，请等 15–30 分钟后重试。"
    if isinstance(exc, ssl.SSLError) or "certificate" in lowered:
        return "TLS 证书校验失败，请确认收件服务器地址是否正确。"
    if isinstance(exc, OSError):
        return f"连接不上邮件服务器（网络不通或端口被拦）：{text}"
    return f"IMAP 连接失败：{text}"


def markdown_to_html(markdown: str, subject: str) -> str:
    safe_subject = html.escape(subject)
    parts = [f'<div style="background:#f4f7fb;padding:24px 12px;font-family:Arial,sans-serif"><div style="max-width:680px;margin:auto;background:#fff;border:1px solid #d9e2ec"><div style="padding:22px 28px;background:#123b63;color:#fff"><div style="font-size:12px;letter-spacing:.08em">CITYU MAIL PILOT</div><h1 style="font-size:21px;margin:8px 0 0">{safe_subject}</h1></div><div style="padding:22px 28px;color:#243447;line-height:1.6">']
    list_open = False
    for raw in markdown.replace("\r", "").splitlines():
        line = raw.strip()
        if not line:
            if list_open:
                parts.append("</ul>"); list_open = False
            continue
        heading = re.match(r"^#{1,4}\s+(.+)$", line)
        bullet = re.match(r"^[-*•]\s+(.+)$", line)
        if heading:
            if list_open: parts.append("</ul>"); list_open = False
            parts.append(f'<h2 style="font-size:17px;color:#123b63;margin:20px 0 8px">{html.escape(heading.group(1))}</h2>')
        elif bullet:
            if not list_open: parts.append('<ul style="padding-left:22px">'); list_open = True
            value = html.escape(bullet.group(1))
            value = re.sub(r"(https://[^\s&]+)", r'<a href="\1">\1</a>', value)
            parts.append(f"<li>{value}</li>")
        else:
            if list_open: parts.append("</ul>"); list_open = False
            parts.append(f"<p>{html.escape(line)}</p>")
    if list_open: parts.append("</ul>")
    parts.append('</div><div style="padding:14px 28px;background:#f8fafc;color:#64748b;font-size:11px">AI 生成内容可能出错；联网事实与推测应按报告标记复核。</div></div></div>')
    return "".join(parts)


def send_report(config: dict[str, Any], password: str, subject: str, markdown: str,
                *, html_body: str | None = None, text_body: str | None = None) -> dict[str, Any]:
    """Send one message and return a receipt for it.

    ``html_body``/``text_body`` let callers supply the structured, action-first
    renderings from :mod:`pilot_app.reports`. Without them the plain-text part is
    the raw markdown and the HTML is the legacy converter, which keeps old call
    sites (and tests) working.

    The return value exists so a caller can *record* that a send happened rather
    than only observe it in the moment. Two fields:

    ``message_id``
        Set explicitly and returned. Until this existed outgoing mail carried no
        Message-ID at all, which is both a deliverability smell and the loss of
        the one identifier that lets a human correlate our send with a line in
        the provider's log or a mail header on the recipient's side. The domain
        is taken from the sender address rather than the machine, so the id does
        not leak the server's hostname.

    ``refused``
        ``smtplib``'s per-recipient refusal map. Empty means every recipient was
        accepted. With a single recipient an all-refused result arrives as an
        exception instead, so this is normally empty; it is returned anyway
        because "accepted for this person" is exactly the claim being made, and a
        non-empty map must never be mistaken for success.
    """
    message = EmailMessage()
    message["From"] = config["email"]
    message["To"] = config["report_to"]
    message["Subject"] = subject
    domain = str(config["email"]).split("@")[-1] or None
    message_id = email.utils.make_msgid(domain=domain)
    message["Message-ID"] = message_id
    message.set_content(text_body if text_body is not None else markdown, charset="utf-8")
    message.add_alternative(html_body or markdown_to_html(markdown, subject), subtype="html", charset="utf-8")
    context = ssl.create_default_context()
    refused: dict[str, Any] = {}
    try:
        if int(config["smtp_port"]) == 465:
            with smtplib.SMTP_SSL(config["smtp_host"], int(config["smtp_port"]), context=context, timeout=30) as client:
                client.login(config["email"], password)
                refused = client.send_message(message)
        else:
            with smtplib.SMTP(config["smtp_host"], int(config["smtp_port"]), timeout=30) as client:
                client.ehlo(); client.starttls(context=context); client.ehlo()
                client.login(config["email"], password)
                refused = client.send_message(message)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise MailError(f"SMTP 发送失败：{exc}") from exc
    return {"message_id": message_id, "refused": refused or {}}
