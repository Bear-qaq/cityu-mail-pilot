"""Standard-library onboarding and dashboard API for the 3-5 user pilot.

Replaces the previous FastAPI/uvicorn layer: the audit found the pinned
Starlette branch carried public advisories and no compatible fixed release was
available, so the HTTP surface is implemented on ``http.server`` instead. Only
``cryptography`` remains as a runtime dependency.

The public API is unchanged:

* ``db`` / ``service`` module attributes for tests and admin tooling
* identical routes, JSON shapes, cookie name and ``{"detail": ...}`` errors
* same-origin Origin fence, security headers, login throttling and body limits
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import os
import re
import secrets
import signal
import sys
import threading
import time
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__ as VERSION
from . import agent as agent_mod
from . import alerting
from . import imageguard
from . import metrics as metrics_mod
from . import pricing as pricing_mod
from . import providers
from . import reports as reports_mod
from .database import Database, utc_now
from .mailpresets import public_mailbox_help
from .providers import (MODEL_PRESETS, SEARCH_PRESETS, normalized_model_config,
                        public_catalog, supports_native_search)
from .security import (
    SecretBox,
    SecurityError,
    hash_password,
    new_token,
    token_hash,
    validate_public_host,
    verify_password,
)
from .service import PilotService

SESSION_COOKIE = "cityu_mail_session"
SESSION_DAYS = 14
APP_ROOT = Path(__file__).resolve().parent

MAX_BODY_BYTES = 64 * 1024
# Background photos are the one request that is not JSON, so they get their own
# ceiling instead of raising the shared one. Raising MAX_BODY_BYTES would hand
# every JSON endpoint (login, signup, profile) a 1.5 MB read budget it has no use
# for, which is a denial-of-service surface bought for nothing.
MAX_BACKGROUND_BYTES = 1_500_000
MAX_JSON_DEPTH_ITEMS = 200

STATIC_ROOT = (APP_ROOT / "static").resolve()
STATIC_FILES: dict[str, tuple[str, str]] = {
    # The root is the marketing page a stranger lands on; the application lives
    # at /app. Keeping them apart is what lets one be indexable prose and the
    # other a single-page app, without either compromising for the other.
    "/": ("landing.html", "text/html; charset=utf-8"),
    "/app": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/landing.js": ("landing.js", "application/javascript; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/theme-boot.js": ("theme-boot.js", "application/javascript; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    # The legacy name iOS probes during Add-to-Home-Screen, seen 404ing in the
    # access log on 2026-09-14. Same bytes as the line above; the copies are
    # pinned identical by a test.
    "/apple-touch-icon-precomposed.png": ("apple-touch-icon-precomposed.png", "image/png"),
    # Deliberately NOT listed: /apple-touch-icon-120x120.png and its
    # "-precomposed" twin. iOS probes them too, but 120px is below the 180px an
    # iPhone home screen actually wants, and the probe order puts them *before*
    # apple-touch-icon.png -- answering 200 would win that race and replace a
    # sharp icon with an upscaled one. Only the two 180 names are served; the
    # reasoning and the log evidence are in docs/phone-install-2026-09-14.md.
    "/bg-paper.png": ("bg-paper.png", "image/png"),
    "/bg-dusk.png": ("bg-dusk.png", "image/png"),
    "/bg-harbour.png": ("bg-harbour.png", "image/png"),
    "/bg-night.png": ("bg-night.png", "image/png"),
    # Screenshots of the running app, shown on the landing page. Real ones: the
    # single strongest signal that a page is describing a product that exists is
    # being able to see it, and a marketing page that never shows the thing is
    # the shape every generated page has. Regenerate with tools/site_shots.js.
    "/app-tasks.png": ("app-tasks.png", "image/png"),
    "/app-tasks-phone.png": ("app-tasks-phone.png", "image/png"),
    "/privacy": ("privacy.html", "text/html; charset=utf-8"),
    "/terms": ("terms.html", "text/html; charset=utf-8"),
}

# The landing page carries a {{PILOT_COUNT}} placeholder so the "N accounts in
# use" sentence is read from the database at request time rather than typed into
# the file, where it would go stale the moment somebody else signed up.
# The legal pages carry {{CONTACT_LINK}}: the contact address is an operator
# setting, so a self-hoster must not inherit ours (and we must not publish theirs
# by accident). Every other static file is still served byte-for-byte.
TEMPLATED_STATIC = frozenset({"/", "/privacy", "/terms"})

# Shown instead of an address when the operator configured no contact channel.
# A privacy policy without a contact route is not a usable policy, so the gap is
# stated out loud rather than rendered as a dead mailto: link.
NO_CONTACT_NOTICE = "本实例的运营者（尚未配置联系邮箱）"

# Appearance is a per-user preference stored on the profile, so the same choice
# follows the account to another browser. The lists below are the only accepted
# values: anything else is rejected instead of being stored and later injected
# into a class name or a url().
THEMES: tuple[str, ...] = ("classic", "paper", "dusk", "harbour", "night")
BACKGROUNDS: tuple[str, ...] = ("", "paper", "dusk", "harbour", "night", "none", "custom")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    # img-src carries blob: because the background-photo preview is an object URL
    # built from the re-encoded image the browser just produced. That adds no
    # real reach: a blob URL can only be created by script, and script-src is
    # locked to 'self', so anything able to mint one is already running our code.
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "img-src 'self' data: blob:; connect-src 'self'"
    ),
}

AUTHENTICATED_METHODS = {"GET", "HEAD", "OPTIONS"}


# --------------------------------------------------------------------------
# errors and responses
# --------------------------------------------------------------------------


class ApiError(Exception):
    """An error that maps to a JSON ``{"detail": ...}`` response."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class Response:
    __slots__ = ("status", "body", "content_type", "headers", "cookies")

    def __init__(
        self,
        status: int = 200,
        body: bytes = b"",
        content_type: str = "application/json; charset=utf-8",
        headers: Optional[dict[str, str]] = None,
        cookies: Optional[list[str]] = None,
    ) -> None:
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = headers or {}
        self.cookies = cookies or []


def json_response(payload: Any, status: int = 200, *, cookies: Optional[list[str]] = None) -> Response:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return Response(status=status, body=body, cookies=cookies)


def error_response(status: int, detail: str, *, cookies: Optional[list[str]] = None) -> Response:
    return json_response({"detail": detail}, status=status, cookies=cookies)


def file_response(target: Path, content_type: str, *, download_name: str = "") -> Response:
    data = target.read_bytes()
    headers = {"Cache-Control": "no-cache"}
    if download_name:
        # `attachment` so no browser ever tries to render the bytes, and the
        # quotes are stripped because this value came from a file name: a stray
        # `"` would end the header early and let the rest be read as a new one.
        headers["Content-Disposition"] = (
            'attachment; filename="' + download_name.replace('"', "").replace("\\", "") + '"')
    return Response(status=200, body=data, content_type=content_type, headers=headers)


def contact_email() -> str:
    """The address published on the legal pages, or "" when none is set.

    ``INFE_PILOT_CONTACT_EMAIL`` exists so an operator can publish a dedicated
    address without making it their admin login (the admin list grants rights;
    the contact address is merely printed on a public page). Falling back to the
    first admin keeps single-operator installs working with no extra config.
    """
    configured = os.environ.get("INFE_PILOT_CONTACT_EMAIL", "").strip()
    if configured:
        return configured
    admins = sorted(alerting.admin_emails())
    return admins[0] if admins else ""


def render_legal_page(target: Path) -> bytes:
    """Fill the contact placeholder in a legal page.

    The address is escaped before it reaches the attribute, because the value
    comes from an environment variable and a quote in it would otherwise break
    out of the href.
    """
    address = contact_email()
    if address:
        link = '<a href="mailto:%s">%s</a>' % (html.escape(address, quote=True), html.escape(address))
    else:
        link = "<code>%s</code>" % html.escape(NO_CONTACT_NOTICE)
    text = target.read_text(encoding="utf-8")
    return text.replace("{{CONTACT_LINK}}", link).encode("utf-8")


def render_landing_page(target: Path) -> bytes:
    """Fill the landing page's live numbers and its bulletin board.

    The page used to state how many accounts were in use as a written-down
    number. That is the same mistake as a hard-coded count anywhere else: true on
    the day it was typed and quietly false afterwards, on the one page whose claim
    is that it tells the truth about a small pilot. So the sentence is rendered
    from the database instead.

    "In use" means *an active account with an enabled mailbox*, not a count of
    rows in `users`. Registering is one click away from doing nothing, and
    counting those would put a number on the page the product cannot back up. The
    definition lives in `Database.landing_user_count` and a test pins it, because
    a number that means whatever is convenient is worse than no number.
    """
    count = get_db().landing_user_count()
    if count <= 0:
        phrase = "现在还在内测的最早期，还没有人开始用。"
    elif count == 1:
        phrase = "现在有 1 个账号在用它收信，那个是我自己。"
    else:
        phrase = f"现在有 {count} 个账号在用它收信，其中一个是我自己。"
    text = target.read_text(encoding="utf-8")
    text = text.replace("{{PILOT_COUNT}}", html.escape(phrase))
    text = text.replace("{{SOURCE_LINK}}", render_source_link())
    # The install instructions are prose and live in the template; only the
    # button is live, because whether this server has an APK at all is a fact
    # about the machine rather than something the page can assert.
    text = text.replace("{{APK_BUTTON}}", render_apk_button())
    return text.replace("{{BULLETIN}}", render_bulletin(get_db().public_announcements(3))).encode("utf-8")


# The published source repository. Optional, because most copies of this software
# are somebody's own installation, and a footer pointing at a repository its owner
# does not control would be a link to a stranger's code.
SOURCE_URL_ENV = "INFE_PILOT_SOURCE_URL"


def source_url() -> str:
    """The operator's public source repository, or "" when unset.

    AGPL-3.0 section 13 is why this exists at all: running modified software as a
    network service obliges the operator to offer *that* source to the people
    using it. A footer link is the cheapest honest way to do that -- and it also
    satisfies the licence for the unmodified case, where the obligation is easy to
    forget precisely because nothing was changed.
    """
    raw = (os.environ.get(SOURCE_URL_ENV) or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    # Only http(s). A `javascript:` value here would turn an operator's typo into
    # a link that runs code in every visitor's browser, on the public page.
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        logging.warning("%s 不是 http(s) 地址，已忽略", SOURCE_URL_ENV)
        return ""
    return raw[:300]


def render_source_link() -> str:
    """The footer link, or nothing at all when no repository is configured."""
    url = source_url()
    if not url:
        return ""
    return (f'<a href="{html.escape(url, quote=True)}" target="_blank" '
            f'rel="noopener">源代码（AGPL-3.0）</a> · ')


# --------------------------------------------------------------------------
# The Android package (a sideloadable APK), and the proof that it is ours
# --------------------------------------------------------------------------

# The APK is a *build artifact*, so it does not live in `static/`. Everything
# under `pilot_app/` ends up in the release tarball, the offline source snapshot
# and the publication export, and a signed multi-megabyte binary has no business
# in any of the three: it is not source, it is rebuilt without the code changing,
# and the export tool would have to grow yet another exclusion to keep a blob out
# of the public tree. Beside the database it is outside all three by construction.
DOWNLOAD_DIR_ENV = "INFE_PILOT_DOWNLOAD_DIR"
DEFAULT_DOWNLOAD_DIR = Path("/var/lib/cityu-mail-pilot/download")
APK_FILENAME = "cityu-mail-pilot.apk"
APK_ROUTE = "/download/" + APK_FILENAME
APK_MEDIA_TYPE = "application/vnd.android.package-archive"

# Chrome only opens an installed package without its address bar if the site
# proves it owns that package, by serving a Digital Asset Links statement from
# this exact well-known path. Unverified, the app still runs but shows a URL bar,
# which is indistinguishable from a browser shortcut -- so the page tells the
# reader how to check rather than promising a chrome-free window it may not get.
ASSETLINKS_PATH = "/.well-known/assetlinks.json"
ANDROID_PACKAGE_ENV = "INFE_PILOT_ANDROID_PACKAGE"
ANDROID_FINGERPRINT_ENV = "INFE_PILOT_ANDROID_FINGERPRINT"

# `com.example.app` shape. Only used to reject nonsense early: the value goes
# into a JSON document this server publishes about somebody else's app, and an
# operator typo there is a claim about a package that is not ours.
_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")


def download_dir() -> Path:
    raw = (os.environ.get(DOWNLOAD_DIR_ENV) or "").strip()
    return Path(raw) if raw else DEFAULT_DOWNLOAD_DIR


def apk_path() -> Optional[Path]:
    """The published APK when the operator has put one there, else None.

    Existence *is* the switch, and that is the point: a page offering a download
    that 404s is worse than a page offering none, and most copies of this
    software -- anything self-hosted -- will never have an APK at all, because
    the package is bound to one domain and one signing key.
    """
    try:
        target = (download_dir() / APK_FILENAME).resolve()
    except OSError:  # pragma: no cover - a path the OS refuses to resolve
        return None
    return target if target.is_file() else None


def android_fingerprint() -> str:
    """The signing certificate's SHA-256 as Asset Links wants it, or "".

    `keytool -list -v` prints it colon-separated and `gradle signingReport` does
    not, in either case, so both spellings are accepted and normalised rather
    than making the operator reformat a 95-character string by hand.
    """
    raw = (os.environ.get(ANDROID_FINGERPRINT_ENV) or "").strip()
    if not raw:
        return ""
    hexed = re.sub(r"[^0-9A-Fa-f]", "", raw).upper()
    if len(hexed) != 64:
        logging.warning("%s 不是 SHA-256（需要 64 位十六进制），已忽略", ANDROID_FINGERPRINT_ENV)
        return ""
    return ":".join(hexed[index:index + 2] for index in range(0, 64, 2))


def assetlinks_document() -> Optional[bytes]:
    """The Digital Asset Links statement, or None when there is nothing to claim.

    Absent configuration means *no document*, not an empty one. This file asserts
    that a named Android package is this site, and a copy of the software that
    has not built its own APK must not make that assertion about an app it does
    not control -- so a self-hoster who sets nothing publishes nothing.
    """
    fingerprint = android_fingerprint()
    package = (os.environ.get(ANDROID_PACKAGE_ENV) or "").strip()
    if not fingerprint or not package:
        return None
    if not _PACKAGE_RE.match(package):
        logging.warning("%s 不是合法的安卓包名，已忽略", ANDROID_PACKAGE_ENV)
        return None
    return json.dumps([{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app",
            "package_name": package,
            "sha256_cert_fingerprints": [fingerprint],
        },
    }], ensure_ascii=False, indent=2).encode("utf-8")


