"""Structured, action-first report rendering (plain text + conservative HTML email).

Two layouts are produced here:

* **Immediate report** — "A / 清晰行动版": importance and the one-line conclusion
  first, then what to do and by when, then the email summary, personal relevance,
  web-search recommendations with clickable sources, risks/inferences, and a
  short English brief.
* **Daily digest** — "C / 学生简报版": what must be handled first, then grouped
  sections (urgent / academic / opportunities / admin / low-value), counts,
  deadlines, failures, and one traceable row per email. No email is ever
  dropped: anything the classifier cannot place still gets a row.

Email HTML is deliberately old-fashioned: tables, inline styles, `width`
attributes, and `mso-` conditional comments only. No JavaScript, no `<style>`
block, no flex/grid, no `<details>`, no remote fonts, no background images.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import re
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

HONG_KONG = "Asia/Hong_Kong"

SECTION_ORDER = (1, 2, 3, 4, 5, 6, 7)
DAILY_SECTION_ORDER = (1, 2, 3, 4, 5, 6, 7)

CONTENT_DISCLAIMER = "AI 生成内容可能出错；邮件事实、联网来源与推测已在报告中分开标注。"

# The optional model-written paragraph in the daily digest. The wording says who
# wrote it and what it is worth, because everything else around it is derived
# from the messages themselves and must not be read as the same kind of thing.
SYNTHESIS_HEADING = "一段综览（模型写的，仅供参考）"

# A URL must stop at whitespace, brackets, and CJK/full-width characters: models
# write Chinese prose directly after a link ("https://x.com/ 以及..."), and those
# runs must never be treated as part of the href. Kept ASCII-only so a Unicode
# regex never splits a percent-encoded URL such as %E4%B8%AD.
URL_PATTERN = re.compile(
    r"https://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]*"
    r"[A-Za-z0-9\-_~:/#\[\]@$&*+=%]"
)
_URL_TRAILING = ".,;:!?)]}，。；：！？、）】》"

# Canonical section identifiers for the action-first immediate report.
S_PRIORITY = 1
S_ACTIONS = 2
S_SUMMARY = 3
S_RELEVANCE = 4
S_RECOMMENDATIONS = 5
S_RISKS = 6
S_ENGLISH = 7

_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("importance", ("importance", "priority", "重要程度", "重要吗", "结论")),
    ("actions", ("needs me", "actions for me", "what i need", "需要我做什么", "必须采取的行动", "行动")),
    ("summary", ("email content summary", "content summary", "邮件内容", "内容总结")),
    ("relevance", ("personal relevance", "relationship with", "关系", "相关性")),
    ("recommendations", ("web search", "recommendation", "联网", "建议")),
    ("risks", ("risk", "inference", "风险", "推测")),
    ("english", ("english", "英文")),
]

PRIORITY_HIGH = "high"
PRIORITY_MEDIUM = "medium"
PRIORITY_LOW = "low"
PRIORITY_UNKNOWN = "unknown"

_PRIORITY_LABELS = {
    PRIORITY_HIGH: ("重要 · 需要尽快处理", "Important"),
    PRIORITY_MEDIUM: ("一般 · 建议今天看", "Normal"),
    PRIORITY_LOW: ("低优先级 · 可以稍后", "Low priority"),
    PRIORITY_UNKNOWN: ("未能判定 · 请自行判断", "Unclear"),
}

_PRIORITY_COLORS = {
    PRIORITY_HIGH: ("#b42318", "#fff1f0", "#f5c4c0"),
    PRIORITY_MEDIUM: ("#8a6100", "#fff8e8", "#f0d9a8"),
    PRIORITY_LOW: ("#475467", "#f2f4f7", "#e4e7ec"),
    PRIORITY_UNKNOWN: ("#475467", "#f2f4f7", "#e4e7ec"),
}

NO_ACTION_PATTERNS = (
    "无需行动", "不需要行动", "无需采取行动", "无行动", "无待办", "没有待办", "没有行动",
    "无。", "无 ", "不需要", "无需", "no action", "nothing to do", "no further action",
    "none required", "n/a",
)

_LOW_VALUE_HINTS = (
    "广告", "优惠", "促销", "营销", "折扣", "推广", "订阅", "newsletter", "促销活动",
    "unsubscribe", "限时", "会员", "返现", "优惠券", "推广邮件",
)

_SEARCH_FAILED_PATTERNS = (
    "未完成联网核实", "未取得可验证来源", "no live web verification",
    "未取得可核实的来源", "没有可验证来源", "未找到可验证来源",
)

_DATE_PATTERNS = (
    (re.compile(r"(?<!\d)(20\d{2})[-/](\d{1,2})[-/](\d{1,2})"), "ymd"),
    (re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"), "ymd"),
    (re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日"), "md"),
)

_DEADLINE_MARKERS = (
    "截止", "之前", "以前", "deadline", "due", "by ", "中午", "上午", "下午", "晚上",
    "今天", "明天", "后天", "本周", "这周", "下周", "周一", "周二", "周三", "周四",
    "周五", "周六", "周日", "星期", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
)

_ACTION_MARKERS = (
    "提交", "完成", "回复", "回信", "确认", "报名", "登记", "注册", "缴费", "付", "预约",
    "参加", "出席", "下载", "填写", "上传", "联系", "检查", "查看", "准备", "打印",
    "submit", "complete", "reply", "confirm", "register", "apply", "pay", "book",
    "attend", "download", "fill", "upload", "contact", "review", "check", "prepare",
)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _clean(text: Any) -> str:
    return str(text or "").replace("\r", "").strip()


def _collapse(text: Any) -> str:
    return re.sub(r"\s+", " ", _clean(text)).strip()


def _inline(text: str) -> str:
    """Conservative inline HTML for one line of model output.

    Only ``https://`` links become anchors: a model-supplied ``http://`` or
    ``javascript:`` target must never reach a mail client as a clickable link.
    """
    safe = html.escape(_clean(text))
    safe = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe)
    safe = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", safe)
    safe = re.sub(r"`([^`]+)`", r"<strong>\1</strong>", safe)
    plain = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://", "[已屏蔽非 https 链接] ", safe)

    def link(match: re.Match[str]) -> str:
        url = match.group(0)
        return f'<a href="{url}" style="color:#1769aa;word-break:break-all">{url}</a>'

    return re.sub(URL_PATTERN, link, plain)


def _strip_inline(text: str) -> str:
    value = re.sub(r"\*\*(.+?)\*\*", r"\1", _clean(text))
    value = re.sub(r"`([^`]+)`", r"\1", value)
    return re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"\1", value)


def _lines(text: str) -> list[str]:
    return [_clean(line) for line in _clean(text).splitlines()]


def _line_body(line: str) -> tuple[str, str]:
    """Split one report line into (indent, body) and drop model sub-headings.

    Models routinely emit ``### 基础信息`` inside a section; that is a label, not
    content, and must never be presented as the conclusion or an action.
    """
    indent = line[: len(line) - len(line.lstrip())]
    stripped = re.sub(r"^\s*#{1,6}\s*", "", line).strip()
    return indent, stripped


_LIST_MARKER = re.compile(r"^\s*(?:[-*•·]|\d+[.)、]|□|✓)\s*(.+)$")
_LABEL_END = ("：", ":") 


def _iter_list_entries(text: str) -> list[str]:
    """Flatten a section into top-level list entries.

    Indented continuations are joined into their parent entry, and a bare label
    such as ``若确认非本人操作：`` is merged with the bullets that follow it, so a
    single decision never explodes into six pseudo-actions.
    """
    entries: list[str] = []
    pending = ""
    for raw in _lines(text):
        indent, body = _line_body(raw)
        if not body:
            continue
        match = _LIST_MARKER.match(raw)
        if match:
            item = _strip_inline(match.group(1)).strip()
            if not item:
                continue
            if indent and entries:
                entries[-1] = f"{entries[-1]} {item}".strip()
                continue
            if item.endswith(_LABEL_END):
                pending = item
                continue
            if pending:
                if entries:
                    entries[-1] = f"{entries[-1]} {pending}".strip()
                else:
                    entries.append(pending)
                pending = ""
            entries.append(item)
            continue
        if indent and entries:
            entries[-1] = f"{entries[-1]} {body}".strip()
            continue
        if body.endswith(_LABEL_END):
            pending = body
            continue
        entries.append(body)
    if pending:
        entries.append(pending)
    return [entry for entry in entries if entry]


def _bullets(text: str) -> list[str]:
    """Extract list items, falling back to sentences when the model used prose."""
    items = _iter_list_entries(text)
    if items:
        return items
    flat = _collapse(_strip_inline(text))
    if flat:
        return [part.strip() for part in re.split(r"(?<=[。；;.!?])\s*", flat) if part.strip()]
    return []


def _paragraphs(text: str) -> list[str]:
    return _iter_list_entries(text)


def _match_key(heading: str) -> str:
    needle = heading.lower()
    for key, keywords in _KEYWORDS:
        for keyword in keywords:
            if keyword in needle:
                return key
    return ""


def _resolve_timezone(name: str | None) -> dt.tzinfo:
    try:
        return ZoneInfo(name or HONG_KONG)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        try:
            return ZoneInfo(HONG_KONG)
        except (ZoneInfoNotFoundError, ValueError, KeyError):  # pragma: no cover - no tzdata
            return dt.timezone(dt.timedelta(hours=8))


def local_day_offset_hours(timezone: str | None, at: dt.datetime | None = None) -> int:
    """The user's UTC offset in whole hours, for bucketing days in SQL.

    SQLite cannot be handed a tz database name, so a day bucket has to be a fixed
    offset. Whole hours cover every zone this pilot has seen; being off by half an
    hour would only misplace usage inside a 30-minute window around local
    midnight. The point is that it is computed from the same timezone the rest of
    the page renders in, so the console cannot show a day that disagrees with the
    timestamps next to it.
    """
    moment = at or dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    offset = moment.astimezone(_resolve_timezone(timezone)).utcoffset() or dt.timedelta(0)
    return int(offset.total_seconds() // 3600)


def to_local(value: str | None, timezone: str | None = None) -> dt.datetime | None:
    """Parse an ISO timestamp (UTC from IMAP) into the user's timezone."""
    text = _clean(value)
    if not text:
        return None
    try:
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(_resolve_timezone(timezone))


