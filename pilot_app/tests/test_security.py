import base64
import os
import secrets
import unittest

from pilot_app.security import SecretBox, SecurityError, hash_password, validate_outbound_https_url, validate_public_host, verify_password


class SecurityTests(unittest.TestCase):
    def test_password_hash_is_salted_and_verifiable(self):
        first = hash_password("a sufficiently long password")
        second = hash_password("a sufficiently long password")
        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("a sufficiently long password", first))
        self.assertFalse(verify_password("wrong password", first))

    def test_rejects_private_and_insecure_api_urls(self):
        for value in ("http://api.example.com/v1", "https://localhost/v1", "https://127.0.0.1/v1", "https://10.0.0.2/v1"):
            with self.subTest(value=value), self.assertRaises(SecurityError):
                validate_outbound_https_url(value, resolve_dns=False)

    def test_rejects_private_mail_hosts(self):
        for value in ("localhost", "127.0.0.1", "10.0.0.3", "bad_host"):
            with self.subTest(value=value), self.assertRaises(SecurityError):
                validate_public_host(value, resolve_dns=False)

    def test_secret_context_prevents_cross_user_decryption(self):
        try:
            import cryptography  # noqa: F401
        except ImportError:
            self.skipTest("cryptography is installed by requirements.txt")
        box = SecretBox(secrets.token_bytes(32))
        encrypted = box.encrypt("private-api-key", context="connection:user-a:model")
        self.assertEqual(box.decrypt(encrypted, context="connection:user-a:model"), "private-api-key")
        with self.assertRaises(SecurityError):
            box.decrypt(encrypted, context="connection:user-b:model")


if __name__ == "__main__":
    unittest.main()