def _human_size(count: int) -> str:
    """A file size the way a download page should print it."""
    if count >= 1024 * 1024:
        return f"{count / (1024 * 1024):.1f} MB"
    return f"{max(1, round(count / 1024))} KB"


def render_apk_button() -> str:
    """The Android download button, or a sentence saying there is not one.

    Both states are true ones. The empty state is not an error: a self-hosted
    copy has no APK by definition, so the page falls back to describing the
    browser route -- which works on every Android phone -- instead of leaving a
    dead button behind.
    """
    target = apk_path()
    if target is None:
        return ('<p class="note">这台服务器上没有准备好安卓安装包，'
                '用下面的「添加到主屏幕」一样能装。</p>')
    try:
        size = _human_size(target.stat().st_size)
    except OSError:  # pragma: no cover - removed between the check and the stat
        size = ""
    label = f"下载安卓安装包（{size}）" if size else "下载安卓安装包"
    return (f'<div class="dl"><a class="btn" href="{APK_ROUTE}" download '
            f'id="apk-download">{label}</a></div>')


# How many notices the public board shows at once. Three fits above the fold
# without turning the page into a feed; older ones stay in the console, and the
# board is not an archive.
BULLETIN_LIMIT = 3

# The board is read by people who are not signed in and may be anywhere, so
# every notice carries an explicit offset rather than a bare clock time. The
# audience is the university, hence Hong Kong, and `reports.to_local` already
# knows how to fall back to a fixed +08:00 when the host has no tzdata.
BULLETIN_TONES = ("info", "warn", "critical")


def bulletin_stamp(value: str | None) -> str:
    """A stored UTC timestamp as the board prints it, offset spelled out.

    The offset is read off the resolved datetime instead of being written as a
    literal, so the marker cannot disagree with the time next to it if the
    timezone ever moves.
    """
    local = reports_mod.to_local(value, None)
    if local is None:
        return ""
    offset = local.utcoffset() or dt.timedelta(hours=8)
    total = int(offset.total_seconds())
    hours, minutes = divmod(abs(total) // 60, 60)
    marker = f"GMT{'+' if total >= 0 else '-'}{hours}"
    if minutes:
        marker += f":{minutes:02d}"
    return f"{local.month}月{local.day}日 {local:%H:%M} ({marker})"


def render_bulletin(notices: list[dict[str, Any]]) -> str:
    """The public board on the landing page, or nothing at all.

    Empty means *no markup*: a heading with an empty list under it reads as a
    page that is broken or abandoned, which is a worse first impression than a
    page that simply has no news. So the section, the rule above it and the
    anchor all appear together or not at all.

    Titles and bodies are operator-written plain text that ends up in our own
    origin's HTML, so they are escaped here and *only* here -- the template gets
    finished markup. No markdown, no links: a notice does not need them, and
    every added syntax is another way for text to become markup.
    """
    rows = list(notices)[:BULLETIN_LIMIT]
    if not rows:
        return ""
    # No rule above the heading: the template already has one between the hero
    # and this placeholder. The rule *below* is ours, because the separator
    # between the board and the screenshot after it only exists when the board
    # does -- emitting both would print two hairlines 40px apart.
    parts = [
        '<section id="board" aria-labelledby="board-title">',
        '<h2 id="board-title">布告栏</h2>',
        '<p class="note">运营者写给所有人的通知。没登录也看得到，所以这里不会有只跟某个账号有关的内容。</p>',
    ]
    for row in rows:
        tone = str(row.get("tone") or "info")
        if tone not in BULLETIN_TONES:
            tone = "info"
        stamp = bulletin_stamp(row.get("public_at") or row.get("created_at"))
        parts.append(f'<article class="notice notice-{tone}">')
        parts.append(f'<h3>{html.escape(str(row.get("title") or "（无标题）"))}</h3>')
        if stamp:
            parts.append(f'<p class="stamp">{html.escape(stamp)}</p>')
        parts.append(f'<p class="post">{html.escape(str(row.get("body") or ""))}</p>')
        parts.append("</article>")
    parts.append("</section>")
    parts.append('<hr class="rule">')
    return "\n".join(parts)


# --------------------------------------------------------------------------
# request
# --------------------------------------------------------------------------


class Request:
    def __init__(
        self,
        *,
        method: str,
        path: str,
        query: dict[str, list[str]],
        headers: Any,
        body: bytes,
        client: str,
    ) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        self.client = client
        self.user: Optional[dict[str, Any]] = None

    @property
    def origin(self) -> str:
        return (self.headers.get("Origin") or "").rstrip("/")

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name) or default

    def cookie(self, name: str) -> Optional[str]:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except Exception:
            return None
        morsel = jar.get(name)
        return morsel.value if morsel else None

    def json_object(self) -> dict[str, Any]:
        if not self.body:
            raise ApiError(422, "请求缺少 JSON 内容。")
        try:
            payload = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(422, "请求内容不是合法的 JSON。") from exc
        if not isinstance(payload, dict):
            raise ApiError(422, "请求内容必须是 JSON 对象。")
        return payload

    def query_int(self, name: str, default: int) -> int:
        values = self.query.get(name)
        if not values:
            return default
        try:
            return int(values[0])
        except (TypeError, ValueError):
            return default


# --------------------------------------------------------------------------
# validated input helpers (stand-ins for the previous pydantic models)
# --------------------------------------------------------------------------


def _field(payload: dict[str, Any], name: str, default: Any = None) -> Any:
    value = payload.get(name, default)
    return default if value is None and default is not None else value


def _string(
    payload: dict[str, Any],
    name: str,
    *,
    default: Optional[str] = None,
    minimum: int = 0,
    maximum: int = 1000,
    required: bool = True,
) -> str:
    value = payload.get(name, default)
    if value is None:
        if required:
            raise ApiError(422, f"缺少字段 {name}。")
        return ""
    if not isinstance(value, str):
        raise ApiError(422, f"字段 {name} 必须是文字。")
    if len(value) < minimum:
        raise ApiError(422, f"字段 {name} 太短。")
    if len(value) > maximum:
        raise ApiError(422, f"字段 {name} 过长。")
    return value


def _boolean(payload: dict[str, Any], name: str, default: bool) -> bool:
    value = payload.get(name, default)
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    raise ApiError(422, f"字段 {name} 必须是布尔值。")


def _port(payload: dict[str, Any], name: str, default: Optional[int] = None) -> int:
    value = payload.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(422, f"字段 {name} 必须是端口号。")
    if not 1 <= value <= 65535:
        raise ApiError(422, f"字段 {name} 必须在 1-65535 之间。")
    return value


def _string_list(payload: dict[str, Any], name: str, *, maximum_items: int, item_maximum: int = 200) -> list[str]:
    value = payload.get(name, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ApiError(422, f"字段 {name} 必须是列表。")
    if len(value) > maximum_items:
        raise ApiError(422, f"字段 {name} 的条目过多。")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ApiError(422, f"字段 {name} 只能包含文字。")
        if len(item) > item_maximum:
            raise ApiError(422, f"字段 {name} 的单个条目过长。")
        cleaned = item.strip()
        if cleaned:
            result.append(cleaned)
    return result


def _config(payload: dict[str, Any], name: str) -> dict[str, Any]:
    value = payload.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ApiError(422, f"字段 {name} 必须是对象。")
    if len(value) > MAX_JSON_DEPTH_ITEMS:
        raise ApiError(422, f"字段 {name} 的条目过多。")
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, (str, int, float, bool)):
            raise ApiError(422, f"字段 {name} 只支持简单的键值对。")
    return dict(value)


def _email(value: str) -> str:
    result = value.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", result):
        raise ApiError(422, "邮箱地址格式不正确。")
    return result


def _cityu_email(value: str) -> str:
    """Validate an optional CityU school mailbox identity."""
    value = value.strip()
    if not value:
        return ""
    result = _email(value)
    domain = result.rsplit("@", 1)[1]
    if not (domain == "cityu.edu.hk" or domain.endswith(".cityu.edu.hk")):
        raise ApiError(422, "学校邮箱必须使用 CityU 域名（例如 @my.cityu.edu.hk）。")
    return result


# --------------------------------------------------------------------------
# process-wide state
# --------------------------------------------------------------------------

_db_singleton: Optional[Database] = None
_service_singleton: Optional[PilotService] = None
_state_lock = threading.Lock()
_login_attempts: dict[str, list[float]] = {}
_attempt_lock = threading.Lock()


def get_db() -> Database:
    global _db_singleton
    with _state_lock:
        if _db_singleton is None:
            database = Database(os.environ.get("INFE_PILOT_DB", "/var/lib/cityu-mail-pilot/pilot.sqlite3"))
            database.initialize()
            _db_singleton = database
        return _db_singleton


def get_service() -> PilotService:
    global _service_singleton
    database = get_db()  # taken before the lock: get_db() locks the same mutex
    with _state_lock:
        if _service_singleton is None:
            _service_singleton = PilotService(database, SecretBox.from_environment())
        return _service_singleton


def __getattr__(name: str) -> Any:  # pragma: no cover - module attribute plumbing
    """Keep ``from pilot_app.web import db, service`` working lazily."""
    if name == "db":
        return get_db()
    if name == "service":
        return get_service()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _rate_limit(key: str, *, failed: bool = False) -> None:
    """Small single-worker login throttle; the reverse proxy adds the IP limit."""
    now = time.monotonic()
    with _attempt_lock:
        recent = [value for value in _login_attempts.get(key, []) if now - value < 900]
        if len(recent) >= 8:
            raise ApiError(429, "登录尝试过多，请 15 分钟后再试。")
        if failed:
            recent.append(now)
        _login_attempts[key] = recent


# The public application form needs its own budget: the login throttle is
# sized for a person mistyping a password, not for a stranger filling in a form.
_signup_attempts: dict[str, list[float]] = {}


def _signup_rate_limit(client: str) -> None:
    now = time.monotonic()
    key = f"signup:{client}"
    with _attempt_lock:
        recent = [value for value in _signup_attempts.get(key, []) if now - value < 3600]
        if len(recent) >= 5:
            raise ApiError(429, "提交过于频繁，请一小时后再试。")
        recent.append(now)
        _signup_attempts[key] = recent


def _clear_attempts(key: str) -> None:
    with _attempt_lock:
        _login_attempts.pop(key, None)


_verify_lock = threading.Lock()
_verify_recent: dict[str, float] = {}
_admin_actions: dict[str, list[float]] = {}


def _verification_allowed(user_id: str) -> bool:
    """Throttle explicit IMAP checks so a user cannot hammer a mail provider.

    Opening a mailbox is a real outbound connection; without this a stuck page
    could lock the account out of its own provider.
    """
    now = time.monotonic()
    with _verify_lock:
        last = _verify_recent.get(user_id, 0.0)
        if now - last < 60:
            return False
        _verify_recent[user_id] = now
        if len(_verify_recent) > 500:
            cutoff = now - 600
            for key in [key for key, value in _verify_recent.items() if value < cutoff]:
                _verify_recent.pop(key, None)
        return True


def _cookie_flags() -> str:
    secure = os.environ.get("INFE_PILOT_COOKIE_SECURE", "1") != "0"
    return "; Secure" if secure else ""


def _session_cookie(user_id: str) -> str:
    token = new_token()
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=SESSION_DAYS)
    get_db().create_session(user_id, token_hash(token), expires.isoformat(timespec="seconds"))
    return (
        f"{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_DAYS * 86400}; "
        f"HttpOnly; SameSite=Lax{_cookie_flags()}"
    )


def _expired_cookie() -> str:
    return f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{_cookie_flags()}"


def _require_user(request: Request) -> dict[str, Any]:
    token = request.cookie(SESSION_COOKIE)
    if not token:
        raise ApiError(401, "请先登录。")
    user = get_db().session_user(token_hash(token))
    if not user:
        raise ApiError(401, "登录已过期。")
    request.user = user
    return user


def _admin_emails() -> set[str]:
    """Operator identities named by the server environment.

    These are the instance owner's own accounts, and they are the floor: an
    operator added from the console can always be removed again, and the accounts
    named here cannot be, so a wrong click in the console can never lock the
    owner out of their own installation.

    Delegates to :mod:`pilot_app.alerting` so the HTTP surface and the alert
    mailer cannot disagree about who counts as an operator.
    """
    return alerting.admin_emails()


def _is_admin(user: dict[str, Any]) -> bool:
    """Owner-named accounts, plus anyone granted rights from the console.

    Until v0.34.0 this was the environment variable alone, and that is still the
    half that cannot be taken away: the stored grants are additive. A session
    still cannot grant itself anything -- every grant goes through an endpoint
    that requires an existing operator and their password.
    """
    if str(user.get("email", "")).strip().lower() in _admin_emails():
        return True
    return bool(user.get("is_admin"))


