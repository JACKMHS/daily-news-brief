# Daily AI News Brief

A production-quality Python system that fetches the latest AI news, selects the most important stories, summarises them with an LLM, and delivers a clean daily brief to your WeChat via **Server酱** or **WeCom** webhook.

---

## Quick Start

```bash
# 1. Clone / download the project
cd daily_news

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
#    → fill in OPENAI_API_KEY and SERVERCHAN_KEY (or WECOM_WEBHOOK)

# 4. Test without pushing
python main.py --dry-run

# 5. Send today's brief
python main.py
```

---

## Project Structure

```
daily_news/
├── main.py          # Entry point — orchestrates the full pipeline
├── fetch.py         # RSS feed fetching + optional scraping
├── rank.py          # Deduplication, scoring, top-N selection
├── summarize.py     # LLM summarisation + message formatting
├── push.py          # Server酱 / WeCom push delivery
├── config.py        # All config from environment variables
├── requirements.txt
├── .env.example     # Copy → .env and fill in secrets
└── README.md
```

---

## Configuration

All settings are read from environment variables (or a `.env` file).

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENAI_API_KEY` | ✅ | — | OpenAI (or compatible) API key |
| `OPENAI_BASE_URL` | | `https://api.openai.com/v1` | Override for Azure, local Ollama, etc. |
| `OPENAI_MODEL` | | `gpt-4o-mini` | Model name |
| `PUSH_MODE` | ✅ | `serverchan` | `serverchan` or `wecom` |
| `SERVERCHAN_KEY` | if serverchan | — | SCT key from sct.ftqq.com |
| `WECOM_WEBHOOK` | if wecom | — | Full WeCom webhook URL |
| `TOP_N` | | `5` | Number of articles per brief |
| `MAX_AGE_HOURS` | | `48` | Ignore articles older than N hours |
| `RSS_FEEDS` | | 5 built-in feeds | Comma-separated feed URLs |
| `AI_KEYWORDS` | | (14 built-in) | Comma-separated ranking keywords |
| `LOG_LEVEL` | | `INFO` | `DEBUG` / `INFO` / `WARNING` |

### Getting a Server酱 key

1. Visit [sct.ftqq.com](https://sct.ftqq.com/) and log in with WeChat scan.
2. Copy the **SendKey** (starts with `SCT`).
3. Set `SERVERCHAN_KEY=SCT...` in your `.env`.

### Getting a WeCom webhook

1. In WeCom, add a **群机器人** (group bot) to any group chat.
2. Copy the webhook URL.
3. Set `PUSH_MODE=wecom` and `WECOM_WEBHOOK=https://...` in `.env`.

---

## Scheduling (Daily Execution)

Do **not** run a scheduler inside Python — let the OS handle it.

### Linux / macOS — cron

```bash
crontab -e
# Add: run every day at 08:00 local time
0 8 * * * cd /path/to/daily_news && /usr/bin/python3 main.py >> /var/log/daily_ai_brief.log 2>&1
```

### Windows — Task Scheduler

1. Open **Task Scheduler → Create Basic Task**
2. Trigger: Daily, 08:00
3. Action: Start a program → `python.exe`
4. Arguments: `d:\daily_news\main.py`
5. Start in: `d:\daily_news`

### GitHub Actions

```yaml
# .github/workflows/daily_brief.yml
name: Daily AI Brief
on:
  schedule:
    - cron: '0 0 * * *'   # 00:00 UTC = 08:00 CST
  workflow_dispatch:       # manual trigger

jobs:
  brief:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install -r requirements.txt
      - run: python main.py
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          SERVERCHAN_KEY: ${{ secrets.SERVERCHAN_KEY }}
          PUSH_MODE: serverchan
```

---

## CLI Options

```
python main.py                   # Run full pipeline and push
python main.py --dry-run         # Run but print to stdout (no push)
python main.py --no-cache        # Ignore yesterday's title cache
python main.py --validate-config # Check config and exit
```

---

## Data Sources (default feeds)

| Feed | Content |
|---|---|
| TechCrunch | Startup & tech news |
| arXiv cs.AI | AI research papers |
| Hacker News front page | Community-curated tech stories |
| VentureBeat AI | Enterprise AI / ML |
| MIT Technology Review | Deep-dive tech journalism |

Add your own via the `RSS_FEEDS` env variable:

```
RSS_FEEDS=https://techcrunch.com/feed/,https://mysite.com/rss.xml
```

---

## Example Output

```
【Daily AI Brief】2026-04-15
====================================

1. OpenAI Launches GPT-5 With Reasoning Upgrades
   OpenAI has unveiled GPT-5, featuring improved multi-step reasoning and a
   new "thinking" mode that lets the model deliberate before answering complex
   questions. The model also introduces real-time web search integration.
   Why it matters: GPT-5 raises the bar for real-world task automation and
   could accelerate enterprise AI adoption.
   🔗 https://techcrunch.com/...
   — TechCrunch

2. Researchers Achieve State-of-the-Art on Mathematical Benchmarks
   A team from DeepMind published a new architecture combining symbolic
   reasoning with transformer attention, outperforming all prior models on
   MATH and GSM8K by a 12-point margin.
   Why it matters: Closing the gap between neural and symbolic reasoning
   could unlock reliable AI agents for scientific discovery.
   🔗 https://arxiv.org/abs/...
   — arXiv cs.AI

...

────────────────────────────────────
Powered by Daily AI Brief • 2026-04-15
```

---

## Architecture

```
main.py
  │
  ├─ fetch.py        feedparser + requests + BeautifulSoup
  │    └─ Article(title, url, summary, published, source)
  │
  ├─ rank.py         age filter → cache filter → score → dedup → top-N
  │    └─ Article (with .score filled)
  │
  ├─ summarize.py    OpenAI chat completion (JSON mode) → ArticleSummary
  │    └─ ArticleSummary(title, summary, why_it_matters, url, source)
  │
  └─ push.py         Server酱 POST  |  WeCom webhook POST
```

### Key design decisions

- **No internal scheduler** — the script exits after one run; the OS owns the schedule.
- **Title cache** (`.title_cache.txt`) prevents the same story appearing two days in a row.
- **Graceful degradation** — a failed feed is skipped; a failed summarisation skips that article; a push failure prints to stdout as a fallback.
- **Token economy** — only the first `MAX_INPUT_CHARS` of each article is sent to the LLM; the JSON-mode response is capped at 300 tokens.
- **Paraphrasing enforced by prompt** — system prompt explicitly forbids quoting source text to avoid copyright issues.

---

## Extending

| Goal | File to edit |
|---|---|
| Add a new feed | `config.py` → `_DEFAULT_FEEDS` |
| Change ranking weights | `rank.py` → `score_article()` |
| Change output format | `summarize.py` → `format_brief()` |
| Add Telegram push | `push.py` → new `push_telegram()` + update dispatcher |
| Switch to a local LLM | Set `OPENAI_BASE_URL=http://localhost:11434/v1` (Ollama) |

---

## License

MIT — do whatever you like, just don't blame us for bad AI takes.
