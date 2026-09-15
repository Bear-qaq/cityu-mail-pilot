"""Operator alerting: mail the admin when something is actually broken.

Why this lives in the worker instead of a monitoring stack
---------------------------------------------------------
The production box is 2 vCPU / 2 GB serving two real users, and the four things
worth watching are already computed here: ``web._service_health()`` derives
stale mailboxes, queue depth and failed reports, and ``metrics.host_metrics()``
derives disk usage. A monitoring daemon would add a process, a database and a
dependency tree to re-derive numbers this code already has. Every candidate was
measured and rejected on that basis (uptime-kuma's container was reported at
800 MB; netdata's own README claims 150 MiB resident; healthchecks wants
PostgreSQL and fifteen packages). The full comparison is in
``docs/open-source-recon-2026-09-14.md``.

The failure this is really about
--------------------------------
``certbot.timer`` and ``cityu-mail-pilot-backup.timer`` can fail silently. A
failed backup is a data-loss risk and an expired certificate takes the whole
site down, and until now neither would have told anybody. Those two are covered
by the systemd ``OnFailure=`` handler, which is a *separate* path because it
still works when this worker is the thing that died.

Design
------
:func:`evaluate` is the pure decision function: a database in, findings out.
Everything that touches the outside world (disk usage, the TLS handshake) is
passed *in*, so tests can drive every threshold without a full disk, a clock or
a socket.

:func:`run_checks` is what the worker calls. It de-duplicates: a problem mails
once, then stays quiet for ``INFE_PILOT_ALERT_REPEAT_SECONDS`` unless the detail
changed, and clearing it sends exactly one recovery notice. State lives in the
``alert_state`` table so a worker restart does not re-send everything.
"""

from __future__ import annotations

import datetime as dt
import html
import logging
import os
import re
import socket
import ssl
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from . import agent, backup, mailio, metrics
from .database import Database, parse_utc
from .security import SecretBox


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.environ.get(name, str(default)))))
    except (TypeError, ValueError):
        return default


# Master switch, so an operator can silence the sentinel without a redeploy.
ALERTS_ENABLED = os.environ.get("INFE_PILOT_ALERTS", "1") != "0"
# How often the worker runs the checks. Seconds.
ALERT_CHECK_SECONDS = _int_env("INFE_PILOT_ALERT_CHECK_SECONDS", 300, 30, 86_400)
# A problem that stays broken re-mails at most this often. Six hours: frequent
# enough to survive a lost message, rare enough that a long outage cannot bury
# the operator in its own alerting.
ALERT_REPEAT_SECONDS = _int_env("INFE_PILOT_ALERT_REPEAT_SECONDS", 6 * 3600, 60, 30 * 86_400)
# No poll attempt at all for this long means the poller thread is wedged or the
# worker is gone, not that one IMAP login failed (a failure still stamps
# last_polled_at and is reported through mailbox_error instead).
ALERT_STALE_MINUTES = _int_env("INFE_PILOT_ALERT_STALE_MINUTES", 15, 5, 1440)
# A mailbox polled every 15 minutes is not "stalled" after 15 minutes, so the
# threshold has to scale with whatever interval that provider asks for. This
# multiple is what turns "no poll yet" into "the poller is probably wedged".
STALE_INTERVAL_MULTIPLE = _int_env("INFE_PILOT_ALERT_STALE_INTERVALS", 3, 2, 20)
ALERT_QUEUE_DEPTH = _int_env("INFE_PILOT_ALERT_QUEUE_DEPTH", 20, 1, 10_000)
ALERT_FAILED_REPORTS = _int_env("INFE_PILOT_ALERT_FAILED_REPORTS", 5, 1, 10_000)
ALERT_DISK_PERCENT = _int_env("INFE_PILOT_ALERT_DISK_PERCENT", 90, 50, 100)
ALERT_CERT_DAYS = _int_env("INFE_PILOT_ALERT_CERT_DAYS", 14, 1, 365)
# Registered this long ago and still not finished. Twelve hours rather than a
# day: the operator invited these people personally, so "signed up this morning
# and never came back" is worth an evening nudge, not tomorrow's.
ALERT_SETUP_STALL_HOURS = _int_env("INFE_PILOT_ALERT_SETUP_STALL_HOURS", 12, 1, 24 * 30)
# A stalled signup is not an incident: it changes when the person acts or when
# the operator does, and it must not re-mail every six hours in the meantime.
# One reminder a day, and the key is per account so finishing one clears only
# that one.
ALERT_SETUP_REPEAT_SECONDS = _int_env("INFE_PILOT_ALERT_SETUP_REPEAT_SECONDS",
                                      24 * 3600, 3600, 30 * 86_400)