def _admin_source(user: dict[str, Any]) -> str:
    """Where this account's rights come from, for the console to display."""
    if str(user.get("email", "")).strip().lower() in _admin_emails():
        return "env"
    return "database" if user.get("is_admin") else ""


def _require_admin(request: Request) -> dict[str, Any]:
    user = _require_user(request)
    if not _is_admin(user):
        # Deliberately identical to a missing resource: an ordinary user should
        # not learn that an admin surface exists.
        raise ApiError(404, "资源不存在。")
    return user


def _confirm_operator(request: Request, admin: dict[str, Any]) -> None:
    """Ask an operator to re-enter their password before changing operator rights.

    Granting operator rights is the most powerful thing this console can do: it
    is the one action whose effect outlives the session that performed it. An
    unattended browser is the realistic threat -- a laptop left open, a shared
    machine -- and the password is the only thing an attacker sitting at that
    browser does not have. The precedent is the account-deletion flow, which
    already makes the operator retype their own address for the same reason.
    """
    payload = request.json_object()
    password = str(payload.get("password") or "")
    if not password:
        raise ApiError(422, "请重新输入你的登录密码。")
    record = get_db().find_user_for_login(admin["email"])
    if not record or not verify_password(password, record["password_hash"]):
        raise ApiError(403, "密码不正确。")


def _admin_rate_limit(user_id: str) -> None:
    """Operators act rarely; this only stops a runaway click or a stuck script."""
    now = time.monotonic()
    with _verify_lock:
        recent = [value for value in _admin_actions.get(user_id, []) if now - value < 60]
        if len(recent) >= 30:
            raise ApiError(429, "管理操作过于频繁，请稍后再试。")
        recent.append(now)
        _admin_actions[user_id] = recent
        if len(_admin_actions) > 200:
            cutoff = now - 600
            for key in [key for key, values in _admin_actions.items() if not values or values[-1] < cutoff]:
                _admin_actions.pop(key, None)


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

Route = tuple[re.Pattern[str], Callable[[Request], Response]]

ROUTES: dict[str, list[Route]] = {}


def route(method: str, pattern: str) -> Callable[[Callable[[Request], Response]], Callable[[Request], Response]]:
    compiled = re.compile("^" + pattern + "$")

    def decorator(function: Callable[[Request], Response]) -> Callable[[Request], Response]:
        ROUTES.setdefault(method, []).append((compiled, function))
        return function

    return decorator


@route("GET", "/health")
def health(request: Request) -> Response:
    return json_response({"status": "ok", "version": VERSION})


@route("GET", "/api/catalog")
def catalog(request: Request) -> Response:
    payload = public_catalog()
    payload["mailbox"] = public_mailbox_help()
    return json_response(payload)


@route("POST", "/api/signup")
def public_signup(request: Request) -> Response:
    """Accept a pilot application from the public landing page.

    This is the only unauthenticated write in the API, so it is deliberately
    narrow: it stores an address and a note, and nothing else. It cannot create
    an account, cannot mint an invite, and cannot read anything back -- approval
    stays a separate, operator-only action. A flood here annoys the operator;
    it cannot grant anybody access.

    Throttled per client, and the reply never confirms whether an address is
    already registered with the pilot.
    """
    client = request.client or "unknown"
    _signup_rate_limit(client)
    payload = request.json_object()
    email = _email(_string(payload, "email", maximum=254))
    note = _string(payload, "note", default="", required=False, maximum=500)
    try:
        row, already = get_db().create_signup_request(email, note, client)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    if not already:
        _notify_new_signup(row)
    return json_response({"ok": True, "already": already})


def _notify_new_signup(row: dict[str, Any]) -> None:
    """Tell the operator an application arrived. Never fails the request.

    The applicant cannot be e-mailed directly: every send in this project goes
    out through a user's own SMTP credentials, and there is no system mailbox.
    So the operator is notified and sends the invite themselves by approving it
    in the console. A notification failure must not lose the application, which
    is why this swallows errors after logging them.
    """
    try:
        service = get_service()
        alerting.send_admin_mail(
            get_db(), service.secrets,
            subject="[CityU Mail Pilot] 新的内测申请",
            text_body=(
                f"有人从网站申请了内测名额。\n\n"
                f"邮箱：{row.get('email', '')}\n"
                f"留言：{row.get('note') or '（没有留言）'}\n"
                f"时间：{row.get('created_at', '')}\n"
                f"来源：{row.get('client', '')}\n\n"
                f"到管理后台的「内测申请」面板一键发邀请码。"
            ),
        )
    except Exception:  # noqa: BLE001 - the application is already stored
        logging.warning("could not notify the operator about a new signup", exc_info=True)


@route("POST", "/api/auth/register")
def register(request: Request) -> Response:
    payload = request.json_object()
    email = _email(_string(payload, "email", maximum=254))
    password = _string(payload, "password", minimum=1, maximum=400)
    invite_code = _string(payload, "invite_code", minimum=1, maximum=200)
    # Consent is enforced here, not only in the browser. A checkbox that the
    # server never checks is decoration, and the disclosure that matters most --
    # that mail bodies go to a third-party model -- is exactly the one a user
    # cannot discover after the fact.
    if not _boolean(payload, "accepted_terms", False):
        raise ApiError(400, "请先阅读并同意《隐私政策》与《服务条款》。")
    database = get_db()
    limit, _source = _max_users()
    if database.count_users() >= limit:
        raise ApiError(403, "当前试点名额已满。")
    try:
        user = database.create_user(email, hash_password(password), token_hash(invite_code.strip()))
    except (ValueError, SecurityError) as exc:
        raise ApiError(400, str(exc)) from exc
    return json_response(user, cookies=[_session_cookie(user["id"])])


@route("POST", "/api/auth/login")
def login(request: Request) -> Response:
    payload = request.json_object()
    email = _email(_string(payload, "email", maximum=254))
    password = _string(payload, "password", minimum=1, maximum=400)
    attempt_key = token_hash(email)
    _rate_limit(attempt_key)
    user = get_db().find_user_for_login(email)
    if not user or not verify_password(password, user["password_hash"]):
        _rate_limit(attempt_key, failed=True)
        raise ApiError(401, "邮箱或密码错误。")
    _clear_attempts(attempt_key)
    return json_response(
        {key: user[key] for key in ("id", "email", "status", "created_at")},
        cookies=[_session_cookie(user["id"])],
    )


@route("POST", "/api/auth/logout")
def logout(request: Request) -> Response:
    token = request.cookie(SESSION_COOKIE)
    if token:
        get_db().delete_session(token_hash(token))
    return json_response({"ok": True}, cookies=[_expired_cookie()])


@route("GET", "/api/me")
def me(request: Request) -> Response:
    user = _require_user(request)
    database = get_db()
    mailbox = database.get_mailbox(user["id"])
    connections: dict[str, Any] = {}
    for kind in ("model", "search"):
        item = database.get_connection(user["id"], kind)
        if item:
            connections[kind] = {
                key: item[key]
                for key in ("provider", "model", "base_url", "enabled", "last_test_at", "last_error")
            }
    # The screen has to distinguish "nothing configured" from "running on the
    # pilot's key", or a user whose reports and citations work is told to go
    # configure something that is not broken. Only the fact that a platform key
    # exists is exposed; the key itself is never in this payload, nor in any other
    # response (see `test_platform_key`).
    for kind, fallback in (("model", providers.platform_model_default()),
                           ("search", providers.platform_search_default())):
        if kind in connections or not fallback:
            continue
        connections[kind] = {
            key: fallback[key]
            for key in ("provider", "model", "base_url", "enabled", "last_test_at", "last_error")
        }
        connections[kind]["platform"] = True
    safe_mailbox = None
    if mailbox:
        safe_mailbox = {
            key: mailbox[key]
            for key in ("email", "report_to", "imap_host", "imap_port", "smtp_host", "smtp_port", "enabled",
                        "last_polled_at", "last_error")
        }
    return json_response(
        # `is_admin` in the identity block is stripped on purpose. The stored
        # column means "granted from the console" and the top-level field means
        # "can actually administer this instance", which is the environment list
        # OR the column. Shipping both under one name would leave the next reader
        # to guess which one the client is checking.
        {"user": {key: value for key, value in user.items() if key != "is_admin"},
         "profile": database.get_profile(user["id"]), "mailbox": safe_mailbox,
         "connections": connections, "is_admin": _is_admin(user),
         # The application shell is a static file, so the footer link to the
         # source cannot be templated into it. It rides here instead, and the
         # shell fills the footer in after login. See `source_url` for why the
         # link exists at all (AGPL-3.0 section 13).
         "source_url": source_url(),
         # The photo itself is fetched from its own route so this response stays
         # small; what the picker needs is whether one exists and which revision
         # to put in the URL.
         "background_image": database.background_summary(user["id"])}
    )


@route("PUT", "/api/profile")
def save_profile(request: Request) -> Response:
    user = _require_user(request)
    payload = request.json_object()
    daily_time = _string(payload, "daily_time", default="22:00", maximum=5)
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", daily_time):
        raise ApiError(422, "每日发送时间必须是 HH:MM。")
    data: dict[str, Any] = {
        "school_email": _cityu_email(_string(payload, "school_email", default="", required=False, maximum=254)),
        "major": _string(payload, "major", default="", maximum=200),
        "year_of_study": _string(payload, "year_of_study", default="", maximum=80),
        "custom_instructions": _string(payload, "custom_instructions", default="", maximum=1000),
        "language": _string(payload, "language", default="bilingual", maximum=40),
        "timezone": _string(payload, "timezone", default="Asia/Hong_Kong", maximum=64),
        "immediate_enabled": _boolean(payload, "immediate_enabled", True),
        "daily_enabled": _boolean(payload, "daily_enabled", True),
        "daily_time": daily_time,
        "courses_json": json.dumps(_string_list(payload, "courses", maximum_items=20), ensure_ascii=False),
        "interests_json": json.dumps(_string_list(payload, "interests", maximum_items=20), ensure_ascii=False),
        "career_goals_json": json.dumps(_string_list(payload, "career_goals", maximum_items=10), ensure_ascii=False),
        "focus_topics_json": json.dumps(_string_list(payload, "focus_topics", maximum_items=20), ensure_ascii=False),
        "less_interested_json": json.dumps(_string_list(payload, "less_interested", maximum_items=20), ensure_ascii=False),
    }
    get_db().upsert_profile(user["id"], data)
    return json_response({"ok": True})


@route("PUT", "/api/appearance")
def save_appearance(request: Request) -> Response:
    """Save only the look of the interface.

    Deliberately separate from ``PUT /api/profile``: that endpoint writes every
    profile field from the request body and defaults anything missing, so saving
    a theme through it would silently wipe the user's courses and instructions.
    """
    user = _require_user(request)
    payload = request.json_object()
    theme = _string(payload, "theme", default="paper", maximum=20)
    if theme not in THEMES:
        raise ApiError(422, "未知的主题。")
    background = _string(payload, "background", default="", required=False, maximum=20)
    if background not in BACKGROUNDS:
        raise ApiError(422, "未知的背景图。")
    if background == "custom" and not get_db().background_summary(user["id"])["present"]:
        # Otherwise the picker offers a blank page: 'custom' with nothing stored
        # falls back to the theme, so the user selects their photo and sees no
        # change at all.
        raise ApiError(422, "还没有上传自定义背景图。")
    get_db().upsert_profile(user["id"], {"theme": theme, "background": background})
    return json_response({"ok": True, "theme": theme, "background": background})


BACKGROUND_PATH = "/api/appearance/background"


@route("PUT", BACKGROUND_PATH)
def upload_background(request: Request) -> Response:
    """Store a user-uploaded background photo.

    The body is the image itself, not multipart. A multipart parser is a large
    piece of code whose failure modes land squarely on attacker-controlled input,
    and there is exactly one field here, so the raw body plus a Content-Type
    header carries the same information with far less to get wrong.

    Validation lives in ``imageguard``; this function's job is the parts that are
    about storage rather than about images.
    """
    user = _require_user(request)
    if not request.body:
        raise ApiError(422, "没有收到图片内容。")
    try:
        media_type, width, height = imageguard.validate(
            request.body, request.header("Content-Type")
        )
    except imageguard.ImageRejected as exc:
        detail = f"（{exc.detail}）" if exc.detail else ""
        raise ApiError(422, exc.reason + detail) from exc

    revision = get_db().set_background_image(user["id"], media_type, request.body)
    # Choosing the photo and selecting it are one action from the user's side, so
    # the server does both; leaving them separate produces an upload that appears
    # to have done nothing.
    get_db().upsert_profile(user["id"], {"background": "custom"})
    return json_response({
        "ok": True, "background": "custom", "rev": revision,
        "media_type": media_type, "width": width, "height": height,
        "size": len(request.body),
    })


@route("DELETE", BACKGROUND_PATH)
def delete_background(request: Request) -> Response:
    user = _require_user(request)
    get_db().clear_background_image(user["id"])
    profile = get_db().get_profile(user["id"])
    if profile.get("background") == "custom":
        get_db().upsert_profile(user["id"], {"background": ""})
    return json_response({"ok": True, "background": ""})


