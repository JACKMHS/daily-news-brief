"""
main.py — Entry point for the Daily AI Brief.

Two operating modes:

  Single-user (original)
    python main.py [--dry-run] [--no-cache]
    Reads SERVERCHAN_KEY / PUSH_MODE from .env and sends one brief.

  Multi-subscriber (web mode)
    Called by app.py's /admin/run endpoint.
    Iterates over all active subscribers in the database, personalises each
    brief by their topic preferences, and sends via their own SendKey.

Scheduling — do NOT add a scheduler inside Python. Use:
  cron (Linux/macOS):  0 8 * * * cd /path && python main.py
  Task Scheduler (Windows): daily trigger, action python main.py
  GitHub Actions: schedule cron '0 0 * * *'
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import config
import fetch
import rank
import summarize
import push
from database import (
    Subscriber,
    get_active_subscribers,
    init_db,
    log_send,
    topic_score_boost,
)

logger = logging.getLogger(__name__)


# ── Shared fetch + global summarise ──────────────────────────────────────────

def _fetch_and_summarise_global(no_cache: bool = False) -> tuple[list, list]:
    """
    Fetch articles, globally rank the top ~15, summarise them all once.

    Returns (top_articles, summaries) — summaries are shared across all
    subscribers so we only call the LLM once per article per day regardless
    of subscriber count.
    """
    articles = fetch.fetch_all_feeds()
    if not articles:
        return [], []

    # Rank globally to get a broad candidate set (larger pool before topic boost)
    candidate_n = max(config.TOP_N * 3, 15)

    if no_cache:
        articles = rank.filter_by_age(articles)
        for a in articles:
            a.score = rank.score_article(a)
        articles.sort(key=lambda a: a.score, reverse=True)
        articles = rank.deduplicate(articles)
        top_articles = articles[:candidate_n]
    else:
        top_articles = rank.select_top(articles, top_n=candidate_n)

    if not top_articles:
        return [], []

    summaries = summarize.summarise_all(top_articles)
    return top_articles, summaries


# ── Personalise for one subscriber ───────────────────────────────────────────

def _personalise(
    top_articles: list,
    summaries: list,
    subscriber: Subscriber,
    top_n: int,
) -> tuple[list, list]:
    """
    Re-rank a pre-summarised article pool by the subscriber's topic preferences
    and return their personalised top_n (articles, summaries) pair.
    """
    if not summaries:
        return [], []

    # Build a {url: ArticleSummary} map
    summary_map = {s.url: s for s in summaries}

    # Re-score with topic boost
    scored: list[tuple[float, object, object]] = []
    for art in top_articles:
        if art.url not in summary_map:
            continue
        text = f"{art.title} {art.summary}"
        boost = topic_score_boost(text, subscriber.topics)
        personal_score = art.score + boost
        scored.append((personal_score, art, summary_map[art.url]))

    scored.sort(key=lambda x: x[0], reverse=True)

    selected_articles = [x[1] for x in scored[:top_n]]
    selected_summaries = [x[2] for x in scored[:top_n]]
    return selected_articles, selected_summaries


# ── Single-user run (original behaviour) ─────────────────────────────────────

def run_daily(*, dry_run: bool = False, no_cache: bool = False) -> bool:
    """
    Single-user pipeline. Reads SERVERCHAN_KEY / PUSH_MODE from config.

    Returns True on success, False on failure.
    """
    today = date.today().strftime("%Y-%m-%d")
    logger.info("=" * 50)
    logger.info("Daily AI Brief (single-user) — %s", today)
    logger.info("=" * 50)

    logger.info("Step 1/4 — Fetching articles …")
    articles = fetch.fetch_all_feeds()
    if not articles:
        logger.error("No articles fetched.")
        return False

    logger.info("Step 2/4 — Ranking …")
    if no_cache:
        articles = rank.filter_by_age(articles)
        for a in articles:
            a.score = rank.score_article(a)
        articles.sort(key=lambda a: a.score, reverse=True)
        articles = rank.deduplicate(articles)
        top_articles = articles[: config.TOP_N]
    else:
        top_articles = rank.select_top(articles)

    if not top_articles:
        logger.warning("No suitable articles found.")
        if not dry_run:
            push.push(
                title="Daily AI Brief — No new stories",
                body=f"No new AI stories found for {today}. Check back tomorrow!",
            )
        return True

    logger.info("Step 3/4 — Summarising %d articles …", len(top_articles))
    summaries = summarize.summarise_all(top_articles)
    if not summaries:
        logger.error("Summarisation produced no results.")
        return False

    logger.info("Step 4/4 — Formatting and pushing …")
    brief_body = summarize.format_brief(summaries, date_str=today)
    brief_title = f"Daily AI Brief {today} ({len(summaries)} stories)"

    if dry_run:
        print("\n" + "=" * 50)
        print("DRY RUN — brief NOT pushed")
        print("=" * 50)
        print(f"\nTitle: {brief_title}\n")
        print(brief_body)
        print("=" * 50 + "\n")
    else:
        ok = push.push(title=brief_title, body=brief_body)
        if not ok:
            print("\n[PUSH FAILED — printing brief to stdout as fallback]\n")
            print(brief_body)
            return False

    if not no_cache:
        rank.update_cache(top_articles)

    logger.info("Single-user pipeline complete.")
    return True


# ── Multi-subscriber run (web mode) ──────────────────────────────────────────

def run_daily_for_subscribers(no_cache: bool = False) -> tuple[int, int]:
    """
    Multi-subscriber pipeline:
      1. Fetch + globally rank a candidate pool
      2. Summarise the pool once (shared across all subscribers)
      3. For each subscriber re-rank by their topics and send a personalised brief
      4. Update the global title cache once after all sends

    Returns (sent_count, failed_count).
    """
    today = date.today().strftime("%Y-%m-%d")
    logger.info("=" * 50)
    logger.info("Daily AI Brief (multi-subscriber) — %s", today)
    logger.info("=" * 50)

    subscribers = get_active_subscribers()
    if not subscribers:
        logger.info("No active subscribers — nothing to do.")
        return 0, 0

    logger.info("Active subscribers: %d", len(subscribers))

    # ── 1+2. Shared fetch & summarise ────────────────────────────────────────
    logger.info("Fetching and summarising global article pool …")
    top_articles, global_summaries = _fetch_and_summarise_global(no_cache=no_cache)

    if not global_summaries:
        logger.warning("No summaries produced — skipping all sends.")
        return 0, len(subscribers)

    # ── 3. Send personalised brief to each subscriber ─────────────────────────
    sent = 0
    failed = 0

    for sub in subscribers:
        try:
            _, personal_summaries = _personalise(
                top_articles, global_summaries, sub, config.TOP_N
            )

            if not personal_summaries:
                logger.warning("No stories for subscriber #%d, skipping.", sub.id)
                continue

            name_tag = f" · {sub.name}" if sub.name else ""
            brief_title = f"Daily AI Brief{name_tag} {today}"

            # Append unsubscribe link to body
            unsub_url = f"https://YOUR_DOMAIN/unsubscribe?token={sub.unsubscribe_token}"
            brief_body = summarize.format_brief(personal_summaries, date_str=today)
            brief_body += f"\n\n[Unsubscribe]({unsub_url})"

            ok = push.push(
                title=brief_title,
                body=brief_body,
                method=sub.delivery_method,
                target=sub.delivery_target,
            )
            log_send(sub.id, len(personal_summaries), ok)

            if ok:
                sent += 1
                logger.info(
                    "Sent to subscriber #%d (%d stories, topics=%s)",
                    sub.id, len(personal_summaries), sub.topics,
                )
            else:
                failed += 1
                logger.error("Send failed for subscriber #%d", sub.id)

        except Exception as exc:
            logger.error("Error processing subscriber #%d: %s", sub.id, exc)
            failed += 1

    # ── 4. Update shared cache ────────────────────────────────────────────────
    if not no_cache and top_articles:
        rank.update_cache(top_articles)

    logger.info(
        "Multi-subscriber run complete — sent=%d failed=%d", sent, failed
    )
    return sent, failed


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Daily AI News Brief",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run",  action="store_true",
                        help="Print the brief to stdout instead of pushing.")
    parser.add_argument("--no-cache", action="store_true",
                        help="Ignore the cross-run title cache.")
    parser.add_argument("--all-subscribers", action="store_true",
                        help="Run in multi-subscriber mode (uses DB).")
    parser.add_argument("--validate-config", action="store_true",
                        help="Validate config and exit.")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    try:
        config.validate(multi_subscriber=args.all_subscribers)
    except EnvironmentError as exc:
        logger.critical("Configuration invalid: %s", exc)
        sys.exit(1)

    if args.validate_config:
        print("Configuration is valid.")
        sys.exit(0)

    if args.all_subscribers:
        init_db()
        _, failed = run_daily_for_subscribers(no_cache=args.no_cache)
        sys.exit(0 if failed == 0 else 1)
    else:
        success = run_daily(dry_run=args.dry_run, no_cache=args.no_cache)
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
