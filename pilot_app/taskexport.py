"""Handing the user's own task list to the phone they already carry.

Two shapes, one source of truth (``line_for``), because the two platforms really
do differ and pretending otherwise would ship a button that works for half the
people who press it:

* **An iCalendar file** (``text/calendar``) that iOS and Android hand to the
  system calendar app.
* **A plain-text checklist** that can be pasted into iOS 提醒事项 / Google Tasks,
  for the people who want a to-do list rather than a calendar.

What the platform research actually says (checked 2026-09-16)
-------------------------------------------------------------
* iOS registers ``text/calendar`` and ``.ics`` against **Calendar**, and Safari
  only hands the file over when the server really sends that content type --
  serving it as ``application/octet-stream`` is the documented way to get a file
  that "cannot be opened". So the route must set the header, not just the name.
* **Reminders has no file import.** A file containing only ``VTODO`` components
  "opens with nothing to show" on iOS, and Google Tasks has no import at all.
  Every item here is therefore a ``VEVENT``, and the to-do path is text, not a
  file.
* ``UID`` is the identity Apple de-duplicates on: a stable UID means exporting
  the same task twice **updates** the existing entry instead of adding a second
  copy. Ours is derived from ``task_key``, which is itself a content
  fingerprint, so a rewritten action becomes a new entry (correctly) while an
  unchanged one does not pile up.
* All-day events dodge the timezone minefield entirely -- a floating time is
  read in whatever zone the phone happens to be in, and a ``TZID`` without a
  matching ``VTIMEZONE`` is undefined. ``DTEND`` for a date-valued event is
  **exclusive**: a one-day event on the 18th must end on the 19th, and getting
  that wrong shows every task a day short.

The date we put on an event
---------------------------
``deadline`` is a *display* string ("9/18/2026 23:59", "9月18日", "明天",
"以邮件为准"): it was built to be read, not parsed. So this module parses back
only the shapes it can prove -- an absolute date, with or without a year, and
the two relative words that are unambiguous -- and otherwise puts the event on
the day the mail arrived. The exact deadline text is kept in the title either
way, because a date we guessed must never replace what the mail actually said.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Iterable, Mapping, Sequence

# RFC 5545 wants CRLF, and some clients are strict about it.
CRLF = "\r\n"
# The calendar's own name where the client shows one (iOS/Google read X-WR-CALNAME).
CALENDAR_NAME = "CityU Mail Pilot 待办"
PRODID = "-//CityU Mail Pilot//Tasks//CN"
# Highest first, as RFC 5545 defines them (1 = highest, 9 = lowest).
_ICS_PRIORITY = {"high": "1", "medium": "5", "low": "9"}
_TITLE_PREFIX = {"high": "【急】", "medium": "【中】", "low": "【缓】"}

_ABSOLUTE_DATE = re.compile(r"(?:(20\d{2})/)?(\d{1,2})/(\d{1,2})(?:/(20\d{2}))?")
_MONTH_DAY = re.compile(r"(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_RELATIVE_DAYS = {"今天": 0, "today": 0, "明天": 1, "tomorrow": 1, "后天": 2}


def _one_line(value: Any) -> str:
    """Whatever the mail said, flattened to something a calendar can hold.

    Carriage returns and control characters are **removed rather than escaped**:
    they are how a crafted subject line would try to end our property and start
    one of its own, and no legitimate deadline or action needs them.
    """
    text = str(value or "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _escape(value: Any) -> str:
    """RFC 5545 TEXT escaping. Order matters: the backslash goes first."""
    text = _one_line(value)
    text = text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return text


def _fold(line: str) -> str:
    """Fold one content line at 75 octets, continuing with a single space.

    Counted in **octets, not characters**: this project is full of Chinese, where
    three bytes per character reaches the limit four times sooner than an ASCII
    line would. Folding in the middle of a multi-byte character produces a file
    that some parsers reject outright.
    """
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    pieces: list[str] = []
    current = ""
    width = 0
    limit = 75
    for character in line:
        size = len(character.encode("utf-8"))
        if width + size > limit:
            pieces.append(current)
            current = character
            width = size
            # Continuation lines carry a leading space, which itself counts.
            limit = 74
        else:
            current += character
            width += size
    pieces.append(current)
    return (CRLF + " ").join(pieces)


def _as_date(value: Any) -> dt.date | None:
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        try:
            return dt.date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def _parse_deadline(deadline: Any, anchor: dt.date) -> dt.date | None:
    """The date a deadline string names, or ``None`` when it cannot be proven.

    ``anchor`` is the day the mail arrived (or today), used for two things: the
    year of a "9月18日" (which is almost always the next occurrence, not the
    current one) and the meaning of 今天/明天/后天.
    """
    text = _one_line(deadline)
    if not text:
        return None
    match = _ABSOLUTE_DATE.search(text)
    if match:
        year = int(match.group(1) or match.group(4) or 0)
        month, day = int(match.group(2)), int(match.group(3))
        if not 1 <= month <= 12 or not 1 <= day <= 31:
            return None
        try:
            return dt.date(year or anchor.year, month, day)
        except ValueError:
            return None
    match = _MONTH_DAY.search(text)
    if match:
        month, day = int(match.group(2)), int(match.group(3))
        if not 1 <= month <= 12 or not 1 <= day <= 31:
            return None
        year = int(match.group(1) or 0)
        if year:
            try:
                return dt.date(year, month, day)
            except ValueError:
                return None
        for candidate_year in (anchor.year, anchor.year + 1):
            try:
                candidate = dt.date(candidate_year, month, day)
            except ValueError:
                return None
            # A date more than a month behind the anchor belongs to next year:
            # a December mail about "1月5日" means January, not last January.
            if (candidate - anchor).days > -31:
                return candidate
        return None
    for word, offset in _RELATIVE_DAYS.items():
        if word in text.lower() or word in text:
            return anchor + dt.timedelta(days=offset)
    return None


def effective_priority(task: Mapping[str, Any]) -> str:
    """What the user should see: their own ranking wins over the model's."""
    from .reports import (PRIORITY_HIGH, PRIORITY_LOW, PRIORITY_MEDIUM,
                          _priority_rank)  # local import: keeps reports optional

    chosen = str(task.get("user_priority") or "")
    if chosen in {PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW}:
        return chosen
    return str(task.get("priority") or "")
    # (_priority_rank is imported for the caller's convenience below.)