@route("GET", BACKGROUND_PATH)
def serve_background(request: Request) -> Response:
    """Serve the caller's own photo, and nobody else's.

    Two headers are load-bearing rather than decorative. ``nosniff`` is already
    global, and the Content-Type comes from our own allowlist rather than from
    anything the uploader supplied, so the bytes can never be interpreted as
    HTML or script. ``private`` keeps the photo out of any shared cache: it is
    personal data belonging to one account.
    """
    user = _require_user(request)
    stored = get_db().get_background_image(user["id"])
    if not stored:
        raise ApiError(404, "还没有自定义背景图。")
    if stored["media_type"] not in (imageguard.JPEG, imageguard.PNG):
        # Defence in depth: a value that somehow predates the allowlist must not
        # become a content-type header we would not choose today.
        raise ApiError(404, "背景图格式不受支持。")
    return Response(
        status=200,
        body=stored["bytes"],
        content_type=stored["media_type"],
        headers={
            "Cache-Control": "private, max-age=604800",
            "ETag": f'"{stored["rev"]}"',
            "Content-Disposition": "inline",
        },
    )



@route("PUT", "/api/mailbox")
def save_mailbox(request: Request) -> Response:
    user = _require_user(request)
    payload = request.json_object()
    mailbox_email = _email(_string(payload, "email", maximum=254))
    # The form promises "leave blank = send back to the same mailbox", so
    # honour that here instead of running "" through email validation.
    report_to_raw = _string(payload, "report_to", default="", required=False, maximum=254).strip()
    data: dict[str, Any] = {
        "email": mailbox_email,
        "report_to": _email(report_to_raw) if report_to_raw else mailbox_email,
        "imap_port": _port(payload, "imap_port"),
        "smtp_port": _port(payload, "smtp_port"),
        "enabled": _boolean(payload, "enabled", True),
    }
    app_password = _string(payload, "app_password", minimum=1, maximum=1000)
    try:
        data["imap_host"] = validate_public_host(_string(payload, "imap_host", maximum=253))
        data["smtp_host"] = validate_public_host(_string(payload, "smtp_host", maximum=253))
    except SecurityError as exc:
        raise ApiError(422, str(exc)) from exc
    data["encrypted_password"] = get_service().secrets.encrypt(app_password, context=f"mailbox:{user['id']}")
    get_db().upsert_mailbox(user["id"], data)
    return json_response({"ok": True})


@route("PUT", r"/api/connections/(?P<kind>[A-Za-z]+)")
def save_connection(request: Request, kind: str) -> Response:
    user = _require_user(request)
    if kind not in {"model", "search"}:
        raise ApiError(404, "未知的连接类型。")
    payload = request.json_object()
    provider = _string(payload, "provider", maximum=64)
    api_key = _string(payload, "api_key", default="", required=False, maximum=4000).strip()
    if not api_key:
        raise ApiError(422, "请填写 API key。")
    model = _string(payload, "model", default="", required=False, maximum=200).strip()
    base_url_input = _string(payload, "base_url", default="", required=False, maximum=500)
    try:
        if kind == "model":
            _, _, base_url = normalized_model_config(provider, model, base_url_input)
        else:
            if provider not in SEARCH_PRESETS:
                raise ValueError("不支持的搜索供应商。")
            base_url = SEARCH_PRESETS[provider]["base_url"]
    except (ValueError, SecurityError) as exc:
        raise ApiError(422, str(exc)) from exc
    data = {
        "kind": kind,
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "encrypted_api_key": get_service().secrets.encrypt(api_key, context=f"connection:{user['id']}:{kind}"),
        "config_json": json.dumps(_config(payload, "config")),
        "enabled": _boolean(payload, "enabled", True),
    }
    get_db().upsert_connection(user["id"], data)
    return json_response({"ok": True})


@route("POST", r"/api/test/(?P<target>[A-Za-z]+)")
def test_connection(request: Request, target: str) -> Response:
    user = _require_user(request)
    service = get_service()
    try:
        if target == "model":
            result = service.test_model(user["id"])
            get_db().record_connection_result(user["id"], "model")
            return json_response({"ok": True, "result": result})
        if target == "search":
            results = service.test_search(user["id"])
            get_db().record_connection_result(user["id"], "search")
            return json_response({"ok": True, "results": results})
        if target == "mailbox":
            result = service.test_mailbox(user["id"])
            mailbox = get_db().get_mailbox(user["id"])
            if mailbox:
                get_db().record_mailbox_verification(mailbox["id"])
            return json_response({"ok": True, **result})
    except ApiError:
        raise
    except Exception as exc:
        if target == "mailbox":
            mailbox = get_db().get_mailbox(user["id"])
            if mailbox:
                get_db().record_mailbox_verification(mailbox["id"], error=str(exc))
        elif target in {"model", "search"}:
            get_db().record_connection_result(user["id"], target, error=str(exc))
        raise ApiError(400, str(exc)) from exc
    raise ApiError(404, "未知的测试目标。")


def _split_tasks(tasks: list[dict[str, Any]], states: dict[str, dict[str, Any]],
                 day: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split freshly derived tasks into (still open, already handled).

    ``states`` is the user's own record of what they ticked off. A task whose
    wording changed since then simply has no matching key, so it comes back --
    which is the point of keying on content: a rewritten action is a new
    question, and hiding it behind an answer to the old one would be wrong.
    """
    open_tasks: list[dict[str, Any]] = []
    done_tasks: list[dict[str, Any]] = []
    for task in tasks:
        state = states.get(task["task_key"])
        if state and state["state"] == "done":
            done_tasks.append({**task, "done_at": state["done_at"]})
        else:
            open_tasks.append(task)
    return open_tasks, done_tasks


def _archived_tasks(states: dict[str, dict[str, Any]], day: str,
                    seen: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Tasks recorded for a day that can no longer be rebuilt from the reports.

    Reconnecting a mailbox purges its messages, and a report whose message is
    gone drops out of the day's join. Without this the archive would lose
    exactly the rows it exists to prove -- "I handled this" -- so the stored
    snapshot is replayed instead. Marked ``archived`` so the UI can say the
    original mail is no longer available.
    """
    open_tasks: list[dict[str, Any]] = []
    done_tasks: list[dict[str, Any]] = []
    for key, state in states.items():
        if key in seen or state["task_day"] != day:
            continue
        entry = {
            "task_key": key, "task_day": state["task_day"], "subject": state["subject"],
            "action": state["action"], "deadline": state["deadline"], "priority": state["priority"],
            "sender": state["sender"], "received_display": "", "message_id": state["message_id"],
            "done_at": state["done_at"], "archived": True,
        }
        (done_tasks if state["state"] == "done" else open_tasks).append(entry)
    return open_tasks, done_tasks


def _local_window(timezone: str, now: dt.datetime | None = None,
                  day: str = "") -> tuple[str, str, dt.datetime, str]:
    """(start_utc, end_utc, local_now, local_date) for one user's local day.

    ``day`` (``YYYY-MM-DD``) picks an earlier local day; anything unparseable
    falls back to today rather than erroring, so a stale bookmark or a typo in a
    URL cannot turn a read-only page into a 500.
    """
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        zone = ZoneInfo("Asia/Hong_Kong")
    local_now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(zone)
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    if day:
        try:
            start = start.replace(year=int(day[0:4]), month=int(day[5:7]), day=int(day[8:10]))
        except (ValueError, IndexError):
            start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1)
    return (
        start.astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
        end.astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
        local_now,
        start.date().isoformat(),
    )


def _next_daily_run(daily_time: str, timezone: str, local_now: dt.datetime) -> dt.datetime:
    try:
        hour, minute = [int(part) for part in str(daily_time or "22:00").split(":", 1)]
    except (TypeError, ValueError):
        hour, minute = 22, 0
    candidate = local_now.replace(hour=min(max(hour, 0), 23), minute=min(max(minute, 0), 59),
                                  second=0, microsecond=0)
    if candidate <= local_now:
        candidate += dt.timedelta(days=1)
    return candidate


def build_dashboard(user: dict[str, Any]) -> dict[str, Any]:
    """Everything the "clear action" home screen needs, in one response.

    No network calls are made here: a per-request IMAP or model probe would make
    the dashboard slow and would hammer providers. Real checks happen only when
    the user presses a test button, whose result is stored and reported as a
    verified-at timestamp instead of being implied.
    """
    db = get_db()
    profile = db.get_profile(user["id"]) or {}
    mailbox = db.get_mailbox(user["id"])
    own_model = db.get_connection(user["id"], "model")
    # Falling back here as well as in the worker matters: if the dashboard said
    # "还没有配置 AI 模型" while reports were being generated with the pilot key,
    # the screen would be telling the user to fix something that is not broken.
    model = own_model or providers.platform_model_default()
    # Same reason as the model fallback just above: with the pilot's search key the
    # dashboard must not say "未配置" while citations are working.
    search = db.get_connection(user["id"], "search") or providers.platform_search_default()
    timezone = profile.get("timezone") or "Asia/Hong_Kong"
    start_utc, end_utc, local_now, local_date = _local_window(timezone, None)

    rows = db.today_reports(user["id"], start_utc, end_utc)
    service = get_service()
    tasks = reports_mod.today_tasks(
        [(row["id"], service.decrypt_report(row["body_markdown"], user["id"]), row["message_id"]) for row in rows],
        [{"id": row["message_id"], "subject": row["message_subject"], "sender_name": row["sender_name"],
          "sender_address": row["sender_address"], "received": row["received_at"],
          "importance": row["importance"]} for row in rows],
        timezone=timezone,
    )
    states = db.task_states(user["id"])
    tasks_open, tasks_done = _split_tasks(tasks, states, local_date)
    # Everything downstream ("today has N things to do", the next-step nudge)
    # counts only what is still open. A task the user just ticked off must stop
    # being the headline immediately, otherwise the dashboard argues with them.
    tasks = tasks_open
    recent = [
        {
            "subject": row["message_subject"],
            "sender": row["sender_name"] or row["sender_address"],
            "priority": reports_mod.derive_priority(
                reports_mod.parse_sections(service.decrypt_report(row["body_markdown"], user["id"])).get("importance", ""),
                row["importance"],
            ),
            "received": row["received_at"],
            "received_display": reports_mod.format_moment(row["received_at"], timezone),
            "status": row["status"],
        }
        for row in rows[:5]
    ]

    verified_at = mailbox.get("last_verified_at") if mailbox else None
    verify_error = (mailbox or {}).get("last_verify_error") or ""
    send_error = (mailbox or {}).get("last_error") or ""
    fresh = False
    moment = reports_mod.to_local(verified_at, "UTC")
    if moment and (dt.datetime.now(dt.timezone.utc) - moment).total_seconds() < 24 * 3600:
        fresh = True
    if not mailbox:
        mailbox_state, mailbox_detail = "missing", "还没有填写私人转发邮箱。"
    elif verify_error:
        mailbox_state, mailbox_detail = "error", verify_error
    elif fresh:
        mailbox_state, mailbox_detail = "ok", f"最近一次直连检查成功（{reports_mod.format_moment(verified_at, timezone)}）。"
    elif verified_at:
        mailbox_state, mailbox_detail = "stale", "上次检查已超过 24 小时，建议重新检查一次。"
    else:
        mailbox_state, mailbox_detail = "unknown", "还没有检查过，点下面的按钮确认一次。"

    native_search = bool(model and supports_native_search(model["provider"]))
    if model and model.get("platform"):
        # Say whose key it is and who is paying, because that is the sentence the
        # landing page and the privacy policy already promised the user would see.
        model_state = "ok"
        model_detail = (f"{model['provider']} · {model['model']}，"
                        f"内测期间用管理员提供的 key，你不花钱。想换成自己的，在下面填一次即可覆盖。")
    elif model:
        model_state = "error" if model.get("last_error") else "ok"
        model_detail = (f"已配置 {model['provider']}" + (f" · {model['model']}" if model["model"] else "")
                        + (f"　上次出错：{model['last_error']}" if model.get("last_error") else ""))
    else:
        model_state, model_detail = "missing", "还没有配置 AI 模型。"
    if native_search:
        search_state = "ok"
        search_detail = f"{model['provider']} 自带联网搜索，第 4 步可以跳过。"
    elif search and search.get("platform"):
        search_state = "ok"
        search_detail = (f"{search['provider']}，内测期间用管理员提供的搜索 key，你不花钱。"
                         "想换成自己的，在下面填一次即可覆盖。")
    elif search:
        search_state = "error" if search.get("last_error") else "ok"
        search_detail = f"已配置 {search['provider']}" + (f"　上次出错：{search['last_error']}" if search.get("last_error") else "")
    else:
        search_state, search_detail = "optional", "未配置：报告仍会生成，只是没有联网核实来源。"

    immediate_enabled = bool(profile.get("immediate_enabled", 1))
    daily_enabled = bool(profile.get("daily_enabled", 1))

    # "Nothing has arrived yet" is only worth raising once the setup has had a
    # realistic amount of time to receive something; otherwise every new user
    # would be greeted by a warning five minutes after signing up. The clock
    # starts at the later of registration and the last successful mailbox check,
    # so someone who finishes the wizard tonight is not warned about a school
    # forwarding rule they have not had a chance to create yet.
    anchors = [reports_mod.to_local(user.get("created_at"), "UTC"),
               reports_mod.to_local(verified_at, "UTC")]
    anchors = [moment for moment in anchors if moment]
    cold_start = bool(
        not anchors
        or (dt.datetime.now(dt.timezone.utc) - max(anchors)) < dt.timedelta(hours=2)
    )
    analysed_any = db.count_analysed_messages(user["id"]) > 0
    next_run = _next_daily_run(profile.get("daily_time") or "22:00", timezone, local_now)

    if not profile.get("school_email") or not profile.get("major"):
        next_step = {"kind": "profile", "title": "先补充个人资料", "detail": "填写 CityU 学校邮箱和专业，报告才能判断相关性。", "action": "去填写"}
    elif not mailbox:
        next_step = {"kind": "mailbox", "title": "设置私人转发邮箱", "detail": "在 CityU Outlook 里把邮件转发到你的私人邮箱，再把授权码填到这里。", "action": "去设置"}
    elif not fresh:
        next_step = {"kind": "verify", "title": "确认邮箱可以收信", "detail": "点一次只读连接检查；不会删除或改动你的邮件。", "action": "立即检查"}
    elif not model:
        next_step = {"kind": "model", "title": "配置 AI 模型 API", "detail": "填入你自己的模型 key，之后每封新邮件都会生成摘要。", "action": "去配置"}
    elif not analysed_any and not cold_start and immediate_enabled:
        # Setup is complete and the mailbox answers, but not one allowed-sender
        # mail has ever arrived. Saying "一切就绪" here is the one thing that
        # would leave a new user stuck without knowing it: the forwarding rule
        # is the only step we cannot verify from our side. Phrased as "confirm",
        # not "broken", because a quiet week is a normal week.
        next_step = {
            "kind": "mailbox",
            "title": "还没有收到过 CityU 邮件",
            "detail": "私人邮箱检查是通过的，但还没有任何 CityU 来信被处理过。"
                      "如果学校那边的自动转发还没设置，按下面向导做一次；已经设置的话，"
                      "可以从学校邮箱给自己发一封测试邮件确认。",
            "action": "检查转发设置",
            "tone": "warn",
        }
    elif tasks:
        first = tasks[0]
        next_step = {"kind": "task", "title": f"今天有 {len(tasks)} 件事要处理", "detail": first["action"]
                     + (f"（截止：{first['deadline']}）" if first["deadline"] else ""),
                     "action": "查看待办"}
    else:
        next_step = {"kind": "done", "title": "一切就绪，没有待处理事项",
                     "detail": f"下一封新邮件到达会自动生成摘要；每日简报下次在 {next_run:%H:%M} 发出。",
                     "action": "查看报告记录"}

    announcement = db.active_announcement_for(user["id"])
    return {
        # The broadcast rides on the dashboard response so it is on screen the
        # moment a user opens the app — no second request, no flicker.
        "announcement": (
            {"id": announcement["id"], "title": announcement["title"], "body": announcement["body"],
             "tone": announcement["tone"], "created_at": announcement["created_at"],
             "created_display": reports_mod.format_moment(announcement["created_at"], timezone)}
            if announcement else None
        ),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "local_date": local_date,
        "local_display": f"{local_now.month}月{local_now.day}日 {reports_mod.weekday_label(local_now)}",
        "greeting": reports_mod.greeting_for(local_now),
        "next_run": next_run.isoformat(timespec="seconds"),
        "next_run_display": f"{next_run.month}月{next_run.day}日 {next_run:%H:%M}",
        "next_step": next_step,
        "channels": {
            "mailbox": {"state": mailbox_state, "detail": mailbox_detail,
                        "verified_at": verified_at, "label": "邮箱收信"},
            "model": {"state": model_state, "detail": model_detail, "label": "AI 摘要"},
            "search": {"state": search_state, "detail": search_detail, "label": "联网搜索",
                       "native": native_search},
            "digest": {
                "state": "ok" if daily_enabled else "optional",
                "detail": (f"下次自动发出：{next_run.month}月{next_run.day}日 {next_run:%H:%M}（{timezone}）。"
                           if daily_enabled else "每日简报已关闭。"),
                "label": "每日简报",
            },
        },
        "today": {
            "messages": len(rows),
            "tasks": len(tasks_open),
            "tasks_done": len(tasks_done),
            "failed": sum(1 for row in rows if row["status"] == "failed"),
            "sent": sum(1 for row in rows if row["status"] == "sent"),
            "immediate_enabled": immediate_enabled,
            "daily_enabled": daily_enabled,
        },
        "tasks": tasks_open[:8],
        "tasks_done": [dict(task) for task in tasks_done[:20]],
        "recent": recent,
        "send_error": send_error,
    }