def format_moment(value: str | None, timezone: str | None = None, *, with_date: bool = True) -> str:
    local = to_local(value, timezone)
    if not local:
        return "时间未提供"
    if with_date:
        return f"{local.month}月{local.day}日 {local:%H:%M}"
    return f"{local:%H:%M}"


def weekday_label(value: dt.datetime) -> str:
    names = ("一", "二", "三", "四", "五", "六", "日")
    return f"星期{names[value.weekday()]}"


def greeting_for(value: dt.datetime) -> str:
    if value.hour < 6:
        return "夜深了"
    if value.hour < 12:
        return "早上好"
    if value.hour < 18:
        return "下午好"
    return "晚上好"


def derive_priority(text: str, importance: str = "normal") -> str:
    """Read the model's importance section; fall back to the mail's own header.

    Ordering matters: an explicit ``重要性：低`` must win, so low-priority markers
    are checked before the generic "高" substring.
    """
    haystack = _clean(text).lower()
    if haystack:
        for token in ("低优先级", "优先级：低", "等级：低", "重要程度：低", "重要性：低", "不重要",
                      "可以忽略", "仅供参考", "low priority", "priority: low", "not urgent"):
            if token in haystack:
                return PRIORITY_LOW
        for token in ("高优先级", "优先级：高", "等级：高", "重要程度：高", "重要性：高", "重要",
                      "紧急", "urgent", "critical", "high priority", "priority: high",
                      "action required"):
            if token in haystack:
                return PRIORITY_HIGH
        for token in ("优先级：中", "等级：中", "重要程度：中", "重要性：中", "一般",
                      "medium", "normal priority", "priority: medium"):
            if token in haystack:
                return PRIORITY_MEDIUM
        if re.search(r"(?<![a-zA-Z])high(?![a-zA-Z])", haystack):
            return PRIORITY_HIGH
        if re.search(r"(?<![a-zA-Z])low(?![a-zA-Z])", haystack):
            return PRIORITY_LOW
        if re.search(r"(?<![a-zA-Z])(?:mid|moderate)(?![a-zA-Z])", haystack):
            return PRIORITY_MEDIUM
    if importance == "high":
        return PRIORITY_HIGH
    if importance == "low":
        return PRIORITY_LOW
    return PRIORITY_UNKNOWN


def priority_label(priority: str) -> str:
    return _PRIORITY_LABELS.get(priority, _PRIORITY_LABELS[PRIORITY_UNKNOWN])[0]


def priority_label_en(priority: str) -> str:
    return _PRIORITY_LABELS.get(priority, _PRIORITY_LABELS[PRIORITY_UNKNOWN])[1]


def _priority_rank(priority: str) -> int:
    return {PRIORITY_HIGH: 0, PRIORITY_MEDIUM: 1, PRIORITY_LOW: 2, PRIORITY_UNKNOWN: 3}.get(priority, 3)


def deadline_note(action: str) -> str:
    """A short "截止：…" note, or "" when the action already states the deadline."""
    deadline = deadline_of(action)
    if not deadline:
        return ""
    if deadline in action or "截止" in action:
        return ""
    return deadline


def is_actionable_line(text: str) -> bool:
    clean = _collapse(_strip_inline(text)).strip("。.；;，, ")
    if not clean:
        return False
    lowered = clean.lower()
    if lowered in {"- 无。", "无", "none", "n/a", "no"}:
        return False
    for pattern in NO_ACTION_PATTERNS:
        if lowered.startswith(pattern) and len(clean) <= len(pattern) + 6:
            return False
    return True


_DEADLINE_LEAD = re.compile(
    r"截止|之前|以前|不晚于|deadline|due\s*(?:by|on|date)|by\s",
    re.I,
)


