"""
database.py - SQLite-backed subscriber store.

Schema
------
subscribers
  id                INTEGER PRIMARY KEY
  name              TEXT
  delivery_method   TEXT    -- 'email' | 'serverchan' | 'wechat_oa'
  delivery_target   TEXT    -- email address | SCT key | WeChat openid
  topics            TEXT    -- JSON list e.g. ["finance","sports","llm"]
  created_at        TEXT    -- ISO-8601 UTC
  active            INTEGER -- 1 = subscribed, 0 = unsubscribed
  unsubscribe_token TEXT UNIQUE

send_log
  id, subscriber_id, sent_at, story_count, success
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Generator, Optional

logger = logging.getLogger(__name__)

DB_PATH: str = os.getenv("DB_PATH", "subscribers.db")

# ── Topic registry ────────────────────────────────────────────────────────────
# Keys must match the feed category_tag values in config.ALL_FEEDS.
# Each entry: label, icon, keywords (used to boost ranking for this subscriber).

TOPICS: dict[str, dict] = {
    # ── Tech & AI ──────────────────────────────────────────────────────────
    "llm": {
        "label": "LLM & Chatbots",   "icon": "🤖",   "category": "AI & Tech",
        "feed_tags": ["tech", "research"],
        "keywords": ["llm", "gpt", "claude", "gemini", "mistral", "llama", "chatgpt",
                     "language model", "chatbot", "prompt", "fine-tun"],
    },
    "research": {
        "label": "AI Research",       "icon": "🔬",   "category": "AI & Tech",
        "feed_tags": ["research"],
        "keywords": ["research", "paper", "arxiv", "benchmark", "dataset",
                     "training", "preprint", "transformer", "neural"],
    },
    "agents": {
        "label": "AI Agents",         "icon": "🤝",   "category": "AI & Tech",
        "feed_tags": ["tech"],
        "keywords": ["agent", "rag", "tool use", "autonomous", "agentic",
                     "workflow", "orchestrat", "multi-agent"],
    },
    "opensource": {
        "label": "Open Source",       "icon": "🔧",   "category": "AI & Tech",
        "feed_tags": ["tech"],
        "keywords": ["open source", "github", "open-source", "hugging face",
                     "release", "repository", "weights"],
    },
    "hardware": {
        "label": "Hardware & Chips",  "icon": "💻",   "category": "AI & Tech",
        "feed_tags": ["tech"],
        "keywords": ["gpu", "chip", "nvidia", "tpu", "inference", "compute",
                     "hardware", "accelerat", "silicon", "datacenter"],
    },
    "safety": {
        "label": "AI Safety",         "icon": "🛡️",  "category": "AI & Tech",
        "feed_tags": ["tech"],
        "keywords": ["safety", "alignment", "ethics", "bias", "regulation",
                     "policy", "governance", "risk", "responsible ai"],
    },
    # ── Finance ────────────────────────────────────────────────────────────
    "markets": {
        "label": "Markets & Stocks",  "icon": "📈",   "category": "Finance",
        "feed_tags": ["finance"],
        "keywords": ["stock", "market", "s&p", "nasdaq", "dow", "shares",
                     "earnings", "ipo", "rally", "fed", "interest rate"],
    },
    "business": {
        "label": "Business",          "icon": "💼",   "category": "Finance",
        "feed_tags": ["finance"],
        "keywords": ["merger", "acquisition", "startup", "funding", "revenue",
                     "ceo", "company", "billion", "deal", "layoff"],
    },
    "crypto": {
        "label": "Crypto & Web3",     "icon": "₿",    "category": "Finance",
        "feed_tags": ["crypto"],
        "keywords": ["bitcoin", "ethereum", "crypto", "blockchain", "defi",
                     "nft", "web3", "solana", "token", "wallet"],
    },
    # ── World ──────────────────────────────────────────────────────────────
    "world": {
        "label": "World News",        "icon": "🌍",   "category": "World",
        "feed_tags": ["world"],
        "keywords": ["international", "global", "united nations", "treaty",
                     "conflict", "diplomat", "foreign", "summit", "sanction"],
    },
    "geopolitics": {
        "label": "Geopolitics",       "icon": "🏛️",  "category": "World",
        "feed_tags": ["geopolitics", "world"],
        "keywords": ["geopolit", "nato", "china", "russia", "usa", "election",
                     "government", "president", "minister", "military"],
    },
    "climate": {
        "label": "Climate",           "icon": "🌱",   "category": "World",
        "feed_tags": ["climate"],
        "keywords": ["climate", "carbon", "emission", "renewable", "solar",
                     "wind", "fossil", "warming", "cop", "environment"],
    },
    # ── Sports ─────────────────────────────────────────────────────────────
    "sports": {
        "label": "Sports",            "icon": "⚽",   "category": "Sports",
        "feed_tags": ["sports"],
        "keywords": ["football", "soccer", "basketball", "tennis", "nba",
                     "nfl", "premier league", "champion", "tournament", "athlete"],
    },
    # ── Health & Science ───────────────────────────────────────────────────
    "health": {
        "label": "Health & Medicine", "icon": "🏥",   "category": "Science",
        "feed_tags": ["health"],
        "keywords": ["health", "medical", "drug", "vaccine", "cancer", "fda",
                     "clinical", "disease", "treatment", "study"],
    },
    "science": {
        "label": "Science & Space",   "icon": "🚀",   "category": "Science",
        "feed_tags": ["health", "research"],
        "keywords": ["science", "nasa", "space", "physics", "biology",
                     "discovery", "experiment", "universe", "planet"],
    },
}

# Group topics by category for the UI
TOPIC_CATEGORIES: dict[str, list[str]] = {}
for _tid, _t in TOPICS.items():
    _cat = _t["category"]
    TOPIC_CATEGORIES.setdefault(_cat, []).append(_tid)

# Supported delivery methods
DELIVERY_METHODS = {
    "email":      {"label": "Email",                  "icon": "📧"},
    "serverchan": {"label": "WeChat via Server酱",     "icon": "💬"},
    "wechat_oa":  {"label": "WeChat Official Account", "icon": "🟢"},
}


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class Subscriber:
    id: int
    name: str
    delivery_method: str    # 'email' | 'serverchan' | 'wechat_oa'
    delivery_target: str    # email | SCT key | openid
    topics: list[str]
    created_at: str
    active: bool
    unsubscribe_token: str


# ── DB connection ─────────────────────────────────────────────────────────────

@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ── Schema + migration ────────────────────────────────────────────────────────

def init_db() -> None:
    """Create / migrate tables. Safe to call on every startup."""
    with _conn() as con:
        # New-style table
        con.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                name              TEXT    NOT NULL DEFAULT '',
                delivery_method   TEXT    NOT NULL DEFAULT 'serverchan',
                delivery_target   TEXT    NOT NULL,
                topics            TEXT    NOT NULL DEFAULT '[]',
                created_at        TEXT    NOT NULL,
                active            INTEGER NOT NULL DEFAULT 1,
                unsubscribe_token TEXT    NOT NULL UNIQUE
            )
        """)

        # Migration: if old schema had serverchan_key, rebuild the table
        cols = {r[1] for r in con.execute("PRAGMA table_info(subscribers)")}
        if "serverchan_key" in cols and "delivery_target" not in cols:
            logger.info("Migrating DB: old serverchan_key schema -> new delivery_method/target schema")
            con.execute("""
                CREATE TABLE subscribers_new (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    name              TEXT    NOT NULL DEFAULT '',
                    delivery_method   TEXT    NOT NULL DEFAULT 'serverchan',
                    delivery_target   TEXT    NOT NULL,
                    topics            TEXT    NOT NULL DEFAULT '[]',
                    created_at        TEXT    NOT NULL,
                    active            INTEGER NOT NULL DEFAULT 1,
                    unsubscribe_token TEXT    NOT NULL UNIQUE
                )
            """)
            con.execute("""
                INSERT INTO subscribers_new
                    (id, name, delivery_method, delivery_target, topics, created_at, active, unsubscribe_token)
                SELECT id, name, 'serverchan', serverchan_key, topics, created_at, active, unsubscribe_token
                FROM subscribers
            """)
            con.execute("DROP TABLE subscribers")
            con.execute("ALTER TABLE subscribers_new RENAME TO subscribers")
            logger.info("Migration complete.")

        con.execute("""
            CREATE TABLE IF NOT EXISTS send_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                subscriber_id INTEGER NOT NULL,
                sent_at       TEXT    NOT NULL,
                story_count   INTEGER NOT NULL DEFAULT 0,
                success       INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (subscriber_id) REFERENCES subscribers(id)
            )
        """)
    logger.info("Database ready at %s", DB_PATH)


