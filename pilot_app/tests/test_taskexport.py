"""The exported file is the one thing here a user opens on another device.

Nobody in this repository can watch an iPhone import the file, so what is pinned
down instead is everything about it that is checkable: that it is a real
iCalendar document (parsed back here, and checked once by hand against a
third-party parser), that its dates mean what the mail said, and -- the part
that matters most -- that text coming out of a mail report cannot turn into
calendar structure of its own.
"""

from __future__ import annotations

import datetime as dt
import re
import unittest

from pilot_app import taskexport as tx


def task(**overrides) -> dict:
    base = {
        "task_key": "a" * 32,
        "task_day": "2026-09-16",
        "action": "提交作业到 Canvas",
        "deadline": "9/18/2026 23:59",
        "priority": "high",
        "user_priority": "",
        "subject": "作业通知",
        "sender": "老师",
    }
    base.update(overrides)
    return base


def unfold(raw: str) -> list[str]:
    """Undo RFC 5545 folding, so the content can be compared literally."""
    lines: list[str] = []
    for piece in raw.split(tx.CRLF):
        if piece.startswith(" ") and lines:
            lines[-1] += piece[1:]
        elif piece:
            lines.append(piece)
    return lines


class StructureTests(unittest.TestCase):
    def test_it_is_a_well_formed_calendar(self):
        raw = tx.build_ics([task()], today=dt.date(2026, 9, 16))
        lines = unfold(raw)
        self.assertEqual(lines[0], "BEGIN:VCALENDAR")
        self.assertEqual(lines[-1], "END:VCALENDAR")
        self.assertEqual(lines.count("BEGIN:VEVENT"), 1)
        self.assertEqual(lines.count("END:VEVENT"), 1)
        self.assertIn("VERSION:2.0", lines)
        self.assertTrue(any(line.startswith("PRODID:") for line in lines))
        self.assertIn("X-WR-CALNAME:CityU Mail Pilot 待办", lines)

    def test_line_endings_are_crlf_only(self):
        """RFC 5545 wants CRLF, and some parsers are strict about it."""
        raw = tx.build_ics([task()], today=dt.date(2026, 9, 16))
        self.assertTrue(raw.endswith(tx.CRLF))
        self.assertNotIn("\n", raw.replace(tx.CRLF, ""))

    def test_one_event_per_task(self):
        raw = tx.build_ics([task(), task(task_key="b" * 32, action="别的事")],
                           today=dt.date(2026, 9, 16))
        self.assertEqual(unfold(raw).count("BEGIN:VEVENT"), 2)

    def test_the_uid_is_stable_so_re_exporting_updates_instead_of_duplicating(self):
        """Apple de-duplicates on UID -- that is what makes a second export safe."""
        first = tx.build_ics([task()], today=dt.date(2026, 9, 16))
        later = tx.build_ics([task(action="提交作业到 Canvas（改过措辞？没有）")],
                             today=dt.date(2026, 9, 17))
        self.assertIn(f"UID:{'a' * 32}@cityu-mail-pilot", unfold(first))
        self.assertIn(f"UID:{'a' * 32}@cityu-mail-pilot", unfold(later))


class DateTests(unittest.TestCase):
    def test_all_day_event_and_the_exclusive_end(self):
        """A one-day event on the 18th must END on the 19th.

        `DTEND` is exclusive for date-valued events; getting it wrong shows every
        task a day short, which is the kind of bug that looks like the user's
        mistake rather than ours.
        """
        raw = tx.build_ics([task()], today=dt.date(2026, 9, 16))
        lines = unfold(raw)
        self.assertIn("DTSTART;VALUE=DATE:20260918", lines)
        self.assertIn("DTEND;VALUE=DATE:20260919", lines)

    def test_all_day_means_no_timezone_at_all(self):
        """只有 DTSTAMP 是 UTC（它必须带 Z）；日期本身不带时区，也不该有 TZID。

        浮动时间会被手机按本地时区解释，而带 TZID 却没有配套 VTIMEZONE 的行为未定义
        —— 全天日期把这两个坑一起绕开了。
        """
        raw = tx.build_ics([task()], today=dt.date(2026, 9, 16))
        self.assertNotIn("TZID", raw)
        for line in unfold(raw):
            if line.startswith(("DTSTART", "DTEND")):
                self.assertFalse(line.endswith("Z"), line)
                self.assertRegex(line, r"^DT(?:START|END);VALUE=DATE:\d{8}$")

    def test_deadline_shapes(self):
        cases = {
            "9/18/2026 23:59": dt.date(2026, 9, 18),
            "2026/9/18": dt.date(2026, 9, 18),
            "2026年9月18日": dt.date(2026, 9, 18),
            "9月18日 23:59": dt.date(2026, 9, 18),
            "明天": dt.date(2026, 9, 17),
            "后天": dt.date(2026, 9, 18),
        }
        for deadline, expected in cases.items():
            with self.subTest(deadline=deadline):
                self.assertEqual(tx.event_day(task(deadline=deadline), today=dt.date(2026, 9, 16)),
                                 expected)

    def test_a_month_day_in_the_past_means_next_year(self):
        """A December mail about "1月5日" means January, not eleven months ago."""
        self.assertEqual(tx.event_day(task(deadline="1月5日"), today=dt.date(2026, 12, 20)),
                         dt.date(2027, 1, 5))

    def test_a_month_day_recently_past_stays_this_year(self):
        """The grace window: "9月14日" read on the 16th is two days ago, not next year."""
        self.assertEqual(tx.event_day(task(deadline="9月14日"), today=dt.date(2026, 9, 16)),
                         dt.date(2026, 9, 14))

    def test_unparseable_deadlines_fall_back_to_the_mail_s_day(self):
        for deadline in ("以邮件为准", "本周", "", "周五", "尽快"):
            with self.subTest(deadline=deadline):
                day = tx.event_day(task(deadline=deadline), today=dt.date(2026, 9, 16))
                self.assertEqual(day, dt.date(2026, 9, 16))

    def test_the_guessed_date_never_replaces_what_the_mail_said(self):
        """The deadline text stays in the title either way."""
        title = tx.line_for(task(deadline="以邮件为准"))
        self.assertIn("以邮件为准", title)


