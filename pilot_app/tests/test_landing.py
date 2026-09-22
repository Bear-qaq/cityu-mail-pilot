"""Tests for the landing page's copy decisions (2026-09-16 optimisation round).

The landing page is the only thing a stranger sees, and every line on it was
written by the operator. So this file does not test "the HTML renders" -- it
pins the *editorial decisions* he made after reading eight annotated
screenshots, because each of them is the kind of thing that quietly comes back:

* **Surplus explanation.** "余剑篪（原话引用，只顺了标点）", "给结论的那部分由
  管理员出钱", "装法在下面那节里写了" -- each one is true, and each one dilutes
  the sentence next to it. Deleting them is the change; *re-adding* them is the
  regression, so the page is asserted to be free of them.
* **Where the converting sentence lives.** "手机上可以装成一个应用，课间看一眼
  就够" decides whether a reader becomes a user. It used to be the last grey line
  of "how it works", two screens down; it now sits in the hero, bold, with its
  own link into the install section. That is a position, not a wording, so the
  test asserts the position.
* **Jargon.** "只读收信（IMAP BODY.PEEK[]）" names the command we happen to use.
  The promises around it ("不删除", "没有任何遥测") are what the reader is
  checking for, so the test asserts the promises are still there *and* the
  jargon is not.

Deleting a fact is not the same as moving it, so the tests that remove a
surplus clause also assert the fact still exists where it is said properly --
otherwise a future round could "tidy up" the last mention of who pays.
"""