@route("GET", "/api/dashboard")
def dashboard(request: Request) -> Response:
    user = _require_user(request)
    return json_response(build_dashboard(user))


# --------------------------------------------------------------------------
# daily tasks: hide one, find it again later
# --------------------------------------------------------------------------


def task_day_view(user: dict[str, Any], day: str = "") -> dict[str, Any]:
    """One local day's action items, split into open and handled.

    Rebuilt from the reports every time; ``task_states`` only decides which of
    them the user has hidden. Nothing the model wrote is ever destroyed, which
    is what makes hiding safe and reversible.
    """
    database = get_db()
    profile = database.get_profile(user["id"])
    timezone = profile.get("timezone") or "Asia/Hong_Kong"
    start_utc, end_utc, _local_now, local_date = _local_window(timezone, day=day)
    rows = database.today_reports(user["id"], start_utc, end_utc)
    service = get_service()
    derived = reports_mod.today_tasks(
        [(row["id"], service.decrypt_report(row["body_markdown"], user["id"]), row["message_id"])
         for row in rows],
        [{"id": row["message_id"], "subject": row["message_subject"], "sender_name": row["sender_name"],
          "sender_address": row["sender_address"], "received": row["received_at"],
          "importance": row["importance"]} for row in rows],
        timezone=timezone,
    )
    states = database.task_states(user["id"])
    open_tasks, done_tasks = _split_tasks(derived, states, local_date)
    seen = {task["task_key"] for task in derived}
    archived_open, archived_done = _archived_tasks(states, local_date, seen)
    open_tasks.extend(archived_open)
    done_tasks.extend(archived_done)
    return {
        "day": local_date,
        "is_today": local_date == _local_window(timezone)[3],
        "tasks": open_tasks,
        "done": done_tasks,
        "counts": {"total": len(open_tasks) + len(done_tasks), "open": len(open_tasks),
                   "done": len(done_tasks)},
        "days": database.task_day_summaries(user["id"]),
    }


@route("GET", "/api/tasks")
def tasks_for_today(request: Request) -> Response:
    user = _require_user(request)
    return json_response(task_day_view(user))


@route("GET", r"/api/tasks/day/(?P<day>[0-9]{4}-[0-9]{2}-[0-9]{2})")
def tasks_for_day(request: Request, day: str) -> Response:
    user = _require_user(request)
    return json_response(task_day_view(user, day=day))


@route("PUT", r"/api/tasks/(?P<task_key>[0-9a-f]{32})")
def set_task(request: Request, task_key: str) -> Response:
    """Hide one task ("handled") or bring it back.

    The stored row is built from the server's own derived task, not from the
    request body: the client sends only the decision, so a tampered payload
    cannot plant arbitrary text in the user's archive. If the task can no longer
    be derived (its mail was purged) the previously stored snapshot is reused,
    which is what lets an old entry still be reopened.
    """
    user = _require_user(request)
    payload = request.json_object()
    state = _string(payload, "state", minimum=1, maximum=20)
    if state not in {"done", "open"}:
        raise ApiError(422, "无效的任务状态。")
    day = _string(payload, "day", default="", required=False, maximum=20)
    database = get_db()
    # A previously stored decision already carries the snapshot, which is the
    # only way to act on a task whose source mail has since been purged.
    snapshot: dict[str, Any] | None = database.task_states(user["id"]).get(task_key)
    view = task_day_view(user, day=day or (snapshot or {}).get("task_day", "") or "")
    for task in view["tasks"] + view["done"]:
        if task["task_key"] == task_key:
            snapshot = task
            break
    if snapshot is None:
        raise ApiError(404, "找不到这个任务。")
    database.set_task_state(user["id"], task_key, state, snapshot)
    view = task_day_view(user, day=day or snapshot.get("task_day", "") or "")
    return json_response({**view, "changed": task_key, "state": state})


@route("POST", "/api/mailbox/verify")
def verify_mailbox(request: Request) -> Response:
    """Explicit, user-triggered read-only IMAP check.

    Read-only by construction (``fetch_new_messages`` opens the mailbox with
    ``readonly=True`` and never marks or deletes), and it never moves the UID
    cursor, so pressing this button cannot cause a duplicated or skipped report.
    """
    user = _require_user(request)
    db = get_db()
    mailbox = db.get_mailbox(user["id"])
    if not mailbox:
        raise ApiError(422, "请先保存私人转发邮箱，再检查连接。")
    if not _verification_allowed(user["id"]):
        raise ApiError(429, "刚刚检查过了，请一分钟后再试。")
    db.record_mailbox_verification(mailbox["id"])
    try:
        result = get_service().test_mailbox(user["id"])
    except Exception as exc:
        message = str(exc)
        db.record_mailbox_verification(mailbox["id"], error=message)
        raise ApiError(400, message) from exc
    return json_response({"ok": True, **result, "dashboard": build_dashboard(user)})


@route("GET", "/api/account/export")
def account_export(request: Request) -> Response:
    """Download everything we hold about the caller, as one JSON file.

    Reading your own data is a right the privacy policy advertises, so it has to
    be a button that works rather than a promise to email us. Report bodies are
    decrypted here because the export is for the user, not for us; credentials
    are excluded by ``export_user_data``.
    """
    user = _require_user(request)
    service = get_service()
    data = get_db().export_user_data(user["id"])
    for row in data["reports"]:
        row["body_markdown"] = service.decrypt_report(row["body_markdown"], user["id"])
    data["exported_at"] = utc_now()
    data["format"] = "cityu-mail-pilot-export/1"
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    return Response(
        status=200,
        body=body,
        content_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="cityu-mail-pilot-{stamp}.json"',
            "Cache-Control": "no-store",
        },
    )


@route("GET", "/api/reports")
def reports(request: Request) -> Response:
    user = _require_user(request)
    rows = get_db().list_reports(user["id"], request.query_int("limit", 30))
    service = get_service()
    for row in rows:
        row["body_markdown"] = service.decrypt_report(row["body_markdown"], user["id"])
    return json_response(rows)


@route("PUT", r"/api/reports/(?P<report_id>[^/]+)/feedback")
def feedback(request: Request, report_id: str) -> Response:
    user = _require_user(request)
    payload = request.json_object()
    rating = _string(payload, "rating", minimum=1, maximum=40)
    note = _string(payload, "note", default="", required=False, maximum=1000)
    try:
        get_db().upsert_feedback(user["id"], report_id, rating, note)
    except (KeyError, ValueError) as exc:
        raise ApiError(404, str(exc)) from exc
    return json_response({"ok": True})


@route("PUT", r"/api/account/status/(?P<status>[A-Za-z]+)")
def account_status(request: Request, status: str) -> Response:
    user = _require_user(request)
    if status not in {"active", "paused", "deleted"}:
        raise ApiError(422, "无效状态。")
    get_db().set_user_status(user["id"], status)
    cookies = [_expired_cookie()] if status == "deleted" else None
    return json_response({"ok": True, "status": status}, cookies=cookies)


# --------------------------------------------------------------------------
# account security: password change and session revocation
# --------------------------------------------------------------------------


def _current_digest(request: Request) -> str | None:
    token = request.cookie(SESSION_COOKIE)
    return token_hash(token) if token else None


@route("GET", "/api/account/security")
def account_security(request: Request) -> Response:
    user = _require_user(request)
    return json_response({
        "email": user["email"],
        "session_days": SESSION_DAYS,
        "active_sessions": get_db().count_sessions(user["id"]),
    })


@route("PUT", "/api/account/password")
def change_password(request: Request) -> Response:
    """Change the password and revoke every other session.

    A password change is exactly the moment a borrowed or stolen device must
    lose access, so this is not optional: all sessions except the caller's are
    deleted, and the caller keeps working with a freshly minted cookie.
    """
    user = _require_user(request)
    _admin_rate_limit(f"password:{user['id']}")
    payload = request.json_object()
    current = _string(payload, "current_password", minimum=1, maximum=400)
    new_password = _string(payload, "new_password", minimum=12, maximum=400)
    database = get_db()
    record = database.find_user_for_login(user["email"])
    if not record or not verify_password(current, record["password_hash"]):
        raise ApiError(400, "当前密码不正确。")
    if verify_password(new_password, record["password_hash"]):
        raise ApiError(422, "新密码不能与当前密码相同。")
    database.set_password(user["id"], hash_password(new_password))
    removed = database.revoke_sessions(user["id"])
    database.record_audit(action="password_changed", actor_user_id=user["id"],
                          actor_email=user["email"], target_user_id=user["id"],
                          target_email=user["email"], detail=f"revoked={removed}",
                          client=request.client or "")
    logging.info("password changed for user %s; %s other session(s) revoked", user["id"], removed)
    # The caller's own session was revoked too, so hand them a new cookie.
    return json_response({"ok": True, "revoked": removed}, cookies=[_session_cookie(user["id"])])


@route("POST", "/api/account/sessions/revoke")
def revoke_own_sessions(request: Request) -> Response:
    """Sign out every device, including this one (a new cookie is issued)."""
    user = _require_user(request)
    _admin_rate_limit(f"revoke:{user['id']}")
    removed = get_db().revoke_sessions(user["id"])
    get_db().record_audit(action="signed_out_all_devices", actor_user_id=user["id"],
                          actor_email=user["email"], target_user_id=user["id"],
                          target_email=user["email"], detail=f"revoked={removed}",
                          client=request.client or "")
    return json_response({"ok": True, "revoked": removed}, cookies=[_session_cookie(user["id"])])


# --------------------------------------------------------------------------
# admin console
#
# Everything below requires an email listed in INFE_PILOT_ADMIN_EMAILS. The
# check is server-side only, operators cannot be created from the web, and no
# response ever contains an encrypted password, API key or invite hash.
# --------------------------------------------------------------------------


MAX_USERS_SETTING = "max_users"


