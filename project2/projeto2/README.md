# Domain Hunter Bot

Production-oriented async automation bot for monitoring, scoring, registering, listing, and tracking expiring domains.

The bot is safe by default: `.env` ships with `PAPER_MODE=true`, so registrations and marketplace listings are simulated while the rest of the workflow runs normally.

## Folder Structure

```text
app/
  core/                 contextvars, event bus, resilience primitives
  db/                   PostgreSQL repository and schema
  observability/        JSON logging, metrics, health/status server
  economics/            valuation, ROI, allocation, pricing, backtests, reports
  config/               Pydantic settings
  scrapers/             WhoisXML public feeds and ExpiredDomains deleted-domain sources
  analyzers/            scoring, backlinks, keywords
  registrars/           GoDaddy and Namecheap clients
  marketplaces/         GoDaddy Auctions, Sedo, Afternic clients
  services/             domain manager, Telegram, risk, transactions, rebalance
  utils/                compatibility helpers
tests/                  async, resilience, idempotency, Telegram, DB-gated tests
```

## Architecture

- Modular monolith, not microservices.
- Internal event bus for `DOMAIN_SCANNED`, `DOMAIN_SCORED`, `DOMAIN_APPROVED`, `DOMAIN_REJECTED`, `DOMAIN_REGISTERED`, `LISTING_CREATED`, `ALERT_TRIGGERED`, and `CRITICAL_FAILURE`.
- Context propagation with `contextvars` for `correlation_id`, `operation_id`, and `execution_mode`.
- Structured JSON logs with automatic secret redaction.
- PostgreSQL persistence through `asyncpg` pooling. If `DATABASE__URL` is empty, local runs use an in-memory repository for safe development.
- Circuit breakers and exponential backoff with jitter around scrapers, GoDaddy, Sedo, Afternic, and Telegram.
- APScheduler drives scan cycles and daily reports.
- `aiohttp` exposes `/health`, `/metrics`, and `/status` during scheduler mode.
- Acquisition is now economics-first: valuation, ROI, liquidity, holding time, and capital concentration must pass before registration.

## Install

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and fill only the values needed for your mode.

```env
PAPER_MODE=true
DATABASE__URL=postgresql://domain_hunter:domain_hunter@localhost:5432/domain_hunter
GODADDY_API_KEY=
GODADDY_API_SECRET=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Keep `.env` out of version control. The bundled discovery scrapers do not require registrar credentials. Registrar keys can control purchases, listings, and account-level domain operations.

The default scan sources are credential-free:

- `scraper.whoisxml_url` discovers public WhoisXML domain-feed sample downloads, and `scraper.whoisxml_download_urls` can pin direct CSV, JSON, ZIP, GZ, or TAR.GZ feed URLs.
- `scraper.expireddomains_url` defaults to `https://www.expireddomains.net/deleted-domains/` and follows pagination up to `scraper.expireddomains_max_pages`.

Set `PAPER_MODE=false` only after:

- GoDaddy credentials are configured.
- PostgreSQL is reachable.
- Risk limits in `config.yaml` are intentionally chosen.
- A small paper-mode run has completed successfully.

## Run

```bash
python -m app.main dashboard
python -m app.main run-once
python -m app.main scheduler
```

Scheduler mode starts:

- scan jobs
- daily Telegram report at 09:00
- `/health`
- `/metrics`
- `/status`

## Telegram

The notifier uses `python-telegram-bot` with async API calls. Bot username: `@saldogodaddy_bot`.

Supported alerts:

- startup
- rebalance execution
- APY/domain opportunity
- transaction success/failure
- critical error
- daily portfolio summary

Secrets are loaded from `.env`; tokens are never hardcoded or logged.

## PostgreSQL Migration

1. Create a database and user.
2. Set `DATABASE__URL` in `.env`.
3. Run:

```bash
python -m app.main dashboard
```

The repository creates these tables automatically:

- `scanned_domains`
- `scored_domains`
- `registrations`
- `listings`
- `sales`
- `risk_events`
- `scheduler_runs`
- `alert_history`

All scanned domains are persisted, including rejected ones.

## Observability

Structured logs include:

- `timestamp`
- `service`
- `event_name`
- `severity`
- `correlation_id`
- `execution_mode`
- `domain`
- `score`
- `operation_id`

Prometheus-compatible metrics are available at:

```text
GET /metrics
```

Operational status is available at:

```text
GET /health
GET /status
```

## Risk Controls

Configured in `config.yaml`:

- max daily registrations
- max capital exposure
- blacklist
- minimum score threshold
- cooldown periods
- emergency stop
- dry-run audit mode

## Economic Engine

The platform uses an interpretable multi-factor valuation engine instead of a simple heuristic score. It estimates:

- fair market value
- resale probability
- expected holding time
- expected ROI
- liquidity-adjusted ROI
- time-adjusted ROI
- purchase confidence
- recommended listing price

Factors include comparable sales, commercial intent, CPC proxy, search demand, TLD quality, linguistic quality, brandability, length, pronounceability, trend momentum, SEO authority, backlink quality, spam safety, trademark safety, archive quality, and liquidity.

Capital allocation rejects acquisitions that would overconcentrate the portfolio by extension or niche, or exceed configured capital exposure.

Dynamic pricing uses valuation, liquidity, and inventory age. Stale domains are discounted through the repricing engine instead of accumulating dead capital indefinitely.

## Profitability KPIs

Track these before increasing live capital:

- expected ROI vs actual ROI
- resale probability calibration
- hit rate
- average holding days
- capital utilization
- ROI by extension
- ROI by niche
- ROI by score band
- false positive rate
- stale inventory ratio
- acquisition-to-sale ratio
- marketplace conversion rate

## Profitability Traps

- Buying high-score domains with weak liquidity.
- Overexposure to `.io`, `.net`, or a single trend.
- Holding low-quality inventory through renewals.
- Treating backlinks as value without spam/anchor quality checks.
- Paying premium acquisition costs for names with long expected hold time.
- Ignoring trademark-like strings that look brandable but are legal risk.
- Raising live limits before paper-mode expected-vs-actual calibration.

## Development

```bash
make install
make test
make lint
make typecheck
```

Run DB-gated integration tests with:

```bash
set TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/domain_hunter_test
python -m pytest tests/test_db_integration.py
```

## Docker

```bash
docker build -t domain-hunter-bot .
docker run --env-file .env -p 8080:8080 domain-hunter-bot
```

The container runs as a non-root user and exposes port `8080`.

## Prioritized Production Rollout

1. Run in paper mode with PostgreSQL enabled.
2. Verify `/health`, `/status`, logs, and Telegram startup alerts.
3. Tune risk limits and blacklist.
4. Run `run-once` repeatedly and inspect persisted rejected domains and valuations.
5. Backtest threshold changes before live changes.
6. Enable live mode with `max_daily_registrations=1`.
7. Gradually raise capital exposure only after profitable validation.