def deadline_of(text: str) -> str:
    """Extract an explicit deadline (date and/or clock time) from one action line.

    Models write either ``截止：2026-09-18 23:59`` or prose like
    ``今天阅读要求，周五 23:59 前提交``. A date that follows a deadline marker is
    preferred; otherwise the last date in the line is used, because the deadline
    is almost always the final date mentioned.
    """
    clean = _collapse(_strip_inline(text))
    if not clean:
        return ""
    marker = _DEADLINE_LEAD.search(clean)
    candidates: list[tuple[int, str]] = []
    for pattern, kind in _DATE_PATTERNS:
        for match in pattern.finditer(clean):
            if kind == "ymd":
                label = f"{int(match.group(1))}/{int(match.group(2))}/{int(match.group(3))}"
            else:
                label = f"{int(match.group(1))}月{int(match.group(2))}日"
            candidates.append((match.start(), label))
    candidates.sort()
    pieces: list[str] = []
    if candidates:
        after_marker = [item for item in candidates if marker and item[0] >= marker.start()]
        chosen = (after_marker or candidates)[-1]
        pieces.append(chosen[1])
    clock_matches = list(re.finditer(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)", clean))
    if clock_matches:
        clock = clock_matches[-1]
        pieces.append(f"{int(clock.group(1)):02d}:{clock.group(2)}")
    for marker_word in ("今天", "明天", "后天", "本周", "这周", "下周", "周一", "周二", "周三",
                        "周四", "周五", "周六", "周日", "星期", "tonight", "today", "tomorrow",
                        "this week", "next week"):
        if marker_word not in clean:
            continue
        if candidates:
            # An absolute date (with or without a clock time) already answers
            # "by when"; also printing "周五" reads as a second, contradicting
            # deadline, so the relative marker is only a last resort.
            break
        if not any(marker_word in piece for piece in pieces):
            pieces.append(marker_word)
        break
    if not pieces:
        for marker_word in _DEADLINE_MARKERS:
            if marker_word in clean.lower() or marker_word in clean:
                pieces.append("以邮件为准")
                break
    return " ".join(pieces)


def has_explicit_deadline(text: str) -> bool:
    return bool(deadline_of(text))


_RELATIVE_DAYS = {"今天": 0, "明天": 1, "后天": 2, "today": 0, "tomorrow": 1}


def deadline_sort_key(text: str, report_date: str = "") -> tuple[int, str]:
    """Order deadlines chronologically; unparseable ones go last, never absent."""
    deadline = deadline_of(text)
    if not deadline:
        return (9, "")
    base = None
    try:
        if report_date:
            base = dt.date.fromisoformat(report_date)
    except ValueError:
        base = None
    for pattern, kind in _DATE_PATTERNS:
        match = pattern.search(deadline)
        if not match:
            continue
        try:
            if kind == "ymd":
                return (0, dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat())
            if base:
                return (0, base.replace(month=int(match.group(1)), day=int(match.group(2))).isoformat())
            return (1, deadline)
        except ValueError:
            return (1, deadline)
    for word, offset in _RELATIVE_DAYS.items():
        if word in deadline and base:
            return (0, (base + dt.timedelta(days=offset)).isoformat())
    if any(word in deadline for word in _RELATIVE_DAYS):
        return (0, (base or dt.date(1970, 1, 1)).isoformat())
    return (1, deadline)


def urls_in(text: str) -> list[str]:
    found = URL_PATTERN.findall(_clean(text))
    return [url.rstrip(_URL_TRAILING) for url in found]


_SOURCE_MARKERS = ("来源", "参考", "出处", "链接", "source", "reference", "link")


_SOURCE_SPLIT = re.compile(
    r"(?:来源|参考|出处|链接|参考文献|引用|source|reference|link)\s*[0-9一二三四五六七八九十]*\s*[:：]",
    re.I,
)


def _source_label(segment: str, url: str) -> str:
    """Best-effort human label for one URL, always falling back to the URL itself."""
    before = segment.split(url, 1)[0]
    explicit = False
    for marker in _SOURCE_MARKERS:
        index = before.lower().rfind(marker.lower())
        if index >= 0:
            before = before[index + len(marker):]
            explicit = True
            break
    if not explicit:
        # Generic prose ("详见 <link> 以及 <link2>") must not turn a whole
        # sentence into a source name; keep only the closest clause.
        before = re.split(r"[；;。！!？?\n]|以及|并且|\band\b", before)[-1]
    candidate = _collapse(before).strip(" -–—:：,，。;；[【(（)】]")[:90]
    if 2 <= len(candidate) <= 90:
        return candidate
    return ""


def dedupe_sources(text: str) -> list[dict[str, str]]:
    """Return labelled, de-duplicated https sources mentioned in a section.

    Models write sources either as ``来源：标题 URL`` rows or inline inside a
    paragraph; only the part that actually belongs to the link becomes a label so
    a whole sentence is never presented as a source name.
    """
    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in _paragraphs(text) or _bullets(text):
        for segment in _SOURCE_SPLIT.split(line):
            for url in urls_in(segment):
                if url in seen:
                    continue
                seen.add(url)
                label = _source_label(segment, url)
                sources.append({"url": url, "label": f"{label}（{url}）" if label else url})
    return sources


def search_was_unavailable(text: str) -> bool:
    lowered = _collapse(text).lower()
    return any(pattern.lower() in lowered for pattern in _SEARCH_FAILED_PATTERNS) or not urls_in(text)


def digest_key(subject: str, sender: str) -> str:
    """Merge repeated copies of the same notice without losing any of them."""
    clean = re.sub(r"(?i)^\s*(?:re|fw|fwd|转发|回复)\s*[:：]\s*", "", _clean(subject))
    clean = re.sub(r"[\s\W_]+", "", clean).lower()
    if len(clean) >= 6:
        return "s:" + clean[:120]
    fallback = re.sub(r"[\s\W_]+", "", _clean(sender)).lower()
    return "x:" + (fallback[:60] or hashlib.sha1(_clean(subject).encode("utf-8")).hexdigest()[:16])


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

def parse_sections(markdown: str) -> dict[str, str]:
    """Split a stored report into canonical sections, tolerating old layouts."""
    text = _clean(markdown)
    matches = list(re.finditer(r"(?m)^\s*#{1,4}\s*([1-7])\s*[.)、]?\s*(.*)$", text))
    if not matches and text:
        return {"summary": text}
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        title = match.group(2)
        key = _match_key(title) or _positional_key(int(match.group(1)))
        content = text[match.end():end].strip()
        if key and content and key not in sections:
            sections[key] = content
    return sections


def _positional_key(number: int) -> str:
    """Fallback mapping for reports written before the action-first layout."""
    return {
        1: "summary", 2: "recommendations", 3: "actions",
        4: "relevance", 5: "risks", 6: "english", 7: "english",
    }.get(number, "")


def _is_metadata_line(text: str) -> bool:
    """Sender/subject/date header lines are traceability metadata, not a conclusion."""
    lowered = _collapse(text).lower()
    return any(marker in lowered for marker in (
        "发件人", "发件邮箱", "显示发件", "sender", "from:", "主题", "subject:", "邮件接收时间",
        "收件时间", "邮件日期",
    ))


def conclusion_of(sections: dict[str, str], subject: str = "") -> str:
    for key in ("importance", "summary", "english"):
        chunk = sections.get(key, "")
        if not _clean(chunk):
            continue
        fallback = ""
        for item in _bullets(chunk) or _paragraphs(chunk):
            clean = _strip_inline(re.sub(r"^\s*#+\s*", "", item)).strip()
            # Strip the model's own label before judging the line, otherwise
            # "结论：xxx" is skipped as a label and the gist falls through to a
            # later bullet.
            clean = re.sub(
                r"^(?:等级|优先级|重要性|重要程度|结论|一句话结论|Priority|Level)\s*[:：]\s*",
                "", clean,
            ).strip()
            if clean.endswith(_LABEL_END) or len(clean) < 8:
                continue
            if _is_metadata_line(clean):
                fallback = fallback or clean
                continue
            return clean[:240]
        if fallback:
            return fallback[:240]
    return f"关于「{_collapse(subject)[:80]}」的摘要" if subject else "未能提炼一句话结论。"


def actions_of(sections: dict[str, str]) -> list[str]:
    found: list[str] = []
    for item in _bullets(sections.get("actions", "")):
        if is_actionable_line(item) and item not in found:
            found.append(item)
    return found


def parse_report(markdown: str, *, subject: str = "", message: dict[str, Any] | None = None,
                 timezone: str | None = None, kind: str = "immediate") -> dict[str, Any]:
    sections = parse_sections(markdown)
    message = message or {}
    priority = derive_priority(sections.get("importance", ""), str(message.get("importance") or "normal"))
    actions = actions_of(sections)
    return {
        "kind": kind,
        "sections": sections,
        "subject": _collapse(message.get("subject") or subject)[:300],
        "sender_name": _collapse(message.get("sender_name"))[:200],
        "sender_address": _collapse(message.get("sender_address"))[:320],
        "received": _collapse(message.get("received") or message.get("received_at")),
        "received_display": format_moment(message.get("received") or message.get("received_at"), timezone),
        "importance": _collapse(message.get("importance") or "normal"),
        "priority": priority,
        "priority_label": priority_label(priority),
        "conclusion": conclusion_of(sections, message.get("subject") or subject),
        "actions": actions,
        "deadline": next((deadline_of(item) for item in actions if has_explicit_deadline(item)), ""),
        "deadlines": [deadline_of(item) for item in actions if has_explicit_deadline(item)],
        "relevance": sections.get("relevance", ""),
        "summary": sections.get("summary", ""),
        "recommendations": sections.get("recommendations", ""),
        "risks": sections.get("risks", ""),
        "english": sections.get("english", ""),
        "sources": dedupe_sources(sections.get("recommendations", "")),
        "search_unavailable": search_was_unavailable(sections.get("recommendations", "")),
        "message_id": _collapse(message.get("id")),
    }


def task_key(report_id: str, action: str) -> str:
    """A stable identity for one action item, derived from its own content.

    Tasks are not stored: they are re-derived from the report text on every
    request. So "handled" cannot be a row id or a position in a list — an index
    would point at a *different* task the moment a report gains or loses an
    action, and the user would see an unrelated item disappear.

    Hashing the report id together with the action's text is stable across
    recomputation. It is deliberately sensitive to the text: if the model
    rewrites an action, the key changes, the old "handled" mark stops matching,
    and the new wording surfaces for a fresh decision instead of being silently
    hidden behind a stale one. This is the rule static-analysis suppressions
    use — fingerprint the content, never a scan-local id.
    """
    payload = f"{_collapse(report_id)}\x1f{_collapse(action)}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


def today_tasks(reports: Sequence[tuple[str, str, str]], messages: Sequence[dict[str, Any]],
                timezone: str | None = None) -> list[dict[str, Any]]:
    """Build the "what must I do" list shown on the dashboard and in the digest.

    ``reports`` items are ``(report_id, markdown, message_id)``. Each returned
    task carries a ``task_key`` and the local ``task_day`` it belongs to, which
    together are what let a user hide one item and find it again later.
    """
    by_id = {_collapse(item.get("id")): item for item in messages}
    tasks: list[dict[str, Any]] = []
    for report_id, markdown, message_id in reports:
        message = by_id.get(_collapse(message_id), {})
        parsed = parse_report(markdown, message=message, timezone=timezone)
        local = to_local(message.get("received"), timezone)
        day = local.date().isoformat() if local else ""
        for action in parsed["actions"]:
            tasks.append({
                "task_key": task_key(report_id, action),
                "task_day": day,
                "subject": parsed["subject"],
                "action": action,
                "deadline": deadline_of(action),
                "priority": parsed["priority"],
                "sender": parsed["sender_name"] or parsed["sender_address"],
                "received_display": parsed["received_display"],
                "message_id": parsed["message_id"],
            })
    tasks.sort(key=lambda item: (_priority_rank(item["priority"]), 0 if item["deadline"] else 1,
                                 item["received_display"]))
    return tasks


# --------------------------------------------------------------------------- #
# classification for the daily digest
# --------------------------------------------------------------------------- #

def classify(parsed: dict[str, Any]) -> str:
    subject = _collapse(parsed.get("subject"))
    corpus = " ".join([subject, _collapse(parsed.get("conclusion")),
                       _collapse(parsed.get("summary")), _collapse(parsed.get("relevance"))]).lower()
    lowered_subject = subject.lower()

    if any(hint in lowered_subject for hint in _LOW_VALUE_HINTS) or any(
        hint in corpus for hint in ("营销邮件", "促销邮件", "广告邮件", "newsletter", "unsubscribe")
    ):
        return "low"
    if parsed.get("actions"):
        return "urgent"
    if parsed.get("priority") == PRIORITY_HIGH:
        return "urgent"
    academic = ("课程", "作业", "考试", "成绩", "学分", "讲座", "教授", "导师", "论文", "选课",
                "tutorial", "lecture", "course", "assignment", "exam", "grade", "gpa", "semester")
    if any(word in corpus for word in academic):
        return "academic"
    opportunity = ("实习", "招聘", "比赛", "竞赛", "活动", "奖学金", "工作坊", "宣讲", "招新",
                   "internship", "career", "job", "competition", "hackathon", "scholarship",
                   "workshop", "seminar", "event")
    if any(word in corpus for word in opportunity):
        return "opportunity"
    administrative = ("缴费", "注册", "宿舍", "图书馆", "系统", "维护", "行政", "通知", "表格",
                      "截止", "tuition", "housing", "library", "portal", "notice", "reminder",
                      "administration", "fee", "enrol")
    if any(word in corpus for word in administrative):
        return "administrative"
    return "low"


CATEGORY_TITLES = {
    "urgent": "紧急待办 / Urgent",
    "academic": "学业相关 / Academic",
    "opportunity": "机会与活动 / Opportunities",
    "administrative": "行政通知 / Administrative",
    "low": "低优先级与营销 / Low priority",
    "failed": "处理失败 · 需要关注 / Failed",
}

CATEGORY_ORDER = ("urgent", "academic", "opportunity", "administrative", "low", "failed")


def _delivery_status(message: dict[str, Any]) -> str:
    """Truthful delivery state for a message that already has a report.

    ``report_status`` wins when present: a report is created before delivery and
    marked 'sent' after, while the message row is only advanced by the worker.
    Without this the 22:00 brief could report a successfully delivered report as
    "failed" simply because the worker had not finished bookkeeping yet.
    """
    report_status = _collapse(message.get("report_status"))
    if report_status in {"sent", "failed"}:
        return report_status
    message_status = _collapse(message.get("status"))
    if message_status == "failed":
        return "failed"
    return report_status or message_status or "sent"


def build_digest(messages: Sequence[dict[str, Any]], reports: dict[str, str],
                 timezone: str | None = None) -> dict[str, Any]:
    """Compose the daily student brief.

    Every processed message gets exactly one row. Messages that the sender
    filter deliberately skipped get their own auditable list instead of being
    counted as failures, so "we did not analyse it" and "it failed" stay
    distinguishable.
    """
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for message in messages:
        if str(message.get("status")) == "skipped":
            skipped.append({
                "subject": _collapse(message.get("subject")) or "（无主题）",
                "sender": _collapse(message.get("sender_name")) or _collapse(message.get("sender_address")),
                "sender_address": _collapse(message.get("sender_address")),
                "received_display": format_moment(message.get("received_at"), timezone),
                "reason": _collapse(message.get("skip_reason")) or "未说明原因",
            })
            continue
        message_id = _collapse(message.get("id"))
        body = reports.get(message_id)
        if not body:
            items.append({
                "key": "m:" + message_id, "message_id": message_id,
                "subject": _collapse(message.get("subject")) or "（无主题）",
                "sender": _collapse(message.get("sender_name")) or _collapse(message.get("sender_address")),
                "sender_address": _collapse(message.get("sender_address")),
                "received": _collapse(message.get("received_at")),
                "received_display": format_moment(message.get("received_at"), timezone),
                "status": _collapse(message.get("status")) or "pending",
                "last_error": _collapse(message.get("last_error")),
                "priority": PRIORITY_UNKNOWN, "priority_label": priority_label(PRIORITY_UNKNOWN),
                "conclusion": "这封邮件还没有生成摘要，请稍后查看或检查错误信息。",
                "actions": [], "deadline": "", "sources": [], "relevance": "",
                "search_unavailable": True, "duplicates": 0,
                "category": "failed" if _collapse(message.get("status")) == "failed" else "urgent",
                "parsed": None,
            })
            continue
        parsed = parse_report(body, message=message, timezone=timezone)
        category = classify(parsed)
        items.append({
            "key": digest_key(parsed["subject"], parsed["sender_address"] or parsed["sender_name"]),
            "message_id": message_id,
            "subject": parsed["subject"] or "（无主题）",
            "sender": parsed["sender_name"] or parsed["sender_address"],
            "sender_address": parsed["sender_address"],
            "received": parsed["received"],
            "received_display": parsed["received_display"],
            # A stored report means the mail WAS analysed. The message row can
            # legitimately still be 'pending'/'processing' (the worker marks it
            # after delivery, and the daily digest may run in between), so the
            # report status is the truthful delivery state here. Treating the
            # message status as authoritative made successful reports show up as
            # "failed" in the brief.
            "status": _delivery_status(message),
            "last_error": _collapse(message.get("last_error")),
            "priority": parsed["priority"],
            "priority_label": parsed["priority_label"],
            "conclusion": parsed["conclusion"],
            "actions": parsed["actions"],
            "deadline": parsed["deadline"],
            "sources": parsed["sources"],
            "relevance": _collapse(parsed["relevance"])[:600],
            "search_unavailable": parsed["search_unavailable"],
            "duplicates": 0,
            "category": category,
            "parsed": parsed,
        })

    merged: list[dict[str, Any]] = []
    index: dict[str, int] = {}
    for item in items:
        existing = index.get(item["key"])
        if existing is None:
            index[item["key"]] = len(merged)
            merged.append(item)
            continue
        primary = merged[existing]
        primary["duplicates"] += 1
        if _priority_rank(item["priority"]) < _priority_rank(primary["priority"]):
            primary["priority"] = item["priority"]
            primary["priority_label"] = item["priority_label"]
        if item["received"] > primary["received"]:
            primary["received"] = item["received"]
            primary["received_display"] = item["received_display"]
        if not primary["actions"] and item["actions"]:
            primary["actions"] = item["actions"]
            primary["deadline"] = item["deadline"]
            primary["conclusion"] = item["conclusion"]
        if not primary["sources"] and item["sources"]:
            primary["sources"] = item["sources"]
            primary["search_unavailable"] = item["search_unavailable"]
        if primary["category"] == "low" and item["category"] != "low":
            primary["category"] = item["category"]

    sections: dict[str, list[dict[str, Any]]] = {name: [] for name in CATEGORY_ORDER}
    for item in merged:
        sections[item["category"]].append(item)
    for name, entries in sections.items():
        rank = {"failed": 0, "urgent": 1, "academic": 2, "opportunity": 3, "administrative": 4, "low": 5}[name]
        entries.sort(key=lambda entry: (_priority_rank(entry["priority"]), rank, entry["received"]))

    all_entries = [entry for name in CATEGORY_ORDER for entry in sections[name]]
    ordered_tasks = sorted(
        ((entry, action) for entry in all_entries for action in entry["actions"]),
        key=lambda pair: (_priority_rank(pair[0]["priority"]), deadline_sort_key(pair[1]),
                          pair[0]["received"]),
    )
    deadlines = sorted(
        {deadline_of(action) for _, action in ordered_tasks if deadline_of(action)},
        key=deadline_sort_key,
    )
    skipped_items = skipped
    failed = len(sections["failed"])
    unprocessed = sum(1 for entry in all_entries if entry["status"] not in {"sent"})
    low_count = sum(1 + entry["duplicates"] for entry in sections["low"])
    without_sources = sum(1 for entry in all_entries if entry["search_unavailable"] and entry["status"] == "sent")

    return {
        "date": "",
        "generated_at": "",
        "timezone": timezone or HONG_KONG,
        "items": all_entries,
        "merged": merged,
        "sections": sections,
        "tasks": [{"subject": entry["subject"], "action": action, "deadline": deadline_of(action),
                   "priority": entry["priority"], "sender": entry["sender"],
                   "received_display": entry["received_display"], "message_id": entry["message_id"]}
                  for entry, action in ordered_tasks],
        "deadlines": deadlines,
        "next_deadline": deadlines[0] if deadlines else "",
        "skipped": skipped_items,
        "metrics": {
            "total": len(messages),
            "skipped": len(skipped_items),
            "merged_total": len(merged),
            "actionable": len(ordered_tasks),
            "failed": failed + max(0, unprocessed - failed),
            "unprocessed": unprocessed,
            "low_priority": low_count,
            "without_sources": without_sources,
            "duplicates": sum(entry["duplicates"] for entry in merged),
            "sections": {name: sum(1 + entry["duplicates"] for entry in entries)
                         for name, entries in sections.items()},
        },
    }


# --------------------------------------------------------------------------- #
# markdown for storage
# --------------------------------------------------------------------------- #

def _synthesis_markdown(digest: dict[str, Any]) -> list[str]:
    """The optional model paragraph, as a block quote.

    A quote rather than a numbered section on purpose: every other heading in
    this document is generated from the messages themselves, and a model-written
    paragraph must not read as one more of them. The heading says who wrote it.
    """
    text = str(digest.get("synthesis") or "").strip()
    if not text:
        return []
    lines = [f"\n> **{SYNTHESIS_HEADING}**"]
    lines.extend(f"> {line}" for line in text.splitlines())
    return lines


def digest_markdown(digest: dict[str, Any]) -> str:
    metrics = digest["metrics"]
    out: list[str] = []
    out.append(f"## 1. 今天最重要 / Most important today")
    if digest["tasks"]:
        for task in digest["tasks"][:3]:
            suffix = f"（截止：{task['deadline']}）" if task["deadline"] else ""
            out.append(f"- {task['action']}{suffix} · 来自「{task['subject']}」")
    else:
        out.append("- 今天没有必须立刻处理的事项。")
        out.append("- 下一封新邮件到达时会自动生成即时摘要。")
    out.extend(_synthesis_markdown(digest))
    for number, name in enumerate(CATEGORY_ORDER, start=2):
        if name == "failed":
            break  # listed in section 7 below
        entries = digest["sections"][name]
        out.append(f"\n## {number}. {CATEGORY_TITLES[name]}")
        if not entries:
            out.append("- 无。")
            continue
        for entry in entries:
            repeat = f"（另有 {entry['duplicates']} 封同类邮件）" if entry["duplicates"] else ""
            line = (f"- **{entry['subject']}**{repeat} · 发件人：{entry['sender'] or '未知'}"
                    f" · 收件：{entry['received_display']}")
            if entry["actions"]:
                line += f" · 待办：{entry['actions'][0]}"
                if entry["deadline"]:
                    line += f"（截止 {entry['deadline']}）"
            out.append(line)
            if entry["status"] != "sent":
                out.append(f"  - ⚠ 状态：{entry['status']} {entry['last_error'][:200]}")
    out.append("\n## 7. 处理失败 · 需要关注 / Failed")
    failed = digest["sections"]["failed"]
    if failed:
        for entry in failed:
            out.append(f"- {entry['subject']} · 发件人：{entry['sender'] or '未知'}"
                       f" · 收件：{entry['received_display']} · 状态：{entry['status']} "
                       f"{entry['last_error'][:200]}")
    else:
        out.append("- 无失败记录；所有邮件都已生成摘要。")
    out.append("\n## 8. 今天的数字与异常 / Today's numbers and exceptions")
    out.append(f"- 收到邮件：{metrics['total']} 封（合并同类后 {metrics['merged_total']} 条）")
    out.append(f"- 需要行动：{metrics['actionable']} 项")
    out.append(f"- 处理失败或未完成：{metrics['failed']} 封")
    out.append(f"- 低优先级/营销：{metrics['low_priority']} 封")
    out.append(f"- 最近截止时间：{digest['next_deadline'] or '无明确截止时间'}")
    if metrics.get("skipped"):
        out.append(f"- 另有 {metrics['skipped']} 封邮件不属于允许的发件人范围，未做 AI 处理（不是为了丢弃，见下方清单）。")
    if metrics["without_sources"]:
        out.append(f"- 有 {metrics['without_sources']} 封邮件本次未取得可验证来源（已按邮件标注，未伪造引用）。")
    if digest["metrics"]["duplicates"]:
        out.append(f"- 已合并 {digest['metrics']['duplicates']} 封同类重复邮件，数量仍计入总数。")
    if not digest["items"]:
        out.append("- 今天没有收到需要处理的新邮件。")
    skipped = digest.get("skipped") or []
    if skipped:
        out.append(f"- 被跳过（非允许发件人，未做 AI 处理）：{len(skipped)} 封")
        for entry in skipped[:10]:
            # Full address, not a display name: this list is the audit trail
            # proving which mail was deliberately not analysed.
            who = entry.get("sender_address") or entry.get("sender") or "未知发件人"
            out.append(f"  - {entry['subject'][:60]} · {who} · {entry.get('reason', '')[:60]}")
    return "\n".join(out)


def with_digest_header(digest: dict[str, Any], report_date: str, generated_at: str) -> dict[str, Any]:
    digest["date"] = report_date
    digest["generated_at"] = generated_at
    return digest


# --------------------------------------------------------------------------- #
# HTML: shared primitives
# --------------------------------------------------------------------------- #

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _table(rows: str, *, width: int = 600, padding: str = "0") -> str:
    """Outlook-safe fixed-width table that also shrinks on a 360px phone.

    ``width``/``max-width`` are written both as HTML attributes and inline CSS
    (the Word renderer trusts the attribute; browsers trust the CSS), and
    ``table-layout:fixed`` plus ``word-break`` stop an unbreakable token in an
    email from pushing the whole layout wider than the viewport.
    """
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'width="100%" style="width:100%;max-width:{width}px;margin:0 auto;'
            f'border-collapse:collapse;padding:{padding};table-layout:fixed;'
            f'word-break:break-word;overflow-wrap:anywhere">{rows}</table>')


