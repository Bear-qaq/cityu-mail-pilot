"""IMAP failure-message tests: pilot users must get actionable advice."""

import imaplib
import ssl
import unittest

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


if __name__ == "__main__":
    unittest.main()
