"""
summarize.py — LLM-powered article summarisation via the Anthropic SDK.

Each article is summarised into:
  • A cleaned title
  • A 2-3 sentence paraphrased summary (never copies original text)
  • A single "Why it matters" sentence

Token usage is bounded by MAX_INPUT_CHARS in config.py.
Default model: claude-haiku-4-5  (fast, cheap, great at structured JSON tasks)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import anthropic

import config
from fetch import Article

logger = logging.getLogger(__name__)

# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class ArticleSummary:
    title: str
    summary: str
    why_it_matters: str
    url: str
    source: str


# ── Prompts ───────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT_EN = """\
You are a concise news journalist writing a daily news brief.
Rules you must follow:
1. Never copy or quote the source text directly. Always paraphrase.
2. Keep the entire response under 120 words.
3. Return ONLY valid JSON — no markdown fences, no extra keys.
4. The "summary" field must be 2-3 sentences.
5. The "why_it_matters" field must be exactly 1 sentence.
6. The "title" field must be a clean, engaging headline (≤12 words).
"""

_USER_TEMPLATE_EN = """\
Summarise the article below in your own words.
Return JSON with exactly three keys: "title", "summary", "why_it_matters".

Article title: {title}

Article content:
{content}
"""

_SYSTEM_PROMPT_ZH = """\
你是一位简洁的新闻记者，正在撰写每日新闻简报。
你必须遵守以下规则：
1. 绝对不要直接抄写或引用原文，务必用自己的语言改写。
2. 整个回答不超过150个字。
3. 只返回有效的JSON，不要使用Markdown代码块，不要添加多余的键。
4. "summary"字段必须是2-3句话。
5. "why_it_matters"字段必须恰好是1句话。
6. "title"字段必须是简洁有吸引力的中文标题（不超过20个字）。
所有字段的内容必须用中文书写。
"""

_USER_TEMPLATE_ZH = """\
请用你自己的语言概括以下文章，全程使用中文。
返回JSON，包含三个键："title"（标题）、"summary"（摘要）、"why_it_matters"（重要性）。

文章标题：{title}

