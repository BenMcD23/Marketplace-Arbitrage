# Electronics Arbitrage Bot

A private, self-hosted pipeline that scans marketplaces for underpriced
electronics, values them against real resale data, and records profitable finds.
No UI, no customers — the engineering is the moat.

```
Sources → Normaliser → Pricing Oracle → Deal Engine → Alerts
```

- **Sources** — each site (eBay API, Gumtree, Facebook Marketplace) is a plug-in
  module that emits the same `Listing` object.
- **Normaliser** — extracts model numbers, cleans titles, standardises condition.
- **Pricing Oracle** — eBay Sold median + Keepa (Amazon) = resale truth. Cached.
- **Deal Engine** — applies the margin / ROI / profit formula, flags winners.
- **Alerts** — notifications are off for now; deals are logged and stored in the DB.

Core principle: sources are swappable, everything downstream never changes.

## Tech stack

Python 3.12 · `uv` · SQLite · httpx · Pydantic · structlog · Playwright ·
APScheduler · Docker.

## Quick start

```bash
# 1. Install (uv creates a venv and installs deps)
uv venv --python 3.12
uv pip install -e ".[dev]"

# 2. Configure
cp .env.example .env      # fill in eBay / Keepa keys + thresholds

# 3. Run the test suite (fully offline — no live API calls)
uv run pytest

# 4. Run the pipeline once
uv run arb run              # or:  uv run arb run --dry   (no alerts)

# 5. See performance stats
uv run arb stats --days 30

# 6. Run continuously
uv run arb schedule --interval 15
```

### Playwright (only needed for scraper sources)

```bash
uv run playwright install chromium
```

## Configuration

Everything tunable lives in `.env` (see `.env.example` for the full list and
defaults). The two things to get right before trusting the profit numbers:

1. **Thresholds** — `MIN_PROFIT`, `MIN_ROI`, `MAX_AMAZON_RANK`. Start
   conservative (higher floors) to cut noise, then loosen.
2. **Fee accuracy** — the deal engine is only as honest as its fee model. Plug
   in your real eBay/Amazon seller fees (`EBAY_FVF_PCT`, `AMAZON_REFERRAL_PCT`,
   `AMAZON_FBA_FEE`, `PACKAGING_COST`). Optimistic fees turn losers into false
   "deals".

## Docker

```bash
docker compose up -d --build     # runs `arb schedule` with a mounted db volume
docker compose run --rm arb arb stats
```

## Sources & responsible scraping

The eBay source uses the official Browse / Marketplace Insights API. The Gumtree
and Facebook Marketplace scrapers are **off by default** (`ENABLE_GUMTREE`,
`ENABLE_FB_MARKETPLACE`) — scraping those sites violates their Terms of Service
(a civil matter: account/IP bans, cease-and-desist, not criminal). They are kept
fully modular behind the `Source` interface with rate limiting, randomised
delays, and rotating user-agents, so any that become painful can be dropped
without touching the core.

## Layout

```
arb/       config, models, db, logging, pipeline, factory, stats, cli
sources/   base Source + normaliser + ebay + scraper_base + gumtree + fb
oracle/    ebay_client, keepa_client, pricing (with SQLite cache + TTL)
engine/    deals (fee model, channel selection, thresholds, scam filter)
alerts/    null (no-op; notifications disabled)
tests/     offline unit + integration tests (fixtures, respx, in-memory sqlite)
```
