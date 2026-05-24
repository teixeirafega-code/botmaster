# Trend Hunter Bot

Professional automated trend monitoring bot that watches Google Trends, Reddit, Twitter/X-compatible public sources, and TikTok hashtag pages; scores emerging opportunities; checks domains; records paper registrations; generates content ideas; and sends Telegram alerts.

Paper mode is enabled by default. In paper mode, the bot never purchases domains and logs Telegram alerts when bot credentials are absent.

## Quick Start

```powershell
cd C:\Users\tgt\Desktop\project4\trend_hunter_bot
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m app.main config-check
.\.venv\Scripts\python.exe -m app.main once
.\.venv\Scripts\python.exe -m app.main dashboard
```

Run continuously:

```powershell
.\.venv\Scripts\python.exe -m app.main run
```

## Commands

- `python -m app.main config-check` validates config and initializes SQLite state.
- `python -m app.main once` runs one full monitoring, scoring, action, and notification cycle.
- `python -m app.main run` starts the 24/7 scheduler with the configured interval.
- `python -m app.main dashboard` shows trends detected today, paper/real domains registered today, and top trends from the last 24 hours.

## Configuration

Primary configuration lives in `config.yaml`. Secrets and environment-specific overrides live in `.env` or real environment variables.

Production domain purchases require:

- `PAPER_MODE=false`
- `GODADDY_API_KEY`
- `GODADDY_API_SECRET`
- GoDaddy contact fields under `domains.contact` in `config.yaml`

Required `domains.contact` keys: `first_name`, `last_name`, `email`, `phone`, `address1`, `city`, `state`, `postal_code`, and `country`.

Telegram delivery in production requires:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Reddit runs through public JSON by default and uses PRAW when `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` are present.

Twitter/X monitoring uses `ntscraper` first and falls back to public Nitter RSS endpoints.

TikTok monitoring uses direct hashtag-page requests. It does not require Playwright, browser binaries, or a TikTok session token.

## Scoring

Each trend receives a 0-100 score from:

- growth velocity
- search volume
- social engagement
- commercial potential
- cross-platform detection boost

Trends at or above `app.trend_score_threshold` trigger opportunity automation.

## Data And Logs

- SQLite state: `trend_hunter.db`
- Rotating logs: `logs/trend_hunter.log`

Both are local to the project directory by default.

## Docker

```powershell
cd C:\Users\tgt\Desktop\project4\trend_hunter_bot
docker build -t trend-hunter-bot .
docker run --env-file .env -v ${PWD}\logs:/opt/trend_hunter_bot/logs trend-hunter-bot
```

## Tests

```powershell
cd C:\Users\tgt\Desktop\project4\trend_hunter_bot
.\.venv\Scripts\python.exe -m pytest
```
