"""
fetch.py — Pull articles from RSS feeds with retry logic and basic scraping fallback.

Each returned Article is a plain dataclass so downstream code never touches
raw feedparser/requests objects.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import feedparser
import requests
from bs4 import BeautifulSoup

import config

logger = logging.getLogger(__name__)

# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Article:
    title: str
    url: str
    summary: str          # raw excerpt / description from feed
    published: datetime
    source: str           # feed hostname label
    score: float = 0.0    # filled by rank.py
    full_text: str = ""   # filled lazily by scrape_full_text()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_date(entry: feedparser.FeedParserDict) -> datetime:
    """Return a timezone-aware datetime from a feedparser entry, or now() on failure."""
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)


def _request_with_retry(
    url: str,
    *,
    method: str = "GET",
    **kwargs,
) -> Optional[requests.Response]:
    """GET/POST with exponential back-off; returns None on total failure."""
    delay = config.RETRY_BACKOFF
    for attempt in range(1, config.RETRY_ATTEMPTS + 1):
        try:
            resp = requests.request(
                method,
                url,
                timeout=config.REQUEST_TIMEOUT,
                headers={"User-Agent": "DailyAIBrief/1.0 (+https://github.com/daily-ai-brief)"},
                **kwargs,
            )
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            logger.warning("Attempt %d/%d failed for %s: %s", attempt, config.RETRY_ATTEMPTS, url, exc)
            if attempt < config.RETRY_ATTEMPTS:
                time.sleep(delay)
                delay *= config.RETRY_BACKOFF
    return None


# ── RSS fetching ──────────────────────────────────────────────────────────────

def fetch_feed(feed_url: str) -> list[Article]:
    """
    Parse one RSS/Atom feed and return a list of Article objects.

    Falls back to an HTTP GET when feedparser cannot retrieve the feed
    directly (e.g. behind unusual redirects).
    """
    logger.info("Fetching feed: %s", feed_url)

    # Try feedparser's built-in retrieval first
    parsed = feedparser.parse(feed_url, agent="DailyAIBrief/1.0")

    # If feedparser got nothing, try fetching raw bytes ourselves
    if parsed.bozo and not parsed.entries:
        logger.debug("feedparser bozo; retrying via requests: %s", feed_url)
        resp = _request_with_retry(feed_url)
        if resp is None:
            logger.error("Skipping feed (unreachable): %s", feed_url)
            return []
        parsed = feedparser.parse(resp.content)

    source_label = parsed.feed.get("title") or _hostname(feed_url)
    articles: list[Article] = []

    for entry in parsed.entries:
        try:
            title = entry.get("title", "").strip()
            url = entry.get("link", "").strip()
            if not title or not url:
                continue

            # Prefer full content over summary when available
            raw_summary = ""
            if hasattr(entry, "content") and entry.content:
                raw_summary = entry.content[0].get("value", "")
            if not raw_summary:
                raw_summary = entry.get("summary", "") or entry.get("description", "")

            # Strip HTML tags from the summary
            clean_summary = _strip_html(raw_summary)[:1000]

            articles.append(
                Article(
                    title=title,
                    url=url,
                    summary=clean_summary,
                    published=_parse_date(entry),
                    source=source_label,
                )
            )
        except Exception as exc:
            logger.debug("Skipping malformed entry in %s: %s", feed_url, exc)

    logger.info("  → %d articles from %s", len(articles), source_label)
    return articles


def fetch_all_feeds(feed_urls: Optional[list[str]] = None) -> list[Article]:
    """
    Fetch every configured feed and merge results into one flat list.

    Args:
        feed_urls: Override the list from config (useful for testing).

    Returns:
        Unsorted, unfiltered list of all fetched articles.
    """
    urls = feed_urls or config.RSS_FEEDS
    all_articles: list[Article] = []

    for url in urls:
        try:
            articles = fetch_feed(url)
            all_articles.extend(articles)
        except Exception as exc:
            logger.error("Unexpected error fetching %s: %s", url, exc)

    logger.info("Total articles fetched: %d", len(all_articles))
    return all_articles


# ── Optional: full-text scraping ──────────────────────────────────────────────

def scrape_full_text(article: Article, max_chars: int = 4000) -> str:
    """
    Best-effort scrape of the article's landing page.

    Extracts the longest <article> or <main> block; falls back to all <p> tags.
    Returns an empty string on any failure so callers can degrade gracefully.
    """
    if article.full_text:
        return article.full_text  # already fetched

    resp = _request_with_retry(article.url)
    if resp is None:
        return ""

    try:
        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove noisy elements
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        # Prefer semantic containers
        container = soup.find("article") or soup.find("main")
        if container:
            text = container.get_text(separator=" ", strip=True)
        else:
            paragraphs = soup.find_all("p")
            text = " ".join(p.get_text(strip=True) for p in paragraphs)

        article.full_text = text[:max_chars]
        return article.full_text
    except Exception as exc:
        logger.debug("Scrape failed for %s: %s", article.url, exc)
        return ""


# ── Internal utilities ────────────────────────────────────────────────────────

def _strip_html(html: str) -> str:
    """Remove HTML markup and collapse whitespace."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        return " ".join(soup.get_text(separator=" ").split())
    except Exception:
        return html


def _hostname(url: str) -> str:
    """Extract a short label from a URL (e.g. 'techcrunch.com')."""
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc or url
    except Exception:
        return url
