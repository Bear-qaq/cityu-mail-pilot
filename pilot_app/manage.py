"""Administrative commands that never print or accept service secrets."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import alerting, mailio, providers, reports
from .database import Database, parse_utc
from .migration import read_legacy_processed_uids
from .security import SecretBox, token_hash


def _mask(address: str) -> str:
    """Enough of an address to identify the account, never the full mailbox."""
    local, _, domain = str(address or "").partition("@")
    if not domain:
        return "***"
    return f"{local[:2]}***@{domain}"


def verify_e2e(db: Database, user_email: str, limit: int, send: bool, show_body: bool,
               *, force_resend: bool = False, send_digest: bool = False, pause: bool = False,
               pull: int = 0, pull_date: str = "", store: bool = False,
               measure: bool = False, measure_model: str = "") -> int:
    """Drive the real pipeline against one user without risking duplicate mail.

    Guarantees, in order of importance:

    * **Never sends a duplicate.** Any message whose immediate report already
      reached ``status='sent'`` is skipped and reported as a reuse, and a
      ``--send`` run refuses to send when the previous report exists but failed.
    * **Never moves the IMAP cursor.** Messages are fetched through
      ``fetch_new_messages`` for inspection only; ``update_mailbox_poll`` is not
      called, so the worker's view of the mailbox is unchanged and nothing can be
      skipped later.
    * **Never prints secrets.** Output shows masked addresses, message counts,
      timings, sizes and section names only — never bodies, keys or app passwords
      unless ``--show-body`` is explicitly passed.

    Sending is opt-in (``--send``); the default mode renders both the immediate
    report and the daily digest so the real chain is proven end to end.
    """
    user = db.find_user_for_login(user_email)
    if not user:
        print(f"找不到试点用户：{_mask(user_email)}")
        return 2
    if user["status"] == "deleted":
        print("该账户已删除。")
        return 2
    mailbox = db.get_mailbox(user["id"])
    if not mailbox:
        print(f"{_mask(user['email'])} 还没有配置邮箱。")
        return 2
    profile = db.get_profile(user["id"]) or {}
    model = db.get_connection(user["id"], "model")
    search = db.get_connection(user["id"], "search")
    mode = "发送" if send else "只渲染（不发信）"
    print(f"用户 {_mask(user['email'])} · 账户状态={user['status']} · 转发邮箱={_mask(mailbox['email'])}")
    print(f"模型={model['provider'] if model else '未配置'} · 搜索={search['provider'] if search else '未配置'}"
          f" · 模式={mode}")

    started = time.time()
    box = SecretBox.from_environment()
    if pause:
        # Only this mailbox is touched; the user's own row is left alone so a
        # --send run still has report_to. A second worker can therefore never
        # consume the same messages while this verification runs.
        with db.connect() as connection:
            connection.execute("UPDATE mailboxes SET enabled=0 WHERE id=?", (mailbox["id"],))
        print("已临时禁用该邮箱（验证结束后恢复），避免第二个 worker 同时消费。")
    try:
        return _e2e_run(db, box, user, mailbox, profile, model, search, limit, send, show_body,
                        force_resend, send_digest, started, pull, pull_date, store, measure,
                        measure_model)
    finally:
        if pause:
            with db.connect() as connection:
                connection.execute("UPDATE mailboxes SET enabled=1 WHERE id=?", (mailbox["id"],))
            print("已恢复该邮箱。")
    return 0



def _override_model(connection, kind: str, model_name: str):
    """Return a copy of a model connection with a different model name."""
    if connection is None or kind != "model":
        return connection
    return {**connection, "model": model_name}


def _store_message(db: Database, box: SecretBox, user, mailbox, target_day, uid: int, message: dict,
                   index: int) -> str:
    """Insert one real message into the throwaway verification database."""
    received = dt.datetime.combine(target_day, dt.time(3, 0), tzinfo=dt.timezone.utc)
    received = received + dt.timedelta(minutes=7 * index)
    stamp = received.isoformat(timespec="seconds")
    message_id = db.insert_message(user["id"], mailbox["id"], "e2e", uid, {
        "subject": message["subject"], "sender_name": message["sender_name"],
        "sender_address": message["sender_address"], "received": stamp,
        "importance": message["importance"],
        "body": box.encrypt(message["body"], context=f"message:{user['id']}"),
    })
    with db.connect() as connection:
        connection.execute("UPDATE messages SET received_at=?,status='sent' WHERE id=?", (stamp, message_id))
    return message_id


def _store_report(db: Database, box: SecretBox, user, message_id: str, markdown: str, report_to: str) -> str:
    with db.connect() as connection:
        row = connection.execute("SELECT subject FROM messages WHERE id=?", (message_id,)).fetchone()
    subject = str(row["subject"]) if row else "验证邮件"
    report_id = db.create_report(
        user_id=user["id"], message_id=message_id, kind="immediate",
        subject=f"【AI邮件摘要】{subject[:120]}",
        body=box.encrypt(markdown, context=f"report:{user['id']}"), sent_to=report_to,
    )
    db.mark_report_sent(report_id)
    return report_id


def _e2e_run(db: Database, box: SecretBox, user, mailbox, profile, model, search, limit: int,
             send: bool, show_body: bool, force_resend: bool, send_digest: bool, started: float,
             pull: int = 0, pull_date: str = "", store: bool = False, measure: bool = False,
             measure_model: str = "") -> int:
    password = box.decrypt(mailbox["encrypted_password"], context=f"mailbox:{user['id']}")
    try:
        if pull:
            messages = mailio.fetch_recent_messages(mailbox, password, count=pull)
            uid_validity = str(mailbox.get("uid_validity") or "")
            print(f"只读连接成功 · UIDVALIDITY={uid_validity} · 为验证取样最近 {len(messages)} 封真实邮件"
                  f" · 当前游标 last_uid={mailbox.get('last_uid')}（未改动）")
        else:
            uid_validity, messages, highest = mailio.fetch_new_messages(mailbox, password)
            print(f"只读连接成功 · UIDVALIDITY={uid_validity} · 新邮件={len(messages)}"
                  f" · 当前游标 last_uid={mailbox.get('last_uid')}（未改动）")
    except Exception as exc:
        print(f"IMAP 失败：{mailio.explain_imap_failure(exc)}")
        return 1

    from .service import PilotService

    service = PilotService(db, box)
    sent = reused = failed = 0
    timed: list[float] = []
    stored = []
    target_day = None
    if pull_date:
        try:
            target_day = dt.date.fromisoformat(pull_date)
        except ValueError:
            print(f"--pull-date 不是合法日期：{pull_date}")
            return 2
    for uid, message in messages[: max(1, limit)]:
        existing = None
        with db.connect() as connection:
            row = connection.execute(
                """SELECT r.id,r.status,r.subject,r.body_markdown,r.sent_at
                   FROM reports r JOIN messages m ON m.id=r.message_id
                   WHERE m.mailbox_id=? AND m.imap_uid=? AND r.kind='immediate'""",
                (mailbox["id"], uid),
            ).fetchone()
            existing = dict(row) if row else None
        subject_preview = str(message["subject"])[:60]
        if measure:
            print(f"[量测] UID {uid} 「{subject_preview}」强制重新生成（不发信、不落库）…")
            began = time.time()
            if measure_model:
                # In-memory only: the user's stored model choice is not touched.
                original = service.db.get_connection
                service.db.get_connection = (
                    lambda uid, kind, _o=original: _override_model(_o(uid, kind), kind, measure_model)
                )
                print(f"  临时模型覆盖：{measure_model}（不改用户配置）")
            markdown = service._analyse(user["id"], {
                "subject": message["subject"], "sender_name": message["sender_name"],
                "sender_address": message["sender_address"], "received": message["received"],
                "importance": message["importance"], "body": message["body"],
            })
            markdown = markdown or ""
            elapsed = time.time() - began
            rendered = reports.render_immediate(
                markdown, message, subject=f"【AI邮件摘要】{message['subject'][:120]}",
                timezone=profile.get("timezone"))
            parsed = reports.parse_report(markdown, message=message, timezone=profile.get("timezone"))
            # 生成耗时只做统计，不写入数据库，也不会发出任何邮件
            print(f"  端到端生成耗时 {elapsed:.1f}s · 报告 {len(markdown)} 字符"
                  f" · HTML {len(rendered['html'])} 字节 · 行动 {len(parsed['actions'])} 条"
                  f" · 来源 {len(parsed['sources'])} 个")
            timed.append(elapsed)
            continue
        if existing and existing["status"] == "sent" and not (send and force_resend):
            print(f"[跳过] UID {uid} 「{subject_preview}」已有成功报告（{existing['sent_at']}）→ 不会重发")
            reused += 1
            continue
        if existing and existing["status"] == "sent" and send and force_resend:
            print(f"[重发取证] UID {uid} 已有成功报告，因 --force-resend 明确允许，重发一封用于验收。")
        if existing and existing["status"] != "sent" and send and not force_resend:
            print(f"[拒绝] UID {uid} 已有失败报告，先人工确认，避免重复发送。")
            failed += 1
            continue

        stored_id = None
        if store and target_day:
            # Land the real mail first: even if generation fails, the digest must
            # still list it (that is exactly the "no silent drop" guarantee).
            stored_id = _store_message(db, box, user, mailbox, target_day, uid, message, len(stored))
            stored.append(stored_id)
            print(f"  已落库到 {target_day.isoformat()}：真实邮件（仅限本次验证数据库）")

        print(f"[处理] UID {uid} 「{subject_preview}」…（模型调用较慢，实测可达 180 秒）")
        began = time.time()
        try:
            markdown = service._analyse(user["id"], {
                "subject": message["subject"], "sender_name": message["sender_name"],
                "sender_address": message["sender_address"], "received": message["received"],
                "importance": message["importance"], "body": message["body"],
            })
        except Exception as exc:
            print(f"  分析失败：{exc}")
            failed += 1
            continue
        elapsed = time.time() - began
        if stored_id:
            _store_report(db, box, user, stored_id, markdown, mailbox["report_to"])
            print("  已落库：本次生成的报告")
        rendered = reports.render_immediate(
            markdown, message, subject=f"【AI邮件摘要】{message['subject'][:120]}",
            timezone=profile.get("timezone"),
        )
        parsed = reports.parse_report(markdown, message=message, timezone=profile.get("timezone"))
        print(f"  完成：{elapsed:.0f}s · 报告 {len(rendered['text'])} 字符 / HTML {len(rendered['html'])} 字节"
              f" · 优先级={parsed['priority_label']} · 行动 {len(parsed['actions'])} 条"
              f" · 来源 {len(parsed['sources'])} 个 · 搜索可用={not parsed['search_unavailable']}")
        if show_body:
            print("  ---- 报告预览（含邮件内容，注意隐私）----")
            print("  " + rendered["text"].replace("\n", "\n  "))
            print("  ---- 预览结束 ----")
        if send:
            try:
                mailio.send_report(
                    mailbox, box.decrypt(mailbox["encrypted_password"], context=f"mailbox:{user['id']}"),
                    rendered["subject"], markdown,
                    html_body=rendered["html"], text_body=rendered["text"],
                )
                print(f"  已发送到 {_mask(mailbox['report_to'])}（未写入数据库，避免与 worker 争用）")
                sent += 1
            except Exception as exc:
                print(f"  发送失败：{exc}")
                failed += 1

    # Prove the 22:00 brief still covers the whole day from stored reports.
    now = dt.datetime.now(dt.timezone.utc)
    zone = profile.get("timezone") or "Asia/Hong_Kong"
    try:
        from zoneinfo import ZoneInfo
        local = now.astimezone(ZoneInfo(zone))
    except Exception:
        local = now
    start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc)
    end = start + dt.timedelta(days=1)
    if store and target_day and stored:
        # Exercise the production daily-report path (send_daily) end to end.
        local_date = target_day.isoformat()
        try:
            sent_daily = PilotService(db, box).send_daily(
                {**user, "timezone": zone, "report_to": mailbox["report_to"]}, local_date)
            print(f"每日简报（真实代码路径 send_daily，{local_date}）发送结果={bool(sent_daily)}")
        except Exception as exc:
            print(f"每日简报发送失败：{exc}")
            failed += 1
        start = dt.datetime.combine(target_day, dt.time(0, 0), tzinfo=dt.timezone.utc)
        end = start + dt.timedelta(days=1)
    rows = db.messages_between(user["id"], start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))
    reports_by_id = {
        row["message_id"]: box.decrypt(row["body_markdown"], context=f"report:{user['id']}")
        for row in rows if row.get("body_markdown") is not None
    }
    digest = reports.build_digest(rows, reports_by_id, timezone=zone)
    reports.with_digest_header(digest, local.date().isoformat(), now.isoformat(timespec="seconds"))
    digest_html = reports.render_digest_html(digest, subject=reports.digest_subject(digest))
    digest_text = reports.render_digest_text(digest, subject=reports.digest_subject(digest))
    titles = [reports.parse_report(body, message=row, timezone=zone)["subject"]
              for row, body in ((row, reports_by_id.get(row["message_id"])) for row in rows)
              if body is not None]
    print(f"每日简报（{local.date().isoformat()} · {zone}）：邮件 {digest['metrics']['total']} 封"
          f" · 合并后 {digest['metrics']['merged_total']} 条 · 待办 {digest['metrics']['actionable']} 件"
          f" · 失败 {digest['metrics']['failed']} 封 · HTML {len(digest_html)} 字节"
          f" · 纯文本 {len(digest_text)} 字符")
    print(f"  标题：{' | '.join(title[:40] for title in titles[:6]) or '（今天还没有邮件）'}")
    missing = digest["metrics"]["total"] - len(titles)
    if missing:
        print(f"  警告：有 {missing} 封邮件没有摘要（简报里会标为失败/未完成，不会静默丢弃）")
    if send and send_digest and not (store and target_day):
        try:
            mailio.send_report(
                mailbox, box.decrypt(mailbox["encrypted_password"], context=f"mailbox:{user['id']}"),
                reports.digest_subject(digest), reports.digest_markdown(digest),
                html_body=digest_html, text_body=digest_text,
            )
            print(f"  每日简报已发送到 {_mask(mailbox['report_to'])}（未写入数据库，避免与 worker 争用）")
            sent += 1
        except Exception as exc:
            print(f"  每日简报发送失败：{exc}")
            failed += 1
    if timed:
        ordered = sorted(timed)
        print(f"生成耗时统计：次数 {len(ordered)} · 最快 {ordered[0]:.1f}s · 中位 {ordered[len(ordered)//2]:.1f}s"
              f" · 最慢 {ordered[-1]:.1f}s")
    print(f"总计：用时 {time.time() - started:.0f}s · 发送 {sent} · 复用已发报告 {reused}"
          f" · 新处理 {len(messages[: max(1, limit)]) - reused - failed} · 失败 {failed}"
          f" · 游标未改动={mailbox.get('last_uid')}")
    return 1 if failed else 0



def _load_env_file(path: str) -> None:
    """Read INFE_* defaults from a local pilot env file without echoing values."""
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        print(f"读取环境文件失败：{exc}")
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("INFE_") and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")



def read_credential_file(path: str) -> tuple[str, str]:
    """Read (email, password) from a local 0600 file. Never prints the contents.

    Accepts two shapes so the user can choose either:
      * a single line holding only the app password, or
      * ``email=`` / ``password=`` lines (``user=`` and ``code=`` also work).
    """
    target = Path(path).expanduser()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"读取凭据文件失败：{exc}")
        return "", ""
    email_value = ""
    password_value = ""
    lone: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if sep and key.strip().lower() in {"email", "user", "username", "account"}:
            email_value = value.strip().strip('"').strip("'")
            continue
        if sep and key.strip().lower() in {"password", "pass", "code", "authcode", "app_password"}:
            password_value = value.strip().strip('"').strip("'")
            continue
        lone.append(line)
    if not password_value and lone:
        password_value = lone[-1]
    return email_value, password_value


def diagnose_forwarding(email_address: str, password: str, *, host: str = "imap.qq.com", port: int = 993,
                        folder: str = "INBOX", limit: int = 40) -> int:
    """Read-only duplicate-delivery diagnosis for one real mailbox.

    Prints **headers only** (never bodies) and no credentials, and never writes,
    moves, marks or deletes anything. It answers one question: do two copies of
    the same original mail carry the same ``Message-ID`` (one mail delivered
    twice) or different ones (two genuinely different forwards)?
    """
    import collections
    import email as email_mod
    import email.policy
    import email.utils
    import imaplib

    local, _, domain = email_address.partition("@")
    print(f"目标邮箱 {local[:3]}***@{domain} · 只读 · 仅打印邮件头（不打印正文、不修改任何邮件）")
    try:
        client = imaplib.IMAP4_SSL(host, int(port), timeout=30)
        client.login(email_address, password)
    except Exception as exc:
        print(f"登录失败：{mailio.explain_imap_failure(exc)}")
        return 1
    try:
        status, _ = client.select(folder, readonly=True)
        if status != "OK":
            print(f"无法以只读方式打开 {folder}。")
            return 1
        status, data = client.uid("search", None, "ALL")
        uids = [value.decode() for value in (data[0].split() if data and data[0] else [])]
        print(f"{folder} 共 {len(uids)} 封，检查最近 {min(limit, len(uids))} 封")
        wanted = ("MESSAGE-ID", "SUBJECT", "DATE", "FROM", "TO", "DELIVERED-TO", "X-ORIGINAL-TO",
                  "X-FORWARDED-TO", "X-FORWARDED-FOR", "RETURN-PATH", "X-MS-EXCHANGE-FORWARDINGLOOP",
                  "AUTO-SUBMITTED", "X-GM-THRID")
        records: list[dict[str, Any]] = []
        for uid in uids[-max(1, limit):]:
            status, content = client.uid(
                "fetch", uid, "(BODY.PEEK[HEADER.FIELDS (" + " ".join(wanted) + ")])"
            )
            raw = next((item[1] for item in content if isinstance(item, tuple) and isinstance(item[1], bytes)), b"")
            message = email_mod.message_from_bytes(raw, policy=email.policy.default)
            records.append({
                "uid": uid,
                "message_id": (message.get("Message-ID") or "").strip(),
                "subject": str(message.get("Subject") or "")[:52],
                "date": str(message.get("Date") or "")[:31],
                "to": str(message.get("To") or "")[:46],
                "delivered_to": str(message.get("Delivered-To") or "")[:46],
                "original_to": str(message.get("X-Original-To") or "")[:46],
                "forwarded_to": str(message.get("X-Forwarded-To") or "")[:46],
                "loop": str(message.get("X-MS-Exchange-ForwardingLoop") or "")[:34],
                "return_path": str(message.get("Return-Path") or "")[:40],
            })
        for row in records:
            print(f"  UID {row['uid']:>6} | {row['date']:<31} | {row['subject']:<52}")
            print(f"          To={row['to']} | Delivered-To={row['delivered_to']} | X-Original-To={row['original_to']}")
            print(f"          X-Forwarded-To={row['forwarded_to']} | loop={row['loop']} | Return-Path={row['return_path']}")
            print(f"          Message-ID={row['message_id']}")

        grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        for row in records:
            grouped[row["message_id"] or f"(no-id-{row['uid']})"].append(row)
        duplicates = {key: value for key, value in grouped.items() if len(value) > 1}
        print(f"\n重复 Message-ID：{len(duplicates)} 组")
        for key, rows in list(duplicates.items())[:10]:
            gaps = ""
            try:
                times = [email.utils.parsedate_to_datetime(r["date"]) for r in rows if r["date"]]
                if len(times) > 1:
                    gaps = f" 间隔={(max(times) - min(times)).total_seconds():.0f}s"
            except Exception:
                gaps = ""
            print(f"  {key}")
            print(f"    UID {[r['uid'] for r in rows]} · 主题「{rows[0]['subject']}」{gaps}")
            for row in rows:
                print(f"      UID {row['uid']}: Delivered-To={row['delivered_to'] or '-'} "
                      f"X-Original-To={row['original_to'] or '-'} loop={row['loop'] or '-'}")
        print("\n判定：")
        if duplicates:
            print("  同一 Message-ID 出现多次 → 同一封邮件被投递了两遍（不是新转发产生的另一封）。")
            print("  说明转发路径里有两个投递动作：Outlook「转发」与「重定向」同时生效，或邮箱侧另加了一条转发规则。")
        else:
            print("  未发现相同 Message-ID 的重复投递（那么两封是不同批次/不同规则产生的不同邮件）。")
    finally:
        try:
            client.close()
        except Exception:
            pass
        try:
            client.logout()
        except Exception:
            pass
    return 0


def _run(command: list[str]) -> str:
    """Run a reporting command, never raising: a missing tool is not a failure."""
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"（无法运行 {' '.join(command)}：{exc}）"
    return (completed.stdout or "") + (completed.stderr or "")


def notify_unit_failure(db: Database, unit: str, *, lines: int = 30) -> int:
    """Mail the operator that a systemd unit failed. Used by ``OnFailure=``.

    Deliberately independent of the worker's sentinel: this path has to work
    when the worker itself is the unit that died. It borrows the admin's own
    mailbox to send, the same way every other message in this project is sent.
    """
    unit = unit.strip()
    if not unit:
        print("缺少 --unit。")
        return 2
    status = _run(["systemctl", "status", "--full", "--no-pager", unit])
    journal = _run(["journalctl", "-u", unit, "--no-pager", "-o", "short-iso",
                    "-n", str(max(1, min(lines, 200)))])
    # Invariant 2: command output leaves the machine inside an e-mail, so it is
    # scrubbed of every credential value in pilot.env before it is embedded.
    body = alerting.redact("\n".join(part for part in (status, journal) if part).strip(),
                           alerting.collect_secret_values())[:8000]
    subject = f"[CityU Mail Pilot] 单元失败：{unit}"
    text = (f"{unit} 进入了 failed 状态。\n\n{body}\n\n"
            f"处理：ssh 到服务器执行 systemctl status --full {unit}\n"
            "（以上输出已自动脱敏；不会打印任何密钥。）")
    try:
        box = SecretBox.from_environment()
        delivered = alerting.send_admin_mail(db, box, subject, text)
    except Exception as exc:
        # A failure handler that raises would obscure the original failure, and
        # OnFailure= must not recurse. The journal keeps the reason instead.
        print(f"无法发出单元失败告警：{exc}", file=sys.stderr)
        return 1
    print(f"已告警：{unit} -> {', '.join(delivered)}")
    return 0


def invitations(db: Database, *, limit: int = 100) -> int:
    """Report what happened to every applicant's invite, and what still cannot be known.

    Answers one question -- "did this person get their code?" -- by putting the
    three observable facts next to each other:

    * the decision and when it was taken;
    * whether the e-mail was handed to the mail server, with the Message-ID to
      quote when asking the provider what they did with it;
    * whether the code was ever **redeemed**, which is the only evidence on this
      side that a human actually read the message.

    The last line states the limit out loud. Delivery to an inbox is not
    observable from here: a 250 from the relay means our provider accepted the
    message, not that it landed. The honest ways to close that gap are the
    redemption record and asking the person. A tracking pixel would close it and
    is refused: the landing page and the privacy policy both promise there is no
    telemetry, and a read receipt is exactly that.

    Exit code is 1 when something needs a human: a failed send, or an invite that
    has been sitting unredeemed well past the point where people normally act.
    """
    rows = db.list_signup_requests(limit)
    if not rows:
        print("还没有任何内测申请。")
        return 0

    now = dt.datetime.now(dt.timezone.utc)
    actionable = 0
    print(f"{'申请邮箱':<34}{'状态':<8}{'发码':<6}{'邮件':<8}{'已使用':<8}{'批准时间'}")
    print("-" * 96)
    for row in rows:
        status = row.get("status", "")
        if status == "pending":
            print(f"{row['email']:<34}{'待处理':<8}{'—':<6}{'—':<8}{'—':<8}{''}")
            continue
        if status == "declined":
            print(f"{row['email']:<34}{'已婉拒':<8}{'—':<6}{'—':<8}{'—':<8}{row.get('decided_at') or ''}")
            continue

        issued = "有" if row.get("invite_label") else "无"
        if db.invite_send_failed(row):
            # Same predicate the sentinel uses; see Database.invite_send_failed.
            mailed = "失败"
            actionable += 1
        elif row.get("invite_sent_at"):
            mailed = "已投递"
        else:
            mailed = "未发送（可能是有意跳过）"
        redeemer = row.get("redeemer_email") or ""
        used = "是" if row.get("invite_used_by") else "否"

        print(f"{row['email']:<34}{'已发码':<8}{issued:<6}{mailed:<8}{used:<8}{row.get('decided_at') or ''}")
        if row.get("invite_send_error"):
            print(f"    发送错误：{row['invite_send_error']}")
        if row.get("invite_message_id"):
            print(f"    Message-ID：{row['invite_message_id']}")
        if redeemer and redeemer != row["email"]:
            print(f"    由这个账号使用：{redeemer}")

        # A code nobody used after three days is usually one of three things: the
        # mail went to spam, the address was mistyped, or the person changed
        # their mind. All three are worth a look and none of them is visible from
        # a status column alone.
        decided = parse_utc(row.get("decided_at") or "")
        if not row.get("invite_used_by") and decided and (now - decided) > dt.timedelta(days=3):
            age = (now - decided).days
            print(f"    ⚠ 已发出 {age} 天仍未使用；若是首次发送，先确认他有没有收到（垃圾邮件箱最常见）")
            actionable += 1

    counts = db.signup_request_counts()
    print()
    print("统计：" + "，".join(f"{key} {value}" for key, value in sorted(counts.items())))
    print()
    print("这一页能证明什么：")
    print("  · 已投递 = 邮件服务器接受了这封信（不是「已送达」）")
    print("  · 已使用 = 本人拿到码并注册成功，这是这一侧能拿到的最强证据")
    print("  · 只有收件人本人能确认「我收到了」；要问就问，不要靠猜")
    print("  · 没有已读回执、没有追踪像素——官网和隐私政策都写着没有任何遥测")
    return 1 if actionable else 0


def check_model(prompt: str = "只回答两个字：可用", timeout: int = 60) -> int:
    """Try the instance-wide fallback key for real.

    This key is set in an environment file and there is no button anywhere that
    exercises it, so until now the only way to learn whether it worked was for a
    real user's report to fail. A minimal call answers it in a second.

    Two rules, both from the iron list: the key is never printed, and neither is
    anything that might contain it. Provider errors sometimes quote the request
    back, so every line printed here goes through ``_scrub`` before it reaches
    the terminal -- a checker that leaks the credential it is checking would be
    worse than no checker.
    """
    connection = providers.platform_model_default()
    if connection is None:
        print("没有配置平台兜底 key（INFE_PILOT_DEFAULT_MODEL_KEY 为空或无效）。")
        print("现状：每个用户都必须自带 key。官网/隐私政策/条款/应用内若写着")
        print("「内测期间由管理员出钱」，那句话目前与事实不符。")
        print("配置方法：sudo bash /opt/cityu-mail-pilot/pilot_app/set_platform_key.sh")
        return 1
    key = providers.platform_model_key()
    provider = connection["provider"]
    model = connection["model"]
    base_url = connection.get("base_url") or ""
    print(f"平台兜底 key：已配置（长度 {len(key)}，内容不显示）")
    print(f"调用：{provider} / {model}" + (f" @ {base_url}" if base_url else ""))

    def _scrub(text: str) -> str:
        value = str(text)
        return value.replace(key, "***已隐藏***") if key else value

    # A three-token probe should not inherit the 300 s report budget. The module
    # global is read inside `generate`, so the only way to shorten it here is to
    # set it -- and it is put back in a `finally` because leaving it changed was
    # not a theoretical problem: it leaked into the next test module in the same
    # process, where an existing guard (`MODEL_TIMEOUT_SECONDS >= 180`) caught it.
    saved_timeout = providers.MODEL_TIMEOUT_SECONDS
    if timeout > 0:
        providers.MODEL_TIMEOUT_SECONDS = int(timeout)
    started = time.monotonic()
    try:
        try:
            result = providers.generate(
                provider=provider, model=model, api_key=key, prompt=prompt,
                base_url=base_url, max_output_tokens=64,
            )
        except Exception as exc:  # noqa: BLE001 - the CLI reports, it does not raise
            print(f"调用失败（{time.monotonic() - started:.1f}s）：{_scrub(exc)}")
            print("key 已写入但不可用。检查账号余额、key 是否被禁用、以及供应商/模型名是否匹配。")
            return 1
    finally:
        providers.MODEL_TIMEOUT_SECONDS = saved_timeout
    elapsed = time.monotonic() - started
    reply = _scrub((result.text or "").strip())
    print(f"调用成功：{elapsed:.1f}s，返回 {len(result.text or '')} 字")
    if result.usage:
        print(f"用量：{result.usage}")
    print(f"模型回复：{reply[:200] or '（空）'}")
    if key and key in reply:
        # Cannot happen after _scrub, but if the primitive is ever changed this
        # is the line that notices.
        print("警告：返回内容里出现了 key 的片段，已隐藏；请检查 provider 实现。")
    print("结论：平台兜底 key 可用，用户没配 key 时会自动用它。")
    return 0


def check_search(query: str = "City University of Hong Kong", timeout: int = 60) -> int:
    """Try the instance-wide search key for real, with one query.

    The sibling of :func:`check_model` and it exists for the same reason: the key
    lives in an environment file and nothing in the UI exercises it. The query is
    a fixed public string, not anything taken from a user's mail -- a diagnostic
    must not be the one thing that sends somebody's subject line to a third party.

    Same rule as the model checker: nothing printed here may contain the key.
    """
    connection = providers.platform_search_default()
    if connection is None:
        print("没有配置平台搜索 key（INFE_PILOT_DEFAULT_SEARCH_KEY 为空或无效）。")
        print("现状：每个用户都必须自带搜索 key，没配的人报告照常生成、只是没有联网核实。")
        print("官网/隐私政策/条款/应用内若写着「管理员提供搜索 key」，那句话目前与事实不符。")
        print("配置方法：sudo bash /opt/cityu-mail-pilot/pilot_app/set_platform_key.sh --search")
        return 1
    key = providers.platform_search_key()
    provider = connection["provider"]
    print(f"平台搜索 key：已配置（长度 {len(key)}，内容不显示）")
    print(f"调用：{provider} · 查询词是固定诊断串，不取自任何用户邮件")

    def _scrub(text: str) -> str:
        value = str(text)
        return value.replace(key, "***已隐藏***") if key else value

    saved_timeout = providers.MODEL_TIMEOUT_SECONDS
    if timeout > 0:
        providers.MODEL_TIMEOUT_SECONDS = int(timeout)
    started = time.monotonic()
    try:
        try:
            results = providers.web_search(provider, key, query, count=3)
        except Exception as exc:  # noqa: BLE001 - the CLI reports, it does not raise
            print(f"调用失败（{time.monotonic() - started:.1f}s）：{_scrub(exc)}")
            print("key 已写入但不可用。检查这个 key 是否开通了搜索服务、以及供应商是否选对。")
            return 1
    finally:
        providers.MODEL_TIMEOUT_SECONDS = saved_timeout
    elapsed = time.monotonic() - started
    print(f"调用成功：{elapsed:.1f}s，返回 {len(results)} 条结果")
    for item in results[:3]:
        print(f"  · {_scrub(item.get('title', ''))[:60]} — {_scrub(item.get('url', ''))[:70]}")
    if not results:
        print("调用通了但没返回结果：多半是查询词或计费额度的问题，不是 key 坏了。")
    print("结论：平台搜索 key 可用，没配搜索的用户会自动用它。")
    return 0


def check_alerts(db: Database, *, dry_run: bool = False) -> int:
    """Run the sentinel by hand. Useful for verifying thresholds after a deploy."""
    if dry_run:
        findings = alerting.evaluate(db)
        for item in findings:
            print(f"[{item['severity']}] {item['key']}\t{item['title']}\t{item['detail']}")
        print(f"DRY RUN：当前有 {len(findings)} 项会告警；未发送邮件，也未写入 alert_state。")
        return 0
    box = SecretBox.from_environment()
    result = alerting.run_checks(db, box)
    print(f"哨兵结果：{result}")
    return 1 if result["errors"] else 0


def restore_drill(db: Database, backup_path: str = "") -> int:
    """Prove a backup can actually be restored — without touching live data.

    "We have backups" and "we can restore" are different claims, and only the
    second one matters on the day it matters. This opens a copy of the newest
    backup, checks SQLite's own integrity verdict, and then decrypts a real
    mailbox credential out of it with the *current* master key. That last step
    is the one that catches the failure a structural check cannot see: a backup
    taken under a master key that no longer exists restores perfectly and is
    still worthless.

    Read-only with respect to production: everything happens on a copy in a
    temporary directory.
    """
    backup_dir = Path(os.environ.get("INFE_PILOT_BACKUP_DIR", "/var/backups/cityu-mail-pilot"))
    if backup_path:
        candidate = Path(backup_path)
    else:
        copies = sorted(backup_dir.glob("pilot-*.sqlite3"), reverse=True)
        if not copies:
            print(f"在 {backup_dir} 里找不到 pilot-*.sqlite3。")
            return 1
        candidate = copies[0]
    if not candidate.is_file():
        print(f"找不到备份文件：{candidate}")
        return 1

    print(f"备份文件：{candidate}")
    print(f"大小    ：{candidate.stat().st_size / 1024:.0f} KB")
    # The fingerprint is printed first and always, because it is the one thing the
    # operator can compare against the copy in their hand. It identifies the key
    # without revealing it -- see security.key_fingerprint.
    try:
        print(f"主密钥指纹：{SecretBox.from_environment().fingerprint()}"
              "（与你自己保存的那份对照；它不等于密钥本身）")
    except Exception as exc:  # noqa: BLE001 - the drill below reports the real problem
        print(f"主密钥指纹：读不出来（{exc}）")

    with tempfile.TemporaryDirectory() as work:
        copy = Path(work) / "restored.sqlite3"
        shutil.copy2(candidate, copy)

        try:
            with sqlite3.connect(copy) as connection:
                verdict = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if verdict != "ok":
                    print(f"✗ 完整性检查未通过：{verdict}")
                    return 1
                print("✓ 完整性检查：ok")
                restored_counts = {
                    table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("users", "messages", "reports")
                }
        except sqlite3.DatabaseError as exc:
            # A badly truncated file makes integrity_check *raise* rather than
            # return a verdict — and a corrupt backup is exactly when someone
            # runs this, so it must report, not traceback.
            print(f"✗ 打不开这份备份：{exc}")
            return 1

        # The decisive step: can the running configuration still read the data?
        restored = Database(str(copy))
        box = SecretBox.from_environment()
        secrets_read = 0
        unreadable = 0
        for row in restored.list_users_overview():
            mailbox = restored.get_mailbox(row["id"])
            if not mailbox or not mailbox.get("encrypted_password"):
                continue
            try:
                box.decrypt(mailbox["encrypted_password"], context=f"mailbox:{row['id']}")
                secrets_read += 1
            except Exception as exc:
                unreadable += 1
                print(f"✗ 无法用当前主密钥解密 {row['email']} 的邮箱授权码：{type(exc).__name__}")
        if unreadable:
            print("✗ 这份备份解不开——很可能它是在另一把主密钥下生成的。")
            return 1
        print(f"✓ 用当前主密钥成功解密 {secrets_read} 个邮箱授权码（未打印任何内容）")

        live = {
            "users": len(db.list_users_overview()),
        }
        print(f"  用户  备份 {restored_counts['users']} / 线上 {live['users']}")
        print(f"  邮件  备份 {restored_counts['messages']}")
        print(f"  报告  备份 {restored_counts['reports']}")

    print("\n✓ 恢复演练通过：这份备份可以被读回，并且与当前主密钥匹配。")
    print("  真正的恢复步骤见 docs/restore-drill.md（停服务 → 换库 → 起服务 → 核对）。")
    return 0


# Readings that must exist wherever /proc does. A null here means the parser or
# the host is broken -- not "nothing to report", which is what a missing number
# in the panel would otherwise look like.
REQUIRED_READINGS = (
    "host.cpu_percent",
    "host.memory.total_mb",
    "host.disk.total_gb",
    "host.uptime_seconds",
    "host.network.rx_kbps",
    "host.network.tx_kbps",
    "process.rss_mb",
    "process.threads",
)


def _dig(snapshot: dict, path: str):
    value: Any = snapshot
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def check_metrics(db: Database) -> int:
    """Take one real reading on this machine and say whether it is plausible.

    The browser suite skips the CPU/memory assertions on a Mac because there is
    no /proc there, so those code paths were only ever exercised against a
    fixture tree -- never against the kernel they exist to read. This is the
    other half of that pair: run it on a box that really has /proc, print every
    reading, and fail loudly if one that must exist is missing.

    It reads the live database for the application numbers, but only with the
    same read-only queries the admin panel uses. Nothing here writes.
    """
    from pilot_app import metrics as metrics_mod

    with db.connect() as connection:
        snapshot = metrics_mod.collect(connection)
    host = snapshot.get("host", {})
    process = snapshot.get("process", {})
    application = snapshot.get("application", {})
    memory = host.get("memory", {}) or {}
    disk = host.get("disk", {}) or {}
    network = host.get("network", {}) or {}

    print(f"平台        {host.get('platform')} · {host.get('cpu_count')} 核 · Python {host.get('python')}")
    print(f"CPU         {host.get('cpu_percent')} %   负载 {host.get('load')}")
    print(f"内存        {memory.get('used_mb')} / {memory.get('total_mb')} MB"
          f"（{memory.get('percent')} %）· 交换 {memory.get('swap_used_mb')} / {memory.get('swap_total_mb')} MB")
    print(f"磁盘 /      {disk.get('used_gb')} / {disk.get('total_gb')} GB（{disk.get('percent')} %）")
    print(f"网络        rx {network.get('rx_kbps')} KB/s · tx {network.get('tx_kbps')} KB/s")
    print(f"开机时长    {host.get('uptime_seconds')} s（本进程 {process.get('uptime_seconds')} s）")
    print(f"本进程      rss {process.get('rss_mb')} MB · 线程 {process.get('threads')}"
          f" · 打开文件 {process.get('open_files')}")
    print(f"应用        近 1 小时邮件 {application.get('messages_1h')} · 队列 {application.get('queue')}"
          f" · 失败 {application.get('failed')} · 中位耗时 {application.get('median_latency_seconds')} s")
    print(f"数据库      {application.get('database_size_mb')} MB（WAL {application.get('wal_size_mb')} MB）")

    problems = [f"host 整块报错：{host['error']}"] if "error" in host else []
    for path in REQUIRED_READINGS:
        if _dig(snapshot, path) is None:
            problems.append(f"{path} 是 None —— 这台机器有 /proc，不该读不到")
    if problems:
        print("\n不合格：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("\n✓ 读数齐全，没有一个是 None —— 这台机器上的 /proc 路径是通的。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    invite = sub.add_parser("create-invite")
    invite.add_argument("--label", default="pilot")
    invite.add_argument("--days", type=int, default=7)
    migrate = sub.add_parser(
        "migrate-legacy-imap-state",
        help="把旧 imap-state.json 的精确 UID 集合安全迁入已暂停的试点账户",
    )
    migrate.add_argument("--user-email", required=True, help="试点网页登录邮箱")
    migrate.add_argument("--state", required=True, help="旧 imap-state.json 的只读副本")
    migrate.add_argument("--uid-validity", required=True, help="网页只读邮箱测试返回的 UIDVALIDITY")
    migrate.add_argument("--apply", action="store_true", help="实际写入；省略时只预览")
    migrate.add_argument(
        "--lookback-hours", type=int,
        default=int(os.environ.get("INFE_PILOT_INITIAL_LOOKBACK_HOURS", "48")),
        help="迁移后第一次回扫的窗口小时数（默认取 INFE_PILOT_INITIAL_LOOKBACK_HOURS 或 48）；"
             "用于判断有多少空洞会被覆盖",
    )
    migrate.add_argument(
        "--allow-unverified-uid-validity", action="store_true",
        help="服务器读不到 UIDVALIDITY 或与传入值不一致时仍强制继续（不推荐，会让去重失效）",
    )
    sub.add_parser("generate-master-key")
    e2e = sub.add_parser(
        "verify-e2e",
        help="真实链路验证：只读抓信 → 模型/搜索 → 渲染，默认不发信、不写游标",
    )
    e2e.add_argument("--user-email", required=True, help="试点网页登录邮箱（不是转发邮箱）")
    e2e.add_argument("--limit", type=int, default=1, help="最多处理几封新邮件")
    e2e.add_argument("--pull", type=int, default=0,
                     help="忽略游标，从邮箱里取最近 N 封真实邮件做验证（只读，不推进游标）")
    e2e.add_argument("--pull-date", default="",
                     help="配合 --pull/--store：把这些真实邮件落成这一天的邮件（YYYY-MM-DD），"
                          "以便真实地验证当日简报")
    e2e.add_argument("--store", action="store_true",
                     help="配合 --pull-date：把真实邮件与即时报告写入数据库（默认不落库）")
    e2e.add_argument("--send", action="store_true",
                     help="真的发送报告（默认只渲染；已有成功报告的邮件永不重发）")
    e2e.add_argument("--force-resend", action="store_true",
                     help="配合 --send：允许重发已有成功报告的邮件，仅用于验收取证")
    e2e.add_argument("--send-digest", action="store_true",
                     help="配合 --send：把今天的每日简报也真实发一次")
    e2e.add_argument("--pause", action="store_true",
                     help="验证期间临时禁用该邮箱，结束后恢复，确保没有第二个 worker 消费")
    e2e.add_argument("--measure", action="store_true",
                     help="强制重新生成以测量耗时/Token，但不发送、不落库（用于延迟调优）")
    e2e.add_argument("--measure-model", default="",
                     help="配合 --measure：临时用这个模型名量测（不修改用户配置）")
    e2e.add_argument("--show-body", action="store_true", help="额外打印报告纯文本预览（含邮件内容，注意隐私）")
    diag = sub.add_parser(
        "diagnose-forwarding",
        help="只读检查某个邮箱是否重复收到同一封邮件（只打印邮件头，不打印正文）",
    )
    diag.add_argument("--email", default="", help="要检查的邮箱地址（例如 operator@example.com）；用 --password-file 时可省略")
    diag.add_argument("--host", default="imap.qq.com")
    diag.add_argument("--port", type=int, default=993)
    diag.add_argument("--folder", default="INBOX")
    diag.add_argument("--limit", type=int, default=40)
    diag.add_argument("--password-env", default="INFE_DIAG_PASSWORD",
                      help="从该环境变量读取授权码；省略时用隐藏输入提示")
    diag.add_argument("--env-file", default="", help="可选的 pilot.env，用于读取 INFE_PILOT_* 默认值")
    diag.add_argument("--password-file", default="",
                      help="只读该文件取授权码（0600）；支持单行只放密码，或 email=/password= 两行格式")
    unit_fail = sub.add_parser(
        "notify-unit-failure",
        help="systemd OnFailure= 处理器：把某个单元失败的事实发给管理员（输出已脱敏）",
    )
    unit_fail.add_argument("--unit", required=True, help="失败的单元名，由 OnFailure=...@%%n 传入")
    unit_fail.add_argument("--lines", type=int, default=30, help="附带的日志行数上限")
    alerts_parser = sub.add_parser(
        "check-alerts",
        help="手动跑一次巡检哨兵；--dry-run 只打印会告警什么，不发信也不写状态",
    )
    alerts_parser.add_argument("--dry-run", action="store_true",
                               help="只列出当前判定结果，不发送、不改动 alert_state")
    invitations_parser = sub.add_parser(
        "invitations",
        help="查每个申请者的邀请码到底发出去了没有、有没有被用掉",
    )
    invitations_parser.add_argument("--limit", type=int, default=100, help="最多看多少条申请")
    drill = sub.add_parser(
        "restore-drill",
        help="验证最新备份能不能真的恢复：完整性 + 用当前主密钥解密（只读，不动线上数据）",
    )
    drill.add_argument("--backup", default="", help="指定某个备份文件；省略时用备份目录里最新的那份")
    # Named with a suffix on purpose: a bare `check_model` here shadows the
    # function of the same name and the dispatch then calls the parser.
    check_model_parser = sub.add_parser(
        "check-model",
        help="验证实例级兜底模型 key 能不能真的调用（只发一句、不打印 key）",
    )
    check_model_parser.add_argument("--prompt", default="只回答两个字：可用",
                                    help="发给模型的最小提示词")
    check_model_parser.add_argument("--timeout", type=int, default=60, help="最长等待秒数")
    check_search_parser = sub.add_parser(
        "check-search",
        help="验证实例级兜底搜索 key 能不能真的调用（固定诊断查询，不打印 key）",
    )
    check_search_parser.add_argument("--query", default="City University of Hong Kong",
                                     help="诊断用的固定查询词（不要填用户邮件里的内容）")
    check_search_parser.add_argument("--timeout", type=int, default=60, help="最长等待秒数")
    sub.add_parser(
        "check-metrics",
        help="在真机上采一次主机指标并核对读数（浏览器套件跳过的那几条由它负责）",
    )
    args = parser.parse_args()
    if args.command == "generate-master-key":
        print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
        return 0
    if args.command == "diagnose-forwarding":
        if args.env_file:
            _load_env_file(args.env_file)
        email_address = args.email
        password = os.environ.get(args.password_env, "")
        if args.password_file:
            file_email, file_password = read_credential_file(args.password_file)
            email_address = email_address or file_email
            password = password or file_password
        if not email_address:
            print("请用 --email 指定要检查的邮箱，或在凭据文件里写 email=。")
            return 2
        if not password:
            import getpass
            password = getpass.getpass("请输入该邮箱的 IMAP 授权码（输入时不显示）：")
        if not password:
            print("没有提供授权码。")
            return 2
        return diagnose_forwarding(email_address, password, host=args.host, port=args.port,
                                   folder=args.folder, limit=args.limit)
    if args.command == "check-model":
        # Dispatched before the database is opened: this check needs no storage,
        # and requiring one made it fail on a host where the app was not
        # installed yet -- which is exactly when someone is setting a key.
        return check_model(args.prompt, args.timeout)
    if args.command == "check-search":
        return check_search(args.query, args.timeout)
    db = Database(os.environ.get("INFE_PILOT_DB", "/var/lib/cityu-mail-pilot/pilot.sqlite3"))
    db.initialize()
    if args.command == "invitations":
        return invitations(db, limit=args.limit)
    if args.command == "check-alerts":
        return check_alerts(db, dry_run=args.dry_run)
    if args.command == "restore-drill":
        return restore_drill(db, args.backup)
    if args.command == "check-metrics":
        return check_metrics(db)
    if args.command == "notify-unit-failure":
        return notify_unit_failure(db, args.unit, lines=args.lines)
    if args.command == "verify-e2e":
        return verify_e2e(db, args.user_email, args.limit, args.send, args.show_body,
                          force_resend=args.force_resend, send_digest=args.send_digest,
                          pause=args.pause, pull=args.pull, pull_date=args.pull_date,
                          store=args.store, measure=args.measure,
                          measure_model=args.measure_model)
    if args.command == "migrate-legacy-imap-state":
        user = db.find_user_for_login(args.user_email)
        if not user:
            parser.error("找不到该试点用户。")
        uids = read_legacy_processed_uids(args.state)
        mailbox = db.get_mailbox(user["id"])
        if not mailbox:
            parser.error("该用户还没有配置私人邮箱，无法核对 UIDVALIDITY。")

        # 缺口 B：把 UIDVALIDITY 与服务器真实值比对。占位记录按 UIDVALIDITY 参与唯一键，
        # 填错会让去重整体失效并把旧邮件全部重发，因此这里必须 fail-closed。
        verified = ""
        holes: list[int] = []
        holes_outside = 0
        probe_error = ""
        try:
            box = SecretBox.from_environment()
            password = box.decrypt(mailbox["encrypted_password"], context=f"mailbox:{user['id']}")
            probe = mailio.probe_mailbox(mailbox, password, lookback_hours=args.lookback_hours)
            verified = str(probe["uid_validity"])
            processed = set(uids)
            holes = [uid for uid in probe["present"] if uid not in processed]
            recent = set(probe["recent"])
            holes_outside = sum(1 for uid in holes if uid not in recent)
        except Exception as exc:  # 网络/凭据问题不应该伪装成"校验通过"
            probe_error = str(exc)

        print(f"用户            : {user['email']}")
        print(f"旧 UID          : {len(uids)} 个，范围 {uids[0]}..{uids[-1]}")
        print(f"传入 UIDVALIDITY: {args.uid_validity}")
        print(f"服务器 UIDVALIDITY: {verified or '（无法读取：' + probe_error + '）'}")

        if verified:
            if verified != args.uid_validity and not args.allow_unverified_uid_validity:
                parser.error(
                    "传入的 --uid-validity 与服务器实际值不一致——这会让占位记录落在错误的去重空间，"
                    "导致旧邮件被重新处理并重复发送。请改用服务器返回值；确有必要时加 "
                    "--allow-unverified-uid-validity 强制继续。"
                )
            print(f"空洞（存在但未处理）: {len(holes)} 个，其中 {len(holes) - holes_outside} 个在 "
                  f"{args.lookback_hours} 小时回扫窗口内")
            if holes_outside:
                print(f"⚠️  有 {holes_outside} 个空洞早于回扫窗口，迁移后不会被补做。"
                      f"如需覆盖，请把 INFE_PILOT_INITIAL_LOOKBACK_HOURS 调到能覆盖最老空洞的值，"
                      f"或手工补做这些 UID：{holes[:20]}")
        else:
            if not args.allow_unverified_uid_validity:
                parser.error(
                    "无法从服务器读取 UIDVALIDITY（" + probe_error + "）。为避免重复发信，"
                    "请确认该邮箱可只读连接后重试；确有必要时加 --allow-unverified-uid-validity 强制继续。"
                )
            print("⚠️  未能校验 UIDVALIDITY（已显式允许）；去重是否成立取决于你填写的值。")

        if not args.apply:
            print("DRY RUN：确认账户已暂停、UIDVALIDITY 与上面一致后，加 --apply 执行。")
            return 0
        result = db.seed_legacy_processed_uids(
            user["id"], args.uid_validity, uids,
            verification="server" if verified == args.uid_validity else "operator-override",
        )
        print(
            "迁移完成：读取 {seen} 个，新增 {inserted} 个，已存在 {already_present} 个。"
            "邮箱游标已设为安全重扫模式；现在可恢复账户。".format(**result)
        )
        return 0
    code = secrets.token_urlsafe(18)
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=max(1, args.days))
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO invites(code_hash,label,expires_at) VALUES(?,?,?)",
            (token_hash(code), args.label[:100], expires.isoformat(timespec="seconds")),
        )
    print(code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