class TextTests(unittest.TestCase):
    def test_escaping_happens_at_the_ics_boundary(self):
        """`line_for` 给的是人读的文本（清单要粘得进去），转义只发生在 iCalendar 那一层。

        所以这里断言的是**文件里**那一行：逗号、分号、反斜杠按 RFC 5545 转义。
        """
        summary = [line for line in unfold(
            tx.build_ics([task(action="提交作业, 并附上;参考文献\\附录")],
                         today=dt.date(2026, 9, 16)))
            if line.startswith("SUMMARY:")][0]
        self.assertIn("\\,", summary)
        self.assertIn("\\;", summary)
        self.assertIn("\\\\", summary)
        # 给人看的那一份不带反斜杠：粘进提醒事项的清单里出现 `\,` 就是另一种错。
        self.assertNotIn("\\,", tx.line_for(task(action="提交作业, 并附上")))

    def test_a_newline_cannot_end_the_property_and_start_a_new_one(self):
        """The injection this file has to survive: a crafted action line.

        Everything in the SUMMARY comes from mail text that the model wrote from
        mail text we did not write. A raw CRLF would end our property and let the
        rest be read as calendar structure -- a second event, a URL, an attendee.
        """
        hostile = "看通知\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:evil\r\nSUMMARY:骗你的"
        raw = tx.build_ics([task(action=hostile, subject="x\r\nATTENDEE:mailto:a@b.c")],
                           today=dt.date(2026, 9, 16))
        lines = unfold(raw)
        self.assertEqual(lines.count("BEGIN:VEVENT"), 1, "注入没有多出一个事件")
        self.assertEqual(lines.count("END:VEVENT"), 1)
        self.assertNotIn("UID:evil", lines)
        self.assertFalse(any(line.startswith("ATTENDEE") for line in lines))

    def test_control_characters_are_dropped_rather_than_escaped(self):
        self.assertNotIn("\x00", tx.line_for(task(action="正常\x00文本")))

    def test_folding_keeps_every_line_within_seventy_five_octets(self):
        """Counted in octets: Chinese reaches the limit four times sooner."""
        raw = tx.build_ics([task(action="看课程安排" * 40)], today=dt.date(2026, 9, 16))
        for line in raw.split(tx.CRLF):
            self.assertLessEqual(len(line.encode("utf-8")), 75, line[:40])

    def test_folding_does_not_split_a_character_and_unfolds_back(self):
        action = "看课程安排" * 40
        raw = tx.build_ics([task(action=action)], today=dt.date(2026, 9, 16))
        summary = [line for line in unfold(raw) if line.startswith("SUMMARY:")]
        self.assertEqual(len(summary), 1)
        self.assertIn(action[:20], summary[0])
        self.assertEqual(summary[0].count("看课程安排"), 40)

    def test_the_checklist_has_one_line_per_task(self):
        text = tx.build_text([task(), task(task_key="b" * 32, action="预习第六章", deadline="明天")])
        self.assertEqual(len(text.splitlines()), 2)
        for line in text.splitlines():
            self.assertTrue(line.startswith("- [ ] "), line)

    def test_the_line_carries_the_deadline_when_there_is_one(self):
        self.assertIn("（截止 9/18/2026 23:59）", tx.line_for(task()))
        self.assertNotIn("（截止", tx.line_for(task(deadline="")))

    def test_a_deadline_the_action_already_states_is_not_repeated(self):
        """模型经常把截止时间写进动作本身，再补一句就成了两个互相矛盾的日期。

        浏览器套件里就是这么看见的：`…（截止：2026-10-06 23:59）（截止 2026/10/6 23:59）`
        ——两种写法在数字上其实是同一天，所以按数字比，不按文本比。
        """
        line = tx.line_for(task(action="阅读第 6 章并整理笔记（截止：2026-10-06 23:59）",
                                deadline="2026/10/6 23:59"))
        self.assertEqual(line.count("截止"), 1, line)
        self.assertIn("2026-10-06 23:59", line)

    def test_a_different_deadline_is_still_appended(self):
        line = tx.line_for(task(action="阅读第 6 章（截止：2026-10-06 23:59）",
                                deadline="10/9/2026 23:59"))
        self.assertEqual(line.count("截止"), 2, line)
        self.assertIn("10/9/2026 23:59", line)

    def test_a_deadline_without_numbers_is_kept(self):
        """「本周」「以邮件为准」没有数字，不能被当成「已经说过了」。"""
        self.assertIn("（截止 本周）", tx.line_for(task(action="交作业", deadline="本周")))


