"""Mailbox preset and onboarding-help tests."""

import unittest

from pilot_app import mailpresets
from pilot_app.security import SecurityError, validate_public_host


class MailboxPresetTests(unittest.TestCase):
    def test_domain_lookup_matches_known_providers(self):
        cases = {
            "someone@qq.com": "qq",
            "Someone@Foxmail.com": "qq",
            "a@163.com": "163",
            "a@yeah.net": "163",
            "a@gmail.com": "gmail",
            "a@icloud.com": "icloud",
            "a@outlook.com": "outlook",
            "a@yahoo.com.hk": "yahoo",
            "student@my.cityu.edu.hk": "custom",
            "a@some-company.example": "custom",
            "": "custom",
            "not-an-email": "custom",
        }
        for email, expected in cases.items():
            self.assertEqual(mailpresets.preset_id_for_email(email), expected, email)

    def test_every_preset_is_complete_and_public(self):
        ids = [item["id"] for item in mailpresets.MAILBOX_PRESETS]
        self.assertEqual(len(ids), len(set(ids)), "preset ids must be unique")
        self.assertIn("custom", ids)
        for item in mailpresets.MAILBOX_PRESETS:
            self.assertTrue(item["label"], item["id"])
            self.assertTrue(item["steps"], item["id"])
            for key in ("imap_port", "smtp_port"):
                self.assertIsInstance(item[key], int, item["id"])
                self.assertTrue(1 <= item[key] <= 65535, item["id"])
        # Every field exposed to the browser must be safe to publish: assert the
        # exact key set rather than keyword-scanning (the glossary legitimately
        # has a key named "password").
        published = mailpresets.public_mailbox_help()
        allowed = {"id", "label", "domains", "imap_host", "imap_port", "smtp_host",
                   "smtp_port", "steps", "help_url", "help_label", "caution"}
        for item in published["presets"]:
            self.assertEqual(set(item), allowed, item["id"])
        self.assertEqual(set(published), {"presets", "glossary"})

    def test_known_hosts_pass_outbound_validation_without_dns(self):
        for item in mailpresets.MAILBOX_PRESETS:
            if item["id"] == "custom":
                continue  # intentionally blank: the user fills their own
            for key in ("imap_host", "smtp_host"):
                host = item[key]
                self.assertEqual(validate_public_host(host, resolve_dns=False), host, host)

    def test_private_hosts_are_still_rejected(self):
        for bad in ("localhost", "imap.local", "127.0.0.1"):
            with self.assertRaises(SecurityError):
                validate_public_host(bad, resolve_dns=False)

    def test_glossary_explains_jargon_in_plain_language(self):
        glossary = mailpresets.GLOSSARY
        for key in ("imap", "smtp", "password"):
            self.assertIn(key, glossary)
            self.assertTrue(glossary[key]["term"])
            self.assertTrue(glossary[key]["plain"])
        # The point of the glossary is that it is *not* the technical label.
        self.assertEqual(glossary["imap"]["term"], "收件服务器")
        self.assertNotEqual(glossary["imap"]["term"], glossary["imap"]["technical"])


if __name__ == "__main__":
    unittest.main()