def _max_users() -> tuple[int, str]:
    """The effective pilot cap, and where it came from.

    The stored setting wins over the environment. ``pilot.env`` is 0600 root and
    is read once at process start, so an operator who wants to raise the cap
    while the service runs has nowhere else to put it; the environment value
    stays as the install-time default. The source is reported so the admin panel
    can say which one is in force instead of leaving the operator guessing why
    an edit to pilot.env appeared to do nothing.
    """
    default = max(1, int(os.environ.get("INFE_PILOT_MAX_USERS", "5")))
    stored = get_db().get_setting(MAX_USERS_SETTING, "").strip()
    if stored:
        try:
            return max(1, min(1000, int(stored))), "settings"
        except ValueError:
            pass
    return default, "environment"


def _service_health() -> dict[str, Any]:
    database = get_db()
    now = dt.datetime.now(dt.timezone.utc)
    boxes = database.list_users_overview()
    considered = [row for row in boxes if row.get("mailbox_email") and row.get("mailbox_enabled")]
    # "Stale" must mean the same thing here as it does in the alert sentinel, and
    # both must follow the interval the mailbox actually gets. Gmail is polled
    # every 15 minutes on Google's own advice, so the flat five-minute threshold
    # this used to carry reported a perfectly healthy mailbox as broken — which
    # is exactly how a real problem would go unnoticed: an operator who has
    # learned the warning is noise stops reading it.
    stale = []
    for row in considered:
        seen = reports_mod.to_local(row.get("last_polled_at"), "UTC")
        if not seen or (now - seen) > alerting.stale_after_for(row):
            stale.append(row)
    return {
        "checked_at": now.isoformat(timespec="seconds"),
        "users": len(boxes),
        "active_users": sum(1 for row in boxes if row["status"] == "active"),
        "paused_users": sum(1 for row in boxes if row["status"] == "paused"),
        "mailboxes": len(considered),
        "mailboxes_polled_recently": len(considered) - len(stale),
        "stale_mailboxes": len(stale),
        # Named, so the warning can point at the mailbox instead of making the
        # operator open every user to find it.
        "stale_mailbox_emails": [str(row.get("mailbox_email") or "") for row in stale],
        "pending_messages": sum(int(row.get("queue_depth") or 0) for row in boxes),
        "failed_reports": sum(int(row.get("failed_reports") or 0) for row in boxes),
        "max_users": _max_users()[0],
        "max_users_source": _max_users()[1],
        "version": VERSION,
    }


@route("GET", "/api/admin/metrics")
def admin_metrics(request: Request) -> Response:
    """Live host and mail-pipeline numbers for the operator.

    Admin-only like the rest of ``/api/admin/*``; the host section reads /proc,
    which means it describes the machine the web service runs on. Values that
    the platform cannot provide come back as null so the panel can print “—”
    instead of pretending a missing reading is zero.
    """
    _require_admin(request)
    database = get_db()
    with database.connect() as connection:
        snapshot = metrics_mod.collect(connection)
    snapshot["service"] = _service_health()
    return json_response(snapshot)


@route("GET", "/api/admin/users")
def admin_users(request: Request) -> Response:
    _require_admin(request)
    database = get_db()
    users = database.list_users_overview()
    # The console sorts and flags on this, so it is computed once here rather
    # than re-derived in the browser from the same columns.
    for row in users:
        row["setup_gap"] = database.setup_gap(row)
    return json_response({
        "users": users,
        "stalled_users": sum(1 for row in users if row["setup_gap"]),
        "announcements": database.list_announcements(20),
        "invites": database.list_invites(100),
        "signups": database.list_signup_requests(100),
        "signup_counts": database.signup_request_counts(),
        "health": _service_health(),
        "admin_emails": sorted(_admin_emails()),
        "admins": _admin_roster(),
        "audit": database.list_audit(20),
    })


@route("PUT", r"/api/admin/users/(?P<user_id>[^/]+)/status/(?P<status>[A-Za-z]+)")
def admin_set_user_status(request: Request, user_id: str, status: str) -> Response:
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    if status not in {"active", "paused", "deleted"}:
        raise ApiError(422, "无效状态。")
    database = get_db()
    try:
        target = database.get_user(user_id)
    except KeyError as exc:
        raise ApiError(404, "用户不存在。") from exc
    if target["id"] == admin["id"] and status != "active":
        raise ApiError(422, "不能暂停或删除你自己正在使用的管理员账户。")
    if status == "deleted":
        # Deletion is the only irreversible operator action, so it is the only
        # one that demands a typed confirmation. Pausing is one click away from
        # being undone and does not need the extra friction (which is also
        # painful on a phone keyboard).
        confirmation = ""
        try:
            confirmation = str(request.json_object().get("confirm_email") or "").strip().lower()
        except ApiError:
            confirmation = ""
        if confirmation != str(target["email"]).strip().lower():
            raise ApiError(422, "删除是不可恢复操作：请输入该用户的完整邮箱以确认。")
    database.set_user_status(user_id, status)
    database.record_audit(action=f"user_status_{status}", actor_user_id=admin["id"],
                          actor_email=admin["email"], target_user_id=target["id"],
                          target_email=target["email"], client=request.client or "")
    # Audit trail also goes to journalctl; deliberately omits every secret.
    logging.info("admin %s set user %s status=%s", admin["id"], target["id"], status)
    return json_response({"ok": True, "user_id": user_id, "status": status,
                          "users": database.list_users_overview()})


# Settings an operator may change on somebody else's account. Everything here
# is reversible and audited; nothing here ever returns a stored secret. The API
# key and the mailbox app password are write-only: an operator can replace a
# broken one, but can never read the one that is there.
ADMIN_EDITABLE_PROFILE = ("school_email", "major", "year_of_study", "timezone",
                          "daily_time", "daily_enabled", "immediate_enabled")


@route("POST", r"/api/announcements/(?P<announcement_id>[^/]+)/dismiss")
def dismiss_announcement(request: Request, announcement_id: str) -> Response:
    """Hide one broadcast for this user only; everybody else still sees it."""
    user = _require_user(request)
    get_db().dismiss_announcement(announcement_id, user["id"])
    return json_response({"ok": True})


@route("POST", "/api/admin/announcements")
def admin_create_announcement(request: Request) -> Response:
    """Publish a broadcast to every active account.

    ``deliver_email`` chooses between a banner only and a banner plus one email
    per user's private mailbox. The emails are *queued* for the worker rather
    than sent here: the console must not hang while N mailboxes are contacted,
    and the worker already owns retries and per-user error reporting.
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object()
    title = _string(payload, "title", minimum=1, maximum=200)
    body = _string(payload, "body", minimum=1, maximum=4000)
    tone = _string(payload, "tone", default="info", required=False, maximum=20)
    if tone not in {"info", "warn", "critical"}:
        raise ApiError(422, "未知的公告类型。")
    deliver_email = _boolean(payload, "deliver_email", False)
    is_public = _boolean(payload, "public", False)
    database = get_db()
    announcement_id = database.create_announcement(
        title=title, body=body, tone=tone, deliver_email=deliver_email, created_by=admin["email"],
        is_public=is_public)
    database.record_audit(action="announcement_published", actor_user_id=admin["id"],
                          actor_email=admin["email"],
                          detail=f"id={announcement_id} email={int(deliver_email)} "
                                 f"board={int(is_public)}",
                          client=request.client or "")
    logging.info("admin %s published announcement %s (email=%s board=%s)",
                 admin["id"], announcement_id, deliver_email, is_public)
    return json_response({"ok": True, "id": announcement_id,
                          "announcements": database.list_announcements(20)})


@route("PUT", r"/api/admin/announcements/(?P<announcement_id>[^/]+)/board")
def admin_set_announcement_board(request: Request, announcement_id: str) -> Response:
    """Put an announcement on the public board at `/`, or take it off.

    Separate from publishing because the two audiences are different: the banner
    goes to accounts that signed up, the board is world-readable and indexable.
    The console asks for it explicitly rather than inferring it from "the
    operator wrote something", which would put every internal note on the open
    web by default.
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object()
    is_public = _boolean(payload, "public", False)
    database = get_db()
    try:
        row = database.set_announcement_public(announcement_id, is_public)
    except KeyError as exc:
        raise ApiError(404, str(exc)) from exc
    except ValueError as exc:
        raise ApiError(422, str(exc)) from exc
    database.record_audit(
        action="announcement_board_on" if is_public else "announcement_board_off",
        actor_user_id=admin["id"], actor_email=admin["email"], detail=f"id={announcement_id}",
        client=request.client or "")
    logging.info("admin %s set announcement %s board=%s", admin["id"], announcement_id, is_public)
    return json_response({"ok": True, "id": announcement_id, "is_public": row["is_public"],
                          "announcements": database.list_announcements(20)})


@route("PUT", r"/api/admin/announcements/(?P<announcement_id>[^/]+)/withdraw")
def admin_withdraw_announcement(request: Request, announcement_id: str) -> Response:
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    database = get_db()
    try:
        database.withdraw_announcement(announcement_id)
    except KeyError as exc:
        raise ApiError(404, str(exc)) from exc
    database.record_audit(action="announcement_withdrawn", actor_user_id=admin["id"],
                          actor_email=admin["email"], detail=f"id={announcement_id}",
                          client=request.client or "")
    return json_response({"ok": True, "announcements": database.list_announcements(20)})


