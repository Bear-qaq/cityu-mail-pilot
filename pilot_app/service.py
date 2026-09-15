"""Orchestration for polling, personalised analysis, delivery, and daily reports."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import socket
import time
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import alerts, mailio, pricing, prompts, providers, reports, triage
from .database import Database
from .security import SecretBox

# Migration safety valve: after the legacy UID set is imported as placeholders,
# the first poll deliberately re-scans a bounded window so any hole the old
# worker left is picked up. A hole older than the window would be skipped
# forever, so this is configurable and reported by `migrate-legacy-imap-state`.
INITIAL_LOOKBACK_HOURS = max(1, int(os.environ.get("INFE_PILOT_INITIAL_LOOKBACK_HOURS", "48")))

# How many tokens the model may spend on one report. Measured reports land
# around 2.2k-2.8k characters, so the old hard-coded 4000 was mostly headroom;
# making it explicit and tunable is what lets the latency work bound the
# worst-case generation time. Raise it with INFE_PILOT_REPORT_MAX_TOKENS.
REPORT_MAX_TOKENS = max(600, int(os.environ.get("INFE_PILOT_REPORT_MAX_TOKENS", "4000")))
# The condensed first report is deliberately small; a short cap keeps a chatty
# model from turning "brief" into another long generation.
BRIEF_MAX_TOKENS = max(300, int(os.environ.get("INFE_PILOT_BRIEF_MAX_TOKENS", "1200")))

# Only mail from these sender domains becomes a report. The user's private
# mailbox also receives their personal mail (shopping, banks, newsletters);
# summarising those is unwanted noise. Empty value disables the filter and
# falls back to the old "process everything" behaviour.
# Two-stage instant delivery: send a condensed report first, then the full one.
# Purely a behaviour switch (no schema or data change), so rolling back is
# "set INFE_PILOT_BRIEF_FIRST=0 and restart" — nothing to migrate.
BRIEF_FIRST = os.environ.get("INFE_PILOT_BRIEF_FIRST", "0") == "1"
# With FULL_REPORT=0 the instant notification is *only* the condensed report.
# The full analysis still runs as a safety net when the brief fails, so a
# message can never end up with no report at all. The 22:00 digest is unaffected
# either way: it aggregates whatever reports exist for the day.
FULL_REPORT = os.environ.get("INFE_PILOT_FULL_REPORT", "1") != "0"

# Feed the deterministic triage verdict to the model as a hint. Rationale: the
# model is a heavy reasoner and re-deriving the category is part of that hidden
# cost. Only kept if a real A/B shows it does not hurt quality or latency.
INCLUDE_TRIAGE_HINT = os.environ.get("INFE_PILOT_TRIAGE_HINT", "1") != "0"

# Send an instant, model-free arrival alert so the user is not left waiting for
# a slow report. Turn off with INFE_PILOT_ALERT_ON_ARRIVAL=0; restrict it to
# deadline-ish mail with INFE_PILOT_ALERT_URGENT_ONLY=1.
ALERT_ON_ARRIVAL = os.environ.get("INFE_PILOT_ALERT_ON_ARRIVAL", "1") != "0"
ALERT_URGENT_ONLY = os.environ.get("INFE_PILOT_ALERT_URGENT_ONLY", "0") == "1"

ALLOWED_SENDER_DOMAINS = tuple(
    item.strip().lower().lstrip("@")
    for item in os.environ.get("INFE_PILOT_ALLOWED_SENDER_DOMAINS", "cityu.edu.hk").split(",")
    if item.strip()
)


def sender_domain(value: str) -> str:
    """The bare domain of a From/sender address, lower-cased."""
    address = str(value or "").strip().strip("<>").lower()
    return address.rsplit("@", 1)[-1] if "@" in address else ""


def is_allowed_sender(address: str) -> bool:
    """True when this sender may be analysed (no allow-list = allow all)."""
    if not ALLOWED_SENDER_DOMAINS:
        return True
    domain = sender_domain(address)
    if not domain:
        return False
    return any(domain == allowed or domain.endswith("." + allowed) for allowed in ALLOWED_SENDER_DOMAINS)


class PilotService:
    def __init__(self, database: Database, secrets: SecretBox):
        self.db = database
        self.secrets = secrets

    @staticmethod
    def _model_attempts() -> int:
        """How many times one generation may be attempted (default 2)."""
        try:
            return max(1, min(4, int(os.environ.get("INFE_PILOT_MODEL_ATTEMPTS", "2"))))
        except ValueError:
            return 2

    @staticmethod
    def _transient(exc: Exception) -> bool:
        if isinstance(exc, providers.TransientProviderError):
            return True
        # Stubs and third-party adapters may raise raw socket errors.
        return isinstance(exc, (TimeoutError, socket.timeout, OSError))

    def _generate_with_retry(self, user_id: str, **kwargs: Any) -> Any:
        """Run one generation, retrying only transient provider failures.

        A long report can be cut off mid-flight (a real 240s run ended with
        "Remote end closed connection without response"), and a single retry
        turns that from a lost email into a slightly slower one. Non-transient
        failures (bad key, bad model name) are raised immediately.
        """
        attempts = self._model_attempts()
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return providers.generate(**kwargs)
            except Exception as exc:
                last = exc
                if isinstance(exc, providers.ProviderTimeout):
                    # A timeout has already spent the whole budget (measured:
                    # ~234 s for a real report against a 300 s ceiling).
                    # Retrying it now would hold this generation slot for twice
                    # as long, for a provider that is evidently struggling. The
                    # message is marked failed and the queue retries it with
                    # exponential backoff instead.
                    logging.warning(
                        "model timed out for user %s; leaving it to the queue backoff", user_id,
                    )
                    raise
                if not self._transient(exc) or attempt == attempts:
                    raise
                delay = 5 * attempt
                logging.warning(
                    "model attempt %s/%s failed for user %s (%s); retrying in %ss",
                    attempt, attempts, user_id, exc, delay,
                )
                time.sleep(delay)
        raise last if last else providers.ProviderError("模型调用失败。")

    def mailbox_password(self, mailbox: dict) -> str:
        return self.secrets.decrypt(mailbox["encrypted_password"], context=f"mailbox:{mailbox['user_id']}")

    def connection_key(self, connection: dict) -> str:
        if connection.get("platform"):
            # The pilot's shared credential is read from the environment on
            # demand rather than decrypted, so it never has to exist as ciphertext
            # in a row that backups copy around. Model and search live in different
            # accounts at different vendors, so the kind decides which variable.
            if connection.get("kind") == "search":
                return providers.platform_search_key()
            return providers.platform_model_key()
        return self.secrets.decrypt(connection["encrypted_api_key"], context=f"connection:{connection['user_id']}:{connection['kind']}")

    def model_connection(self, user_id: str) -> Optional[dict]:
        """The model credential to use for this user.

        Their own connection wins whenever they have one; the instance-wide pilot
        key is only a fallback for accounts that never configured a model. That
        order is what the landing page, the privacy policy and the in-app copy
        all promise, so inverting it would make three documents untrue at once.
        """
        own = self.db.get_connection(user_id, "model")
        if own:
            return own
        return providers.platform_model_default()

    def search_connection(self, user_id: str) -> Optional[dict]:
        """The search credential to use for this user, on the same terms.

        Their own wins; the fallback only covers accounts that never configured
        one. Source-checking is optional, and the operator used to hand the key out
        by hand -- so a user who never got it silently lost citations without
        anything saying so.
        """
        own = self.db.get_connection(user_id, "search")
        if own:
            return own
        return providers.platform_search_default()

    def decrypt_report(self, value: str | bytes, user_id: str) -> str:
        # String support allows a controlled migration from early pilot data.
        return self.secrets.decrypt(value, context=f"report:{user_id}") if isinstance(value, bytes) else str(value)

    def encrypt_report(self, value: str, user_id: str) -> bytes:
        return self.secrets.encrypt(value, context=f"report:{user_id}")

    def decrypt_message(self, value: str | bytes, user_id: str) -> str:
        # A skipped mail stores an empty body on purpose; it is never queued, so
        # this is a safety net rather than a normal path.
        if not value:
            return ""
        return self.secrets.decrypt(value, context=f"message:{user_id}") if isinstance(value, bytes) else str(value)

    def poll_mailbox(self, mailbox: dict) -> int:
        password = self.mailbox_password(mailbox)
        uid_validity, messages, highest = mailio.fetch_new_messages(
            mailbox, password, initial_lookback_hours=INITIAL_LOOKBACK_HOURS
        )
        stored = 0
        skipped = 0
        for uid, message in messages:
            sender = message.get("sender_address", "")
            allowed = is_allowed_sender(sender)
            # Mail outside the allowed domains is recorded for audit but must
            # not keep its body: the privacy policy promises that a skipped row
            # stores metadata only, and an unread body sitting in the database
            # would make that promise false. The sender check therefore runs
            # *before* the body is encrypted, so a body we will never analyse
            # never reaches storage at all. The empty body is stored as-is
            # rather than encrypted, because encrypting nothing is meaningless
            # (and SecretBox refuses an empty plaintext); a skipped row is never
            # queued, so nothing ever tries to decrypt it.
            body = message["body"] if allowed else b""
            protected = {**message, "body": self.secrets.encrypt(body, context=f"message:{mailbox['user_id']}")
                         if allowed else b""}
            if self.db.insert_message(mailbox["user_id"], mailbox["id"], uid_validity, uid, protected) is None:
                # Same RFC 5322 Message-ID already stored: this is a second copy
                # of one mail (two forwarding rules), so it must not become a
                # second AI report.
                logging.info(
                    "skipping duplicate delivery of %s for mailbox %s (uid %s)",
                    message.get("message_key", "")[:80], mailbox["id"], uid,
                )
                continue
            if not allowed:
                # Stored and marked, never queued: the row is what lets the
                # digest say honestly "N messages were skipped, here is why".
                reason = (
                    f"发件人不在允许名单内（{sender or '未知发件人'}）；"
                    f"只处理：{', '.join(ALLOWED_SENDER_DOMAINS)}"
                )
                self.db.mark_message_skipped_by_uid(mailbox["id"], uid_validity, uid, reason)
                skipped += 1
                logging.info(
                    "skipped non-allowed sender %s (uid %s, mailbox %s, body discarded)",
                    sender, uid, mailbox["id"],
                )
                continue
            stored += 1
        self.db.update_mailbox_poll(mailbox["id"], last_uid=highest, uid_validity=uid_validity)
        if skipped:
            logging.info("mailbox %s: stored %s, skipped %s by sender filter", mailbox["id"], stored, skipped)
        return stored

    def poll_all(self) -> tuple[int, list[str]]:
        """Single-threaded reference path; ``worker.cycle`` is the production one.

        Kept as the simple fallback for diagnostics on a machine where the
        thread pool is not wanted.
        """
        total = 0
        errors: list[str] = []
        for mailbox in self.db.active_mailboxes():
            try:
                total += self.poll_mailbox(mailbox)
            except Exception as exc:
                logging.exception("mailbox poll failed for %s", mailbox["id"])
                errors.append(f"{mailbox['id']}: {exc}")
                self.db.update_mailbox_poll(
                    mailbox["id"], last_uid=int(mailbox.get("last_uid") or 0),
                    uid_validity=str(mailbox.get("uid_validity") or ""), error=str(exc),
                )
        return total, errors

    def _analyse(self, user_id: str, message: dict) -> str:
        profile = self.db.get_profile(user_id)
        model = self.model_connection(user_id)
        if not model or not model["enabled"]:
            raise providers.ProviderError("尚未配置可用的模型 API。")
        config = json.loads(model.get("config_json") or "{}")
        search_results: list[dict[str, str]] = []
        native = providers.supports_native_search(model["provider"])
        generated = ""
        prompt = ""
        search_status = ""
        usage: dict[str, int] = {}
        timings: dict[str, float] = {}
        try:
            hint = triage.prompt_hint(message)
        except Exception:
            hint = ""

        if native:
            # This provider searches the web on its own, so the user does not
            # need a second, separately billed search API. A search failure must
            # never abort the summary: we fall through to the external path.
            prompt = prompts.immediate_prompt(
                profile, message, [], "模型内置联网搜索已开启", native_search=True,
                triage_hint=hint if INCLUDE_TRIAGE_HINT else "",
            )
            started = time.monotonic()
            try:
                result = self._generate_with_retry(
                    user_id,
                    provider=model["provider"], model=model["model"], base_url=model["base_url"],
                    api_key=self.connection_key(model), prompt=prompt, config=config,
                    max_output_tokens=REPORT_MAX_TOKENS, native_search=True,
                )
                generated = result.text
                search_results = result.sources
                usage = result.usage
                search_status = "模型内置联网搜索已提供来源" if search_results else "模型内置联网搜索未返回可引用来源"
            except Exception as exc:
                logging.warning("native search failed for user %s: %s", user_id, exc)
                native = False  # degrade to the user's own search API, if any
            timings["generate"] = time.monotonic() - started

        if not native:
            search_status = "no search connection configured"
            search = self.search_connection(user_id)
            query = prompts.public_search_query(message)
            if search and search["enabled"] and query:
                started = time.monotonic()
                try:
                    search_results = providers.web_search(search["provider"], self.connection_key(search), query, count=5)
                    search_status = "live results supplied" if search_results else "provider returned no results"
                except Exception as exc:
                    # Search must never block the mail summary.
                    logging.warning("search failed for user %s: %s", user_id, exc)
                    search_status = "live search failed; no verification available"
                timings["search"] = time.monotonic() - started
            elif not query:
                search_status = "no privacy-safe public query could be derived"
            prompt = prompts.immediate_prompt(
                profile, message, search_results, search_status,
                triage_hint=hint if INCLUDE_TRIAGE_HINT else "",
            )
            started = time.monotonic()
            result = self._generate_with_retry(
                user_id,
                provider=model["provider"], model=model["model"], base_url=model["base_url"],
                api_key=self.connection_key(model), prompt=prompt, config=config,
                max_output_tokens=REPORT_MAX_TOKENS, native_search=False,
            )
            generated = result.text
            usage = result.usage
            timings["generate"] = time.monotonic() - started

        # One line that makes the 5-minute question answerable from journalctl:
        # how long search took, how long generation took, and how many tokens.
        logging.info(
            "analysis for user %s used %s search with %d source(s); search=%.1fs generate=%.1fs "
            "prompt=%d chars answer=%d chars tokens=%s",
            user_id, "native" if native else "external", len(search_results),
            timings.get("search", 0.0), timings.get("generate", 0.0),
            len(prompt), len(generated), usage or "n/a",
        )
        self._record_usage(user_id, "immediate", model, usage, message_id=str(message.get("id") or ""))
        generated = prompts.sanitize_calendar_dates(generated, prompt)
        return prompts.normalize_report(generated, allowed_source_urls={item["url"] for item in search_results})

    def send_announcement_emails(self, limit: int = 20) -> dict[str, Any]:
        """Deliver queued broadcast emails, one per user, through their own mailbox.

        Each user's mailbox credentials are the only SMTP the system has, so a
        broadcast goes out the same way reports do — from the user's own mailbox
        to the address they already read. Failures are recorded per user and
        never stop the rest: one broken mailbox must not silence the broadcast
        for everybody else.
        """
        sent = failed = 0
        rows = self.db.pending_announcement_deliveries(limit)
        for row in rows:
            try:
                password = self.secrets.decrypt(row["encrypted_password"],
                                                context=f"mailbox:{row['user_id']}")
                mailio.send_report(
                    row, password,
                    reports.announcement_subject(row["title"]),
                    "",
                    html_body=reports.render_announcement_html(row["title"], row["body"], row["tone"]),
                    text_body=reports.render_announcement_text(row["title"], row["body"]),
                )
            except Exception as exc:
                failed += 1
                logging.warning("announcement %s could not be mailed to %s: %s",
                                row["announcement_id"], row["user_id"], exc)
                self.db.finish_announcement_delivery(row["announcement_id"], row["user_id"], error=str(exc))
            else:
                sent += 1
                self.db.finish_announcement_delivery(row["announcement_id"], row["user_id"])
        return {"sent": sent, "failed": failed, "queued": len(rows)}

    def _record_usage(self, user_id: str, kind: str, connection: dict[str, Any] | None,
                      usage: dict[str, Any] | None, *, message_id: str = "") -> None:
        """Log one model call's token use and what it cost.

        Never raises: token accounting is bookkeeping, and a bookkeeping failure
        must not lose a report the user is waiting for.
        """
        try:
            if not usage:
                return
            provider = str((connection or {}).get("provider") or "")
            model = str((connection or {}).get("model") or "")
            overrides = {(row["provider"].lower(), row["model"]): row
                         for row in self.db.list_model_prices()}
            price = pricing.lookup(provider, model, overrides)
            cost = pricing.estimate(usage, price)
            self.db.record_usage(user_id=user_id, kind=kind, provider=provider, model=model,
                                 usage=usage, cost=cost, price=price, message_id=message_id)
        except Exception:
            logging.exception("could not record token usage for user %s", user_id)

    def _send_arrival_alert(self, mailbox: dict, password: str, message: dict) -> bool:
        """Send the instant heads-up, before the slow report is generated.

        Never raises: an alert that fails must not stop the report, and a sender
        outside the allow-list must not produce an alert either.
        """
        if not ALERT_ON_ARRIVAL:
            return False
        if not is_allowed_sender(message.get("sender_address", "")):
            return False
        try:
            body = self.decrypt_message(message.get("body", ""), mailbox["user_id"])
            alert = alerts.build_alert({**message, "body": body}, mailbox_email=mailbox.get("email", ""))
            from . import triage as _triage
            if not alerts.should_alert(_triage.triage({**message, "body": body}), urgent_only=ALERT_URGENT_ONLY):
                return False
            mailio.send_report(mailbox, password, alert["subject"], alert["text"],
                               html_body=alert["html"], text_body=alert["text"])
            logging.info(
                "arrival alert sent for user %s (category=%s urgent=%s)",
                mailbox["user_id"], alert["category"], alert["urgent"],
            )
            return True
        except Exception as exc:
            logging.warning("arrival alert failed for message %s: %s", message.get("id"), exc)
            return False

    def _analyse_brief(self, user_id: str, message: dict) -> str:
        """Condensed three-section report: importance, actions, key points.

        Deliberately reuses the same provider plumbing and search fallback as
        the full analysis, so switching modes cannot change *which* provider or
        search is used — only how much the model is asked to write.
        """
        profile = self.db.get_profile(user_id)
        model = self.model_connection(user_id)
        if not model or not model["enabled"]:
            raise providers.ProviderError("尚未配置可用的模型 API。")
        config = json.loads(model.get("config_json") or "{}")
        try:
            hint = triage.prompt_hint(message) if INCLUDE_TRIAGE_HINT else ""
        except Exception:
            hint = ""
        search_results: list[dict[str, str]] = []
        usage: dict[str, Any] = {}
        if providers.supports_native_search(model["provider"]):
            prompt = prompts.brief_prompt(profile, message, [], "模型内置联网搜索已开启",
                                          native_search=True, triage_hint=hint)
            result = self._generate_with_retry(
                user_id, provider=model["provider"], model=model["model"], base_url=model["base_url"],
                api_key=self.connection_key(model), prompt=prompt, config=config,
                max_output_tokens=BRIEF_MAX_TOKENS, native_search=True,
            )
            generated, search_results = result.text, result.sources
            usage = result.usage or {}
        else:
            search = self.search_connection(user_id)
            query = prompts.public_search_query(message)
            search_status = "no search connection configured"
            if search and search["enabled"] and query:
                try:
                    search_results = providers.web_search(
                        search["provider"], self.connection_key(search), query, count=3)
                    search_status = "live results supplied" if search_results else "provider returned no results"
                except Exception as exc:
                    logging.warning("search failed for user %s: %s", user_id, exc)
                    search_status = "live search failed; no verification available"
            elif not query:
                search_status = "no privacy-safe public query could be derived"
            prompt = prompts.brief_prompt(profile, message, search_results, search_status, triage_hint=hint)
            brief = self._generate_with_retry(
                user_id, provider=model["provider"], model=model["model"], base_url=model["base_url"],
                api_key=self.connection_key(model), prompt=prompt, config=config,
                max_output_tokens=BRIEF_MAX_TOKENS, native_search=False,
            )
            generated = brief.text
            usage = brief.usage or {}
        self._record_usage(user_id, "brief", model, usage, message_id=str(message.get("id") or ""))
        logging.info("brief analysis for user %s produced %d chars", user_id, len(generated or ""))
        return prompts.normalize_brief_report(prompts.sanitize_calendar_dates(generated, prompt))

    def process_message(self, message: dict) -> bool:
        if not self.db.mark_message_processing(message["id"]):
            return False
        try:
            mailbox = self.db.get_mailbox(message["user_id"])
            if not mailbox:
                raise mailio.MailError("邮箱配置已不存在。")
            profile = self.db.get_profile(message["user_id"])
            existing = self.db.report_for_message(message["id"])
            if existing and existing["status"] == "sent":
                self.db.finish_message(message["id"])
                return True
            brief_mode = False
            if existing:
                report = self.decrypt_report(existing["body_markdown"], message["user_id"])
                subject, report_id = existing["subject"], existing["id"]
                brief_mode = reports.is_brief(report)
            else:
                payload = {
                    "subject": message["subject"], "sender_name": message["sender_name"],
                    "sender_address": message["sender_address"], "received": message["received_at"],
                    "importance": message["importance"], "body": self.decrypt_message(message["body"], message["user_id"]),
                }
                if BRIEF_FIRST:
                    # Stage 1 exists so the FIRST message the user sees already
                    # carries the essentials. In two-stage mode it is sent here
                    # and then replaced by the full report; in brief-only mode it
                    # becomes the report itself, so this path must not also fall
                    # through to the common send below (that sent it twice).
                    brief = self._analyse_brief(message["user_id"], payload)
                    report = brief
                    brief_mode = True
                    if FULL_REPORT:
                        brief_subject = f"【AI邮件摘要·精简】{message['subject'][:110]}"
                        brief_rendered = reports.render_brief(
                            brief, message, subject=brief_subject,
                            timezone=(profile or {}).get("timezone"),
                        )
                        try:
                            mailio.send_report(mailbox, self.mailbox_password(mailbox), brief_subject, brief,
                                               html_body=brief_rendered["html"], text_body=brief_rendered["text"])
                            logging.info("brief report sent for message %s", message["id"])
                        except Exception as exc:
                            # A failed brief send must not stop the full report.
                            logging.warning("brief report failed for message %s: %s", message["id"], exc)
                        # Stage 2: the full seven-section analysis, sent below.
                        report = self._analyse(message["user_id"], payload)
                        brief_mode = False
                else:
                    # Single-stage: instant rule alert, then the full report.
                    self._send_arrival_alert(mailbox, self.mailbox_password(mailbox), message)
                    report = self._analyse(message["user_id"], payload)
                subject = f"【AI邮件摘要】{message['subject'][:120]}"
                report_id = self.db.create_report(
                    user_id=message["user_id"], message_id=message["id"], kind="immediate",
                    subject=subject, body=self.encrypt_report(report, message["user_id"]), sent_to=mailbox["report_to"],
                )
            if brief_mode:
                rendered = reports.render_brief(report, message, subject=subject,
                                                timezone=(profile or {}).get("timezone"))
            else:
                rendered = reports.render_immediate(report, message, subject=subject,
                                                    timezone=(profile or {}).get("timezone"))
            password = self.mailbox_password(mailbox)
            mailio.send_report(mailbox, password, subject, report,
                               html_body=rendered["html"], text_body=rendered["text"])
            self.db.mark_report_sent(report_id)
            self.db.finish_message(message["id"])
            return True
        except Exception as exc:
            logging.exception("message processing failed for %s", message["id"])
            attempts = int(message.get("attempts") or 0) + 1
            retry_seconds = min(3600, 60 * (2 ** min(attempts, 6)))
            retry_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=retry_seconds)).isoformat(timespec="seconds")
            self.db.fail_message(message["id"], str(exc), retry_at)
            existing = self.db.report_for_message(message["id"])
            if existing:
                self.db.fail_report(existing["id"], str(exc))
            return False

    def process_due(self, limit: int = 20) -> tuple[int, int]:
        """Single-threaded reference path.

        The worker groups the queue by user and runs those groups in parallel
        (see ``pilot_app.worker.process_due``); this stays as the simple,
        sequential version used by manual tooling and as a readable statement of
        what the parallel version is supposed to do.
        """
        succeeded = failed = 0
        for message in self.db.due_messages(limit):
            if self.process_message(message): succeeded += 1
            else: failed += 1
        return succeeded, failed

    def daily_due(self, user: dict, now_utc: dt.datetime | None = None) -> tuple[bool, str]:
        now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
        try:
            local = now_utc.astimezone(ZoneInfo(user["timezone"]))
        except ZoneInfoNotFoundError:
            local = now_utc.astimezone(ZoneInfo("Asia/Hong_Kong"))
        try:
            hour, minute = [int(item) for item in user["daily_time"].split(":", 1)]
        except (ValueError, AttributeError):
            hour, minute = 22, 0
        report_date = local.date().isoformat()
        return (local.hour, local.minute) >= (hour, minute) and not self.db.daily_report_exists(user["id"], report_date), report_date

    def send_daily(self, user: dict, report_date: str) -> bool:
        """Build and send the student-brief digest for one local day.

        The digest is composed locally from the stored immediate reports instead
        of asking a model to re-summarise them. That guarantees two product
        promises: no email can be silently dropped by a model, and every row
        keeps a traceable sender, subject, received time and source URLs.
        """
        profile = self.db.get_profile(user["id"]) or {}
        try:
            zone = ZoneInfo(user["timezone"])
        except ZoneInfoNotFoundError:
            zone = ZoneInfo("Asia/Hong_Kong")
        local_start = dt.datetime.fromisoformat(report_date).replace(tzinfo=zone)
        start_utc = local_start.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
        end_utc = (local_start + dt.timedelta(days=1)).astimezone(dt.timezone.utc).isoformat(timespec="seconds")

        rows = self.db.messages_between(user["id"], start_utc, end_utc)
        reports_by_id = {}
        messages = []
        for row in rows:
            message_id = row.get("message_id") or row.get("id")
            if not message_id:
                continue
            messages.append(row)
            if row.get("body_markdown") is not None:
                reports_by_id[message_id] = self.decrypt_report(row["body_markdown"], user["id"])
        digest = reports.build_digest(messages, reports_by_id, timezone=user["timezone"])
        reports.with_digest_header(
            digest, report_date, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        )

        existing = self.db.daily_report_for_date(user["id"], report_date)
        if existing:
            subject, report_id = existing["subject"], existing["id"]
        else:
            subject = reports.digest_subject(digest)
            report_id = self.db.create_report(
                user_id=user["id"], message_id=None, kind="daily", subject=subject,
                body=self.encrypt_report(reports.digest_markdown(digest), user["id"]),
                sent_to=user["report_to"], report_date=report_date,
            )
        mailbox = self.db.get_mailbox(user["id"])
        if not mailbox:
            raise mailio.MailError("邮箱配置不存在。")
        rendered = reports.render_digest(digest, subject=subject)
        try:
            mailio.send_report(mailbox, self.mailbox_password(mailbox), subject,
                               reports.digest_markdown(digest),
                               html_body=rendered["html"], text_body=rendered["text"])
            self.db.mark_report_sent(report_id)
            return True
        except Exception as exc:
            self.db.fail_report(report_id, str(exc))
            raise

    def run_daily_due(self) -> tuple[int, list[str]]:
        sent = 0
        errors: list[str] = []
        for user in self.db.daily_users():
            due, report_date = self.daily_due(user)
            if not due:
                continue
            try:
                sent += int(self.send_daily(user, report_date))
            except Exception as exc:
                logging.exception("daily report failed for %s", user["id"])
                errors.append(f"{user['id']}: {exc}")
        return sent, errors

    def test_model(self, user_id: str) -> str:
        connection = self.model_connection(user_id)
        if not connection:
            raise providers.ProviderError("尚未配置模型 API。")
        text = providers.generate_text(
            provider=connection["provider"], model=connection["model"], base_url=connection["base_url"],
            api_key=self.connection_key(connection), prompt="只回复：连接成功 / Connection successful",
            config=json.loads(connection.get("config_json") or "{}"), max_output_tokens=200,
        )
        # A provider that answers every real prompt with an empty string used to
        # pass this test, which is how a reasoning model that consumed its whole
        # budget on hidden thinking went unnoticed until a real report came out
        # empty. An empty answer is not a working connection.
        if not str(text or "").strip():
            raise providers.ProviderError("模型连接成功，但没有返回任何文本；请换一个模型名再试。")
        return text

    def test_search(self, user_id: str) -> list[dict[str, str]]:
        connection = self.search_connection(user_id)
        if not connection:
            raise providers.ProviderError("尚未配置搜索 API。")
        return providers.web_search(connection["provider"], self.connection_key(connection), "City University of Hong Kong", count=3)

    def test_mailbox(self, user_id: str) -> dict[str, str]:
        mailbox = self.db.get_mailbox(user_id)
        if not mailbox:
            raise mailio.MailError("尚未配置邮箱。")
        # Read-only search tests IMAP. SMTP is tested with an explicit report in
        # the UI, avoiding an unexpected outbound email from a connection test.
        validity, _, _ = mailio.fetch_new_messages({**mailbox, "last_uid": 2**31 - 1}, self.mailbox_password(mailbox))
        return {"imap": "ok", "uid_validity": validity}