# The daily backup runs at 03:20, so 36 hours means one run was missed rather
# than one being a little late. `OnFailure=` on the unit only covers a run that
# happened and failed: a disabled timer, a host that stayed down, or a unit
# that was never re-enabled after maintenance produce no failure event at all.
# A backup that is stale stays stale for a while; re-mailing every six hours
# about the same unchanged fact is how an operator learns to skim past alerts.
ALERT_BACKUP_REPEAT_SECONDS = _int_env("INFE_PILOT_ALERT_BACKUP_REPEAT_SECONDS",
                                       24 * 3600, 3600, 30 * 86_400)
# An invite e-mail that failed leaves the applicant with nothing: no code, no
# error, no way to know they were approved. The sentinel notices within five
# minutes; this is only how often the same unchanged failure may re-mail, and
# a day is the operator's own phrasing ("每日巡检").
ALERT_INVITE_REPEAT_SECONDS = _int_env("INFE_PILOT_ALERT_INVITE_REPEAT_SECONDS",
                                       24 * 3600, 3600, 30 * 86_400)
ALERT_BACKUP_HOURS = _int_env("INFE_PILOT_ALERT_BACKUP_HOURS", 36, 2, 24 * 30)
# An offsite push that has stopped is quieter than a local backup that stopped:
# the local copy keeps succeeding, so nothing looks wrong until the day the
# machine is gone. Two days of slack, then say so.
ALERT_OFFSITE_HOURS = _int_env("INFE_PILOT_ALERT_OFFSITE_HOURS", 48, 2, 24 * 30)

# Which hostname to inspect for certificate expiry. An explicit
# INFE_PILOT_TLS_HOST wins; otherwise it is derived from INFE_PILOT_ORIGIN,
# which every install already sets to the name users actually reach. Only an
# install with neither gets no certificate alerting — better than guessing a
# hostname and reporting on a certificate nobody serves.
def _tls_host_from_environment() -> str:
    explicit = os.environ.get("INFE_PILOT_TLS_HOST", "").strip()
    if explicit:
        return explicit
    origin = os.environ.get("INFE_PILOT_ORIGIN", "").strip()
    if not origin:
        return ""
    return urllib.parse.urlsplit(origin).hostname or ""


ALERT_TLS_HOST = _tls_host_from_environment()
ALERT_TLS_PORT = _int_env("INFE_PILOT_TLS_PORT", 443, 1, 65535)

# Every credential key in pilot.env matches this. Used to scrub command output
# before it is copied into an operator e-mail.
_SECRET_KEY_PATTERN = re.compile(r"(KEY|SECRET|PASSWORD|PASSWD|TOKEN|AUTH)", re.IGNORECASE)