文章内容：
{content}
"""

_PROMPTS = {
    "en": (_SYSTEM_PROMPT_EN, _USER_TEMPLATE_EN),
    "zh": (_SYSTEM_PROMPT_ZH, _USER_TEMPLATE_ZH),
}


# ── Anthropic client (lazy singleton) ─────────────────────────────────────────

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


# ── Core summarisation ────────────────────────────────────────────────────────

def _call_llm(title: str, content: str, language: str = "en") -> dict:
    """
    Call Claude and parse the JSON response.

    Uses claude-haiku-4-5 by default — fast and cheap for batch summarisation.
    Retries on rate-limit / transient errors with exponential back-off.
    Raises ValueError if parsing fails after all retries.
    """
    system_prompt, user_template = _PROMPTS.get(language, _PROMPTS["en"])
    user_msg = user_template.format(
        title=title,
        content=content[: config.MAX_INPUT_CHARS],
    )
    client = _get_client()

    last_exc: Exception = RuntimeError("No attempts made")
    delay = config.RETRY_BACKOFF

    for attempt in range(1, config.RETRY_ATTEMPTS + 1):
        try:
            message = client.messages.create(
                model=config.ANTHROPIC_MODEL,
                max_tokens=300,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )

            raw = message.content[0].text if message.content else ""
            # Strip accidental markdown fences Claude might add
            raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

            parsed = json.loads(raw)

            # Validate required keys
            for key in ("title", "summary", "why_it_matters"):
                if key not in parsed or not isinstance(parsed[key], str):
                    raise ValueError(f"Missing or invalid key in LLM response: {key!r}")

            return parsed

        except anthropic.RateLimitError as exc:
            logger.warning(
                "Claude rate-limit (attempt %d/%d): %s",
                attempt, config.RETRY_ATTEMPTS, exc,
            )
            last_exc = exc
            time.sleep(delay)
            delay *= config.RETRY_BACKOFF

        except anthropic.APITimeoutError as exc:
            logger.warning(
                "Claude timeout (attempt %d/%d): %s",
                attempt, config.RETRY_ATTEMPTS, exc,
            )
            last_exc = exc
            time.sleep(delay)
            delay *= config.RETRY_BACKOFF

        except anthropic.APIError as exc:
            logger.error("Claude API error: %s", exc)
            raise

        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "Bad JSON from Claude (attempt %d/%d): %s — raw=%r",
                attempt, config.RETRY_ATTEMPTS, exc, raw[:200] if "raw" in dir() else "",
            )
            last_exc = exc
            time.sleep(delay)
            delay *= config.RETRY_BACKOFF

    raise ValueError(
        f"LLM summarisation failed after {config.RETRY_ATTEMPTS} attempts: {last_exc}"
    )


def summarise_article(article: Article, language: str = "en") -> Optional[ArticleSummary]:
    """
    Summarise a single article.

    Uses the feed summary if full_text is empty (avoids an extra HTTP request
    for most feeds that include a decent excerpt).

    Returns None if summarisation fails so the caller can skip gracefully.
    """
    content = (article.full_text or article.summary or "").strip()
    if not content:
        logger.warning("No content available for: %s", article.title)
        return None

    logger.info("Summarising [%s]: %s", language, article.title[:70])
    try:
        data = _call_llm(article.title, content, language=language)
        return ArticleSummary(
            title=data["title"].strip(),
            summary=data["summary"].strip(),
            why_it_matters=data["why_it_matters"].strip(),
            url=article.url,
            source=article.source,
        )
    except Exception as exc:
        logger.error("Failed to summarise '%s': %s", article.title[:60], exc)
        return None


def summarise_all(articles: list[Article], language: str = "en") -> list[ArticleSummary]:
    """
    Summarise a list of articles, skipping any that fail.

    Args:
        articles: Pre-ranked articles from rank.py
        language: 'en' for English, 'zh' for Chinese

    Returns:
        List of ArticleSummary (may be shorter than input if some fail).
    """
    summaries: list[ArticleSummary] = []
    for article in articles:
        result = summarise_article(article, language=language)
        if result:
            summaries.append(result)
        # Small politeness delay between API calls
        time.sleep(0.3)

    logger.info(
        "Summarised %d/%d articles successfully [%s].",
        len(summaries), len(articles), language,
    )
    return summaries


# ── Message formatter ─────────────────────────────────────────────────────────

def format_brief(
    summaries: list[ArticleSummary],
    date_str: str = "",
    language: str = "en",
) -> str:
    """
    Render summaries as the final plain-text brief.

    Args:
        summaries: List of ArticleSummary objects.
        date_str:  Human-readable date to include in the header.
        language:  'en' (default) or 'zh' for Chinese formatting.

    Returns:
        Formatted string ready to push to WeChat / WeCom.
    """
    from datetime import date as _date

    header_date = date_str or _date.today().strftime("%Y-%m-%d")

    if language == "zh":
        lines: list[str] = [
            f"【每日新闻简报】{header_date}",
            "=" * 36,
            "",
        ]
        for i, s in enumerate(summaries, 1):
            lines += [
                f"{i}. {s.title}",
                f"   {s.summary}",
                f"   重要性：{s.why_it_matters}",
                f"   🔗 {s.url}",
                f"   — {s.source}",
                "",
            ]
        lines += [
            "─" * 36,
            f"每日新闻简报 • {header_date}",
        ]
    else:
        lines = [
            f"【Daily News Brief】{header_date}",
            "=" * 36,
            "",
        ]
        for i, s in enumerate(summaries, 1):
            lines += [
                f"{i}. {s.title}",
                f"   {s.summary}",
                f"   Why it matters: {s.why_it_matters}",
                f"   🔗 {s.url}",
                f"   — {s.source}",
                "",
            ]
        lines += [
            "─" * 36,
            f"Powered by Daily News Brief • {header_date}",
        ]

    return "\n".join(lines)