def _inner_table(rows: str, *, extra: str = "") -> str:
    """A 100%-wide table inside the frame.

    ``table-layout:fixed`` plus per-cell ``word-break`` are what keep an
    unbreakable token (a 200-character URL in a model citation, or a long CJK
    run) from widening the whole email on a phone.
    """
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="width:100%;max-width:100%;border-collapse:collapse;table-layout:fixed;'
            f'word-break:break-word;overflow-wrap:anywhere;{extra}">{rows}</table>')


def _badge(text: str, priority: str) -> str:
    color, background, border = _PRIORITY_COLORS.get(priority, _PRIORITY_COLORS[PRIORITY_UNKNOWN])
    return (f'<span style="display:inline-block;padding:4px 10px;border-radius:99px;'
            f'background:{background};color:{color};border:1px solid {border};'
            f'font-size:12px;font-weight:700">{html.escape(text)}</span>')


def _paragraph_html(text: str, *, size: int = 14, color: str = "#243447", muted: bool = False) -> str:
    if not _clean(text):
        return ""
    align = "left"
    return (f'<p style="margin:8px 0;font-size:{size}px;line-height:1.6;text-align:{align};'
            f'color:{"#667085" if muted else color}">{_inline(text)}</p>')


def _bullets_html(items: Iterable[str], *, ordered: bool = False, show_deadline: bool = False,
                  mark_inference: bool = False) -> str:
    entries = [item for item in items if _clean(item)]
    if not entries:
        return ""
    tag = "ol" if ordered else "ul"
    rows = []
    for item in entries:
        deadline = deadline_note(item) if show_deadline else ""
        suffix = (f'<br><span style="font-size:12px;color:#8a6100">截止：{html.escape(deadline)}</span>'
                  if deadline else "")
        inference = ""
        if mark_inference and re.search(r"推测|推断|inference|assumption|uncertain|不确定", item, re.I):
            inference = ('<span style="font-size:11px;color:#8a6100;background:#fff8e8;'
                         'border:1px solid #f0d9a8;border-radius:99px;padding:1px 7px;margin-right:6px">'
                         'AI 推测</span>')
        rows.append(f'<li style="margin:7px 0;font-size:14px;line-height:1.6">'
                    f'{inference}{_inline(item)}{suffix}</li>')
    return (f'<{tag} style="margin:8px 0;padding-left:22px;color:#243447">{ "".join(rows) }</{tag}>')


