import os
import re
from datetime import datetime
import humanize

_FULL_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

_BARE_DOMAIN_RE = re.compile(
    r"(?:www\.)?"
    r"(?:youtu\.be|youtube\.com|instagram\.com|tiktok\.com|"
    r"vm\.tiktok\.com|vt\.tiktok\.com|twitter\.com|x\.com|"
    r"facebook\.com|fb\.watch|reddit\.com|v\.redd\.it|"
    r"vimeo\.com|dailymotion\.com|twitch\.tv|pinterest\.com|"
    r"linkedin\.com|snapchat\.com|rumble\.com|bilibili\.com|"
    r"streamable\.com|medal\.tv|triller\.co|likee\.video|coub\.com|"
    r"t\.co|bit\.ly|tinyurl\.com|ow\.ly|short\.io)"
    r"[^\s<>\"']*",
    re.IGNORECASE,
)


PLANS = {
    "10days": {"days": 10, "price_usd": 2.99,  "label": "10 Days"},
    "30days": {"days": 30, "price_usd": 6.99,  "label": "30 Days"},
    "60days": {"days": 60, "price_usd": 11.99, "label": "60 Days"},
    "90days": {"days": 90, "price_usd": 15.99, "label": "90 Days"},
}

PAYMENT_METHODS = {
    "paypal":      "PayPal",
    "crypto":      "Crypto (BTC/ETH/USDT)",
    "apple_pay":   "Apple Pay",
    "credit_card": "Credit / Debit Card",
    "upi":         "UPI",
}

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "").strip().lstrip("@")


def admin_link() -> str:
    """Return a clickable @admin mention, or plain 'admin' if not set."""
    if ADMIN_USERNAME:
        return f"[@{ADMIN_USERNAME}](https://t.me/{ADMIN_USERNAME})"
    return "the admin"


def admin_url() -> str | None:
    """Return the admin's t.me URL, or None if not set."""
    if ADMIN_USERNAME:
        return f"https://t.me/{ADMIN_USERNAME}"
    return None


def is_url(value: str) -> bool:
    return value.strip().startswith(("http://", "https://"))


def extract_url(text: str) -> str | None:
    m = _FULL_URL_RE.search(text)
    if m:
        return m.group(0).rstrip(".,!?;:)>\"'")
    m = _BARE_DOMAIN_RE.search(text)
    if m:
        return "https://" + m.group(0).rstrip(".,!?;:)>\"'")
    return None


def format_size(size_bytes: int) -> str:
    if not size_bytes:
        return "Unknown"
    return humanize.naturalsize(size_bytes, binary=True)


def format_duration(seconds: int) -> str:
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def format_time_left(expiry: datetime) -> str:
    if not expiry:
        return "N/A"
    now = datetime.now()
    if expiry < now:
        return "Expired"
    return humanize.naturaldelta(expiry - now)


def progress_bar(percent: int, length: int = 10) -> str:
    filled = int(length * percent / 100)
    return f"[{'█' * filled}{'░' * (length - filled)}] {percent}%"


def get_help_text(is_admin: bool = False) -> str:
    contact = f"\n**Support:** {admin_link()}" if ADMIN_USERNAME else ""

    text = (
        "**📥 Video Downloader Bot**\n\n"
        "Drop any video link and I'll download it for you — that's it.\n\n"
        "**Works with:**\n"
        "YouTube • Instagram • TikTok • Twitter/X • Facebook • Reddit • "
        "Vimeo • Dailymotion • Twitch • Pinterest • Rumble • Bilibili • and 1000+ more\n\n"
        "**Commands:**\n"
        "/start — Main menu\n"
        "/help — This message\n"
        "/status — Check your downloads & plan\n"
        "/plans — See premium pricing\n\n"
        "**Free plan:** 10 downloads (lifetime)\n"
        "**Premium:** Unlimited, all platforms\n\n"
        f"**Payment options:** PayPal • Crypto • Apple Pay • Card • UPI"
        f"{contact}"
    )

    if is_admin:
        text += (
            "\n\n**─── Admin ───**\n"
            "/addpremium <user_id> <days>\n"
            "/revokepremium <user_id>\n"
            "/ban <user_id>  •  /unban <user_id>\n"
            "/stats  •  /users [limit]\n"
            "/broadcast <message>\n"
            "/backup  •  /listbackups  •  /restore <file>"
        )

    return text