@route("PUT", r"/api/admin/users/(?P<user_id>[^/]+)/settings")
def admin_update_user_settings(request: Request, user_id: str) -> Response:
    """Change another account's settings.

    Deliberately a *selective* patch: only the keys present in the body change.
    That is the whole reason this does not reuse ``PUT /api/profile``, which
    writes every field and defaults whatever is missing — an operator fixing a
    model name that way would silently wipe the user's courses and notes.
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object()
    database = get_db()
    try:
        target = database.get_user(user_id)
    except KeyError as exc:
        # Deleting an account removes the row outright (privacy deletion), so a
        # removed user simply does not exist here; there is no "deleted but
        # still editable" state to guard against.
        raise ApiError(404, "用户不存在。") from exc

    changed: list[str] = []

    # ---- profile fields -------------------------------------------------
    profile_update: dict[str, Any] = {}
    if "school_email" in payload:
        profile_update["school_email"] = _cityu_email(
            _string(payload, "school_email", default="", required=False, maximum=254))
    if "major" in payload:
        profile_update["major"] = _string(payload, "major", default="", required=False, maximum=200)
    if "year_of_study" in payload:
        profile_update["year_of_study"] = _string(payload, "year_of_study", default="", required=False, maximum=80)
    if "timezone" in payload:
        zone = _string(payload, "timezone", default="Asia/Hong_Kong", maximum=64)
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ApiError(422, "时区名称无效，例如 Asia/Hong_Kong。") from exc
        profile_update["timezone"] = zone
    if "daily_time" in payload:
        daily_time = _string(payload, "daily_time", default="22:00", maximum=5)
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", daily_time):
            raise ApiError(422, "每日发送时间必须是 HH:MM。")
        profile_update["daily_time"] = daily_time
    if "daily_enabled" in payload:
        profile_update["daily_enabled"] = _boolean(payload, "daily_enabled", True)
    if "immediate_enabled" in payload:
        profile_update["immediate_enabled"] = _boolean(payload, "immediate_enabled", True)
    if profile_update:
        database.upsert_profile(user_id, profile_update)
        changed.extend(sorted(profile_update))

    # ---- model / search connections -------------------------------------
    for kind, provider_key, name_key, base_key, secret_key, catalog in (
        ("model", "model_provider", "model_name", "model_base_url", "model_api_key", MODEL_PRESETS),
        ("search", "search_provider", "search_name", "search_base_url", "search_api_key", SEARCH_PRESETS),
    ):
        touched = [key for key in (provider_key, name_key, base_key, secret_key) if key in payload]
        if not touched:
            continue
        existing = database.get_connection(user_id, kind)
        provider = _string(payload, provider_key, default=(existing or {}).get("provider") or "", maximum=60)
        if provider not in catalog:
            raise ApiError(422, f"未知的{'模型' if kind == 'model' else '搜索'}供应商。")
        model = _string(payload, name_key, default=(existing or {}).get("model") or "", required=False, maximum=120)
        base_url = _string(payload, base_key, default=(existing or {}).get("base_url") or "",
                           required=False, maximum=300)
        if kind == "model":
            _, model, base_url = normalized_model_config(provider, model, base_url)
        elif not base_url:
            base_url = SEARCH_PRESETS[provider].get("base_url", "")
        if secret_key in payload:
            secret = _string(payload, secret_key, minimum=1, maximum=400)
            encrypted = get_service().secrets.encrypt(secret, context=f"connection:{user_id}:{kind}")
        elif existing:
            encrypted = existing["encrypted_api_key"]      # keep the stored one untouched
        else:
            raise ApiError(422, "首次配置需要提供 API key。")
        database.upsert_connection(user_id, {
            "kind": kind, "provider": provider, "model": model, "base_url": base_url,
            "encrypted_api_key": encrypted,
            "config_json": (existing or {}).get("config_json") or "{}",
            "enabled": True,
        })
        # Only the field names are recorded, never the values.
        changed.extend(sorted(touched))

    # ---- mailbox --------------------------------------------------------
    mailbox = database.get_mailbox(user_id)
    mailbox_touched = [key for key in ("report_to", "mailbox_app_password") if key in payload]
    if mailbox_touched:
        if not mailbox:
            raise ApiError(422, "该用户还没有配置私人邮箱。")
        # One upsert call covers both fields; the mailbox identity (address and
        # IMAP host) is passed through unchanged, so the UID cursor is kept and
        # no already-processed mail can be replayed.
        encrypted = mailbox["encrypted_password"]
        if "mailbox_app_password" in payload:
            secret = _string(payload, "mailbox_app_password", minimum=1, maximum=400)
            encrypted = get_service().secrets.encrypt(secret, context=f"mailbox:{user_id}")
        database.upsert_mailbox(user_id, {
            "email": mailbox["email"],
            "report_to": (_email(_string(payload, "report_to", maximum=254))
                          if "report_to" in payload else mailbox["report_to"]),
            "imap_host": mailbox["imap_host"], "imap_port": mailbox["imap_port"],
            "smtp_host": mailbox["smtp_host"], "smtp_port": mailbox["smtp_port"],
            "encrypted_password": encrypted,
        })
        changed.extend(sorted(mailbox_touched))

    if not changed:
        raise ApiError(422, "没有需要修改的字段。")

    database.record_audit(action="admin_user_settings_changed", actor_user_id=admin["id"],
                          actor_email=admin["email"], target_user_id=target["id"],
                          target_email=target["email"], detail="fields=" + ",".join(changed),
                          client=request.client or "")
    logging.info("admin %s changed %s for user %s", admin["id"], ",".join(changed), target["id"])
    overview = [row for row in database.list_users_overview() if row["id"] == target["id"]]
    return json_response({"ok": True, "user_id": user_id, "changed": changed,
                          "user": overview[0] if overview else None,
                          "users": database.list_users_overview(),
                          "audit": database.list_audit(20)})


def _delivery_state(row: dict[str, Any]) -> str:
    """One word for "what happened to this mail", from the operator's view.

    ``messages.status`` is authoritative for delivery: the worker flips it to
    ``sent`` only after the mail went out, and the stored report row is a
    *detail* (when, where to, latency) that older, migrated messages do not
    have. Judging delivery by the report row instead marked 41 already-delivered
    migration rows as "never sent" — right in the database, wrong on screen.
    """
    if row.get("status") == "skipped":
        return "skipped"
    if row.get("status") == "failed" or row.get("report_status") == "failed":
        return "failed"
    if row.get("status") == "sent" or row.get("report_status") == "sent":
        return "sent"
    if row.get("report_status") == "generated":
        return "generated"
    return "pending"


@route("GET", "/api/admin/messages")
def admin_messages(request: Request) -> Response:
    """Every incoming mail across all accounts, with its delivery outcome.

    Metadata only: subject, sender, times, statuses and errors. Message bodies
    are erased on delivery by design and are not exposed here either, so this
    panel answers "was it processed and delivered" without turning the operator
    console into a way to read other people's mail.
    """
    _require_admin(request)
    status = str(request.query.get("status", ["all"])[0] or "all")
    if status not in get_db().MESSAGE_FILTERS:
        raise ApiError(422, "未知的筛选条件。")
    try:
        limit = int(request.query.get("limit", ["50"])[0])
        offset = int(request.query.get("offset", ["0"])[0])
    except (TypeError, ValueError):
        raise ApiError(422, "分页参数必须是数字。")
    user_id = str(request.query.get("user_id", [""])[0] or "")

    database = get_db()
    page = database.list_messages_overview(limit=limit, offset=offset, status=status, user_id=user_id)
    for row in page["messages"]:
        row["delivery"] = _delivery_state(row)
        row["latency_seconds"] = None
        if row.get("sent_at") and row.get("received_at"):
            sent = reports_mod.to_local(row["sent_at"], "UTC")
            received = reports_mod.to_local(row["received_at"], "UTC")
            if sent and received:
                row["latency_seconds"] = round((sent - received).total_seconds(), 1)
        # The report *subject* (already selected) is enough to confirm the
        # generated report belongs to the right mail. The report body itself is
        # deliberately not decrypted here: this panel is about delivery, and the
        # operator console should not become a reader for other people's mail.
    page["status"] = status
    page["filters"] = sorted(database.MESSAGE_FILTERS)
    page["users"] = [{"id": row["id"], "email": row["email"]} for row in database.list_users_overview()]
    return json_response(page)


@route("GET", "/api/admin/usage")
def admin_usage(request: Request) -> Response:
    """Per-user token consumption and what it cost.

    Cost is only shown where a verified price exists (see ``pricing``); calls by
    an unpriced model are counted and reported as "价格未配置" rather than costed
    at zero, because a total that is silently too low is worse than no total.
    """
    _require_admin(request)
    try:
        days = int(request.query.get("days", ["30"])[0])
    except (TypeError, ValueError):
        raise ApiError(422, "天数必须是数字。")
    database = get_db()
    page = database.usage_overview(days=days)
    overrides = database.list_model_prices()
    page["prices"] = overrides
    page["known_prices"] = [
        {"provider": provider, "model": model, **price}
        for (provider, model), price in sorted(pricing_mod.DEFAULT_PRICES.items())
    ]
    page["currency_note"] = "费用为按供应商公开价目表估算，仅供参考；实际以你的账单为准。"
    return json_response(page)


@route("GET", "/api/admin/capacity")
def admin_capacity(request: Request) -> Response:
    """The pilot cap, plus a recommendation derived from live measurements.

    Deliberately not folded into ``/api/admin/metrics``: that one is polled every
    three seconds and describes the machine right now, whereas this is about how
    many *accounts* to admit and changes on the scale of days.
    """
    _require_admin(request)
    database = get_db()
    current, source = _max_users()
    # Imported here rather than at module scope so the web layer does not pull in
    # the scheduler (and its signal handling) just to read one constant.
    from . import capacity as capacity_mod
    from . import worker as worker_mod
    return json_response(capacity_mod.advise(
        volume=database.recent_volume(14),
        host=metrics_mod.host_metrics(),
        workers=worker_mod.REPORT_WORKERS,
        current=current,
        source=source,
    ))


@route("PUT", "/api/admin/capacity")
def admin_set_capacity(request: Request) -> Response:
    """Change how many accounts the pilot admits — from the panel, live.

    This is the one operator knob that used to require editing a 0600 root-owned
    file over SSH and restarting the service, which is why it now lives in the
    database. The environment value stays as the install-time default, and
    ``reset`` drops back to it.
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object()
    database = get_db()

    if _boolean(payload, "reset", False):
        database.delete_setting(MAX_USERS_SETTING)
        database.record_audit(action="capacity_reset", actor_user_id=admin["id"],
                              actor_email=admin["email"], client=request.client or "")
        current, source = _max_users()
        return json_response({"ok": True, "max_users": current, "source": source})

    try:
        value = int(payload.get("max_users"))
    except (TypeError, ValueError):
        raise ApiError(422, "名额必须是整数。")
    if value < 1 or value > 1000:
        raise ApiError(422, "名额需要在 1 到 1000 之间。")
    existing = database.count_users()
    if value < existing:
        # Not dangerous — the cap only gates new registrations — but the panel
        # would show a cap that nobody could fit under, which reads as a bug.
        raise ApiError(422, f"已经有 {existing} 个账号了，名额不能低于这个数。"
                            "要减少用户请到「已注册用户」里暂停或删除。")
    database.set_setting(MAX_USERS_SETTING, str(value), actor=admin["email"])
    database.record_audit(action="capacity_changed", actor_user_id=admin["id"],
                          actor_email=admin["email"], detail=str(value),
                          client=request.client or "")
    current, source = _max_users()
    return json_response({"ok": True, "max_users": current, "source": source})


@route("GET", "/api/admin/agent")
def admin_agent(request: Request) -> Response:
    """The AI operations assistant: switch, budget, recent analyses.

    Opening the panel must not cost money, so nothing here calls a model. The
    paid path is the explicit POST below, and the automatic one is the sentinel.
    """
    _require_admin(request)
    database = get_db()
    budget = agent_mod.budget_state(database)
    return json_response({
        "enabled": agent_mod.enabled(database),
        "install_default": agent_mod.enabled_from_environment(),
        "has_model_key": providers.platform_model_default() is not None,
        "budget": budget,
        "limits": {
            "daily_calls": agent_mod.AGENT_DAILY_CALLS,
            "per_mail": agent_mod.AGENT_MAX_PER_MAIL,
            "cooldown_hours": round(agent_mod.AGENT_COOLDOWN_SECONDS / 3600, 1),
        },
        "reports": agent_mod.report_for_panel(database, get_service().secrets, limit=10),
    })


@route("PUT", "/api/admin/agent")
def admin_set_agent(request: Request) -> Response:
    """Turn the assistant on or off. Database, not pilot.env — no SSH needed."""
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object()
    if "enabled" not in payload:
        raise ApiError(422, "缺少 enabled 字段。")
    wanted = _boolean(payload, "enabled", False)
    database = get_db()
    agent_mod.set_enabled(database, wanted, actor=admin["email"])
    database.record_audit(action="agent_toggled", actor_user_id=admin["id"],
                          actor_email=admin["email"], detail="on" if wanted else "off",
                          client=request.client or "")
    return json_response({"ok": True, "enabled": wanted})


@route("POST", "/api/admin/agent/analyze")
def admin_agent_analyze(request: Request) -> Response:
    """Explain what is wrong right now, on demand.

    Runs through the same budget gate and cooldown as the automatic path: the
    button is a convenience, not a way around the ceiling.
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    database = get_db()
    try:
        findings = alerting.evaluate(database)
    except Exception as exc:
        raise ApiError(500, f"巡检判定失败：{exc}")
    if not findings:
        return json_response({"ok": True, "findings": 0, "analyses": [],
                              "note": "现在没有异常。"})
    results = agent_mod.analyse_many(database, findings, secrets=get_service().secrets)
    database.record_audit(action="agent_analyzed", actor_user_id=admin["id"],
                          actor_email=admin["email"],
                          detail=f"findings={len(findings)} analysed={len(results)}",
                          client=request.client or "")
    return json_response({"ok": True, "findings": len(findings), "analyses": [
        {**item, "finding": item.get("finding")} for item in results]})


@route("PUT", "/api/admin/prices")
def admin_set_price(request: Request) -> Response:
    """Set or clear an operator price override for one provider+model."""
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object()
    provider = _string(payload, "provider", maximum=60)
    model = _string(payload, "model", maximum=120)
    database = get_db()
    if _boolean(payload, "remove", False):
        database.delete_model_price(provider, model)
        database.record_audit(action="price_removed", actor_user_id=admin["id"],
                              actor_email=admin["email"], detail=f"{provider}/{model}",
                              client=request.client or "")
        return json_response({"ok": True, "removed": f"{provider}/{model}",
                              "prices": database.list_model_prices()})

    def rate(name: str) -> float:
        try:
            value = float(payload.get(name))
        except (TypeError, ValueError):
            raise ApiError(422, f"{name} 必须是数字（每 100 万 token 的价格）。")
        if value < 0 or value > 100000:
            raise ApiError(422, f"{name} 超出合理范围。")
        return value

    database.set_model_price(
        provider, model,
        input_cache_hit=rate("input_cache_hit"),
        input_cache_miss=rate("input_cache_miss"),
        output=rate("output"),
        peak_multiplier=float(payload.get("peak_multiplier") or 1.0),
        currency=_string(payload, "currency", default="USD", required=False, maximum=8),
    )
    database.record_audit(action="price_set", actor_user_id=admin["id"], actor_email=admin["email"],
                          detail=f"{provider}/{model}", client=request.client or "")
    return json_response({"ok": True, "prices": database.list_model_prices()})


@route("POST", r"/api/admin/signups/(?P<request_id>[^/]+)")
def admin_decide_signup(request: Request, request_id: str) -> Response:
    """Approve or decline a pilot application.

    Approving mints a single-use invite and returns its code **exactly once** --
    the same rule as the invites panel, because only the hash is stored. The
    operator copies it into a reply; nothing here can hand out access twice.
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object()
    status = _string(payload, "status", minimum=1, maximum=20)
    if status not in {"invited", "declined", "pending"}:
        raise ApiError(422, "无效的申请状态。")
    database = get_db()
    try:
        row = database.decide_signup_request(request_id, status)
    except KeyError as exc:
        raise ApiError(404, "申请不存在。") from exc
    except ValueError as exc:
        raise ApiError(422, str(exc)) from exc

    code = ""
    emailed = False
    email_error = ""
    if status == "invited":
        # Approving the same applicant twice is a resend, not a mistake to
        # refuse: the usual reason is that the first message never arrived. Each
        # issuance therefore gets its own label, so the record of what was sent
        # to whom stays one row per attempt instead of two invites sharing one
        # name and a join that cannot tell them apart.
        label = f"signup-{row['email'][:40]}-{secrets.token_hex(3)}"
        code = database.create_invite(label, days=14)
        row = database.decide_signup_request(request_id, "invited", invite_label=label)
        if payload.get("email", True) is not False:
            # Sending is best-effort on purpose: the code is returned to the
            # operator either way, and losing a freshly minted single-use code
            # because SMTP hiccuped would be the worse failure.
            emailed, email_error, message_id = _email_invite(row, code)
            # Recorded whether it worked or not. "We tried and it failed" needs a
            # different response from "we never tried", and until this was stored
            # the only trace was a log line and the response to this one click.
            database.record_invite_email(request_id, sent=emailed, error=email_error,
                                         message_id=message_id)
    database.record_audit(action=f"signup_{status}", actor_user_id=admin["id"],
                          actor_email=admin["email"], target_email=row["email"],
                          detail=f"application {request_id}", client=request.client or "")
    logging.info("admin %s set signup %s status=%s", admin["id"], request_id, status)
    return json_response({"ok": True, "signup": row, "code": code,
                          "emailed": emailed, "email_error": email_error,
                          "signups": database.list_signup_requests(100),
                          "signup_counts": database.signup_request_counts(),
                          "invites": database.list_invites(100)})