def _section(title: str, body: str, *, accent: str = "#123b63") -> str:
    if not _clean(body):
        body = '<p style="margin:8px 0;font-size:14px;color:#667085">本次报告未提供这一部分。</p>'
    return (
        '<tr><td style="padding:18px 22px 0">'
        f'<div style="font-size:13px;font-weight:700;color:{accent};letter-spacing:.02em;'
        f'border-bottom:1px solid #e4eaf0;padding-bottom:6px">{html.escape(title)}</div>'
        f'<div style="padding-top:4px">{body}</div>'
        '</td></tr>'
    )


def _callout(label: str, body: str, *, background: str = "#eaf4fc", border: str = "#b9d7ee",
             color: str = "#123b63") -> str:
    return (
        f'<div style="background:{background};border:1px solid {border};border-radius:10px;'
        f'padding:14px 16px;margin:6px 0">'
        f'<div style="font-size:12px;color:{color};font-weight:700">{html.escape(label)}</div>'
        f'<div style="font-size:15px;line-height:1.6;color:#1d2939;margin-top:5px">{_inline(body)}</div>'
        '</div>'
    )


def _email_shell(title: str, subtitle: str, body_rows: str, *, footer: str = CONTENT_DISCLAIMER) -> str:
    """Outlook-safe single-column shell: tables, inline styles, no JS, no <style>.

    Compatibility choices follow the reviewed open-source guidance (react-email
    styling rules, leemunroe's responsive template, caniemail): the 600px frame
    is a fixed table with the width duplicated as an attribute and as inline CSS,
    colours use ``bgcolor`` where it matters, every cell carries explicit
    padding, and nothing depends on a ``<style>`` block, ``@media``, flex, grid,
    remote fonts, images or JavaScript.
    """
    return (
        '<!doctype html>'
        '<html lang="zh-Hans"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="x-apple-disable-message-reformatting">'
        '<meta name="format-detection" content="telephone=no">'
        '<title>' + html.escape(title) + '</title></head>'
        '<body style="margin:0;padding:0;background-color:#f3f6f9;'
        f'-webkit-text-size-adjust:100%;font-family:{FONT};color:#1d2939;'
        'word-break:break-word;overflow-wrap:anywhere">'
        '<div style="display:none;font-size:1px;color:#f3f6f9;max-height:0;overflow:hidden;'
        'mso-hide:all">' + html.escape(subtitle[:120]) + '</div>'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        'bgcolor="#f3f6f9" style="width:100%;background-color:#f3f6f9;border-collapse:collapse">'
        '<tr><td align="center" style="padding:20px 10px">'
        '<!--[if mso]><table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'width="600"><tr><td width="600"><![endif]-->'
        + _table(
            '<tr><td bgcolor="#123b63" style="background-color:#123b63;padding:20px 22px;color:#ffffff">'
            '<div style="font-size:11px;letter-spacing:.10em;color:#bcd7ea">CITYU MAIL PILOT</div>'
            f'<div style="font-size:19px;font-weight:700;line-height:1.35;margin-top:6px">{html.escape(title)}</div>'
            f'<div style="font-size:12px;color:#cfe3f2;margin-top:6px">{_inline(subtitle)}</div>'
            '</td></tr>'
            '<tr><td bgcolor="#ffffff" style="background-color:#ffffff;border:1px solid #dbe3eb;'
            'border-top:0;padding:0 0 18px">'
            + body_rows +
            '</td></tr>'
            '<tr><td style="padding:12px 4px 0;font-size:11px;line-height:1.6;color:#98a2b3">'
            + html.escape(footer) + '</td></tr>',
        )
        + '<!--[if mso]></td></tr></table><![endif]-->'
        + '</td></tr></table></body></html>'
    )


