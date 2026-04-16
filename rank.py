"""
rank.py — Deduplicate, score, and select the top-N articles for the brief.

Scoring considers:
  • Recency          – exponential decay over MAX_AGE_HOURS
  • Keyword match    – count of AI_KEYWORDS found in title + summary
  • Title length     – slight bonus for descriptive titles (up to 80 chars)

Deduplication uses a fast bigram-based Jaccard similarity so we don't pull
in heavy NLP dependencies for an MVP.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from math import exp
from pathlib import Path
from typing import Optional

import config
from fetch import Article

logger = logging.getLogger(__name__)


# ── Scoring ───────────────────────────────────────────────────────────────────

def _age_hours(article: Article) -> float:
    """Hours since article was published (non-negative)."""
    now = datetime.now(timezone.utc)
    pub = article.published
    # Ensure both are timezone-aware
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    delta = now - pub
    return max(delta.total_seconds() / 3600, 0.0)


def _recency_score(age_hours: float) -> float:
    """
    Exponential decay: score=1.0 when fresh, ~0.37 at MAX_AGE_HOURS.
    Articles older than MAX_AGE_HOURS still receive a score > 0 so they
    can appear if nothing fresher covers the topic.
    """
    return exp(-age_hours / max(config.MAX_AGE_HOURS, 1))


def _keyword_score(article: Article) -> float:
    """
    Fraction of AI_KEYWORDS that appear in title + summary (capped at 1.0).
    Returns a value in [0, 1].
    """
    text = f"{article.title} {article.summary}".lower()
    hits = sum(1 for kw in config.AI_KEYWORDS if kw in text)
    # Normalise: each keyword is worth 1/len; cap at 1.0
    return min(hits / max(len(config.AI_KEYWORDS), 1) * 5, 1.0)


def _title_length_bonus(title: str) -> float:
    """Small bonus (0–0.1) for descriptive titles between 30–80 chars."""
    n = len(title)
    if 30 <= n <= 80:
        return 0.1
    return 0.0


def score_article(article: Article) -> float:
    """
    Compute a composite relevance score in [0, ~2.1].

    Weights:
      0.5 × recency  +  1.0 × keyword  +  title_bonus
    """
    age = _age_hours(article)
    recency = _recency_score(age)
    keyword = _keyword_score(article)
    bonus = _title_length_bonus(article.title)

    total = 0.5 * recency + 1.0 * keyword + bonus
    logger.debug(
        "score=%.3f  recency=%.2f  keyword=%.2f  title='%s'",
        total, recency, keyword, article.title[:60],
    )
    return total


# ── Deduplication ─────────────────────────────────────────────────────────────

def _bigrams(text: str) -> set[str]:
    """Return character bigrams of normalised text."""
    t = re.sub(r"[^a-z0-9 ]", "", text.lower())
    t = re.sub(r"\s+", " ", t).strip()
    return {t[i : i + 2] for i in range(len(t) - 1)}


def _jaccard(a: str, b: str) -> float:
    """Jaccard similarity of bigram sets; returns 0 if both are empty."""
    sa, sb = _bigrams(a), _bigrams(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def deduplicate(articles: list[Article]) -> list[Article]:
    """
    Remove near-duplicate articles using bigram Jaccard on titles.

    When two titles are more similar than DEDUP_THRESHOLD, only the
    higher-scored article is kept.  Articles are assumed to be pre-scored.
    """
    kept: list[Article] = []
    for candidate in articles:
        is_dup = False
        for existing in kept:
            sim = _jaccard(candidate.title, existing.title)
            if sim >= config.DEDUP_THRESHOLD:
                logger.debug(
                    "Dedup: sim=%.2f  '%s'  ≈  '%s'",
                    sim, candidate.title[:50], existing.title[:50],
                )
                # Keep the higher-scored one
                if candidate.score > existing.score:
                    kept.remove(existing)
                    kept.append(candidate)
                is_dup = True
                break
        if not is_dup:
            kept.append(candidate)
    return kept


# ── Age filter ────────────────────────────────────────────────────────────────

def filter_by_age(articles: list[Article]) -> list[Article]:
    """Drop articles published more than MAX_AGE_HOURS ago."""
    fresh = [a for a in articles if _age_hours(a) <= config.MAX_AGE_HOURS]
    dropped = len(articles) - len(fresh)
    if dropped:
        logger.info("Age filter removed %d old articles.", dropped)
    return fresh


# ── Title cache (cross-run deduplication) ─────────────────────────────────────

def _load_cache() -> set[str]:
    """Load previously seen (normalised) titles from disk."""
    path = Path(config.CACHE_FILE)
    if not path.exists():
        return set()
    try:
        return set(path.read_text(encoding="utf-8").splitlines())
    except OSError as exc:
        logger.warning("Could not read cache file: %s", exc)
        return set()


def _save_cache(titles: list[str], existing: set[str]) -> None:
    """Append new titles to the on-disk cache, trimming to CACHE_MAX_LINES."""
    path = Path(config.CACHE_FILE)
    updated = list(existing) + titles
    # Keep only the most recent MAX_LINES entries
    updated = updated[-config.CACHE_MAX_LINES :]
    try:
        path.write_text("\n".join(updated), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write cache file: %s", exc)


def _normalise_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.lower().strip())


def filter_cached(articles: list[Article]) -> list[Article]:
    """Remove articles whose normalised title appeared in yesterday's run."""
    seen = _load_cache()
    fresh = [a for a in articles if _normalise_title(a.title) not in seen]
    logger.info(
        "Cache filter: %d seen before, %d remain.",
        len(articles) - len(fresh), len(fresh),
    )
    return fresh


def update_cache(articles: list[Article]) -> None:
    """Persist the titles of the articles we just published."""
    seen = _load_cache()
    new_titles = [_normalise_title(a.title) for a in articles]
    _save_cache(new_titles, seen)
    logger.info("Cache updated with %d titles.", len(new_titles))


# ── Main pipeline ─────────────────────────────────────────────────────────────

def select_top(articles: list[Article], top_n: Optional[int] = None) -> list[Article]:
    """
    Full ranking pipeline:
      1. Age filter
      2. Cross-run cache filter
      3. Score every article
      4. Deduplicate within-run
      5. Return top_n by score

    Args:
        articles: Raw articles from fetch.py
        top_n:    Override config.TOP_N (handy for tests)

    Returns:
        Ranked list of up to top_n unique, fresh articles.
    """
    n = top_n or config.TOP_N

    if not articles:
        logger.warning("No articles to rank.")
        return []

    # 1 – Age
    articles = filter_by_age(articles)
    if not articles:
        logger.warning("All articles are older than %d hours.", config.MAX_AGE_HOURS)
        return []

    # 2 – Cross-run cache
    articles = filter_cached(articles)
    if not articles:
        logger.info("All fresh articles were already in cache — nothing new today.")
        return []

    # 3 – Score
    for art in articles:
        art.score = score_article(art)

    # Sort descending by score
    articles.sort(key=lambda a: a.score, reverse=True)

    # 4 – Deduplicate (operates on sorted list so we always keep the best)
    articles = deduplicate(articles)

    # 5 – Top N
    selected = articles[:n]
    logger.info(
        "Selected %d/%d articles for the brief.",
        len(selected), len(articles),
    )
    for i, art in enumerate(selected, 1):
        logger.info("  %d. [%.3f] %s", i, art.score, art.title[:70])

    return selected