class PriorityTests(unittest.TestCase):
    def test_the_users_own_ranking_wins_over_the_models(self):
        """Two facts, one shown: theirs."""
        self.assertEqual(tx.effective_priority(task(priority="low", user_priority="high")), "high")
        self.assertEqual(tx.effective_priority(task(priority="high", user_priority="")), "high")
        self.assertEqual(tx.effective_priority(task(priority="unknown", user_priority="")), "unknown")

    def test_the_title_says_the_level_because_calendars_ignore_priority(self):
        self.assertTrue(tx.line_for(task(user_priority="high")).startswith("【急】"))
        self.assertTrue(tx.line_for(task(priority="low", user_priority="")).startswith("【缓】"))
        self.assertFalse(tx.line_for(task(priority="unknown")).startswith("【"))

    def test_ics_priority_follows_the_same_rule(self):
        raw = tx.build_ics([task(priority="low", user_priority="high"),
                            task(task_key="b" * 32, priority="unknown"),
                            task(task_key="c" * 32, priority="medium")],
                           today=dt.date(2026, 9, 16))
        self.assertEqual(unfold(raw).count("PRIORITY:1"), 1)
        self.assertEqual(unfold(raw).count("PRIORITY:5"), 1)
        self.assertNotIn("PRIORITY:9", unfold(raw))

    def test_an_unknown_priority_is_not_invented_into_one(self):
        self.assertFalse(any(line.startswith("PRIORITY")
                             for line in unfold(tx.build_ics([task(priority="unknown")],
                                                             today=dt.date(2026, 9, 16)))))


class FilenameTests(unittest.TestCase):
    def test_the_file_name_is_ascii_and_ends_in_ics(self):
        """iOS keys on the extension; a Chinese file name survives a download."""
        name = tx.filename("2026-09-16")
        self.assertEqual(name, "cityu-tasks-2026-09-16.ics")
        self.assertTrue(name.isascii())
        self.assertRegex(tx.filename(""), r"^cityu-tasks-\d{4}-\d{2}-\d{2}\.ics$")
        self.assertRegex(tx.filename("../../etc/passwd"), r"^cityu-tasks-[0-9-]+\.ics$")

    def test_nothing_in_the_builder_produces_a_path_separator(self):
        self.assertNotIn("/", tx.filename("2026/09/16").replace("cityu-tasks-", ""))
        self.assertNotIn("\\", tx.filename("2026\\09\\16"))

    def test_the_uid_is_not_repeated_inside_the_file(self):
        raw = tx.build_ics([task(), task(task_key="b" * 32)], today=dt.date(2026, 9, 16))
        uids = [line for line in unfold(raw) if line.startswith("UID:")]
        self.assertEqual(len(uids), len(set(uids)))

    def test_dtstamp_is_utc_and_well_formed(self):
        raw = tx.build_ics([task()], today=dt.date(2026, 9, 16),
                           now=dt.datetime(2026, 9, 16, 15, 0, tzinfo=dt.timezone.utc))
        stamps = [line for line in unfold(raw) if line.startswith("DTSTAMP:")]
        self.assertEqual(stamps, ["DTSTAMP:20260916T150000Z"])
        self.assertTrue(re.fullmatch(r"DTSTAMP:\d{8}T\d{6}Z", stamps[0]))


if __name__ == "__main__":
    unittest.main()