# ── CRUD ──────────────────────────────────────────────────────────────────────

def add_subscriber(
    delivery_method: str,
    delivery_target: str,
    topics: list[str],
    name: str = "",
) -> Subscriber:
    """
    Insert a new subscriber or reactivate an existing one with the same target.

    Args:
        delivery_method: 'email' | 'serverchan' | 'wechat_oa'
        delivery_target: the actual address/key/openid
        topics:          list of topic IDs from TOPICS
        name:            optional display name

    Returns the saved Subscriber. Raises ValueError on bad input.
    """
    method = delivery_method.lower().strip()
    target = delivery_target.strip()

    if method not in DELIVERY_METHODS:
        raise ValueError(f"Unknown delivery method: {method!r}")

    if not target:
        raise ValueError("Delivery target cannot be empty.")

    # Per-method validation
    if method == "serverchan" and not target.startswith("SCT"):
        raise ValueError("Server酱 key must start with 'SCT'.")
    if method == "email" and "@" not in target:
        raise ValueError("Please enter a valid email address.")

    valid_topics = [t for t in topics if t in TOPICS]
    if not valid_topics:
        raise ValueError("Please select at least one topic.")

    token = secrets.token_hex(20)
    now = datetime.now(timezone.utc).isoformat()

    with _conn() as con:
        existing = con.execute(
            "SELECT id FROM subscribers WHERE delivery_target = ? AND delivery_method = ?",
            (target, method),
        ).fetchone()

        if existing:
            con.execute(
                "UPDATE subscribers SET name=?, topics=?, active=1, created_at=? WHERE id=?",
                (name, json.dumps(valid_topics), now, existing["id"]),
            )
            row = con.execute("SELECT * FROM subscribers WHERE id=?", (existing["id"],)).fetchone()
        else:
            con.execute(
                """INSERT INTO subscribers
                   (name, delivery_method, delivery_target, topics, created_at, active, unsubscribe_token)
                   VALUES (?, ?, ?, ?, ?, 1, ?)""",
                (name, method, target, json.dumps(valid_topics), now, token),
            )
            row = con.execute(
                "SELECT * FROM subscribers WHERE delivery_target=? AND delivery_method=?",
                (target, method),
            ).fetchone()

    logger.info(
        "Subscriber added/updated: method=%s target=%s topics=%s",
        method, _mask(target), valid_topics,
    )
    return _row_to_sub(row)


