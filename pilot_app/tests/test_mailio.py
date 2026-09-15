"""IMAP failure-message tests: pilot users must get actionable advice."""

import imaplib
import ssl
import unittest
from unittest import mock

from pilot_app import mailio
from pilot_app.mailio import explain_imap_failure, normalize_message


class ExplainImapFailureTests(unittest.TestCase):
    def test_basic_auth_disabled_points_at_another_provider(self):
        # Exactly what outlook.office365.com returns for a correct app password.
        message = explain_imap_failure(imaplib.IMAP4.error(b"Basic authentication is disabled."))
        self.assertIn("授权码", message)
        self.assertIn("QQ", message)
        self.assertIn("Gmail", message)
        self.assertNotIn("IMAP 连接失败", message)

    def test_bad_credentials_asks_for_a_new_app_password(self):
        message = explain_imap_failure(imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials"))
        self.assertIn("授权码", message)
        self.assertIn("重新生成", message)

    def test_unknown_user_mentions_the_address(self):
        message = explain_imap_failure(imaplib.IMAP4.error(b"LOGIN failed: Unknown user"))
        self.assertIn("邮箱地址", message)

    def test_network_problem_is_reported_as_network(self):
        message = explain_imap_failure(OSError("Connection refused"))
        self.assertIn("网络", message)

    def test_certificate_problem_mentions_tls(self):
        message = explain_imap_failure(ssl.SSLError("certificate verify failed"))
        self.assertIn("证书", message)

    def test_unknown_error_keeps_the_original_text(self):
        message = explain_imap_failure(imaplib.IMAP4.error(b"NO [SERVERBUG] something odd"))
        self.assertIn("IMAP 连接失败", message)
        self.assertIn("something odd", message)

    def test_qq_login_rejection_lists_the_real_causes(self):
        message = explain_imap_failure(imaplib.IMAP4.error(
            b"Login fail. Account is abnormal, service is not open, password is incorrect"
        ))
        self.assertIn("IMAP/SMTP", message)
        self.assertIn("重新生成授权码", message)

    def test_login_frequency_limit_asks_to_wait(self):
        message = explain_imap_failure(imaplib.IMAP4.error(b"Login frequency limited"))
        self.assertIn("15", message)

    def test_message_id_is_extracted_and_stable(self):
        """Message-ID identifies the mail no matter how often it was forwarded."""
        raw = (
            b'From: "Cap" <noreply_cap275421@cityu.edu.hk>\r\n'
            b"To: student@my.cityu.edu.hk\r\n"
            b"Subject: [CAP] Posting digest\r\n"
            b"Date: Sun, 13 Sep 2026 17:16:27 +0800\r\n"
            b"Message-ID: <20260913091828.5982EBAE32@smtp82.ad.cityu.edu.hk>\r\n"
            b"X-MS-Exchange-ForwardingLoop: ForwardingHandled;2109ce83\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\nbody\r\n"
        )
        first = normalize_message(raw)
        second = normalize_message(raw)
        self.assertEqual(first["message_key"], "<20260913091828.5982EBAE32@smtp82.ad.cityu.edu.hk>")
        self.assertEqual(first["message_key"], second["message_key"])

    def test_missing_message_id_yields_empty_key(self):
        raw = b"Subject: No id\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        self.assertEqual(normalize_message(raw)["message_key"], "")


class DisclaimerTests(unittest.TestCase):
    """Who is allowed to claim 「AI 生成内容可能出错」.

    Audit §5-1: the operator's own mails -- the invite code, the setup reminder,
    the unit-failure alert and the new-application notice -- all carried that
    sentence. It was appended by `markdown_to_html`, the *fallback* used when a
    caller passes no `html_body`, and every report and alert path passes its own
    rendering. So the only messages that ever reached that line were the four
    that no model had touched.

    The claim was not deleted, it moved to the code that writes the AI text
    (`reports.CONTENT_DISCLAIMER`). These tests hold both halves: the transport
    stays silent, and the report still says it.
    """

    def sent_html(self, html_body=None) -> str:
        """Run the real `send_report` against a stubbed SMTP; return the HTML part.

        Asserted on the assembled message rather than on `markdown_to_html`
        directly: "the fallback is clean" and "the mail the operator receives is
        clean" are two different claims, and only the second one was broken.
        """
        captured = {}

        class FakeSMTP:
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def login(self, *args): pass
            def send_message(self, message): captured["message"] = message

        with mock.patch.object(mailio.smtplib, "SMTP_SSL", FakeSMTP):
            mailio.send_report(
                {"email": "me@example.com", "report_to": "you@example.com",
                 "smtp_host": "smtp.example.com", "smtp_port": 465},
                "a-password", "主题", "正文", html_body=html_body)
        parts = [part for part in captured["message"].walk()
                 if part.get_content_type() == "text/html"]
        self.assertEqual(len(parts), 1, "应该正好有一个 HTML 部分")
        return parts[0].get_content()

    def test_an_operator_mail_carries_no_ai_disclaimer(self):
        """Passing no html_body is exactly how the invite and reminder mails go out."""
        html = self.sent_html()
        self.assertNotIn("AI 生成", html)
        self.assertIn("正文", html)

    def test_the_shell_is_still_a_well_formed_card(self):
        html = self.sent_html()
        self.assertIn("CITYU MAIL PILOT", html)
        self.assertEqual(html.count("<div"), html.count("</div>"),
                         "拿掉页脚那一段之后标签必须还是配平的")

    def test_a_caller_supplied_report_is_passed_through_untouched(self):
        """The transport must not edit text it did not write -- in either direction."""
        mine = "<div>报告正文</div><div>AI 生成内容可能出错；仅供参考。</div>"
        self.assertIn("AI 生成内容可能出错", self.sent_html(html_body=mine))

    def test_the_report_side_still_says_it(self):
        """Moved, not removed: the claim lives with the code that writes the AI text."""
        from pilot_app import reports
        self.assertIn("AI 生成内容", reports.CONTENT_DISCLAIMER)
        shell = reports._email_shell("标题", "副标题", "<tr><td>正文</td></tr>")
        self.assertIn(reports.CONTENT_DISCLAIMER, shell)


if __name__ == "__main__":
    unittest.main()
