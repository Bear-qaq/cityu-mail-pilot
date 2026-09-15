"""Small scheduler suitable for a 2 GB pilot server.

Polling and report generation run as **two independent loops**:

* a poller thread wakes every ``INFE_PILOT_POLL_SECONDS`` and checks every
  mailbox, with ``INFE_PILOT_POLL_WORKERS`` of them in flight at once;
* the main thread drains the report queue continuously, with
  ``INFE_PILOT_REPORT_WORKERS`` users being analysed in parallel.

They used to be two phases of one loop, which looked harmless with one user and
is not: generation measures ~234 s per report, so while a batch of reports was
being written the worker did not poll at all. Production logs of a 16-message
catch-up show three cycle lines in twenty-two minutes instead of the expected
forty-four — new mail was simply not being noticed during that window.

Generation is parallelised **per user, not per message** (``INFE_PILOT_USER_BATCH``
messages per user per pass). Two reasons: a user's messages are analysed with
that user's own API key, so one call at a time per user respects whatever rate
limit their key has and keeps reports in arrival order; and it makes the pool
fair, because one user's burst cannot occupy every slot while another waits.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from . import agent as agent_mod
from . import alerting, backup as backup_mod, idle, mailio
from .database import Database, utc_now
from .security import SecretBox
from .service import PilotService, log_job_failure


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.environ.get(name, str(default)))))
    except (TypeError, ValueError):
        return default


# 60 s, not 10 s: every poll is an IMAP login from a single server IP, and
# missing a mail for one minute costs far less than a provider throttling that
# IP. It also stops polling from competing with report generation for slots.
POLL_SECONDS = _int_env("INFE_PILOT_POLL_SECONDS", 60, 15, 3600)

# Gmail's floor is declared in mailio, next to the provider facts, because the
# alert sentinel needs the same number to judge what "no poll for a while"
# means for a mailbox that is only polled every 15 minutes.
GMAIL_POLL_SECONDS = mailio.GMAIL_MIN_POLL_SECONDS

# How often the scheduler wakes to see which mailboxes are due. Only mailboxes
# whose own interval has elapsed are actually polled, so this is a scheduling
# granularity, not a request rate.
POLL_TICK_SECONDS = _int_env("INFE_PILOT_POLL_TICK_SECONDS", 15, 1, 300)

# A mailbox that keeps failing is polled less and less often, up to this cap.
# Without it, one broken credential means a failing IMAP login every minute
# forever, which is exactly the connection volume the providers throttle.
MAX_POLL_BACKOFF_SECONDS = _int_env("INFE_PILOT_POLL_MAX_BACKOFF_SECONDS", 3600, 60, 86400)

QUEUE_SECONDS = _int_env("INFE_PILOT_QUEUE_SECONDS", 5, 1, 300)
POLL_WORKERS = _int_env("INFE_PILOT_POLL_WORKERS", 4, 1, 16)
REPORT_WORKERS = _int_env("INFE_PILOT_REPORT_WORKERS", 6, 1, 32)
USER_BATCH = _int_env("INFE_PILOT_USER_BATCH", 3, 1, 50)
DUE_LIMIT = _int_env("INFE_PILOT_DUE_LIMIT", 200, 1, 2000)


def build_service() -> PilotService:
    db = Database(os.environ.get("INFE_PILOT_DB", "/var/lib/cityu-mail-pilot/pilot.sqlite3"))
    db.initialize()
    return PilotService(db, SecretBox.from_environment())


def poll_interval_for(mailbox: dict[str, Any]) -> int:
    """Seconds to leave between polls of this mailbox.

    The base interval applies unless the provider publishes a slower floor —
    today only Gmail does. A host we fail to recognise simply gets the base
    interval, which is the safe direction.
    """
    return max(POLL_SECONDS, mailio.minimum_poll_seconds(mailbox))


def next_poll_delay(mailbox: dict[str, Any], consecutive_failures: int) -> int:
    """Seconds until this mailbox should be polled again.

    A mailbox that keeps failing doubles its interval, up to the cap. Without
    that, a revoked app password means a failing IMAP login every minute for as
    long as nobody notices — which is exactly the runaway connection volume the
    providers throttle, aimed at an account that is already in trouble.
    """
    base = poll_interval_for(mailbox)
    if consecutive_failures <= 0:
        return base
    return min(base * (2 ** min(consecutive_failures, 16)), MAX_POLL_BACKOFF_SECONDS)


def poll_all(service: PilotService,
             mailboxes: list[dict[str, Any]] | None = None,
             on_progress: "Callable[[], None] | None" = None) -> dict[str, Any]:
    """One pass over the given (or every active) mailbox. Never raises.

    ``on_progress`` fires once per mailbox that finishes, which is what the
    watchdog reads: "no mailbox has completed" is the property worth restarting
    over, and unlike "a pass has not finished" it does not get harder to satisfy
    as the user count grows.
    """
    if mailboxes is None:
        mailboxes = service.db.active_mailboxes()
    ingested = 0
    errors: list[str] = []
    failed: list[str] = []
    if not mailboxes:
        return {"mailboxes": 0, "ingested": 0, "errors": errors, "failed": failed}
    with ThreadPoolExecutor(max_workers=min(POLL_WORKERS, len(mailboxes))) as pool:
        jobs = {pool.submit(service.poll_mailbox, mailbox): mailbox for mailbox in mailboxes}
        for job in as_completed(jobs):
            mailbox = jobs[job]
            if on_progress is not None:
                on_progress()
            try:
                ingested += job.result()
            except Exception as exc:
                log_job_failure("mailbox poll", mailbox["id"], exc)
                errors.append(f"{mailbox['id']}: {exc}")
                # Reported separately so the scheduler can back this mailbox
                # off without parsing the human-readable message above.
                failed.append(str(mailbox["id"]))
                service.db.update_mailbox_poll(
                    mailbox["id"], last_uid=int(mailbox.get("last_uid") or 0),
                    uid_validity=str(mailbox.get("uid_validity") or ""), error=str(exc),
                )
    return {"mailboxes": len(mailboxes), "ingested": ingested, "errors": errors,
            "failed": failed}


def _user_batches(messages: list[dict[str, Any]], limit: int) -> dict[str, list[dict[str, Any]]]:
    """Group the queue by user, keeping arrival order and capping each user."""
    batches: dict[str, list[dict[str, Any]]] = {}
    for message in messages:
        bucket = batches.setdefault(message["user_id"], [])
        if len(bucket) < limit:
            bucket.append(message)
    return batches


def process_due(service: PilotService) -> dict[str, Any]:
    """Analyse the due queue, one user at a time per slot. Never raises."""
    due = service.db.due_messages(DUE_LIMIT)
    if not due:
        return {"queued": 0, "users": 0, "sent": 0, "failed": 0}
    batches = _user_batches(due, USER_BATCH)
    sent = failed = 0

    def run_batch(messages: list[dict[str, Any]]) -> tuple[int, int]:
        batch_sent = batch_failed = 0
        for message in messages:
            try:
                if service.process_message(message):
                    batch_sent += 1
                else:
                    batch_failed += 1
            except Exception as exc:
                log_job_failure("message processing", message.get("id"), exc)
                batch_failed += 1
        return batch_sent, batch_failed

    with ThreadPoolExecutor(max_workers=min(REPORT_WORKERS, len(batches))) as pool:
        jobs = [pool.submit(run_batch, messages) for messages in batches.values()]
        for job in as_completed(jobs):
            batch_sent, batch_failed = job.result()
            sent += batch_sent
            failed += batch_failed
    return {"queued": len(due), "users": len(batches), "sent": sent, "failed": failed}


def deliver_announcements(service: PilotService) -> dict[str, Any]:
    """Push any queued broadcast emails. Never raises."""
    try:
        return service.send_announcement_emails(20)
    except Exception as exc:
        logging.exception("announcement delivery pass failed")
        return {"sent": 0, "failed": 0, "errors": [str(exc)]}


def run_daily(service: PilotService) -> dict[str, Any]:
    try:
        sent, errors = service.run_daily_due()
    except Exception as exc:  # a broken digest must not stop the queue loop
        logging.exception("daily digest pass failed")
        return {"sent": 0, "errors": [str(exc)]}
    return {"sent": sent, "errors": errors}


def cycle(service: PilotService) -> dict[str, Any]:
    """One poll + one queue pass + the daily check, in order.

    Kept for ``--once`` and for tests, which want a single deterministic pass;
    the running service executes the two halves as independent loops.
    """
    poll = poll_all(service)
    queue = process_due(service)
    daily = run_daily(service)
    deliver_announcements(service)
    return {
        "ingested": poll["ingested"],
        "sent": queue["sent"],
        "failed": queue["failed"],
        "daily": daily["sent"],
        "errors": poll["errors"] + daily["errors"],
    }


# How long the poller thread may go without making progress before the process
# gives up on itself. A healthy loop wakes every POLL_TICK_SECONDS (15), and one
# pass over N mailboxes is bounded by ceil(N / POLL_WORKERS) x the 30-second IMAP
# timeout -- which is why the wing is stamped per *mailbox finished* and not per
# pass. Stamping per pass would have put a scaling cliff in the middle of a
# provider outage: at 40 mailboxes all timing out, one pass legitimately takes
# 300 s, and the watchdog would have killed a perfectly healthy worker at the
# exact moment it was doing the most work.
POLLER_STALL_SECONDS = _int_env("INFE_PILOT_POLLER_STALL_SECONDS", 300, 60, 86_400)


class PollerWatchdog:
    """Notices a poller thread that stopped while the process stayed alive.

    ``Restart=always`` covers a worker that *dies*. It cannot cover one that is
    alive but stuck, and that is the case this deployment actually hits: the
    sentinel keeps sending, the queue keeps draining, every liveness check
    passes, and no mail has been fetched for an hour. systemd is watching for
    exit, not for silence.

    The poller stamps this every loop; when the stamps stop, the main loop exits
    and systemd restarts the process five seconds later. That is safe rather
    than merely convenient -- ``recover_inflight()`` runs at startup and
    requeues anything that was mid-report, so the restart costs a retry and
    nothing else.

    Deliberately a plain rule over two numbers, with no model anywhere near it:
    the assistant may *suggest* that the poller looks stuck, but what actually
    restarts a service must be something that cannot be talked into it.
    """

    def __init__(self, limit_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit_seconds
        self._clock = clock
        self._beat = clock()

    def beat(self) -> None:
        self._beat = self._clock()

    def wedged(self) -> bool:
        return (self._clock() - self._beat) > self._limit


def _action_restart_worker(service: PilotService) -> tuple[str, bool]:
    """Ask for a restart by exiting. systemd's `Restart=always` does the rest.

    A restart rather than a repair, because a wedged thread cannot be killed in
    Python and starting a second poller would double-poll every mailbox. The
    restart is lossless: `recover_inflight()` runs at startup and requeues
    anything that was mid-report.
    """
    return "已请求重启：worker 即将退出，systemd 会在 5 秒内拉起", True


def _action_run_backup(service: PilotService) -> tuple[str, bool]:
    """Ask for a backup now instead of waiting for 03:20.

    **Not** ``backup_mod.main([])`` in this process. It was, and it failed on the
    real machine with ``unable to open database file``: the worker runs with
    ``ProtectSystem=strict`` and ``ReadWritePaths=/var/lib/cityu-mail-pilot``, so
    SQLite cannot create a file under ``/var/backups/cityu-mail-pilot``. Widening
    the sandbox is the wrong repair -- the worker holds the live database, and the
    backups are what survives the worker being wrong.

    So this drops a marker inside the directory the worker may already write, and
    ``cityu-mail-pilot-backup-request.path`` starts the ordinary backup unit. The
    cost is that the outcome is no longer known here: this reports the *request*,
    and a failed backup is reported by that unit's ``OnFailure=`` and by the
    sentinel's freshness checks -- which is where the operator looks anyway.
    """
    backup_mod.request_backup()
    return "已请求立刻备份：systemd 会在几秒内跑一次，结果看备份告警或 journalctl", False


# Key -> implementation. Every entry in `agent.ACTIONS` must appear here, and a
# test enforces that both ways: an action the console offers with no handler
# would be a button that silently does nothing, and a handler with no catalogue
# entry could never be confirmed.
AGENT_ACTION_HANDLERS: dict[str, Callable[[PilotService], tuple[str, bool]]] = {
    "restart_worker": _action_restart_worker,
    "run_backup": _action_run_backup,
}


def run_agent_actions(service: PilotService) -> dict[str, Any]:
    """Carry out the suggestions an operator has confirmed.

    The assistant only ever *names* an action from `agent.ACTIONS`. This is the
    only place in the program where one actually happens, and it happens because
    a human pressed a button in the console. That split is the whole safety
    argument: the module that reads untrusted text cannot act, and the module
    that can act never decides.
    """
    pending = service.db.pending_agent_actions()
    done = failed = 0
    restart = False
    for row in pending:
        key = str(row.get("action") or "")
        handler = AGENT_ACTION_HANDLERS.get(key)
        if handler is None:
            service.db.finish_agent_action(row["id"], ok=False,
                                           result="这条建议对应的动作已经不存在了", now=utc_now())
            failed += 1
            continue
        try:
            message, wants_restart = handler(service)
        except Exception as exc:
            logging.exception("confirmed action %s failed", key)
            service.db.finish_agent_action(row["id"], ok=False, result=str(exc), now=utc_now())
            failed += 1
            continue
        service.db.finish_agent_action(row["id"], ok=True, result=message, now=utc_now())
        logging.info("confirmed action %s executed (asked by %s)", key, row.get("requested_by") or "?")
        done += 1
        restart = restart or wants_restart
    return {"done": done, "failed": failed, "restart": restart}


def _start_poller(service: PilotService, stop: threading.Event,
                  watchdog: "PollerWatchdog | None" = None) -> threading.Thread:
    """Poll each mailbox on its own schedule.

    One shared interval used to mean the least tolerant provider set the rate
    for everybody: Gmail's documented limit is one request per 15 minutes, while
    QQ publishes no figure at all. Each mailbox now carries its own next-due
    time, and a mailbox that keeps failing backs off instead of retrying at full
    speed forever.
    """
    def loop() -> None:
        next_due: dict[str, float] = {}
        backoff: dict[str, int] = {}
        quiet = 0
        while not stop.is_set():
            # Stamped even when the pass below fails: the watchdog is asking
            # "is this thread still running?", not "did the poll succeed?".
            # A provider outage must not look like a wedged thread.
            if watchdog is not None:
                watchdog.beat()
            try:
                mailboxes = service.db.active_mailboxes()
            except Exception:
                logging.exception("poll pass could not list mailboxes")
                stop.wait(POLL_TICK_SECONDS)
                continue

            now = time.monotonic()
            live = {str(row["id"]) for row in mailboxes}
            for gone in [key for key in next_due if key not in live]:
                next_due.pop(gone, None)
                backoff.pop(gone, None)

            due = [row for row in mailboxes if next_due.get(str(row["id"]), 0.0) <= now]
            if due:
                result = poll_all(service, due, on_progress=watchdog.beat if watchdog else None)
                failed = set(result["failed"])
                for row in due:
                    key = str(row["id"])
                    if key in failed:
                        backoff[key] = backoff.get(key, 0) + 1
                    else:
                        backoff.pop(key, None)
                    next_due[key] = time.monotonic() + next_poll_delay(row, backoff.get(key, 0))

                if result["ingested"] or result["errors"]:
                    logging.info("poll %s", result)
                    quiet = 0
                else:
                    # A heartbeat every ten quiet passes proves the poller is
                    # alive without writing a line every single time.
                    quiet += 1
                    if quiet % 10 == 1:
                        logging.info("poll heartbeat %s", result)

            # Sleep only until the next mailbox is due, so a slow provider
            # cannot hold up a fast one; the tick is the floor, which also stops
            # an empty or fully backed-off fleet from spinning.
            upcoming = [stamp for stamp in next_due.values() if stamp > now]
            wait = POLL_TICK_SECONDS if not upcoming else max(1.0, min(upcoming) - now)
            stop.wait(min(wait, POLL_TICK_SECONDS))

    thread = threading.Thread(target=loop, name="poller", daemon=True)
    thread.start()
    return thread


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    service = build_service()
    service.db.recover_inflight()

    if args.once:
        result = cycle(service)
        logging.info("worker cycle %s", result)
        return 1 if result["errors"] or result["failed"] else 0

    logging.info(
        "worker starting: poll every %ss, Gmail every %ss (%s at a time), "
        "up to %s users generating at once, %s message(s) per user per pass",
        POLL_SECONDS, GMAIL_POLL_SECONDS, POLL_WORKERS, REPORT_WORKERS, USER_BATCH,
    )
    # Say out loud that the sentinel is armed, and against what. Without this
    # line a silent sentinel and a dead sentinel look identical in the journal,
    # which is the one thing an alerting system must never be.
    logging.info(
        "alert sentinel: %s, checks every %ss, repeats every %ss, tls host %s",
        "enabled" if alerting.ALERTS_ENABLED else "DISABLED (INFE_PILOT_ALERTS=0)",
        alerting.ALERT_CHECK_SECONDS, alerting.ALERT_REPEAT_SECONDS,
        alerting.ALERT_TLS_HOST or "(none, certificate check skipped)",
    )
    stop = threading.Event()

    def shutdown(_signum: int, _frame: Any) -> None:
        logging.info("worker stopping")
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # One watchdog for the poller thread. The sentinel already *reports* a
    # wedged poller (`mailbox_stale`); this is the half that does something
    # about it, and it has to live on the main loop because a thread cannot
    # notice its own absence.
    watchdog = PollerWatchdog(POLLER_STALL_SECONDS)
    _start_poller(service, stop, watchdog)
    # IDLE is a latency optimisation layered on top of the poller above, never a
    # replacement for it: the poller keeps its interval, so the worst case for
    # noticing mail is unchanged and a dead watcher costs nothing.
    idle_watcher = idle.start_supervisor(service, stop)
    if idle_watcher is not None:
        logging.info("idle watchers: on (one per active mailbox, reconnect %ss, idle %ss), "
                     "safety poll still every %ss", idle.RECONNECT_SECONDS, idle.IDLE_SECONDS,
                     POLL_SECONDS)
    # The sentinel rides the queue loop rather than the poller: a wedged poller
    # is one of the things it must be able to report, and a dead worker is
    # covered separately by the systemd OnFailure= handler.
    next_alert_check = time.monotonic()
    while not stop.is_set():
        try:
            result = process_due(service)
            if result["queued"]:
                logging.info("queue %s", result)
        except Exception:
            logging.exception("queue pass failed")
        daily = run_daily(service)
        if daily["sent"] or daily["errors"]:
            logging.info("daily %s", daily)
        broadcast = deliver_announcements(service)
        if broadcast["sent"] or broadcast["failed"]:
            logging.info("announcement emails %s", broadcast)
        try:
            actions = run_agent_actions(service)
        except Exception:
            logging.exception("confirmed actions could not be run")
            actions = {"done": 0, "failed": 0, "restart": False}
        if actions["done"] or actions["failed"]:
            logging.info("confirmed actions %s", actions)
        if actions["restart"]:
            # Same exit path as the watchdog: mark first, leave last, let systemd
            # bring us back. Returning normally keeps the shutdown orderly.
            logging.info("restarting on a confirmed action")
            return 0
        if time.monotonic() >= next_alert_check:
            next_alert_check = time.monotonic() + alerting.ALERT_CHECK_SECONDS
            alerts = alerting.run_checks(service.db, service.secrets)
            if alerts["sent"] or alerts["errors"]:
                logging.info("alert sentinel %s", alerts)
        if watchdog.wedged():
            # Exit rather than try to fix the thread: a wedged thread cannot be
            # killed in Python, and starting a second poller would double-poll
            # every mailbox. Letting systemd restart the process is the one
            # repair that leaves no half-state behind.
            logging.error("poller thread has not completed a pass in %ss; "
                          "exiting so systemd restarts the worker", POLLER_STALL_SECONDS)
            return 1
        stop.wait(QUEUE_SECONDS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