def _numbers(value: Any) -> list[int]:
    return [int(piece) for piece in re.findall(r"\d+", _one_line(value))]


def _already_states(action: str, deadline: str) -> bool:
    """Whether the action text already carries this deadline's numbers.

    Compared as numbers rather than as text because the two spellings really do
    differ: the report writes ``2026-10-06 23:59`` and our label is
    ``2026/10/6 23:59``. A substring test would call those different and print
    both, and a task line with two deadlines that look like two dates is worse
    than one with none.
    """
    wanted = _numbers(deadline)
    if not wanted:
        return False
    remaining = iter(_numbers(action))
    return all(any(token == value for token in remaining) for value in wanted)


def line_for(task: Mapping[str, Any]) -> str:
    """One task as one line -- the ONLY place the exported wording is decided.

    Both the calendar entry's title and the pasted checklist come from here, so
    the two can never drift into describing the same task differently.
    """
    action = _one_line(task.get("action")) or "（无描述）"
    prefix = _TITLE_PREFIX.get(effective_priority(task), "")
    deadline = _one_line(task.get("deadline"))
    if deadline and _already_states(action, deadline):
        deadline = ""
    return f"{prefix}{action}" + (f"（截止 {deadline}）" if deadline else "")


def event_day(task: Mapping[str, Any], *, today: dt.date) -> dt.date:
    """The day this task's event lands on: its deadline, else the mail's day.

    "The day the mail arrived" is the honest fallback: the task genuinely
    belongs to that day, and inventing today's date for an old item would move
    something the user already knows about.
    """
    anchor = _as_date(task.get("task_day")) or today
    return _parse_deadline(task.get("deadline"), anchor) or anchor


def build_ics(tasks: Sequence[Mapping[str, Any]], *, origin: str = "",
              now: dt.datetime | None = None, today: dt.date | None = None) -> str:
    """A calendar holding one all-day event per task. Import-safe to repeat."""
    moment = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    base = today or moment.date()
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        # PUBLISH, not REQUEST: this is the user's own copy, not an invitation
        # that expects a reply, and a REQUEST without an ATTENDEE is what makes
        # some clients show Accept/Decline buttons on your own to-do.
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(CALENDAR_NAME)}",
    ]
    for task in tasks:
        day = event_day(task, today=base)
        description_bits = [line_for(task)]
        subject = _one_line(task.get("subject"))
        sender = _one_line(task.get("sender"))
        if subject:
            description_bits.append(f"来自邮件：{subject}")
        if sender:
            description_bits.append(f"发件人：{sender}")
        description_bits.append("由 CityU Mail Pilot 导出；在应用里点「✓ 处理好了」不会同步回这里。")
        if origin:
            description_bits.append(origin)
        lines.extend([
            "BEGIN:VEVENT",
            # Stable per task, so re-exporting updates instead of duplicating.
            f"UID:{_one_line(task.get('task_key'))}@cityu-mail-pilot",
            f"DTSTAMP:{stamp}",
            # Date-valued and therefore timezone-free; DTEND is exclusive.
            f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(day + dt.timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_escape(line_for(task))}",
            f"DESCRIPTION:{_escape(chr(10).join(description_bits))}",
            "TRANSP:TRANSPARENT",
            f"CATEGORIES:{_escape(CALENDAR_NAME)}",
        ])
        priority = _ICS_PRIORITY.get(effective_priority(task))
        if priority:
            lines.append(f"PRIORITY:{priority}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return CRLF.join(_fold(line) for line in lines) + CRLF


def build_text(tasks: Sequence[Mapping[str, Any]]) -> str:
    """The paste-able checklist (iOS 提醒事项 / Google Tasks, one line each)."""
    return "\n".join(f"- [ ] {line_for(task)}" for task in tasks)


def filename(day: str = "") -> str:
    """An ASCII file name: the .ics extension is what iOS keys on."""
    stamp = _one_line(day) or dt.date.today().isoformat()
    stamp = re.sub(r"[^0-9-]", "", stamp) or dt.date.today().isoformat()
    return f"cityu-tasks-{stamp}.ics"


__all__ = ["CRLF", "CALENDAR_NAME", "PRODID", "build_ics", "build_text", "line_for",
           "event_day", "effective_priority", "filename"]
