"""Tests for the illustrated forwarding tutorial (desktop Outlook).

Desktop Outlook has no 「转发」 switch at all -- it forwards with a **rule**, and
the app used to describe that path in one line of prose ("新建规则 → 对所有收到
的邮件 → 转发到私人邮箱"). Following that line, people stop at the condition
dropdown: 「所有邮件」 is not at the top of it, it is at the very bottom.

So the operator walked the path himself and sent four screenshots. They are of a
**real account**, which is why this file exists:

* The account line (his name and school address) is **excluded by the crop**,
  not painted over -- `tools/forward_shots.js` refuses to run if a crop reaches
  up into it. Excluding a field cannot be one pixel wrong.
* The forwarding address in the finished-rule shot **is** painted over.
* The privacy scanner in `publish_export.py` cannot see inside an image, so
  nothing else in this repository would catch a screenshot that leaks an
  address. Hence the sha256 pins below: they are the record that a human looked
  at these exact bytes. **Regenerating the images fails this suite on purpose** --
  look at them again, then update the hashes.
"""

import hashlib
import os
import pathlib
import re
import unittest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
TOOL = pathlib.Path(__file__).resolve().parents[2] / "tools" / "forward_shots.js"

# sha256 of the shipped files. See the module docstring: changing these is a
# deliberate act that requires looking at the new picture.
SHIPPED = {
    "forward-rule-1-add.png":
        "8e430f19f147b7e5f319a36dcee1b2b550dda0b1ac6ef68683481849d96f80b3",
    "forward-rule-2-condition.png":
        "e7d2b67eb98d5f1e00d89ae2d3ff7ffd10a27393305df080a4c81ed3e4481d63",
    "forward-rule-3-action.png":
        "24cff3d5250aeb009c0159f728899b2fa1ea6ad813b4da39c9318a6a44115a25",
    "forward-rule-4-done.png":
        "2d17034ae091e63157bd397fdd88359b7e6fbd75000a34c95a17e19128c7a5ce",
}

# A screenshot that grew past this is either not the cropped pane or not scaled.
SIZE_CAP = 200 * 1024


class ShippedImageTests(unittest.TestCase):
    def test_every_image_is_a_png_of_a_sane_size(self):
        for name in SHIPPED:
            path = STATIC / name
            self.assertTrue(path.is_file(), f"{name} 不在 static/ 里")
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"), f"{name} 不是 PNG")
            self.assertLess(len(raw), SIZE_CAP, f"{name} 超过 200 KB：{len(raw)}")

    def test_the_bytes_are_the_ones_a_human_looked_at(self):
        """Re-running the tool must not be able to ship an unreviewed picture."""
        for name, expected in SHIPPED.items():
            digest = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()
            self.assertEqual(
                digest, expected,
                f"{name} 变过了。**先打开图看一遍**（有没有带出真实姓名/邮箱），"
                f"确认干净再把这个测试里的 sha256 改成 {digest}")

    def test_the_files_are_served_not_just_on_disk(self):
        """On disk is not enough: static files come from an allowlist."""
        from pilot_app import web
        for name in SHIPPED:
            self.assertIn(f"/{name}", web.STATIC_FILES, name)
            self.assertEqual(web.STATIC_FILES[f"/{name}"][1], "image/png")


class TutorialMarkupTests(unittest.TestCase):
    def _page(self) -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    def test_the_forwarding_step_shows_all_four_pictures(self):
        page = self._page()
        for name in SHIPPED:
            self.assertIn(f'src="/{name}"', page, name)

    def test_every_picture_has_alt_text_and_a_caption(self):
        page = self._page()
        figures = re.findall(r'<figure class="shot">(.*?)</figure>', page, re.S)
        self.assertEqual(len(figures), 4, "四张图各是一个 figure")
        for figure in figures:
            image = re.search(r"<img[^>]*>", figure).group(0)
            self.assertRegex(image, r'alt="[^"]{8,}"', image[:60])
            self.assertIn("figcaption", figure)

    def test_the_step_that_people_get_stuck_on_says_where_the_item_is(self):
        """「适用于所有邮件」 sits at the bottom of the dropdown, not the top."""
        page = self._page()
        self.assertIn("适用于所有邮件", page)
        self.assertRegex(page, r"拉到最下面|列表最下面")

    def test_the_page_says_the_screenshots_are_redacted(self):
        """A masked screenshot that does not say it is masked looks like a bug."""
        page = self._page()
        self.assertRegex(page, r"已经打码|已打码")

    def test_the_tutorial_is_collapsed_by_default(self):
        """It is the fallback path; the switch path stays the first thing read."""
        page = self._page()
        step = page[page.index('id="step-2"'):page.index('id="step-3"')]
        self.assertIn("<details", step)
        # …and it is a real collapse: the first thing in step 2 is the switch
        # path, and the pictures only appear after the summary.
        self.assertLess(step.index("打开「邮件 → 转发」"), step.index("<details"))


class GeneratorTests(unittest.TestCase):
    """The tool is the only path from the raw screenshots to the shipped files."""

    def test_the_crop_guard_is_still_there(self):
        source = TOOL.read_text(encoding="utf-8")
        self.assertIn("ACCOUNT_CHIP", source)
        # The guard itself, not just the constant: a crop that reaches the
        # account line has to abort the run.
        self.assertRegex(source, r"spec\.crop\.y < chipBottom")
        self.assertIn("process.exit(3)", source)

    def test_the_address_mask_covers_the_field_it_claims_to(self):
        source = TOOL.read_text(encoding="utf-8")
        self.assertIn("RULE_ADDRESS", source)
        self.assertIn("RULE_LABEL", source)
        # The finished-rule page is the one that has an address in frame.
        self.assertRegex(source, r"source: '1-result\.jpg'[\s\S]{0,400}masks: \[\{ rect: RULE_ADDRESS")


if __name__ == "__main__":
    unittest.main()
