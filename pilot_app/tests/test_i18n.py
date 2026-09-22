"""`i18n.t()`：**查不到译文时，占位符也必须被替换**。

这条测试存在的唯一理由是一次真实的生产事故（2026-09-23）：`t()` 在「词典里没有这条
文案」时直接 `return text`，把参数替换整段跳过了。而中文是原文语、`catalog("zh")`
按设计是空词典——**那正是绝大多数访问者走的那条路**，于是：

* 客服群那一节印出 `这张码 {when}有效`（少了「9 月 29 日前」）；
* 安卓按钮印出 `下载安卓安装包（{size}）`。

判据写成三条，因为它们各自会被一种"看起来像修好了"的改法绕过：

1. 空词典（= 中文原文）下带占位符 → 占位符必须没了；
2. 命中了译文时 → 译文里的占位符照样要替换（这条原来就是对的，别改坏）；
3. 文案里出现**不是占位符**的花括号（CSS/模板片段）→ 原样保留、**不抛异常**。
   这是 `t()` 刻意不用 `str.format` 的理由，别为了图省事换回去。
"""

from __future__ import annotations

import unittest

from pilot_app import i18n


class PlaceholderTests(unittest.TestCase):
    def test_a_missing_translation_still_gets_its_parameters_filled_in(self):
        """空词典是**正常路径**（中文原文），不是异常分支。"""
        self.assertEqual(i18n.catalog(i18n.DEFAULT_LOCALE), {},
                         "中文词典按设计是空的；这条测试的前提就是它")
        self.assertEqual(
            i18n.t("这张码 {when}有效", i18n.DEFAULT_LOCALE, when="9 月 29 日前"),
            "这张码 9 月 29 日前有效")
        self.assertEqual(
            i18n.t("下载安卓安装包（{size}）", i18n.DEFAULT_LOCALE, size="8.4 MB"),
            "下载安卓安装包（8.4 MB）")

    def test_a_translated_string_also_gets_its_parameters_filled_in(self):
        """命中了译文时同样要替换——这条原来就是对的，钉住它别被改回去。"""
        original = i18n.catalog
        i18n.catalog = lambda locale: {"你好 {name}": "Hello {name}"}  # type: ignore[assignment]
        try:
            self.assertEqual(i18n.t("你好 {name}", "en", name="小明"), "Hello 小明")
        finally:
            i18n.catalog = original  # type: ignore[assignment]

    def test_a_brace_that_is_not_a_placeholder_survives(self):
        """不是占位符的花括号原样留下，而且**不许抛异常**（`str.format` 会 500）。"""
        text = "html[data-theme=\"dark\"] { --ink: #111 }"
        self.assertEqual(i18n.t(text, i18n.DEFAULT_LOCALE), text)
        self.assertEqual(i18n.t(text, i18n.DEFAULT_LOCALE, unused="x"), text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
