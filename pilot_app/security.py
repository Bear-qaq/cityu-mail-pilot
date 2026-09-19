"""Authentication, secret encryption, and outbound URL safety helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import secrets
import socket
from dataclasses import dataclass
from urllib.parse import urlparse


class SecurityError(ValueError):
    """A configuration fails a security requirement."""


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    if len(password) < 12:
        raise SecurityError("密码至少需要 12 个字符。")
    salt = salt or secrets.token_bytes(16)
    iterations = 600_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)
    return "pbkdf2_sha256$" + str(iterations) + "$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        count = int(iterations)
        if count < 300_000 or count > 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), base64.urlsafe_b64decode(salt_text), count, dklen=32,
        )
        return hmac.compare_digest(actual, base64.urlsafe_b64decode(digest_text))
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# 临时密码的字符表：**故意去掉 0 O 1 l I**。这串东西要走的路是「运营者念出来／微信
# 发过去 → 用户在手机上敲一遍」，而 `0` 和 `O` 在这条路上分不清是最常见的一次失败。
# 它的表现是「用户说**还是**登不上」——我们会去查服务器，服务器一切正常，因为密码
# 本身只差一个字符。去掉这五个字符后 16 位仍有约 93 bit，换来这条通道少一个假故障。
#
# 它住在 `security.py` 而不是某个调用方：现在是**两个入口**（运营者的命令行
# `manage reset-password` 与后台的「重设密码」按钮），而「临时密码长什么样」必须是
# 一个定义——两处各写一份，迟早一份改了一份没改（这个项目已经栽过好几次）。
TEMPORARY_PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TEMPORARY_PASSWORD_LENGTH = 16


def generate_temporary_password(length: int = TEMPORARY_PASSWORD_LENGTH) -> str:
    """A password a person can retype from a chat message (see the table above)."""
    return "".join(secrets.choice(TEMPORARY_PASSWORD_ALPHABET) for _ in range(max(12, int(length))))


def new_token() -> str:
    return secrets.token_urlsafe(32)


def key_fingerprint(secret: str | bytes) -> str:
    """A short, hand-transcribable fingerprint of the master key.

    Uppercase base32 in three groups of four: 60 bits, which is far more than
    enough to tell two keys apart. The base32 alphabet is A-Z plus 2-7, so the
    digits 0, 1, 8 and 9 never appear -- which is what makes `O` and `I` readable
    off paper, because the digits they are usually confused with cannot occur.
    (`O` and `I` themselves are perfectly fine; the first version of this comment
    claimed they were excluded, and a test of that claim was wrong.)

    It reveals nothing useful about the key: 32 random bytes have no shortcut, and
    a truncated hash of them cannot be brute-forced or used to decrypt anything.

    What it is for: writing down **next to** the offline copy, so that "is the key
    in my hand the one these backups were made with?" is answered by reading twelve
    characters aloud -- instead of comparing secrets by eye or pasting one
    somewhere to check.
    """
    raw = _canonical_key_bytes(secret)
    digest = hashlib.sha256(raw).digest()
    encoded = base64.b32encode(digest).decode("ascii")[:12]
    return "-".join(encoded[index:index + 4] for index in range(0, 12, 4))


def _canonical_key_bytes(secret: str | bytes) -> bytes:
    """One key, one fingerprint -- whichever form it arrives in.

    `pilot.env` holds the base64 text; `SecretBox.key` holds the 32 decoded bytes.
    Hashing whatever happened to be passed produced **two different fingerprints
    for the same key**, which would have made the whole exercise useless: the
    operator writes down one string, and the server prints another.
    """
    if isinstance(secret, bytes):
        return secret
    text = secret.strip()
    try:
        decoded = base64.urlsafe_b64decode(text)
    except Exception:
        return text.encode("utf-8")
    # 32 bytes is what the master key is; anything else was not base64 key text.
    return decoded if len(decoded) == 32 else text.encode("utf-8")


@dataclass(frozen=True)
class SecretBox:
    """AES-256-GCM storage for per-user API keys and mailbox app passwords."""

    key: bytes

    @classmethod
    def from_base64(cls, value: str) -> "SecretBox":
        try:
            key = base64.urlsafe_b64decode(value.strip())
        except Exception as exc:
            raise SecurityError("INFE_PILOT_MASTER_KEY 不是有效的 Base64。") from exc
        if len(key) != 32:
            raise SecurityError("INFE_PILOT_MASTER_KEY 解码后必须正好为 32 字节。")
        return cls(key)

    def fingerprint(self) -> str:
        """Identify this key without revealing it. See :func:`key_fingerprint`."""
        return key_fingerprint(self.key)

    @classmethod
    def from_environment(cls) -> "SecretBox":
        value = os.environ.get("INFE_PILOT_MASTER_KEY", "")
        if not value:
            raise SecurityError("缺少 INFE_PILOT_MASTER_KEY。")
        return cls.from_base64(value)

    def encrypt(self, value: str, *, context: str) -> bytes:
        if not value:
            raise SecurityError("不能加密空密钥。")
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:  # pragma: no cover - dependency check path
            raise SecurityError("缺少 cryptography 依赖，不能安全保存密钥。") from exc
        nonce = secrets.token_bytes(12)
        encrypted = AESGCM(self.key).encrypt(nonce, value.encode("utf-8"), context.encode("utf-8"))
        return b"v1:" + base64.urlsafe_b64encode(nonce + encrypted)

    def anonymized(self, value: str) -> str:
        """A stable label for a client address, with no way back.

        Rate limiting and duplicate suppression need to recognise the same client
        again; they never need the address itself. A bare SHA-256 of an IPv4
        address is *not* anonymous -- the whole space is 2^32 and enumerable in
        minutes -- so this is a keyed digest, and the key is the process-wide
        master key, which is the only secret this program has.
        """
        return hmac.new(self.key, str(value or "").encode("utf-8"), hashlib.sha256).hexdigest()[:32]

    def decrypt(self, value: bytes, *, context: str) -> str:
        if not value.startswith(b"v1:"):
            raise SecurityError("未知的密钥密文版本。")
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            raw = base64.urlsafe_b64decode(value[3:])
            return AESGCM(self.key).decrypt(raw[:12], raw[12:], context.encode("utf-8")).decode("utf-8")
        except Exception as exc:
            raise SecurityError("无法解密密钥；主密钥或数据可能不匹配。") from exc


def validate_outbound_https_url(url: str, *, resolve_dns: bool = True) -> str:
    """Reject credentials, HTTP, and private-network targets to reduce SSRF risk."""
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise SecurityError("API Base URL 必须是完整的 HTTPS 地址。")
    if parsed.username or parsed.password:
        raise SecurityError("API Base URL 不能包含用户名或密码。")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise SecurityError("API Base URL 不能指向本机或局域网。")
    addresses: set[str] = set()
    try:
        addresses.add(str(ipaddress.ip_address(host)))
    except ValueError:
        if resolve_dns:
            try:
                addresses.update(item[4][0] for item in socket.getaddrinfo(host, parsed.port or 443))
            except socket.gaierror as exc:
                raise SecurityError("API 域名目前无法解析。") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise SecurityError("API Base URL 解析到了非公网地址。")
    return url.strip().rstrip("/")


def validate_public_host(host: str, *, resolve_dns: bool = True) -> str:
    """Validate an IMAP/SMTP hostname without allowing private network access."""
    host = host.strip().rstrip(".").lower()
    if not re_full_hostname(host):
        raise SecurityError("邮件服务器域名格式不正确。")
    if host == "localhost" or host.endswith(".local"):
        raise SecurityError("邮件服务器不能指向本机或局域网。")
    addresses: set[str] = set()
    try:
        addresses.add(str(ipaddress.ip_address(host)))
    except ValueError:
        if resolve_dns:
            try:
                addresses.update(item[4][0] for item in socket.getaddrinfo(host, 993))
            except socket.gaierror as exc:
                raise SecurityError("邮件服务器域名目前无法解析。") from exc
    if any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise SecurityError("邮件服务器解析到了非公网地址。")
    return host


def re_full_hostname(value: str) -> bool:
    if len(value) > 253 or not value:
        return False
    labels = value.split(".")
    return all(label and len(label) <= 63 and label[0].isalnum() and label[-1].isalnum()
               and all(char.isalnum() or char == "-" for char in label) for label in labels)
