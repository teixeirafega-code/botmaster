# Asset Flip Bot

Asset Flip Bot monitors digital asset marketplaces, estimates fair value from monthly cashflow, scores resale upside, and alerts on undervalued opportunities.

## What It Does

- Scrapes Flippa, Empire Flippers, and Acquire.com every 30 minutes.
- Scores assets from 0 to 100 using revenue, price-to-value discount, age, traffic, and niche quality.
- Estimates real value from monthly revenue/profit multipliers:
  - Websites: 30-40x monthly cashflow
  - Apps: 24-36x monthly cashflow
  - YouTube channels: 24-30x monthly cashflow
- Flags assets where asking price is less than 50% of estimated real value.
- Sends Telegram alerts with asset name, asking price, estimated value, upside, and score.
- Provides a CLI dashboard with monitored assets, opportunities found, and total potential profit.
- Runs in paper mode by default so scans and alerts are safe while testing.

## Quick Start

```powershell
cd C:\Users\tgt\Desktop\project3\asset_flip_bot
python -m pip install -r requirements.txt
python -m app.main config-check
python -m app.main scan-once
python -m app.main dashboard
```

Run continuously:

```powershell
python -m app.main run
```

## Telegram Alerts

1. Create a bot with BotFather.
2. Send a message to the bot from the destination chat.
3. Set `.env`:

```dotenv
PAPER_MODE=false
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

Keep `PAPER_MODE=true` for dry runs. In paper mode, alerts are logged but not sent.

## Authenticated Marketplaces

Some marketplace pages may require a logged-in session or may render selected data client-side. If your account and the marketplace terms permit automated monitoring, set the appropriate cookie variable in `.env`:

```dotenv
FLIPPA_COOKIE=session_cookie_here
EMPIREFLIPPERS_COOKIE=session_cookie_here
ACQUIRECOM_COOKIE=session_cookie_here
```

The scraper uses public page data, JSON-LD, Next.js embedded data, official marketplace APIs where available, and conservative HTML fallback parsing. Empire Flippers is read through its public Listings API so price, monthly profit, revenue, monetization, and niche fields come from structured data instead of brittle page text. Failed marketplace requests are logged and do not stop the scan.

## Configuration

Edit `config.yaml` to tune:

- Marketplace URLs and per-site delays
- Scan interval
- Revenue multipliers
- Minimum alert score
- Undervalued threshold
- Niche scoring bonuses

Environment variables override the most important runtime switches.

## Docker

```powershell
cd C:\Users\tgt\Desktop\project3\asset_flip_bot
docker build -t asset-flip-bot .
docker run --env-file .env -v ${PWD}\logs:/opt/asset_flip_bot/logs -v ${PWD}\data:/opt/asset_flip_bot/data asset-flip-bot
```

## Tests

```powershell
cd C:\Users\tgt\Desktop\project3\asset_flip_bot
pytest
```

## Notes For Production

- Keep paper mode enabled until configuration, scraping access, and Telegram delivery are verified.
- Use marketplace-specific cookies only when allowed by the marketplace and your account terms.
- Monitor `logs/asset_flip_bot.log` and rotate Docker/container logs in your deployment platform.
- Run one process per deployment to avoid duplicate alerts from shared state.