def get_active_subscribers() -> list[Subscriber]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM subscribers WHERE active=1 ORDER BY created_at"
        ).fetchall()
    return [_row_to_sub(r) for r in rows]


def get_subscriber_by_openid(openid: str) -> Optional[Subscriber]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM subscribers WHERE delivery_method='wechat_oa' AND delivery_target=?",
            (openid,),
        ).fetchone()
    return _row_to_sub(row) if row else None


def deactivate_by_token(token: str) -> bool:
    with _conn() as con:
        result = con.execute(
            "UPDATE subscribers SET active=0 WHERE unsubscribe_token=? AND active=1",
            (token,),
        )
    return result.rowcount > 0


def log_send(subscriber_id: int, story_count: int, success: bool) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO send_log (subscriber_id, sent_at, story_count, success) VALUES (?,?,?,?)",
            (subscriber_id, datetime.now(timezone.utc).isoformat(), story_count, int(success)),
        )


def subscriber_count() -> int:
    with _conn() as con:
        row = con.execute("SELECT COUNT(*) FROM subscribers WHERE active=1").fetchone()
    return row[0]


# ── Relevance boost ───────────────────────────────────────────────────────────

def topic_score_boost(text: str, selected_topics: list[str]) -> float:
    """
    Extra relevance score for an article based on subscriber's selected topics.
    Each keyword hit adds 0.1; total capped at 1.5.
    """
    text_lower = text.lower()
    boost = 0.0
    for topic_id in selected_topics:
        topic = TOPICS.get(topic_id)
        if not topic:
            continue
        for kw in topic["keywords"]:
            if kw in text_lower:
                boost += 0.1
    return min(boost, 1.5)


def relevant_feed_urls(selected_topics: list[str]) -> list[str]:
    """
    Return the feed URLs relevant to a subscriber's topic selection.
    Falls back to all feeds if no topics are matched.
    """
    import config
    needed_tags: set[str] = set()
    for tid in selected_topics:
        t = TOPICS.get(tid)
        if t:
            needed_tags.update(t.get("feed_tags", []))

    if not needed_tags:
        return config.RSS_FEEDS

    urls = []
    seen: set[str] = set()
    for url, tag in config.ALL_FEEDS:
        if tag in needed_tags and url not in seen:
            urls.append(url)
            seen.add(url)
    return urls or config.RSS_FEEDS


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row_to_sub(row: sqlite3.Row) -> Subscriber:
    return Subscriber(
        id=row["id"],
        name=row["name"],
        delivery_method=row["delivery_method"],
        delivery_target=row["delivery_target"],
        topics=json.loads(row["topics"]),
        created_at=row["created_at"],
        active=bool(row["active"]),
        unsubscribe_token=row["unsubscribe_token"],
    )


def _mask(target: str) -> str:
    """Safely truncate for logging."""
    if len(target) <= 10:
        return target[:3] + "***"
    return target[:6] + "..." + target[-3:]