def _email_invite(row: dict[str, Any], code: str) -> tuple[bool, str, str]:
    """Mail one applicant their invite code. Never raises.

    The message states plainly that the pilot is free and whose model account
    the mail will pass through, because the person reading it has not opened the
    site again and this may be the only place they see either fact.

    Returns ``(sent, error, message_id)``. The message id is kept because it is
    the only handle a human has for correlating our send with the provider's log
    or with the headers of the message the applicant says never arrived.
    """
    origin = os.environ.get("INFE_PILOT_ORIGIN", "").rstrip("/")
    app_url = f"{origin}/app" if origin else "/app"
    subject = "你的 CityU Mail Pilot 内测邀请码"
    body = (
        f"你好，\n\n"
        f"你在 CityU Mail Pilot 的网站上申请了内测名额，已经通过了。\n\n"
        f"邀请码：{code}\n"
        f"（只能用一次，14 天内有效）\n\n"
        f"从这里注册：{app_url}\n\n"
        f"几点需要你知道：\n"
        f"· 内测期间完全免费。模型调用默认用管理员提供的 key，费用由管理员承担；\n"
        f"  你也可以在「AI 模型」里换成自己的 key，那样费用和调用记录都归你自己。\n"
        f"· 生成报告时，邮件正文会发送给大模型服务商。用管理员的 key 时，\n"
        f"  服务商把这次调用记在管理员账号下——如果你不接受，请填自己的 key。\n"
        f"· 报告由 AI 生成，可能出错，不构成学校的官方通知，请以原始邮件为准。\n"
        f"· 我们只读你的邮箱，不删信、不改动；报告发出后数据库里的正文会立即清空。\n\n"
        f"完整说明见 {origin or ''}/privacy 和 {origin or ''}/terms 。\n"
    )
    try:
        service = get_service()
        receipt = alerting.send_as_operator(get_db(), service.secrets, row["email"], subject, body)
        logging.info("invite emailed to %s from %s id=%s",
                     row["email"], receipt.get("from", ""), receipt.get("message_id", ""))
        if receipt.get("refused"):
            # send_message only raises when *every* recipient is refused; a
            # partial refusal comes back as a map. Treating that as success would
            # record a delivery that did not happen.
            return False, f"收件人被拒绝：{receipt['refused']}", receipt.get("message_id", "")
        return True, "", receipt.get("message_id", "")
    except Exception as exc:  # noqa: BLE001 - the code is still returned
        logging.warning("could not email the invite to %s", row["email"], exc_info=True)
        return False, str(exc)[:200], ""


@route("POST", "/api/admin/admins")
def admin_grant(request: Request) -> Response:
    """Give an existing account operator rights.

    Re-authentication is required (see ``_confirm_operator``), the target has to
    be an account that already exists, and the whole thing is audited. The three
    together are what keep "an operator can create operators" from being a
    privilege-escalation bug: a stolen session alone is not enough, a typo cannot
    hand rights to a stranger who registers later, and the grant is attributable.
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object()
    _confirm_operator(request, admin)
    email = _email(_string(payload, "email", maximum=254))
    try:
        row = get_db().grant_admin(email)
    except KeyError as exc:
        raise ApiError(404, str(exc)) from exc
    except ValueError as exc:
        raise ApiError(422, str(exc)) from exc
    get_db().record_audit(action="admin_granted", actor_user_id=admin["id"],
                          actor_email=admin["email"], target_user_id=row["id"],
                          target_email=row["email"], client=request.client or "")
    logging.info("admin %s granted operator rights to %s", admin["id"], row["email"])
    return json_response({"ok": True, "admins": _admin_roster(),
                          "audit": get_db().list_audit(60)})


@route("POST", r"/api/admin/admins/(?P<user_id>[A-Za-z0-9_]+)/revoke")
def admin_revoke(request: Request, user_id: str) -> Response:
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    _confirm_operator(request, admin)
    database = get_db()
    try:
        row = database.revoke_admin(user_id)
    except KeyError as exc:
        raise ApiError(404, str(exc)) from exc
    # Refuse to leave the instance with nobody who can administer it. Environment
    # operators count, which is why the check is here and not inside the store:
    # this is the only layer that knows about them.
    if not _admin_emails() and database.count_admin_capable() == 0:
        database.grant_admin(row["email"])
        raise ApiError(422, "这是最后一个管理员，不能移除；否则没人能再管理这个实例。")
    database.record_audit(action="admin_revoked", actor_user_id=admin["id"],
                          actor_email=admin["email"], target_user_id=row["id"],
                          target_email=row["email"], client=request.client or "")
    logging.info("admin %s revoked operator rights from %s", admin["id"], row["email"])
    return json_response({"ok": True, "admins": _admin_roster(),
                          "audit": database.list_audit(60)})


def _admin_roster() -> list[dict[str, Any]]:
    """Everyone who can administer this instance, and where the right comes from.

    The environment-named accounts are listed even though they are not rows, so
    the console answers the question an operator actually has -- "who can do what
    I am doing?" -- rather than only the subset the console can edit.
    """
    roster = [
        {"id": "", "email": address, "source": "env", "removable": False, "status": "active"}
        for address in sorted(_admin_emails())
    ]
    stored = {row["email"].lower() for row in roster}
    for row in get_db().database_admins():
        if row["email"].lower() in stored:
            # Named in both places. The environment wins for display, because
            # removing the stored flag would not actually take the rights away
            # and a console that implied otherwise would be lying.
            continue
        roster.append({"id": row["id"], "email": row["email"], "source": "database",
                       "removable": True, "status": row["status"]})
    return roster


@route("POST", "/api/admin/invites")
def admin_create_invite(request: Request) -> Response:
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object()
    label = _string(payload, "label", default="pilot", required=False, maximum=100)
    try:
        days = int(payload.get("days", 7))
    except (TypeError, ValueError):
        raise ApiError(422, "有效天数必须是数字。")
    if not 1 <= days <= 90:
        raise ApiError(422, "有效天数需在 1–90 之间。")
    code = get_db().create_invite(label or "pilot", days)
    get_db().record_audit(action="invite_created", actor_user_id=admin["id"],
                          actor_email=admin["email"], detail=f"label={label or 'pilot'} days={days}",
                          client=request.client or "")
    logging.info("admin %s created invite label=%s days=%s", admin["id"], label, days)
    # The plaintext code is returned exactly once and never stored.
    return json_response({"ok": True, "code": code, "label": label or "pilot", "days": days,
                          "invites": get_db().list_invites(100)})


@route("DELETE", r"/api/admin/invites/(?P<label>[^/]+)")
def admin_expire_invite(request: Request, label: str) -> Response:
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    retired = get_db().expire_invite(label)
    get_db().record_audit(action="invite_revoked", actor_user_id=admin["id"],
                          actor_email=admin["email"], detail=f"label={label} retired={retired}",
                          client=request.client or "")
    logging.info("admin %s expired %s invite(s) labelled %s", admin["id"], retired, label)
    return json_response({"ok": True, "retired": retired, "invites": get_db().list_invites(100)})


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------


def dispatch(request: Request) -> Response:
    """Resolve a request to a response; never raises for expected failures."""
    if request.method not in AUTHENTICATED_METHODS:
        allowed_origin = os.environ.get("INFE_PILOT_ORIGIN", "").rstrip("/")
        if allowed_origin and request.origin and request.origin != allowed_origin:
            return error_response(403, "Origin rejected")
    # The two Android-distribution routes sit next to the static files rather
    # than in the `@route` table: both are "read a document off disk and send
    # it", which is what the block below does, and neither is part of the API.
    if request.path == ASSETLINKS_PATH and request.method in {"GET", "HEAD"}:
        document = assetlinks_document()
        if document is None:
            return error_response(404, "页面不存在。")
        # `application/json`, without the charset the API responses carry:
        # Android's verifier is strict about the media type of this document.
        return Response(status=200, body=document,
                        content_type="application/json",
                        headers={"Cache-Control": "public, max-age=300"})
    if request.path == APK_ROUTE and request.method in {"GET", "HEAD"}:
        target = apk_path()
        if target is None:
            return error_response(404, "安装包尚未提供。")
        return file_response(target, APK_MEDIA_TYPE, download_name=APK_FILENAME)
    if request.path in STATIC_FILES and request.method in {"GET", "HEAD"}:
        name, content_type = STATIC_FILES[request.path]
        target = (STATIC_ROOT / name).resolve()
        if STATIC_ROOT not in target.parents or not target.is_file():
            return error_response(404, "页面不存在。")
        if request.path in TEMPLATED_STATIC:
            body = (render_landing_page(target) if request.path == "/"
                    else render_legal_page(target))
            return Response(status=200, body=body,
                            content_type=content_type, headers={"Cache-Control": "no-cache"})
        return file_response(target, content_type)
    candidates = ROUTES.get(request.method, [])
    path_matched = False
    for pattern, handler in candidates:
        match = pattern.match(request.path)
        if not match:
            continue
        path_matched = True
        try:
            groups = {key: value for key, value in match.groupdict().items() if value is not None}
            return handler(request, **groups)
        except ApiError as exc:
            return error_response(exc.status, exc.detail)
    for method, entries in ROUTES.items():
        if method != request.method and any(pattern.match(request.path) for pattern, _ in entries):
            path_matched = True
            break
    if path_matched:
        return error_response(405, "方法不被允许。")
    return error_response(404, "资源不存在。")


class PilotHandler(BaseHTTPRequestHandler):
    server_version = "CityUMailPilot/" + VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        # Request-line logging for journalctl. The query string is stripped so a
        # ?token=... never reaches the logs, and bodies are never logged.
        message = re.sub(r"\?[^\s]*", "", format % args)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.server.log.append((stamp, message))  # type: ignore[attr-defined]
        self.server.log = self.server.log[-500:]  # type: ignore[attr-defined]
        print(f"{stamp} {message}", file=sys.stderr, flush=True)

    def _discard(self, length: int) -> None:
        """Drain a bounded amount so a client can finish writing before we refuse.

        The bound has to be at least as large as the biggest body any route will
        announce, or "your file is too big" is delivered as a connection reset
        instead of a 413: we stop reading, the client is still writing, and the
        kernel answers with RST. The background-upload cap is 1.5 MB, so a 4 MB
        window covers every refusal we can actually issue and stays bounded for
        the ones we cannot.
        """
        remaining = min(length, 4_194_304)
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _read_body(self, limit: int = MAX_BODY_BYTES) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            return b""
        try:
            length = int(raw_length)
        except ValueError as exc:
            self.close_connection = True
            raise ApiError(400, "Content-Length 无效。") from exc
        if length < 0:
            self.close_connection = True
            raise ApiError(400, "Content-Length 无效。")
        if length > limit:
            self._discard(length)
            self.close_connection = True
            raise ApiError(413, "请求内容过大。")
        return self.rfile.read(length) if length else b""

    def _respond(self, response: Response, *, head_only: bool = False) -> None:
        # `head_only` suppresses the *write*, not the body. The response is built
        # in full either way, so Content-Length is the real length -- emptying
        # the body at the call site instead (which is what four handlers here
        # used to do before the APK download needed an honest size) reports
        # `Content-Length: 0` for every HEAD, which nothing notices until a
        # client wants the size before committing to the bytes.
        self.send_response(response.status)
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        for cookie in response.cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if not head_only and response.body:
            self.wfile.write(response.body)

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        body = b""
        # The body has to be read before the router runs, so the one route that
        # accepts more than JSON declares itself here as well as below. Keeping
        # the path in a constant is what stops the two from drifting apart.
        limit = MAX_BACKGROUND_BYTES if parsed.path == BACKGROUND_PATH else MAX_BODY_BYTES
        try:
            body = self._read_body(limit)
        except ApiError as exc:
            self._respond(error_response(exc.status, exc.detail))
            return
        request = Request(
            method=method,
            path=parsed.path,
            query=parse_qs(parsed.query, keep_blank_values=True),
            headers=self.headers,
            body=body,
            client=self.client_address[0],
        )
        try:
            response = dispatch(request)
        except Exception:  # pragma: no cover - defensive
            traceback.print_exc()
            response = error_response(500, "服务器内部错误。")
        try:
            self._respond(response, head_only=method == "HEAD")
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - client went away
            pass

    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")


class PilotServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, PilotHandler)
        self.log: list[tuple[str, str]] = []


def create_server(host: str = "127.0.0.1", port: int = 8787) -> PilotServer:
    """Build a ready-to-serve instance; ``port=0`` picks a free port."""
    return PilotServer((host, port))


def configure_logging() -> None:
    """Turn on INFO logging for this process.

    Only worker.py used to call logging.basicConfig, so every logging.info() in
    the web process went to a root logger still sitting at WARNING and was thrown
    away. That is how "invite emailed to ..." came to leave no trace anywhere: the
    message may well have gone out, and nothing recorded that it had. The audit
    table still holds admin decisions, but the log is what a person reads when
    they want to know what the process actually did, and it was silently empty.

    Split out of main() so a test can call it: the bug was invisible precisely
    because nothing exercised this line.
    """
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CityU Mail Pilot web service")
    parser.add_argument("--host", default=os.environ.get("INFE_PILOT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("INFE_PILOT_PORT", "8787")))
    args = parser.parse_args(argv)
    configure_logging()
    server = create_server(args.host, args.port)
    host, port = server.server_address[0], server.server_address[1]
    print(f"CityU Mail Pilot {VERSION} listening on http://{host}:{port}", flush=True)

    def _stop(signum: int, frame: Any) -> None:  # pragma: no cover - signal path
        # shutdown() must run on another thread than serve_forever().
        threading.Thread(target=server.shutdown, daemon=True).start()

    for name in ("SIGTERM", "SIGINT"):
        handler = getattr(signal, name, None)
        if handler is not None:
            signal.signal(handler, _stop)

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
