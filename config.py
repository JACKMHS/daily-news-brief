"""
config.py — Centralised configuration loaded from environment variables.

All secrets and toggles live here. Set them in a .env file or your shell
before running main.py.
"""

import os
import logging

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Anthropic / Claude ────────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL: str   = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
MAX_INPUT_CHARS: int   = int(os.getenv("MAX_INPUT_CHARS", "3000"))

# ── Single-user push (legacy / testing) ──────────────────────────────────────
# Used only when running  python main.py  (not multi-subscriber web mode).
# "serverchan" | "wecom" | "email"
PUSH_MODE: str      = os.getenv("PUSH_MODE", "serverchan").lower()
SERVERCHAN_KEY: str = os.getenv("SERVERCHAN_KEY", "")
WECOM_WEBHOOK: str  = os.getenv("WECOM_WEBHOOK", "")

# ── Email delivery (SMTP) ─────────────────────────────────────────────────────
EMAIL_HOST: str     = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT: int     = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USER: str     = os.getenv("EMAIL_USER", "")       # sender address
EMAIL_PASS: str     = os.getenv("EMAIL_PASS", "")       # app password
EMAIL_FROM: str     = os.getenv("EMAIL_FROM", EMAIL_USER)
EMAIL_SUBJECT: str  = os.getenv("EMAIL_SUBJECT", "Your Daily News Brief")

# ── WeChat Official Account (公众号) ──────────────────────────────────────────
WECHAT_APPID: str       = os.getenv("WECHAT_APPID", "")
WECHAT_APPSECRET: str   = os.getenv("WECHAT_APPSECRET", "")
WECHAT_TEMPLATE_ID: str = os.getenv("WECHAT_TEMPLATE_ID", "")  # template message ID
# Your public domain (needed for WeChat OAuth redirect)
APP_BASE_URL: str = os.getenv("APP_BASE_URL", "http://localhost:5000")

# ── RSS feeds — grouped by category ──────────────────────────────────────────
# Each feed is tagged so we only fetch what's needed for subscribed topics.
# Format: (url, category_tag)
# category_tag matches the keys in database.TOPICS

ALL_FEEDS: list[tuple[str, str]] = [
    # AI & Tech
    ("https://techcrunch.com/feed/",                        "tech"),
    ("https://rss.arxiv.org/rss/cs.AI",                     "research"),
    ("https://hnrss.org/frontpage",                         "tech"),
    ("https://feeds.feedburner.com/venturebeat/SZYF",       "tech"),
    ("https://www.technologyreview.com/feed/",              "tech"),
    ("https://www.theverge.com/rss/index.xml",              "tech"),

    # Finance & Business
    ("https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines", "finance"),
    ("https://www.cnbc.com/id/100003114/device/rss/rss.html",             "finance"),
    ("https://finance.yahoo.com/news/rssindex",                           "finance"),
    ("https://feeds.reuters.com/reuters/businessNews",                    "finance"),

    # World / International Relations
    ("https://feeds.reuters.com/reuters/worldNews",         "world"),
    ("http://feeds.bbci.co.uk/news/world/rss.xml",          "world"),
    ("https://www.aljazeera.com/xml/rss/all.xml",           "world"),
    ("https://feeds.npr.org/1004/rss.xml",                  "world"),

    # Sports
    ("https://www.espn.com/espn/rss/news",                  "sports"),
    ("http://feeds.bbci.co.uk/sport/rss.xml",               "sports"),
    ("https://www.skysports.com/rss/12040",                 "sports"),

    # Health & Science
    ("https://feeds.reuters.com/reuters/healthNews",        "health"),
    ("https://rss.sciencedaily.com/rss.xml",                "health"),
    ("https://www.statnews.com/feed/",                      "health"),

    # Geopolitics / Politics
    ("https://foreignpolicy.com/feed/",                     "geopolitics"),
    ("https://feeds.reuters.com/Reuters/PoliticsNews",      "geopolitics"),

    # Climate & Environment
    ("https://www.theguardian.com/environment/rss",         "climate"),
    ("https://grist.org/feed/",                             "climate"),

    # Crypto & Web3
    ("https://coindesk.com/arc/outboundfeeds/rss/",         "crypto"),
    ("https://cointelegraph.com/rss",                       "crypto"),
]

# Build a flat deduplicated list for single-user (non-topic-filtered) runs
_seen: set[str] = set()
RSS_FEEDS: list[str] = []
for _url, _ in ALL_FEEDS:
    if _url not in _seen:
        _seen.add(_url)
        RSS_FEEDS.append(_url)

# ── Fallback global keywords (used for single-user mode ranking) ──────────────
AI_KEYWORDS: list[str] = [
    kw.strip().lower()
    for kw in os.getenv(
        "AI_KEYWORDS",
        "ai,llm,openai,gpt,claude,gemini,mistral,llama,transformer,"
        "machine learning,deep learning,neural,benchmark,research,agent,rag",
    ).split(",")
    if kw.strip()
]

# ── Ranking ───────────────────────────────────────────────────────────────────
TOP_N: int           = int(os.getenv("TOP_N", "5"))
MAX_AGE_HOURS: int   = int(os.getenv("MAX_AGE_HOURS", "48"))
DEDUP_THRESHOLD: float = float(os.getenv("DEDUP_THRESHOLD", "0.55"))

# ── Cache ─────────────────────────────────────────────────────────────────────
CACHE_FILE: str      = os.getenv("CACHE_FILE", ".title_cache.txt")
CACHE_MAX_LINES: int = int(os.getenv("CACHE_MAX_LINES", "500"))

# ── HTTP ──────────────────────────────────────────────────────────────────────
REQUEST_TIMEOUT: int   = int(os.getenv("REQUEST_TIMEOUT", "15"))
RETRY_ATTEMPTS: int    = int(os.getenv("RETRY_ATTEMPTS", "3"))
RETRY_BACKOFF: float   = float(os.getenv("RETRY_BACKOFF", "1.5"))


def validate(multi_subscriber: bool = False) -> None:
    """
    Raise early with a helpful message if required config is missing.

    Args:
        multi_subscriber: True when running --all-subscribers mode.
                          Skips single-user push-key checks because each
                          subscriber carries their own delivery credentials.
    """
    errors: list[str] = []

    if not ANTHROPIC_API_KEY:
        errors.append("ANTHROPIC_API_KEY is not set.")

    # Single-user push validation — not needed in multi-subscriber mode
    # because every subscriber row stores its own key/address.
    if not multi_subscriber:
        if PUSH_MODE == "serverchan" and not SERVERCHAN_KEY:
            errors.append("PUSH_MODE=serverchan but SERVERCHAN_KEY is not set.")
        if PUSH_MODE == "wecom" and not WECOM_WEBHOOK:
            errors.append("PUSH_MODE=wecom but WECOM_WEBHOOK is not set.")
        if PUSH_MODE == "email" and not EMAIL_USER:
            errors.append("PUSH_MODE=email but EMAIL_USER is not set.")
        if PUSH_MODE not in ("serverchan", "wecom", "email"):
            errors.append(f"PUSH_MODE must be serverchan/wecom/email, got: {PUSH_MODE!r}")

    if errors:
        for e in errors:
            logger.error("Config error: %s", e)
        raise EnvironmentError("Invalid configuration. Fix the errors above and retry.")

    logger.info(
        "Config OK - model=%s | multi_subscriber=%s | feeds=%d | top_n=%d",
        ANTHROPIC_MODEL, multi_subscriber, len(RSS_FEEDS), TOP_N,
    )