def admin_emails() -> set[str]:
    """The operator set. Solely from the server environment, never the database.

    This is the same rule ``web._admin_emails`` enforces for incoming requests;
    keeping one implementation means "who is an admin" cannot drift between the
    HTTP surface and the mail surface.
    """
    raw = os.environ.get("INFE_PILOT_ADMIN_EMAILS", "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


# ---------------------------------------------------------------------------
# pure decision function
# ---------------------------------------------------------------------------
def _human_age(delta: dt.timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    if seconds < 86400:
        return f"{seconds // 3600} 小时"
    return f"{seconds // 86400} 天"


def _finding(key: str, severity: str, title: str, detail: str) -> dict[str, str]:
    return {"key": key, "severity": severity, "title": title, "detail": detail}


def stale_after_for(mailbox: dict[str, Any]) -> dt.timedelta:
    """How long without a poll counts as stalled for *this* mailbox.

    Gmail is only polled every 15 minutes because Google documents that as its
    limit, so a flat 15-minute threshold would flag a perfectly healthy mailbox
    on every single pass — the fastest way to teach an operator to ignore
    alerts. The threshold therefore scales with whatever floor the provider
    publishes.
    """
    minutes = ALERT_STALE_MINUTES
    floor = mailio.minimum_poll_seconds(mailbox)
    if floor:
        minutes = max(minutes, (floor * STALE_INTERVAL_MULTIPLE) // 60)
    return dt.timedelta(minutes=minutes)


def evaluate(
    db: Database,
    *,
    now: dt.datetime | None = None,
    disk_percent: float | None = None,
    certificate_days: float | None = None,
    backup_dir: "Path | None" = None,
) -> list[dict[str, str]]:
    """Return every condition that currently deserves the operator's attention.

    Reads the database, the two injected readings, and the backup directory (also
    injectable, for the same reason). Deterministic for a given ``now``, which is
    what makes the thresholds testable.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    findings: list[dict[str, str]] = []
    rows = db.list_users_overview()

    queue_depth = 0
    failed_reports = 0
    for row in rows:
        # A paused account is *meant* to stop polling, so exclude it rather than
        # waking the operator up for a state they chose.
        if str(row.get("status") or "") != "active":
            continue
        queue_depth += int(row.get("queue_depth") or 0)
        failed_reports += int(row.get("failed_reports") or 0)
        if not row.get("mailbox_email") or not row.get("mailbox_enabled"):
            continue

        account = str(row.get("email") or "")
        label = f"{account} 的转发邮箱"

        error = str(row.get("mailbox_error") or "").strip()
        if error:
            findings.append(_finding(
                f"mailbox_error:{row['id']}", "critical",
                f"收信失败：{account}",
                f"最近一次轮询报错：{error}（这不会丢邮件，恢复后会自动补做）",
            ))
        else:
            # Only look for a *silent* stall when there is no error to report.
            # A failed poll is the cause and "no poll for N minutes" is its
            # consequence: reporting both counts one incident twice, and the
            # consequence is the noisier of the two. On 2026-09-15 that pair sent
            # the operator ~70 mails in a day, one every five minutes.
            last_poll = parse_utc(row.get("last_polled_at"))
            stale_after = stale_after_for(row)
            stale_minutes = int(stale_after.total_seconds() // 60)
            if last_poll is None:
                findings.append(_finding(
                    f"mailbox_stale:{row['id']}", "critical", f"从未轮询成功：{account}",
                    f"{label} 没有任何轮询记录，后台轮询线程可能没有启动。",
                ))
            elif now - last_poll > stale_after:
                # No live counter in the detail. `_should_send` deliberately
                # re-mails a condition whose detail *changed*, so an "N minutes
                # ago" that grows every minute re-mailed every pass and turned a
                # dedupe rule into a metronome. The threshold is stable, and the
                # exact age is one click away in the console.
                findings.append(_finding(
                    f"mailbox_stale:{row['id']}", "critical", f"收信停顿：{account}",
                    f"{label} 已超过 {stale_minutes} 分钟没有轮询。",
                ))

    if queue_depth > ALERT_QUEUE_DEPTH:
        findings.append(_finding(
            "queue_backlog", "warning", "生成队列堆积",
            f"有 {queue_depth} 封邮件在排队（阈值 {ALERT_QUEUE_DEPTH}），"
            "通常意味着模型 key 被限流或供应商变慢。",
        ))

    if failed_reports > ALERT_FAILED_REPORTS:
        findings.append(_finding(
            "failed_reports", "warning", "报告持续失败",
            f"有 {failed_reports} 份报告处于失败状态（阈值 {ALERT_FAILED_REPORTS}）。",
        ))

    if disk_percent is not None and disk_percent >= ALERT_DISK_PERCENT:
        findings.append(_finding(
            "disk", "critical", "磁盘空间不足",
            f"根分区已用 {disk_percent}%（阈值 {ALERT_DISK_PERCENT}%）。",
        ))

    # Two different failures look identical from here: someone who never set the
    # mailbox up, and someone whose authorisation code is wrong. Both end with a
    # user who receives nothing and no error anywhere, so both are reported --
    # with different wording, because the fix is different.
    for row in db.stalled_setups(hours=ALERT_SETUP_STALL_HOURS, now=now):
        account = str(row.get("email") or "")
        reason = db.SETUP_GAP_LABELS.get(row["setup_gap"], row["setup_gap"])
        findings.append(_finding(
            f"setup_stalled:{row['id']}", "warning",
            f"注册后没配完：{account}",
            f"注册已 {row['age_hours']:.0f} 小时（阈值 {ALERT_SETUP_STALL_HOURS} 小时），{reason}。"
            "这样的人不会收到任何报告，也不会产生任何错误——需要你去问一句。",
        ))

    # Invitations. The applicant is the one party who cannot see this failure:
    # from their side nothing happened at all, so silence is the whole symptom.
    for row in db.failed_invite_sends(100):
        account = str(row.get("email") or "")
        findings.append(_finding(
            f"invite_failed:{row['id']}", "warning",
            f"邀请码邮件发不出去：{account}",
            f"给 {account} 的邀请码已经生成，但邮件发送失败："
            f"{str(row.get('invite_send_error') or '')[:200]}。"
            "他那边什么都不会发生，也就不会来问你——需要你手动把码转达给他，或者重发。",
        ))

    # Backups. A database with no fresh copy is the one failure nobody notices
    # until it is unrecoverable, because everything keeps working right up to the
    # moment it does not.
    # `Path(...)` even when a path was injected: a caller passing a string is an
    # ordinary mistake, and a sentinel that dies on it takes every other check down
    # with it -- which is exactly the failure this block was just fixed for.
    directory = (Path(backup_dir) if backup_dir
                 else Path(os.environ.get("INFE_PILOT_BACKUP_DIR",
                                          "/var/backups/cityu-mail-pilot")))
    # `is_dir()` on a path whose *parent* is not searchable raises rather than
    # returning False -- `/var/backups` is root-only on macOS and on a host where
    # the installer has not run yet. A sentinel that dies on that is worse than
    # one that checks less, and "I cannot look" must not be reported as "there is
    # nothing there": the first alerts on every developer machine, the second
    # silently skips a real problem. So: unreadable => say nothing about backups.
    try:
        can_look = directory.is_dir() and os.access(directory, os.R_OK)
    except OSError:
        can_look = False
    if can_look:
        age = backup.newest_backup_age_hours(directory, now=now)
        if age is None:
            findings.append(_finding(
                "backup_missing", "critical", "没有任何数据库备份",
                f"{directory} 里一份备份都没有。备份定时器可能从没跑过，"
                "或者备份目录被清空了。",
            ))
        elif age > ALERT_BACKUP_HOURS:
            findings.append(_finding(
                "backup_stale", "critical", "数据库备份已经过期",
                f"最新一份备份是 {age:.0f} 小时前的（阈值 {ALERT_BACKUP_HOURS} 小时）。"
                "备份定时器可能被停用了——注意 `OnFailure=` 只在「跑了但失败」时触发，"
                "「根本没跑」不会触发。",
            ))
        # No "everything is fine" finding when the copy is fresh. It used to emit
        # one ("备份正常", severity info) and it behaved exactly like an alert:
        # `_should_send` re-mails a finding whose detail changed, and this one's
        # detail carried the age in whole hours, so the operator got a cheerful
        # e-mail every hour. Every other check here is silent while healthy, and
        # a stale-then-fresh backup still produces one recovery notice through
        # the normal path. Observability is not lost: `manage backup --check`
        # reports the state on demand, and a *dead* backup check is a problem no
        # e-mail could reveal anyway.
        offsite = backup.read_offsite_state(directory)
        if offsite and offsite.get("configured"):
            if not offsite.get("ok"):
                findings.append(_finding(
                    "offsite_failed", "warning", "异地备份推送失败",
                    f"最近一次推送失败（{offsite.get('at') or '时间未知'}）："
                    f"{str(offsite.get('error') or '未知错误')[:200]}",
                ))
            else:
                pushed = parse_utc(offsite.get("at"))
                if pushed and (now - pushed).total_seconds() > ALERT_OFFSITE_HOURS * 3600:
                    findings.append(_finding(
                        "offsite_stale", "warning", "异地备份已经过期",
                        f"最后一次成功推送是 {(now - pushed).total_seconds() / 3600:.0f} 小时前"
                        f"（阈值 {ALERT_OFFSITE_HOURS} 小时）。本地备份还在成功，"
                        "所以这件事只有这里会告诉你。",
                    ))

    if certificate_days is not None and certificate_days < ALERT_CERT_DAYS:
        if certificate_days < 0:
            detail = "已过期，站点随时会报证书错误。"
        else:
            detail = f"还有 {certificate_days:.0f} 天到期（阈值 {ALERT_CERT_DAYS} 天）。"
        findings.append(_finding(
            "tls_cert", "critical", "HTTPS 证书即将失效",
            f"{ALERT_TLS_HOST} 的证书{detail}请检查 certbot 续期是否失败。",
        ))

    return findings


# ---------------------------------------------------------------------------
# outside-world readings
# ---------------------------------------------------------------------------
def certificate_days_remaining(*, host: str | None = None, port: int | None = None,
                               timeout: float = 8.0) -> float | None:
    """Days until the served certificate expires, or ``None`` if unreadable.

    Verification stays **on** deliberately. The tempting alternative — disable
    it so an expired certificate still yields a readable ``notAfter`` — does not
    even work: ``getpeercert()`` returns an empty dict when the peer was not
    validated, so the one case worth reporting would come back as "unknown".
    A failed handshake is therefore reported as a negative number, which is the
    honest reading: users reaching this host get a certificate error. A genuine
    network problem still returns ``None``, because that is not the site's
    certificate being wrong.
    """
    host = ALERT_TLS_HOST if host is None else host
    if not host:
        return None
    port = ALERT_TLS_PORT if port is None else port
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                certificate = tls.getpeercert()
    except ssl.SSLCertVerificationError as exc:
        logging.warning("certificate for %s:%s failed verification (%s)", host, port, exc)
        return -1.0
    except (OSError, ssl.SSLError) as exc:
        logging.warning("certificate check could not reach %s:%s (%s)", host, port, exc)
        return None
    not_after = str((certificate or {}).get("notAfter") or "")
    if not not_after:
        logging.warning("certificate for %s:%s carried no notAfter field", host, port)
        return None
    try:
        expiry = dt.datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    return (expiry - dt.datetime.now(dt.timezone.utc)).total_seconds() / 86400


def disk_percent() -> float | None:
    """Root-filesystem usage, via the same reader the admin panel uses."""
    try:
        return metrics.host_metrics()["disk"]["percent"]
    except Exception:  # a metrics failure must never take the sentinel down
        logging.exception("disk reading failed for the alert sentinel")
        return None


# ---------------------------------------------------------------------------
# de-duplication
# ---------------------------------------------------------------------------
def _repeat_for(key: str) -> int | None:
    """How often a particular kind of finding may re-mail, or None for the default.

    Kept as a prefix lookup rather than a field on the finding so the finding
    stays a flat dict of strings -- the shape the e-mail templates and the alert
    state table both assume.
    """
    if key.startswith("setup_stalled:"):
        return ALERT_SETUP_REPEAT_SECONDS
    if key.startswith("backup_"):
        return ALERT_BACKUP_REPEAT_SECONDS
    if key.startswith("invite_failed:"):
        return ALERT_INVITE_REPEAT_SECONDS
    return None


def _should_send(previous: dict[str, Any] | None, finding: dict[str, str],
                 now: dt.datetime, repeat_seconds: int | None = None) -> bool:
    """Whether this finding is due for another e-mail right now."""
    if previous is None:
        return True
    if not previous.get("open"):
        # It was reported, then cleared. Seeing it again is news again.
        return True
    if str(previous.get("detail") or "") != finding["detail"]:
        return True
    last_sent = parse_utc(previous.get("last_sent_at"))
    if last_sent is None:
        return True
    return (now - last_sent).total_seconds() >= (ALERT_REPEAT_SECONDS if repeat_seconds is None
                                                 else repeat_seconds)


# ---------------------------------------------------------------------------
# rendering and delivery
# ---------------------------------------------------------------------------
def _render_text(due: list[dict[str, str]], recovered: list[dict[str, Any]],
                 analyses: list[dict[str, Any]] | None = None) -> str:
    lines: list[str] = []
    if due:
        lines.append("需要处理：")
        for item in due:
            lines.append(f"  - [{item['severity']}] {item['title']}")
            if item["detail"]:
                lines.append(f"      {item['detail']}")
        lines.append("")
    if recovered:
        lines.append("已恢复：")
        for item in recovered:
            lines.append(f"  - {item.get('title') or item['key']}")
        lines.append("")
    lines.append(agent.render_text_section(analyses or []))
    lines.append("这封邮件由 CityU Mail Pilot 的哨兵自动发出；登录管理后台可看到实时指标。")
    return "\n".join(lines)


def _render_html(due: list[dict[str, str]], recovered: list[dict[str, Any]],
                 analyses: list[dict[str, Any]] | None = None) -> str:
    """600 px single-column table, inline CSS, no script, no external images.

    The same constraints the report mails live under (see AGENTS.md invariant
    5); an operator e-mail is not an excuse to relax them.
    """
    rows: list[str] = []
    for item in due:
        colour = "#b42318" if item["severity"] == "critical" else "#b54708"
        rows.append(
            '<tr><td style="padding:12px 20px;border-bottom:1px solid #e4e7ec">'
            f'<div style="font-weight:bold;color:{colour};font-size:14px">{html.escape(item["title"])}</div>'
            f'<div style="color:#475467;font-size:13px;line-height:1.5;margin-top:4px">'
            f'{html.escape(item["detail"])}</div></td></tr>'
        )
    for item in recovered:
        rows.append(
            '<tr><td style="padding:12px 20px;border-bottom:1px solid #e4e7ec">'
            '<div style="font-weight:bold;color:#027a48;font-size:14px">已恢复：'
            f'{html.escape(str(item.get("title") or item["key"]))}</div></td></tr>'
        )
    heading = []
    if due:
        heading.append(f"{len(due)} 项异常")
    if recovered:
        heading.append(f"{len(recovered)} 项已恢复")
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#f4f7fb;padding:24px 12px"><tr><td align="center">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        'style="width:600px;background:#ffffff;border:1px solid #d9e2ec">'
        '<tr><td style="padding:18px 20px;background:#123b63;color:#ffffff">'
        '<div style="font-size:11px;letter-spacing:.08em">CITYU MAIL PILOT</div>'
        f'<div style="font-size:18px;margin-top:6px">{"，".join(heading)}</div></td></tr>'
        + "".join(rows) + agent.render_html_section(analyses or []) +
        '<tr><td style="padding:12px 20px;color:#64748b;font-size:11px">'
        '由巡检哨兵自动发出，请勿直接回复。</td></tr>'
        '</table></td></tr></table>'
    )


def _subject(due: list[dict[str, str]], recovered: list[dict[str, Any]]) -> str:
    parts = []
    if due:
        parts.append(f"{len(due)} 项异常")
    if recovered:
        parts.append(f"{len(recovered)} 项已恢复")
    return "[CityU Mail Pilot] " + "，".join(parts)


def send_admin_mail(db: Database, secrets: SecretBox, subject: str, text_body: str,
                    html_body: str | None = None) -> list[str]:
    """Send one operational e-mail from the admin's own mailbox to the admin.

    There is no system mailbox: every send in this project goes out through a
    user's own SMTP credentials, and the operator's account is no exception.
    Returns the addresses the message was sent to; raises if no admin has a
    usable mailbox, so the caller can report the failure instead of silently
    dropping the alert.
    """
    recipients = admin_emails()
    if not recipients:
        raise RuntimeError("INFE_PILOT_ADMIN_EMAILS 未配置，无法发送管理员告警。")
    delivered: list[str] = []
    failures: list[str] = []
    for address in sorted(recipients):
        user = db.find_user_for_login(address)
        if not user:
            failures.append(f"{address}（没有这个账号）")
            continue
        mailbox = db.get_mailbox(user["id"])
        if not mailbox or not mailbox.get("enabled"):
            failures.append(f"{address}（没有可用的转发邮箱，无法借它发信）")
            continue
        config = {
            "email": mailbox["email"],
            "report_to": address,
            "smtp_host": mailbox["smtp_host"],
            "smtp_port": mailbox["smtp_port"],
        }
        password = secrets.decrypt(mailbox["encrypted_password"], context=f"mailbox:{user['id']}")
        mailio.send_report(config, password, subject, text_body,
                           html_body=html_body, text_body=text_body)
        delivered.append(address)
    if not delivered:
        raise RuntimeError("没有任何管理员账号能发出告警邮件：" + "；".join(failures))
    return delivered


def _operator_sender(db: Database, secrets: SecretBox) -> tuple[dict[str, Any], str, str]:
    """Find an admin account that can actually send, and unlock it.

    Returns ``(smtp_config, password, address)``. There is no system mailbox in
    this project -- every outgoing message borrows a user's own SMTP
    credentials -- so anything that needs to mail an arbitrary person has to go
    through the operator's account.
    """
    for address in sorted(admin_emails()):
        user = db.find_user_for_login(address)
        if not user:
            continue
        mailbox = db.get_mailbox(user["id"])
        if not mailbox or not mailbox.get("enabled"):
            continue
        config = {
            "email": mailbox["email"],
            "report_to": address,
            "smtp_host": mailbox["smtp_host"],
            "smtp_port": mailbox["smtp_port"],
        }
        password = secrets.decrypt(mailbox["encrypted_password"], context=f"mailbox:{user['id']}")
        return config, password, address
    raise RuntimeError("没有可用作发件人的管理员邮箱：需要 INFE_PILOT_ADMIN_EMAILS 里至少有一个"
                       "已配置好转发邮箱的管理员账号。")


def send_as_operator(db: Database, secrets: SecretBox, to: str, subject: str, text_body: str,
                     html_body: str | None = None) -> dict[str, Any]:
    """Send one message from the operator's own mailbox to any address.

    Only the approval path uses this, and only an operator can reach it: the
    recipient is an address a stranger typed into the public form, so an
    automatic caller here would be an open relay.

    Raises on failure so the caller can tell the operator their click did not
    deliver -- the invite code is still shown either way, because losing the
    code would be worse than losing the e-mail.

    Returns a receipt (``from``, ``message_id``, ``refused``) rather than just
    the sender address. The caller stores it: whether an invite was actually
    handed to the mail server is the one part of "did they get it?" this side can
    answer, and answering it only in the moment meant it was unanswerable a day
    later.
    """
    config, password, sender = _operator_sender(db, secrets)
    config = {**config, "report_to": to}
    receipt = mailio.send_report(config, password, subject, text_body,
                                 html_body=html_body, text_body=text_body)
    return {"from": sender, **receipt}


# ---------------------------------------------------------------------------
# worker entry point
# ---------------------------------------------------------------------------
def run_checks(
    db: Database,
    secrets: SecretBox,
    *,
    now: dt.datetime | None = None,
    disk: float | None = None,
    certificate_days: float | None = None,
    sender: Callable[..., list[str]] = send_admin_mail,
) -> dict[str, Any]:
    """Evaluate, de-duplicate, and mail. Never raises.

    ``disk``/``certificate_days``/``sender`` are injectable so a test can drive
    every branch without a socket, a full disk or an SMTP server.
    """
    if not ALERTS_ENABLED:
        return {"enabled": False, "findings": 0, "sent": 0, "errors": []}

    now = now or dt.datetime.now(dt.timezone.utc)
    if disk is None:
        disk = disk_percent()
    if certificate_days is None:
        certificate_days = certificate_days_remaining()

    try:
        findings = evaluate(db, now=now, disk_percent=disk, certificate_days=certificate_days)
    except Exception as exc:
        logging.exception("alert evaluation failed")
        return {"enabled": True, "findings": 0, "sent": 0, "errors": [str(exc)]}

    active = {item["key"]: item for item in findings}
    known = {row["key"]: row for row in db.list_alert_states()}

    # Keys carried over from an earlier pass are cleared here too, so a finding
    # that stops being true (the account finished, or the operator paused it)
    # arrives as a recovery notice instead of silently disappearing.
    due = [item for item in findings
           if _should_send(known.get(item["key"]), item, now, _repeat_for(item["key"]))]
    recovered = [row for row in known.values() if row.get("open") and row["key"] not in active]

    if not due and not recovered:
        return {"enabled": True, "findings": len(active), "sent": 0, "errors": [], "analyses": 0}

    # Explain before sending, so the analysis rides in the same message as the
    # finding it is about -- two e-mails for one incident is exactly the noise the
    # de-duplication above exists to prevent. `analyse_many` cannot raise and has
    # its own budget gate; if the model is down or the ceiling was reached the
    # alert still goes out, because a model outage must never be able to silence
    # the sentinel.
    analyses: list[dict[str, Any]] = []
    if due and agent.enabled(db):
        # The switch is checked here as well as inside `analyse`, so a disabled
        # assistant leaves the alert byte-for-byte what it always was: no
        # "未分析：未开启" line in every mail, and no work at all.
        try:
            analyses = agent.analyse_many(db, due, secrets=secrets, now=now)
        except Exception:  # pragma: no cover - defensive; analyse_many swallows its own
            logging.exception("agent analysis failed")
            analyses = []

    errors: list[str] = []
    try:
        sender(db, secrets, _subject(due, recovered),
               _render_text(due, recovered, analyses),
               _render_html(due, recovered, analyses))
    except Exception as exc:
        # Leave the state untouched: the next pass tries again rather than
        # pretending the operator was told.
        logging.warning("admin alert could not be sent: %s", exc)
        errors.append(str(exc))
        return {"enabled": True, "findings": len(active), "sent": 0, "errors": errors,
                "analyses": 0}

    for item in due:
        db.record_alert(item["key"], item["severity"], item["detail"], item["title"], now)
    for row in recovered:
        db.clear_alert(row["key"], now)
    logging.info("admin alert sent: %s new, %s recovered", len(due), len(recovered))
    return {"enabled": True, "findings": len(active), "sent": len(due) + len(recovered),
            "analyses": len(analyses), "errors": errors}


# ---------------------------------------------------------------------------
# systemd OnFailure= handler support
# ---------------------------------------------------------------------------
def collect_secret_values(env_path: str = "/etc/cityu-mail-pilot/pilot.env") -> set[str]:
    """Credential values from the environment file, for output scrubbing.

    Only *values* are returned, never keys, and they are used solely to blank
    themselves out of text that is about to be e-mailed.
    """
    values: set[str] = set()
    try:
        with open(env_path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                value = value.strip().strip('"').strip("'")
                if value and _SECRET_KEY_PATTERN.search(name):
                    values.add(value)
    except OSError:
        pass
    return values


def redact(text: str, secrets_to_hide: set[str] | None = None) -> str:
    """Blank out known credentials and any ``NAME=value`` secret assignment.

    The handler copies ``systemctl status`` output into a message that leaves
    the machine, so this is the backstop for invariant 2. Called with no
    arguments it still catches inline ``KEY=...``/``PASSWORD=...`` text, which
    is how systemd prints an ``Environment=`` line.
    """
    cleaned = text
    for value in sorted(secrets_to_hide or set(), key=len, reverse=True):
        if len(value) >= 6:
            cleaned = cleaned.replace(value, "***")
    return re.sub(
        r"(?i)\b([A-Za-z0-9_]*(?:KEY|SECRET|PASSWORD|PASSWD|TOKEN|AUTH)[A-Za-z0-9_]*)\s*=\s*\S+",
        r"\1=***",
        cleaned,
    )
