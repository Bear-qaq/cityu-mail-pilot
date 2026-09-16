"""Tests for the invite mail's deliverability hygiene.

The report says the same thing every day and the recipient is expecting it; the
**invite** is the one message that arrives out of the blue, and it carries a
short token plus a link. That is the exact shape of verification spam, so it is
the message most likely to be filed as junk -- and when it is, the applicant
concludes the pilot ignored them.

What this file can and cannot pin is worth stating, because most of deliverability
is out of reach here:

* **Out of reach**: SPF, DKIM, DMARC and sender reputation. Every outgoing
  message borrows the operator's own consumer mailbox, so those verdicts belong
  to that provider. No test in this repository can move them.
* **In reach, and pinned below**: the message must read like a person wrote it
  (display name, a reply invitation, a sign-off), stay short, keep the links to
  two, carry no shouting, and tell the reader where to look if it is not in the
  inbox. The site has to say the same thing at the moment the applicant is
  looking at the screen.

The last one is the only genuinely effective fix available: a junked message
cannot tell the reader it was junked, so the warning has to arrive *before* the
looking.
"""

import os
import pathlib
import re
import unittest

from pilot_app import alerting, mailio

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"


def invite_body(code: str = "AbC-123_xyz") -> tuple[str, str]:
    """The subject and body the applicant actually receives.

    Called on the real function rather than retyped here: a copy of the text
    would keep passing after somebody edited the message, which is the failure
    mode this whole file is about.
    """
    from pilot_app import web
    return web.invite_letter(code, origin="https://example.test")


class InviteShapeTests(unittest.TestCase):
    """The rules a first-contact message has to obey to look hand-written."""

    def test_the_body_keeps_the_links_down_to_two(self):
        """Every URL in a first-contact message is a spam signal."""
        _, body = invite_body()
        urls = re.findall(r"https?://\S+|(?<![\w.])/(?:app|privacy|terms)\b", body)
        self.assertLessEqual(len(urls), 2, urls)

    def test_it_carries_the_code_as_text(self):
        code = "Zz9-QQ_42"
        _, body = invite_body(code)
        self.assertIn(code, body)

    def test_it_does_not_shout(self):
        """No ALL-CAPS words -- the code is the only uppercase run allowed."""
        code = "Zz9-QQ_42"
        _, body = invite_body(code)
        without_code = body.replace(code, "")
        caps = [word for word in re.findall(r"[A-Za-z]{4,}", without_code) if word.isupper()]
        self.assertEqual(caps, [], f"全大写的词：{caps}")

    def test_it_asks_for_a_reply_and_signs_off(self):
        """A reply is the strongest 'this was wanted' signal a provider gets."""
        _, body = invite_body()
        self.assertIn("回这封信", body)
        self.assertTrue(body.rstrip().endswith(alerting.sender_name()),
                        "结尾应当是署名，而不是一句系统提示")

    def test_it_tells_the_reader_where_to_look(self):
        """The one fix that works: the warning arrives before the looking."""
        _, body = invite_body()
        self.assertIn("垃圾邮件", body)
        self.assertIn("不是垃圾邮件", body)

    def test_it_stays_short(self):
        """A long body with several sections is what a bulk sender looks like."""
        _, body = invite_body()
        self.assertLess(len(body), 900, f"{len(body)} 字")

    def test_the_subject_is_not_a_system_notification(self):
        subject, _ = invite_body()
        self.assertIn("邀请码", subject)
        self.assertNotIn("【", subject)
        self.assertLess(len(subject), 30)

    def test_the_compliance_facts_survive_the_rewrite(self):
        """Shorter is not the same as quieter about who pays.

        The applicant may never open the site again; this letter is where they
        learn whose model account their mail will pass through.
        """
        _, body = invite_body()
        # 「立即清空」 is pinned by test_signup too; it is the retention promise,
        # and the first version of this rewrite dropped it while shortening.
        for fact in ("免费", "管理员", "自己的 key", "大模型服务商", "AI 生成", "立即清空"):
            self.assertIn(fact, body, fact)


class MessageHeaderTests(unittest.TestCase):
    """What actually goes on the wire (headers, not prose)."""

    def _message(self, **kwargs):
        import email
        sent = {}

        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def login(self, *a): pass
            def send_message(self, message):
                sent["raw"] = message
                return {}
            def ehlo(self): pass
            def starttls(self, **k): pass
            def quit(self): pass

        original_ssl, original_plain = mailio.smtplib.SMTP_SSL, mailio.smtplib.SMTP
        mailio.smtplib.SMTP_SSL = FakeSMTP
        mailio.smtplib.SMTP = FakeSMTP
        try:
            mailio.send_report(
                {"email": "sender@example.com", "report_to": "applicant@example.com",
                 "smtp_host": "smtp.example.com", "smtp_port": 465},
                "pw", "你要的 CityU Mail Pilot 邀请码", "body",
                text_body="body", html_body="<p>body</p>", **kwargs)
        finally:
            mailio.smtplib.SMTP_SSL, mailio.smtplib.SMTP = original_ssl, original_plain
        return email.message_from_string(sent["raw"].as_string())

    def test_the_sender_carries_a_display_name(self):
        message = self._message(from_name="CityU 邮件助手", reply_to="sender@example.com")
        raw = str(message["From"])
        self.assertIn("sender@example.com", raw)
        # A Chinese display name has to be RFC 2047 encoded, not sent raw.
        self.assertIn("=?", raw)
        self.assertEqual(str(message["Reply-To"]), "sender@example.com")

    def test_without_a_name_nothing_changes_for_the_report_path(self):
        message = self._message()
        self.assertEqual(str(message["From"]), "sender@example.com")
        self.assertIsNone(message["Reply-To"])

    def test_a_header_injection_in_the_name_is_removed_not_escaped(self):
        """The display name is operator-set text that goes into a header.

        An environment value containing a newline would end the header and
        whatever follows would be read as another one -- so control characters
        are dropped, not escaped.
        """
        os.environ[alerting.SENDER_NAME_ENV] = "CityU\r\nBcc: victim@example.com"
        try:
            self.assertEqual(alerting.sender_name(), "CityUBcc: victim@example.com")
        finally:
            os.environ.pop(alerting.SENDER_NAME_ENV, None)

    def test_it_is_still_a_plain_text_message_with_an_html_twin(self):
        message = self._message(from_name="CityU 邮件助手", reply_to="sender@example.com")
        kinds = [part.get_content_type() for part in message.walk()]
        self.assertIn("text/plain", kinds)
        self.assertIn("text/html", kinds)
        self.assertTrue(message["Message-ID"])


class SiteCopyTests(unittest.TestCase):
    """The warning has to be on the page, at the moment of applying."""

    def test_the_apply_section_says_where_to_look(self):
        page = (STATIC / "landing.html").read_text(encoding="utf-8")
        apply_section = page[page.index('id="apply"'):page.index('id="signup-form"')]
        self.assertIn("垃圾邮件", apply_section)
        self.assertIn("没有系统邮箱", apply_section)

    def test_the_confirmation_message_repeats_it(self):
        script = (STATIC / "landing.js").read_text(encoding="utf-8")
        self.assertIn("垃圾邮件", script)
        self.assertIn("不是系统邮箱", script)

    def test_the_console_tells_the_operator_what_to_do(self):
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("让他先看垃圾邮件", script)


if __name__ == "__main__":
    unittest.main()