def _meta_row(parsed: dict[str, Any]) -> str:
    sender = parsed["sender_name"] or parsed["sender_address"] or "未知发件人"
    address = f' &lt;{html.escape(parsed["sender_address"])}&gt;' if parsed["sender_address"] else ""
    return (f'<div style="font-size:13px;color:#475467;margin-top:8px;line-height:1.6">'
            f'发件人：{html.escape(sender)}{address}<br>'
            f'收件时间：{html.escape(parsed["received_display"])}　·　'
            f'邮件头优先级：{html.escape(parsed["importance"] or "normal")}</div>')


def _sources_html(parsed: dict[str, Any]) -> str:
    if parsed["sources"]:
        rows = []
        for index, source in enumerate(parsed["sources"][:8], 1):
            rows.append(
                '<li style="margin:6px 0;font-size:13px;line-height:1.6;word-break:break-all">'
                f'<a href="{html.escape(source["url"], quote=True)}" '
                f'style="color:#1769aa;text-decoration:underline">{html.escape(source["label"])}</a></li>'
            )
        return ('<ul style="margin:8px 0;padding-left:20px">' + "".join(rows) + '</ul>')
    return _paragraph_html("本次未取得可验证来源 / No verifiable live source was retrieved for this message. "
                           "报告中的建议仅供参考，未做联网核实。", muted=True)


# --------------------------------------------------------------------------- #
# HTML: immediate report (A — action first)
# --------------------------------------------------------------------------- #

def render_immediate_html(parsed: dict[str, Any], *, subject: str | None = None) -> str:
    title = _clean(subject) or parsed["subject"] or "邮件摘要"
    critical = [
        ("这封邮件重要吗", parsed["priority_label"]),
        ("一句话结论", parsed["conclusion"]),
    ]
    if parsed["actions"]:
        first = parsed["actions"][0]
        critical.append(("你要做什么", first))
        critical.append(("什么时候之前", deadline_of(first) or "邮件没有给出明确截止时间"))
    else:
        critical.append(("你要做什么", "这封邮件不需要你采取行动。"))
        critical.append(("什么时候之前", "无截止时间"))
    cells = []
    for label, value in critical:
        cells.append(
            '<tr><td style="padding:9px 0;border-bottom:1px solid #eef2f6;vertical-align:top">'
            f'<div style="font-size:12px;color:#667085">{html.escape(label)}</div>'
            f'<div style="font-size:14px;line-height:1.6;color:#1d2939;margin-top:3px">{_inline(value)}</div>'
            '</td></tr>'
        )
    priority_badge = _badge(parsed["priority_label"], parsed["priority"])
    rows = (
        '<tr><td style="padding:18px 22px 0">'
        f'<div>{priority_badge}</div>'
        f'<div style="font-size:13px;color:#475467;margin-top:10px">{html.escape(title)}</div>'
        + _meta_row(parsed) +
        '</td></tr>'
        '<tr><td style="padding:14px 22px 0">'
        + _inner_table("".join(cells)) + '</td></tr>'
        + _section("你应该做什么 / What to do",
                   _bullets_html(parsed["actions"], ordered=True, show_deadline=True)
                   or _paragraph_html("无需行动 / No action required.", muted=True))
        + _section("邮件讲了什么 / What the email says",
                   _bullets_html(_bullets(parsed["summary"]))
                   or _paragraph_html(parsed["summary"]) or _paragraph_html("邮件正文为空或无法解析。", muted=True))
        + _section("为什么与你有关 / Why it matters to you", _paragraph_html(parsed["relevance"]))
        + _section("联网搜索后的建议 / Suggestions from web search",
                   _paragraph_html(parsed["recommendations"]) + _sources_html(parsed))
        + _section("风险、未知与推测 / Risks, unknowns and inferences",
                   _bullets_html(_bullets(parsed["risks"]), mark_inference=True)
                   or _paragraph_html(parsed["risks"]))
        + _section("English brief", _paragraph_html(parsed["english"]) or
                   _paragraph_html("English summary was not provided for this message.", muted=True),
                   accent="#475467")
    )
    subtitle = f'即时摘要 · {parsed["received_display"]} · {priority_label_en(parsed["priority"])}'
    return _email_shell(title, subtitle, rows)


def render_immediate_text(parsed: dict[str, Any], *, subject: str | None = None) -> str:
    title = _clean(subject) or parsed["subject"] or "邮件摘要"
    out = [title, "=" * min(len(title), 60), f"重要程度：{parsed['priority_label']}",
           f"结论：{parsed['conclusion']}",
           f"发件人：{parsed['sender_name'] or parsed['sender_address'] or '未知'}",
           f"收件时间：{parsed['received_display']}", ""]
    out.append("【你应该做什么】")
    if parsed["actions"]:
        for index, action in enumerate(parsed["actions"], 1):
            note = deadline_note(action)
            out.append(f"{index}. {action}" + (f"（截止：{note}）" if note else ""))
    else:
        out.append("无需行动。")
    out.append("")
    out.append("【邮件讲了什么】")
    out.extend(f"- {item}" for item in (_paragraphs(parsed["summary"]) or ["未提供。"]))
    out.append("")
    out.append("【为什么与你有关】")
    out.extend(f"- {item}" for item in (_paragraphs(parsed["relevance"]) or ["未提供。"]))
    out.append("")
    out.append("【联网搜索后的建议】")
    out.extend(f"- {item}" for item in (_paragraphs(parsed["recommendations"]) or ["本次未取得可验证来源。"]))
    for source in parsed["sources"][:8]:
        out.append(f"  · {source['label']}")
    out.append("")
    out.append("【风险、未知与推测】")
    out.extend(f"- {item}" for item in (_paragraphs(parsed["risks"]) or ["未提供。"]))
    out.append("")
    out.append("【English brief】")
    out.extend(f"- {item}" for item in (_paragraphs(parsed["english"]) or ["Not provided."]))
    out.append("")
    out.append(CONTENT_DISCLAIMER)
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# HTML: daily digest (C — student brief)
# --------------------------------------------------------------------------- #

def _digest_item_html(entry: dict[str, Any]) -> str:
    repeat = (f'<span style="font-size:12px;color:#667085">（另有 {entry["duplicates"]} 封同类邮件）</span>'
              if entry["duplicates"] else "")
    status = ""
    if entry["status"] != "sent":
        warning = (f'⚠ 状态：{html.escape(entry["status"])} '
                   f'{html.escape(entry["last_error"][:200])}')
        status = (f'<div style="font-size:12px;color:#b42318;margin-top:5px">{warning}</div>')
    actions = ""
    if entry["actions"]:
        rows = "".join(
            f'<li style="margin:4px 0;font-size:13px;line-height:1.6">{_inline(action)}'
            + (f'<span style="color:#8a6100">（截止 {html.escape(deadline_note(action))}）</span>'
               if deadline_note(action) else "")
            + '</li>'
            for action in entry["actions"][:4]
        )
        actions = f'<ul style="margin:6px 0 0;padding-left:20px">{rows}</ul>'
    sources = ""
    if entry["sources"]:
        links = " · ".join(
            f'<a href="{html.escape(source["url"], quote=True)}" style="color:#1769aa;word-break:break-all">'
            f'{html.escape(source["label"][:70])}</a>' for source in entry["sources"][:3]
        )
        sources = f'<div style="font-size:12px;margin-top:6px">来源：{links}</div>'
    elif entry["status"] == "sent":
        sources = '<div style="font-size:12px;color:#667085;margin-top:6px">本次未取得可验证来源</div>'
    return (
        '<tr><td style="padding:14px 0;border-top:1px solid #eef2f6;word-break:break-word;overflow-wrap:anywhere">'
        f'<div style="font-size:14px;font-weight:700;line-height:1.5;color:#1d2939">'
        f'{html.escape(entry["subject"])} {repeat}</div>'
        f'<div style="font-size:12px;color:#667085;margin-top:4px">'
        f'{html.escape(entry["sender"] or "未知发件人")} · {html.escape(entry["received_display"])} · '
        f'{html.escape(entry["priority_label"])}</div>'
        f'<div style="font-size:13px;line-height:1.6;color:#344054;margin-top:6px">'
        f'{_inline(entry["conclusion"])}</div>'
        + actions + sources + status +
        '</td></tr>'
    )


