"""Mailbox presets and plain-language onboarding help.

Most pilot users have never heard of IMAP, SMTP, a port, or an app password.
Instead of showing four empty technical fields, the UI offers "which mailbox do
you use?" and fills the server names and ports automatically; the jargon moves
behind short explanations and per-provider steps for obtaining an app password.

The preset-data + human support-page split follows the MIT-licensed
`Monogramm/autodiscover-email-settings` project (Thunderbird autoconfig style):
settings are data, and the user gets prose rather than raw fields.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# How to explain the jargon (shown in the UI, without the user asking)
# ---------------------------------------------------------------------------

GLOSSARY: dict[str, dict[str, str]] = {
    "imap": {
        "term": "收件服务器",
        "technical": "IMAP 服务器 / 端口",
        "plain": "别人替你看信时的“取信箱地址”。程序从这里只读地把新邮件取回来，不会删除或改动你的邮件。",
        "typical": "多数邮箱是 imap.你的邮箱.com，端口 993（加密）。",
    },
    "smtp": {
        "term": "发件服务器",
        "technical": "SMTP 服务器 / 端口",
        "plain": "别人替你寄信时的“寄信箱地址”。程序从这里把写好的报告发到你指定的收件地址。",
        "typical": "多数邮箱是 smtp.你的邮箱.com，端口 465（加密）。",
    },
    "password": {
        "term": "授权码（应用专用密码）",
        "technical": "App password / 授权码",
        "plain": "专门发给“程序”用的另一套密码，不是你的邮箱登录密码。它只对这一件事有效，你随时能在邮箱设置里作废重发。",
        "typical": "通常是一串 16 位字符，只在生成时显示一次。",
    },
}


# ---------------------------------------------------------------------------
# Per-provider presets
# ---------------------------------------------------------------------------

MAILBOX_PRESETS: list[dict[str, Any]] = [
    {
        "id": "qq",
        "label": "QQ 邮箱 / Foxmail",
        "domains": ["qq.com", "foxmail.com", "vip.qq.com"],
        "imap_host": "imap.qq.com",
        "imap_port": 993,
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "help_url": "https://help.mail.qq.com/detail/0/1087",
        "help_label": "QQ 邮箱官方帮助：如何开启 IMAP/SMTP 并取得授权码",
        "steps": [
            "用电脑浏览器登录 QQ 邮箱网页版。",
            "打开「设置 → 账户」，找到「IMAP/SMTP 服务」。",
            "点「开启」，按提示用手机发一条短信完成验证。",
            "屏幕会弹出一串 16 位字符，那就是授权码，复制它。",
            "它只显示一次：先粘贴到下面的输入框，再关掉那个弹窗。",
        ],
    },
    {
        "id": "163",
        "label": "网易邮箱（163 / 126 / yeah.net）",
        "domains": ["163.com", "126.com", "yeah.net", "vip.163.com", "vip.126.com"],
        "imap_host": "imap.163.com",
        "imap_port": 993,
        "smtp_host": "smtp.163.com",
        "smtp_port": 465,
        "help_url": "https://help.mail.163.com/faqDetail.do?code=d7a5dc8471cd0c0e8b4b8f4f8e49998b374173cfe9171305fa1ce630d7f67ac286624f309a1a7089",
        "help_label": "网易邮箱官方帮助：客户端授权码",
        "steps": [
            "用电脑浏览器登录网易邮箱网页版。",
            "打开「设置 → POP3/SMTP/IMAP」。",
            "勾选开启「IMAP/SMTP 服务」，按提示用手机发短信验证。",
            "设置一个「客户端授权码」（自己起名字，例如“邮件助手”）。",
            "复制生成的那串授权码填到下面。",
        ],
    },
    {
        "id": "gmail",
        "label": "Gmail",
        "domains": ["gmail.com", "googlemail.com"],
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 465,
        "help_url": "https://myaccount.google.com/apppasswords",
        "help_label": "Google 官方页面：应用专用密码",
        "steps": [
            "先给 Google 账号开启「两步验证」（没开的话应用专用密码不可用）。",
            "打开 myaccount.google.com/apppasswords。",
            "输入一个名字，例如 “CityU Mail Pilot”，点生成。",
            "复制弹出的 16 位密码（可以去掉空格）。",
            "如果页面提示不可用，通常是没开两步验证，或账号由学校/单位托管。",
        ],
    },
    {
        "id": "icloud",
        "label": "iCloud 邮箱",
        "domains": ["icloud.com", "me.com", "mac.com"],
        "imap_host": "imap.mail.me.com",
        "imap_port": 993,
        "smtp_host": "smtp.mail.me.com",
        "smtp_port": 587,
        "help_url": "https://support.apple.com/zh-hk/102654",
        "help_label": "Apple 官方支持：使用 App 专用密码",
        "steps": [
            "打开 appleid.apple.com 并登录。",
            "进入「登录与安全 → App 专用密码」。",
            "点「+」生成一个，名字随意，例如 “邮件助手”。",
            "复制生成的那串密码填到下面。",
        ],
    },
    {
        "id": "outlook",
        "label": "Outlook / Hotmail / Live",
        "domains": ["outlook.com", "hotmail.com", "live.com", "msn.com"],
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "smtp_host": "smtp-mail.outlook.com",
        "smtp_port": 587,
        "help_url": "https://account.live.com/proofs/AppPassword",
        "help_label": "微软账号：应用密码页面",
        "steps": [
            "先给微软账号开启「双重验证」。",
            "打开 account.live.com/proofs/AppPassword 生成应用密码。",
            "复制生成的那串密码填到下面。",
        ],
        "caution": "微软正在逐步停用“账号密码直连邮箱”的方式。如果生成不了应用密码或连接一直失败，建议改用 QQ 邮箱或 Gmail 作为转发邮箱，城市大学的邮件照样能转过去。",
    },
    {
        "id": "yahoo",
        "label": "Yahoo Mail",
        "domains": ["yahoo.com", "yahoo.com.hk", "ymail.com"],
        "imap_host": "imap.mail.yahoo.com",
        "imap_port": 993,
        "smtp_host": "smtp.mail.yahoo.com",
        "smtp_port": 465,
        "steps": [
            "登录 Yahoo 账号安全设置，生成「应用密码 / App password」。",
            "复制那串密码填到下面。",
        ],
    },
    {
        "id": "custom",
        "label": "其它邮箱（我自己填服务器）",
        "domains": [],
        "imap_host": "",
        "imap_port": 993,
        "smtp_host": "",
        "smtp_port": 465,
        "steps": [
            "在你的邮箱设置里搜索 “IMAP” 和 “SMTP”，把两个服务器地址和端口抄下来。",
            "如果邮箱提供「授权码 / 应用专用密码」，用它；只有在你确认支持时才用登录密码。",
        ],
    },
]

PRESETS_BY_ID: dict[str, dict[str, Any]] = {item["id"]: item for item in MAILBOX_PRESETS}


def preset_id_for_email(email: str) -> str:
    """Return the preset id matching an email domain, else ``custom``."""
    domain = (email or "").strip().lower().rsplit("@", 1)[-1].rstrip(".")
    if not domain:
        return "custom"
    for item in MAILBOX_PRESETS:
        if domain in item["domains"]:
            return item["id"]
    return "custom"


def public_mailbox_help() -> dict[str, Any]:
    """Client-facing preset data. Never contains secrets."""
    return {
        "presets": [
            {
                "id": item["id"],
                "label": item["label"],
                "domains": item["domains"],
                "imap_host": item["imap_host"],
                "imap_port": item["imap_port"],
                "smtp_host": item["smtp_host"],
                "smtp_port": item["smtp_port"],
                "steps": item["steps"],
                "help_url": item.get("help_url", ""),
                "help_label": item.get("help_label", ""),
                "caution": item.get("caution", ""),
            }
            for item in MAILBOX_PRESETS
        ],
        "glossary": GLOSSARY,
    }
