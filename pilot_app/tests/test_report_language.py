"""报告语言：**正文**用哪种语言写（2026-09-23）。

背景：界面上早就有「报告语言」这个下拉，它也真的作为 `preferred_language` 进了资料块
—— 但**没有任何一句指令要求模型照它写**，所以那个下拉基本是装饰。这一轮：

* 语言合并成一个设置：`users.ui_locale`（界面切换器写的）就是报告语言；
  老的 `profiles.language` 仍然认（选了 English 的人不该被悄悄换回中文）；
* `prompts.language_block()` 把这件事**写成指令**；
* 第 7 节从「固定的 English summary」改成「**另一种语言**的简短小结」。

三条判据都钉在「默认路径一个字都没变」上：不传 `locale` 时，prompt 的章节标题、
`normalize_report` 发射的标题、`report_locale` 的答案全部与改造前一致 ——
两千多条既有测试与 13 位存量用户的报告都不该因为这一轮改动而动。
"""

import json
import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ.setdefault("INFE_PILOT_DB", _TMP + "/reportlang.sqlite3")
os.environ.setdefault("INFE_PILOT_MASTER_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ.setdefault("INFE_PILOT_COOKIE_SECURE", "0")
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import database as database_mod  # noqa: E402
from pilot_app import prompts  # noqa: E402

PROFILE_DEFAULTS = {
    "school_email": "", "major": "", "year_of_study": "", "courses": [], "interests": [],
    "career_goals": [], "focus_topics": [], "less_interested": [], "custom_instructions": "",
    "language": "bilingual", "timezone": "Asia/Hong_Kong", "theme": "paper", "background": "",
    "report_mode": "",
}


class PromptLanguageTests(unittest.TestCase):
    def test_the_language_is_actually_an_instruction_now(self):
        """**这是这一轮的重点**：语言必须出现在指令里，而不只是资料里的一行 JSON。"""
        for locale, name in (("zh-Hans", "简体中文"), ("zh-Hant", "繁體中文"), ("en", "English"),
                             ("ja", "日本語"), ("ko", "한국어")):
            block = prompts.language_block(locale)
            self.assertIn("<OUTPUT_LANGUAGE>", block)
            self.assertIn(name, block, locale)
        # 认不出的语言回落中文，而不是造出一句「用 None 写」。
        self.assertIn("简体中文", prompts.language_block("klingon"))

    def test_the_prompts_carry_it(self):
        profile = {"language": "bilingual"}
        message = {"subject": "s", "sender_name": "n", "sender_address": "a@cityu.edu.hk",
                   "received": "2026-09-23T00:00:00+00:00"}
        prompt = prompts.immediate_prompt(profile, message, [], "no search", locale="ja")
        self.assertIn("<OUTPUT_LANGUAGE>", prompt)
        self.assertIn("日本語", prompt)
        brief = prompts.brief_prompt(profile, message, [], "no search", locale="en")
        self.assertIn("<OUTPUT_LANGUAGE>", brief)

    def test_the_other_language_section_follows_the_report_language(self):
        """第 7 节是**另一种语言**的小结，不是固定的英文。

        选英文的人原来会看到一节叫「English summary」的英文小结 —— 重复，而且标题
        是假话。现在中文报告附英文、英文报告附中文。
        """
        self.assertEqual(prompts.immediate_sections("zh-Hans")[-1], "## 7. English summary")
        self.assertEqual(prompts.immediate_sections("zh-Hant")[-1], "## 7. English summary")
        self.assertEqual(prompts.immediate_sections("ja")[-1], "## 7. English summary")
        self.assertEqual(prompts.immediate_sections("ko")[-1], "## 7. English summary")
        self.assertIn("中文", prompts.immediate_sections("en")[-1])
        # 前 6 节一个不动：改的只有第 7 节那一行标题。
        self.assertEqual(prompts.immediate_sections("en")[:-1], prompts.IMMEDIATE_SECTIONS[:-1])

    def test_the_normaliser_emits_the_matching_heading(self):
        """`normalize_report` 会把标题**重写回**它认定的那一套，所以它也得知道语言。

        忘了传的话，英文报告的第 7 节会被改回「English summary」—— 标题与内容对不上。
        """
        raw = ("## 1. 重要程度与一句话结论\n- 等级：高\n\n"
               "## 7. English summary\n- Submit by Friday.\n")
        default = prompts.normalize_report(raw)
        english = prompts.normalize_report(raw, locale="en")
        self.assertIn("## 7. English summary", default)
        self.assertIn("中文", english)
        self.assertNotIn("## 7. English summary", english)

    def test_the_default_path_is_byte_identical(self):
        """不传 `locale` 时，章节标题与改造前逐字相同。"""
        self.assertEqual(prompts.immediate_sections(), prompts.IMMEDIATE_SECTIONS)
        self.assertEqual(prompts.DEFAULT_REPORT_LANGUAGE, "zh-Hans")


class ReportLocaleTests(unittest.TestCase):
    """一个设置管两件事：界面语言与报告语言。"""

    @classmethod
    def setUpClass(cls):
        cls.db = database_mod.Database(os.path.join(_TMP, "report-locale.sqlite3"))
        cls.db.initialize()

    def user(self, email: str) -> str:
        return self.db.create_user(email, "x" * 20, "")["id"]

    def test_nothing_chosen_means_chinese(self):
        """**存量用户一个字都不变**：老默认 `bilingual` 的含义正是中文正文 + 英文小结。"""
        user_id = self.user("a@example.com")
        self.assertEqual(self.db.report_locale(user_id), "zh-Hans")

    def test_the_ui_switch_decides_the_report_language(self):
        user_id = self.user("b@example.com")
        for code in ("en", "zh-Hant", "ja", "ko", "zh-Hans"):
            self.db.set_ui_locale(user_id, code)
            self.assertEqual(self.db.report_locale(user_id), code)

    def test_the_old_profile_choice_is_still_honoured(self):
        """选了 English 的人不该因为这次合并被悄悄换回中文。"""
        user_id = self.user("c@example.com")
        self.db.upsert_profile(user_id, dict(PROFILE_DEFAULTS, language="en"))
        self.assertEqual(self.db.report_locale(user_id), "en")
        # 而新切换器一旦表过态，它说了算。
        self.db.set_ui_locale(user_id, "ja")
        self.assertEqual(self.db.report_locale(user_id), "ja")
        self.db.set_ui_locale(user_id, "")
        self.assertEqual(self.db.report_locale(user_id), "en")
        # `bilingual` 是历史默认值，落在兜底上（= 中文），不是「英文」。
        self.db.upsert_profile(user_id, dict(PROFILE_DEFAULTS, language="bilingual"))
        self.assertEqual(self.db.report_locale(user_id), "zh-Hans")

    def test_an_unknown_account_does_not_crash(self):
        self.assertEqual(self.db.report_locale("usr_nope"), "zh-Hans")


if __name__ == "__main__":
    unittest.main()