def _digest_section_html(title: str, entries: list[dict[str, Any]], *, note: str = "") -> str:
    if not entries:
        return ""
    rows = "".join(_digest_item_html(entry) for entry in entries)
    tables = _inner_table(rows)
    footer = (f'<div style="font-size:12px;color:#667085;margin-top:8px">{_inline(note)}</div>'
              if note else "")
    return (
        '<tr><td style="padding:18px 22px 0">'
        f'<div style="font-size:13px;font-weight:700;color:#123b63;letter-spacing:.02em;'
        f'border-bottom:1px solid #e4eaf0;padding-bottom:6px">{html.escape(title)}</div>'
        f'<div>{tables}{footer}</div></td></tr>'
    )


def _digest_metric_html(digest: dict[str, Any]) -> str:
    metrics = digest["metrics"]
    cells = [
        ("今日邮件", f'{metrics["total"]} 封'),
        ("需要行动", f'{metrics["actionable"]} 项'),
        ("最近截止", digest["next_deadline"] or "无明确截止"),
        ("异常/失败", f'{metrics["failed"]} 封'),
    ]
    rendered = []
    for index, (label, value) in enumerate(cells):
        border = "border-left:1px solid #e4e7ec;" if index else ""
        rendered.append(
            f'<td width="25%" style="padding:10px 8px;word-break:break-word;overflow-wrap:anywhere;{border}vertical-align:top">'
            f'<div style="font-size:11px;color:#667085">{html.escape(label)}</div>'
            f'<div style="font-size:14px;font-weight:700;color:#1d2939;margin-top:3px;'
            f'word-break:break-word">{html.escape(value)}</div></td>'
        )
    return _inner_table(
        '<tr>' + "".join(rendered) + '</tr>',
        extra='background-color:#f8fafc;border:1px solid #e4e7ec',
    )


def render_digest_html(digest: dict[str, Any], *, subject: str | None = None) -> str:
    date_label = digest.get("date") or ""
    headline = (f'{date_label} · ' if date_label else "") + \
        (f'今天有 {digest["metrics"]["actionable"]} 件事需要处理'
         if digest["metrics"]["actionable"] else "今天没有必须立刻处理的事项")
    title = _clean(subject) or f'每日简报 {date_label}'.strip()
    top = digest["tasks"][:3]
    if top:
        rows = "".join(
            f'<div style="padding:8px 0;border-bottom:1px solid #e6eef5">'
            f'<div style="font-size:14px;line-height:1.6;color:#ffffff">{_inline(task["action"])}</div>'
            f'<div style="font-size:12px;color:#cfe3f2;margin-top:3px">'
            f'{html.escape(task["deadline"] or "无明确截止时间")} · 来自「{html.escape(task["subject"][:60])}」</div>'
            '</div>'
            for task in top
        )
        now_box = (
            '<div style="background:#123b63;border-radius:10px;padding:16px;margin:6px 0">'
            '<div style="font-size:12px;color:#bcd7ea">现在就要处理 / Do this first</div>'
            f'<div style="font-size:16px;font-weight:700;color:#ffffff;margin:6px 0">{_inline(headline)}</div>'
            + rows + '</div>'
        )
    else:
        now_box = _callout("现在就要处理 / Do this first",
                           f"{headline}。下一封新邮件到达时会自动生成即时摘要。",
                           background="#eef8f2", border="#cbe8d9", color="#0a5c42")

    synthesis = str(digest.get("synthesis") or "").strip()
    body = (
        '<tr><td style="padding:18px 22px 0">'
        + now_box
        + (f'<div style="margin-top:12px">{_callout(SYNTHESIS_HEADING, synthesis, background="#fbf7ee", border="#eadfc6", color="#4a3c1e")}</div>'
           if synthesis else "")
        + '<div style="margin-top:12px">' + _digest_metric_html(digest) + '</div>'
        '</td></tr>'
    )
    body += _digest_section_html(CATEGORY_TITLES["failed"], digest["sections"]["failed"],
                                 note="这些邮件没有成功生成摘要；报告不会丢弃它们，worker 会按退避策略重试。")
    body += _digest_section_html(CATEGORY_TITLES["urgent"], digest["sections"]["urgent"])
    body += _digest_section_html(CATEGORY_TITLES["academic"], digest["sections"]["academic"])
    body += _digest_section_html(CATEGORY_TITLES["opportunity"], digest["sections"]["opportunity"])
    body += _digest_section_html(CATEGORY_TITLES["administrative"], digest["sections"]["administrative"])
    body += _digest_section_html(CATEGORY_TITLES["low"], digest["sections"]["low"],
                                 note="营销或低价值邮件统一放在这里；它们没有被丢弃，仍可在报告中追溯。")
    exceptions = [f'有 {digest["metrics"]["without_sources"]} 封邮件本次未取得可验证来源，已按邮件标注。'
                  ] if digest["metrics"]["without_sources"] else []
    if digest["metrics"]["duplicates"]:
        exceptions.append(f'已合并 {digest["metrics"]["duplicates"]} 封同类重复邮件，数量仍计入总数。')
    if not digest["items"]:
        exceptions.append("今天没有收到需要处理的新邮件。")
    body += _section("异常与整体说明 / Exceptions and notes",
                     _bullets_html(exceptions) or _paragraph_html("无异常。", muted=True))
    subtitle = f'{digest["metrics"]["total"]} 封邮件 · {digest["metrics"]["actionable"]} 件待办' \
               f' · 生成于 {format_moment(digest.get("generated_at"), digest.get("timezone"))}'
    return _email_shell(title, subtitle, body, footer=CONTENT_DISCLAIMER + " 每日简报在 22:00 生成。")


ANNOUNCEMENT_TONES = {
    "info": ("通知", "#123b63", "#eaf4fc"),
    "warn": ("提醒", "#8a6100", "#fff8e8"),
    "critical": ("重要", "#b42318", "#fff1f0"),
}


def announcement_subject(title: str) -> str:
    return f"【CityU Mail Pilot 公告】{_clean(title) or '来自管理员的通知'}"


def render_announcement_text(title: str, body: str) -> str:
    """Plain-text part of a broadcast.

    Deliberately plain: an announcement is read, not skimmed like a report, and
    a wall of formatted text in a personal inbox reads like marketing.
    """
    label = ANNOUNCEMENT_TONES.get("info", ANNOUNCEMENT_TONES["info"])[0]
    lines = [f"【{label}】{_clean(title)}", ""]
    for paragraph in str(body or "").splitlines():
        lines.append(paragraph.rstrip())
    lines += ["", "——", "这条消息由试点管理员发给所有试点用户。",
              "你也可以随时登录网页版查看：https://pilot.example.com/"]
    return "\n".join(lines).strip() + "\n"


def render_announcement_html(title: str, body: str, tone: str = "info") -> str:
    """HTML part of a broadcast, under the same strict email rules as reports.

    One 600px table, inline CSS duplicated as attributes, no JS, no <style>, no
    @media, no remote images — the constraint set is documented in
    ``docs/email-html-compatibility-2026-09-13.md``.
    """
    label, ink, background = ANNOUNCEMENT_TONES.get(tone, ANNOUNCEMENT_TONES["info"])
    safe_title = html.escape(_clean(title) or "来自管理员的通知")
    paragraphs = []
    for paragraph in str(body or "").split("\n"):
        text = html.escape(paragraph.strip())
        if text:
            paragraphs.append(
                f'<p style="margin:0 0 12px;font-size:15px;line-height:1.65;color:#243447">{text}</p>')
    return "".join([
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="background:#f4f7fb;padding:24px 12px"><tr><td align="center">',
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
        'style="width:600px;max-width:600px;background:#ffffff;border:1px solid #d9e2ec;'
        'table-layout:fixed;word-break:break-word">',
        f'<tr><td bgcolor="{ink}" style="background:{ink};padding:20px 28px;color:#ffffff">',
        f'<div style="font-size:11px;letter-spacing:.10em">CITYU MAIL PILOT · {label}</div>',
        f'<div style="font-size:20px;font-weight:700;margin-top:6px">{safe_title}</div></td></tr>',
        f'<tr><td bgcolor="{background}" style="background:{background};padding:22px 28px">',
        "".join(paragraphs),
        '</td></tr>',
        '<tr><td style="padding:16px 28px;border-top:1px solid #d9e2ec;font-size:12px;color:#64748b">',
        '这条消息由试点管理员发给所有试点用户。你也可以随时登录网页版查看。',
        '</td></tr></table></td></tr></table>',
    ])