import os
import pathlib
import re
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ.setdefault("INFE_PILOT_DB", _TMP + "/landing.sqlite3")
os.environ.setdefault("INFE_PILOT_MASTER_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ.setdefault("INFE_PILOT_COOKIE_SECURE", "0")
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import web  # noqa: E402

STATIC = pathlib.Path(web.__file__).resolve().parent / "static"
TEMPLATE = STATIC / "landing.html"


def landing() -> str:
    """The page exactly as a visitor receives it, placeholders substituted."""
    return web.render_landing_page(TEMPLATE).decode("utf-8")


def without_comments(markup: str) -> str:
    """The page with HTML comments stripped.

    The template explains its own decisions in comments, and those comments do
    mention the jargon the visible list dropped. What a *visitor reads* is the
    thing under test, so the jargon check runs on the page with comments gone.
    """
    return re.sub(r"<!--.*?-->", "", markup, flags=re.S)


class HeroTests(unittest.TestCase):
    def test_the_hero_line_no_longer_explains_who_pays(self):
        """「内测期间免费」 is the whole sentence; the rest was surplus.

        "给结论的那部分由管理员出钱" is true, but in the hero nobody knows what
        「给结论的那部分」 means yet, and the fact is said properly twice further
        down. This asserts the surplus is gone from *this line* only.
        """
        page = landing()
        match = re.search(r'<p class="note" id="pilot-count">(.*?)</p>', page, re.S)
        self.assertIsNotNone(match, "首屏那句小字不见了")
        line = match.group(1)
        self.assertIn("内测期间免费", line)
        self.assertNotIn("管理员", line, "首屏又解释起谁出钱了")

    def test_removing_that_clause_did_not_remove_the_fact(self):
        """The clause was surplus, not the disclosure.

        Two places say it better, and both must survive: `#how` (which key is
        used by default) and the box before the signup form (whose account the
        calls land in). Dropping either one makes the page wrong, not shorter.
        """
        page = landing()
        self.assertIn("内测期间默认用管理员提供的模型 key", page)
        self.assertIn("管理员的模型账号", page)
        self.assertIn("换成你自己的 key", page)

    def test_the_pocket_sentence_is_in_the_hero_and_appears_once(self):
        """The converting sentence moved up; it must not be in both places.

        Position, not wording: it has to sit above the personal story (the top
        of the page), above `#how`, and carry its own way into the install
        section. Once moved, the old copy at the end of `#how` has to be gone --
        two copies of one sentence in one page is how a page starts disagreeing
        with itself.
        """
        page = landing()
        self.assertEqual(page.count("课间看一眼就够"), 1, "那句关于手机的话出现了不止一次")
        pitch_at = page.index('class="pitch"')
        self.assertLess(pitch_at, page.index("2025年，我一个人来到cityu"),
                        "这句还在故事下面，等于没提到首屏")
        self.assertLess(pitch_at, page.index('id="how"'))
        pitch = page[pitch_at:pitch_at + 400]
        self.assertIn('href="#download"', pitch, "加粗了却没有通往安装那一节的入口")
        self.assertIn("<b>", pitch, "这句没有加粗")

    def test_the_signature_carries_no_editorial_note(self):
        """「只留余剑篪」 -- the note about punctuation was for a proofreader."""
        page = landing()
        self.assertIn('<p class="sig">余剑篪</p>', page)
        self.assertNotIn("只顺了标点", page)
        # The story itself was not part of that instruction; it stays verbatim.
        self.assertIn("第一个sem，没有朋友，没有帮助，只有自己", page)

    def test_the_screenshot_caption_is_a_caption(self):
        page = landing()
        self.assertIn("软件每日推送消息真实运行界面（非效果图）", page)
        self.assertNotIn("这是它每天发给你的东西", page)


class ListingTests(unittest.TestCase):
    def test_the_privacy_list_is_written_for_a_student_not_an_engineer(self):
        """Plain language, without losing a single promise.

        Every jargon word here was the name of our own implementation; every
        promise next to it is what the reader actually checks. So both halves
        are asserted: the words are gone, the promises are not.
        """
        page = without_comments(landing())
        for jargon in ("BODY.PEEK", "IMAP", "发件域", "吊销全部会话",
                       "数据库里的邮件正文", "AES-256-GCM"):
            self.assertNotIn(jargon, page, f"首页又在说行话：{jargon}")
        for promise in ("不标记已读、不移动、不删除", "没有任何遥测",
                        "加密保存", "不用你的邮件训练模型",
                        "每一封都会写在日报里"):
            self.assertIn(promise, page, f"白话改写时弄丢了承诺：{promise}")
        # The algorithm is not dropped, it is moved: the policy states it exactly.
        self.assertIn("AES-256-GCM", (STATIC / "privacy.html").read_text(encoding="utf-8"))

    def test_the_install_section_keeps_the_phrases_the_browser_check_reads(self):
        """The browser suite reads four phrases out of this section by name.

        They are the four places a phone install actually goes wrong, so they
        are asserted here too: a copy edit that loses one of them would only be
        caught by a browser run otherwise.
        """
        page = landing()
        for text in ("允许安装未知应用", "添加到主屏幕", "必须用 Safari",
                     "看不到浏览器的地址栏"):
            self.assertIn(text, page)

    def test_every_install_step_opens_with_a_verb(self):
        """Step markers: the order is the content, so it must be scannable.

        Each `<li>` under `ol.steps` has to open with a bold verb ("下载",
        "允许安装", ...), so "what do I do at step 3" is answerable by scanning
        instead of by reading. Structural, because the point is that it holds
        for every step including ones added later.
        """
        page = landing()
        lists = re.findall(r'<ol class="steps">(.*?)</ol>', page, re.S)
        self.assertEqual(len(lists), 3, "三个安装流程各应有一份步骤清单")
        steps = [item for block in lists for item in re.findall(r"<li>(.*?)</li>", block, re.S)]
        self.assertGreaterEqual(len(steps), 10)
        for step in steps:
            self.assertTrue(step.lstrip().startswith("<b>"),
                            f"这一步没有粗体动词开头：{step.strip()[:40]}")

    def test_the_two_android_routes_say_how_they_differ(self):
        """方法一/方法二 said nothing about which one to pick."""
        page = landing()
        # The eyebrow used to read 「先装它」 -- written when this section came
        # *before* the form. Reordering the page made that sentence false, which
        # is how the assertion caught it; it now names the step that comes first.
        self.assertIn("拿到邀请码之后", page)
        self.assertIn("安卓 · 方法一：下载安装包", page)
        self.assertIn("功能最全", page)
        self.assertIn("安卓 · 方法二：用浏览器直接装", page)
        self.assertIn("最省事", page)

    def test_the_repository_block_keeps_its_shape_and_loses_a_type_size(self):
        """Only a reader who intends to self-host opens it -- so shrink, not cut.

        The block is AGPL-3.0 section 13's visible entry point, so it keeps its
        card, its links and its list; what changed is that it no longer competes
        with the prose for attention.
        """
        template = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("#source{", template)
        self.assertRegex(template, r"#source\{[^}]*font-size:14\.5px")
        self.assertIn("#source li{", template)
        self.assertRegex(template, r"#source li\{[^}]*font-size:14\.5px")

    def test_applying_explains_that_downloading_is_not_enough(self):
        """A visitor who just read the install steps thinks that is all there is.

        It is not: the tool needs a server that keeps reading his mail, so the
        pilot hands out an account, not a file. The old wording also promised
        that applying "验证了它能收信" -- that check happens later, when the
        auth code is entered, so the promise is gone.
        """
        page = landing()
        self.assertIn("光下载、装到手机上还用不了", page)
        self.assertIn("名额", page)
        self.assertIn("留一个邮箱，我人工看过之后把邀请码发给你", page)
        self.assertNotIn("顺便验证了它能收信", page)


class ApplyBeforeInstallTests(unittest.TestCase):
    """入口的顺序就是这条路的顺序：先拿到邀请码，再去装。

    「申请内测」那一节排在「装到手机上」前面，这件事 v0.63.51 就做了，浏览器
    套件也一直在量。但**对外的入口**还是反的：导航里「装到手机」在「申请内测」
    前面，首屏那句「看怎么装 →」又在申请按钮前面 —— 390px 真机上量过，申请按钮
    在 908px 处，而屏幕只有 844px 高，**第一屏上根本没有申请入口**。于是有人照着
    第一屏唯一那条链接去装，装好了打开软件，才撞上「邀请码」那一栏，再回头找
    门——找不到，来问了（原话「内测码最好放在下载之前不然找不到」）。

    所以这三条钉的是**入口**而不是章节：导航里的先后、首屏里的先后、以及直接
    落到安装那一节的人手边有没有一个真按钮。
    """

    def test_the_nav_offers_applying_before_installing(self):
        # **闭合标签要从开头之后找**（`index("</header>", start)`），不能拿整页第一个匹配。
        # 这条测试 2026-09-22 之前是红的，原因正是后者：页面 `<style>` 里那条讲窄屏抽屉的
        # CSS 注释为了说清 DOM 位置，把 header 的结束标签**照着标签的样子写了一遍**，
        # 于是 `page.index("</header>")` 找到的是那句注释，切片起点落在终点之后 —— `nav`
        # 恒为空串。**而它守的正是「导航里申请排在装到手机后面」这件事，红着就等于没人守。**
        # 同一天 `landing.html` 那条注释也改了措辞（不再写成标签的样子）。两处都改是有意的：
        # 注释里少一个雷是运气，取法不再依赖注释才是判据。
        page = landing()
        start = page.index('<header class="top"')
        nav = page[start:page.index("</header>", start)]
        self.assertIn('href="#apply"', nav)
        self.assertIn('href="#download"', nav)
        self.assertLess(nav.index('href="#apply"'), nav.index('href="#download"'),
                        "导航里「申请内测」又排到「装到手机」后面了")

    def test_the_first_screen_offers_applying_before_installing(self):
        page = landing()
        # `.lead` 是**契约**（`tools/landing_check.js` 按它量首屏按钮的坐标），但它
        # 同时还是样式钩子——外观重做时它多了个伴（`<div class="lead hero-copy">`）。
        # 所以按「class 里有 lead 这个词」找，而不是按那个精确字符串找：精确匹配会把
        # 「多挂一个类」当成「契约没了」。
        match = re.search(r'<div class="[^"]*\blead\b[^"]*">', page)
        self.assertIsNotNone(match, "首屏那块 .lead 不见了（landing_check 的契约）")
        hero = page[match.end():page.index('<hr class="rule">')]
        self.assertLess(hero.index('href="#apply"'), hero.index('class="pitch"'),
                        "首屏又先请人去看装法，申请按钮躲在它后面")
        # 申请按钮还在首屏那一组动作里，而且是第一个 —— 换掉它的位置等于把这条
        # 路的第一步藏起来。
        actions = hero[hero.index('class="actions"'):hero.index('class="pitch"')]
        self.assertLess(actions.index('href="#apply"'), actions.index('href="/demo"'))

    def test_the_install_section_opens_with_a_way_back(self):
        """直接落到安装那一节的人（导航、搜索、别人转的链接）看得到回头的路。"""
        page = landing()
        section = page[page.index('id="download"'):]
        head = section[:section.index('<ol class="steps">')]
        self.assertIn('<div class="need-invite">', head)
        callout = head[head.index('class="need-invite"'):head.index('</div>')]
        self.assertIn("还没有邀请码", callout)
        self.assertIn('href="#apply"', callout)
        # 一个按钮，不是一行灰色小字：这一节是「照做就行」的地方，而灰色小字在这里
        # 读起来像注释。
        self.assertIn('class="btn"', callout)
        self.assertNotIn('class="note"', callout)


class StillOpenFromTheSameAnnotations(unittest.TestCase):
    """The three things the second reading of the same screenshots turned up.

    The first pass (v0.63.48) shipped twelve changes. Re-reading the annotations
    found three that had been skipped or only half-done -- and each of them is
    the kind that is easy to *think* is done: a placeholder nobody re-reads, a
    question ("what happens after I apply?") that one sentence seemed to answer,
    and a request for visual step markers that was met with bold verbs only.
    """

    def test_the_guestbook_placeholder_is_not_small_talk(self):
        """「可以更专业」 -- the box a stranger types into sets the register."""
        page = landing()
        self.assertIn('placeholder="例如：哪一步卡住了', page)
        self.assertNotIn("用起来怎么样、哪里卡住了、想要什么功能", page)

    def test_applying_says_what_happens_next(self):
        """「申请内测以后会怎么样？有了名额会有什么不同？」"""
        page = landing()
        self.assertIn("申请之后：", page)
        self.assertIn("邀请码发到你留的邮箱", page)
        # …and what the quota actually buys, in the reader's terms.
        self.assertIn("这台服务器上的一个账号", page)

    def test_the_install_steps_carry_a_visible_step_marker(self):
        """「多一点步骤，比如手势那样的标识引导」.

        Done with CSS rather than an emoji or an icon font: one glyph renders
        differently on every system, and this page has no other emoji at all.
        The marker is what a reader sees; `list-style` must therefore be off, or
        the browser prints its own number next to ours.
        """
        template = TEMPLATE.read_text(encoding="utf-8")
        self.assertRegex(template, r"ol\.steps\{[^}]*list-style:none")
        self.assertRegex(template, r"ol\.steps\{[^}]*counter-reset:step")
        self.assertRegex(template, r"ol\.steps li::before\{[^}]*content:counter\(step\)")
        self.assertRegex(template, r"ol\.steps li\{[^}]*counter-increment:step")
        # The colour comes from the theme, not from a literal.
        self.assertRegex(template, r"ol\.steps li::before\{[^}]*background:var\(--accent\)")


class BoardAndGuestbookTests(unittest.TestCase):
    def test_the_bulletin_board_has_no_standing_explanation(self):
        """「布告栏」 plus the notice title already says what the blurb said."""
        board = web.render_bulletin([{
            "title": "本周维护", "body": "周日凌晨重启一次。", "tone": "info",
            "created_at": "2026-09-01T00:00:00Z",
        }])
        self.assertIn("布告栏", board)
        self.assertIn("本周维护", board)
        self.assertNotIn("运营者写给所有人的通知", board)

    def test_the_guestbook_intro_is_scannable(self):
        """The old sentence packed three facts into one dash.

        Who may write / who sees it first / why the wall stays clean are three
        separate points, so they are a sentence plus a list now. The limit on
        links lives in the form hint only -- one fact, one place.
        """
        page = landing()
        # 留言板 2026-09-20 从 hero 之后挪到了「申请内测名额」之后、安装说明之前
        # （原来紧跟 hero，陌生人的第三眼就是一张表单），所以切片终点跟着换成下载那一节。
        section = page[page.index('id="guestbook"'):page.index('id="download"')]
        self.assertIn("不用注册也能留言", section)
        self.assertIn("由运营者决定", section)
        self.assertIn("刊登时一律匿名，不会出现任何人的邮箱", section)
        self.assertNotIn("先只有我看到", section)
        self.assertEqual(section.count("最多 2 个链接"), 1, "链接上限被说了两遍")
        # One section, one voice: the intro was made professional, so the labels
        # and the empty-board line next to it stop saying 「我」.
        self.assertIn("只给运营者看", section)
        self.assertNotIn("只给我看", section)
        self.assertNotIn("留了我才能回你", section)

    def test_no_placeholder_reaches_a_visitor(self):
        page = landing()
        self.assertNotIn("{{", page)


if __name__ == "__main__":
    unittest.main()