def render_digest_text(digest: dict[str, Any], *, subject: str | None = None) -> str:
    title = _clean(subject) or f'每日简报 {digest.get("date", "")}'.strip()
    out = [title, "=" * min(len(title), 60),
           f"收到邮件 {digest['metrics']['total']} 封 · 需要行动 {digest['metrics']['actionable']} 项 · "
           f"异常/失败 {digest['metrics']['failed']} 封",
           f"最近截止时间：{digest['next_deadline'] or '无明确截止时间'}", ""]
    out.append("【今天/明天必须处理什么】")
    if digest["tasks"]:
        for index, task in enumerate(digest["tasks"][:8], 1):
            suffix = f"（截止：{task['deadline']}）" if task["deadline"] else ""
            out.append(f"{index}. {task['action']}{suffix} · 来自「{task['subject']}」")
    else:
        out.append("今天没有必须立刻处理的事项。")
    synthesis = str(digest.get("synthesis") or "").strip()
    if synthesis:
        out.append("")
        out.append(f"【{SYNTHESIS_HEADING}】")
        out.extend(synthesis.splitlines())
    for name in CATEGORY_ORDER:
        entries = digest["sections"][name]
        out.append("")
        out.append(f"【{CATEGORY_TITLES[name]}】")
        if not entries:
            out.append("- 无。")
            continue
        for entry in entries:
            repeat = f"（另有 {entry['duplicates']} 封同类）" if entry["duplicates"] else ""
            out.append(f"- {entry['subject']}{repeat} | 发件人：{entry['sender'] or '未知'} | "
                       f"收件：{entry['received_display']} | {entry['priority_label']}")
            out.append(f"  {entry['conclusion']}")
            for action in entry["actions"][:4]:
                deadline = deadline_of(action)
                out.append(f"  · 待办：{action}" + (f"（截止 {deadline}）" if deadline else ""))
            if entry["status"] != "sent":
                out.append(f"  · ⚠ 状态：{entry['status']} {entry['last_error'][:200]}")
    out.append("")
    out.append("【异常与整体说明】")
    if digest["metrics"]["without_sources"]:
        out.append(f"- 有 {digest['metrics']['without_sources']} 封邮件本次未取得可验证来源，未伪造引用。")
    if digest["metrics"]["duplicates"]:
        out.append(f"- 已合并 {digest['metrics']['duplicates']} 封同类重复邮件，数量仍计入总数。")
    if not digest["items"]:
        out.append("- 今天没有收到需要处理的新邮件。")
    out.append("")
    out.append(CONTENT_DISCLAIMER)
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# browser preview (used only for responsive checks; never sent as email)
# --------------------------------------------------------------------------- #

PREVIEW_CSS = """
  :root{color-scheme:light}
  *{box-sizing:border-box}
  body{margin:0;background:#e9eef3;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
  .frame{max-width:680px;margin:18px auto;background:#fff;border-radius:10px;overflow:hidden;
         box-shadow:0 10px 30px #0f274518}
  .frame > *{max-width:100%}
  .frame table{max-width:100%!important}
  img{max-width:100%;height:auto}
  .caption{margin:14px auto 6px;max-width:680px;font-size:13px;color:#475467;padding:0 12px}
  .stack{display:flex;flex-wrap:wrap;gap:18px;justify-content:center;padding:10px}
  .device{background:#cbd5e1;border-radius:18px;padding:8px}
  .device.mobile{width:390px}
  @media(max-width:520px){.device.mobile{width:100%}}
"""


# --------------------------------------------------------------------------- #
# entry helpers used by the delivery layer
# --------------------------------------------------------------------------- #

def render_immediate(markdown: str, message: dict[str, Any], *, subject: str,
                     timezone: str | None = None) -> dict[str, str]:
    parsed = parse_report(markdown, message=message, timezone=timezone, kind="immediate")
    return {
        "subject": subject,
        "html": render_immediate_html(parsed, subject=subject),
        "text": render_immediate_text(parsed, subject=subject),
        "priority": parsed["priority"],
        "deadline": parsed["deadline"],
    }


def render_digest(digest: dict[str, Any], *, subject: str) -> dict[str, str]:
    return {
        "subject": subject,
        "html": render_digest_html(digest, subject=subject),
        "text": render_digest_text(digest, subject=subject),
    }


def digest_subject(digest: dict[str, Any]) -> str:
    """A concrete, scannable subject line for the 22:00 brief."""
    date_label = digest.get("date") or ""
    metrics = digest.get("metrics", {})
    parts = [f"【CityU 每日简报】{date_label}".strip()]
    if metrics.get("total"):
        parts.append(f"{metrics['total']} 封邮件")
    if metrics.get("actionable"):
        parts.append(f"{metrics['actionable']} 件待办")
    if metrics.get("failed"):
        parts.append(f"{metrics['failed']} 封失败")
    return " · ".join(parts)


def preview_document(blocks: Sequence[tuple[str, str]]) -> str:
    parts = [
        '<!doctype html><html lang="zh-Hans"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>CityU Mail Pilot — report preview</title>',
        f"<style>{PREVIEW_CSS}</style></head><body>",
    ]
    for caption, markup in blocks:
        parts.append(f'<div class="caption">{html.escape(caption)}</div>')
        parts.append(f'<div class="frame">{markup}</div>')
    parts.append("</body></html>")
    return "".join(parts)

BRIEF_TRAILER = (
    "这是精简即时摘要。完整的中英双语报告（含邮件内容总结、与你的相关性、"
    "联网核实来源、风险与 AI 推测标注、English summary）稍后单独发送。"
)


def is_brief(markdown: str) -> bool:
    """True when this report only carries the condensed three sections."""
    text = _clean(markdown)
    if "## 7." in text or "English summary" in text or "Personal relevance" in text \
            or "与我的学业" in text:
        return False
    return "## 3. 邮件内容要点" in text or "Key points" in text


def render_brief_html(markdown: str, message: dict[str, Any], *, subject: str,
                      timezone: str | None = None) -> str:
    """Compact email for the condensed report: essentials only, no filler."""
    parsed = parse_report(markdown, message=message, timezone=timezone, kind="brief")
    title = _clean(subject) or parsed["subject"] or "邮件摘要"
    priority_badge = _badge(parsed["priority_label"], parsed["priority"])
    deadline = deadline_note(parsed["actions"][0]) if parsed["actions"] else ""
    rows = (
        '<tr><td style="padding:16px 20px 0">'
        f'<div>{priority_badge}</div>'
        f'<div style="font-size:13px;color:#475467;margin-top:10px">{html.escape(title)}</div>'
        + _meta_row(parsed) +
        '</td></tr>'
        '<tr><td style="padding:12px 20px 0">'
        + _callout("一句话结论 / In one line", parsed["conclusion"]) +
        '</td></tr>'
        '<tr><td style="padding:12px 20px 0">'
        + _callout(
            "你要做什么 / What to do",
            ("" if not parsed["actions"] else ""),
            background="#fff8e8", border="#f0d9a8", color="#8a6100",
        )
        + _bullets_html(parsed["actions"], ordered=True, show_deadline=True)
        + (f'<div style="font-size:12px;color:#8a6100;margin-top:4px">截止：{html.escape(deadline)}</div>'
           if deadline else "")
        + '</td></tr>'
        + _section("邮件内容要点 / Key points",
                   _bullets_html(_bullets(parsed["summary"])) or _paragraph_html(parsed["summary"], muted=True))
        + '<tr><td style="padding:14px 20px 0">'
        + _paragraph_html(BRIEF_TRAILER, size=12, muted=True)
        + '</td></tr>'
    )
    subtitle = f'精简即时摘要 · {parsed["received_display"]}'
    return _email_shell(title, subtitle, rows)


def render_brief_text(markdown: str, message: dict[str, Any], *, subject: str,
                      timezone: str | None = None) -> str:
    parsed = parse_report(markdown, message=message, timezone=timezone, kind="brief")
    title = _clean(subject) or parsed["subject"] or "邮件摘要"
    out = [title, "=" * min(len(title), 60),
           f"重要程度：{parsed['priority_label']}",
           f"结论：{parsed['conclusion']}",
           f"发件人：{parsed['sender_name'] or parsed['sender_address'] or '未知'}",
           f"收件时间：{parsed['received_display']}", "", "【你要做什么】"]
    if parsed["actions"]:
        for index, action in enumerate(parsed["actions"], 1):
            note = deadline_note(action)
            out.append(f"{index}. {action}" + (f"（截止：{note}）" if note else ""))
    else:
        out.append("无需行动。")
    out += ["", "【邮件内容要点】"]
    out.extend(f"- {item}" for item in (_paragraphs(parsed["summary"]) or ["未提供。"]))
    out += ["", BRIEF_TRAILER, "", CONTENT_DISCLAIMER]
    return "\n".join(out)


def render_brief(markdown: str, message: dict[str, Any], *, subject: str,
                 timezone: str | None = None) -> dict[str, str]:
    return {
        "subject": subject,
        "html": render_brief_html(markdown, message, subject=subject, timezone=timezone),
        "text": render_brief_text(markdown, message, subject=subject, timezone=timezone),
    }

