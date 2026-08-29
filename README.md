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

Alongside that runs a second, simpler idea — **search profiles**. A profile is a
buy-to-keep hunt (`Sources → Profile filters → Ranking → Alerts`): it skips the
pricing oracle entirely, because the question is "is this worth buying for me?"
rather than "can I flip this?". See [Mini / micro PC profile](#mini--micro-pc-profile).

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

# 5. Hunt mini PCs for the home-server rack
uv run arb mini-pc

# 6. See performance stats
uv run arb stats --days 30

# 7. Run continuously
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

## Mini / micro PC profile

`arb mini-pc` hunts used micro-form-factor desktops to run as home-server / k3s
cluster nodes. It runs each of its eight search terms as a **separate** eBay
query (an OR-ed query lets eBay's relevance ranking swallow the long tail) and
covers both Buy It Now and auction listings.

```bash
uv run arb mini-pc                    # rank + push
uv run arb mini-pc --dry              # rank only, no alerts
uv run arb mini-pc --include-broken   # also consider "for parts or not working"
```

**What it looks for** — the search terms live in `profiles/mini_pc.py`
(`SEARCH_TERMS`) and cover OptiPlex Micro, ProDesk 400 G6, EliteDesk 800 G4,
OptiPlex 7060 Micro and ThinkCentre M70q / Tiny in the CPUs below.

**Filters**

| Rule | Behaviour |
| --- | --- |
| Condition | Used / refurbished. "For parts or not working" only with `--include-broken`. New is out of profile. |
| CPU | Must be a T-series part (`<= 35W`) from the tier table. Plain desktop chips (`i5-10500`, `i5-9400`) are rejected — 65W is the wrong shape for a node that idles 24/7. |
| RAM | `>= 16GB`. 8-15GB survives only when the price is exceptional *and* the memory is socketed, flagged "needs RAM upgrade" with the cost. Under 8GB is always rejected, and unverifiable RAM is rejected too (a listing that never states RAM cannot be proven to clear 16GB). |
| Storage | `>= 128GB` unless the price is exceptional. |
| Delivery | Always resolved to a number: free, itemised, or — when eBay only quotes at checkout — `MINI_PC_ASSUMED_DELIVERY`, flagged as an estimate. |

**Ranking** is CPU tier first, then total landed cost ascending:

`i7-10700T > i5-10500T > i5-10400T > i5-9500T > i5-8500T`

**Total landed cost** (item + delivery) is the only figure the profile ever
compares on, and every pushed message spells it out — delivery hidden behind a
listing link is how a £120 machine turns into a £150 machine:

```
🔥 EXCEPTIONAL MINI PC DEAL — ebay_mini_pc
Lenovo ThinkCentre M70q Tiny i5-10400T 2x8GB DDR4 1TB SSD
CPU: i5-10400T (35W, tier 3/5)
RAM: 16GB   Storage: 1TB
Condition: used

Total landed: £104.00 (item £96.00 + delivery est. £8.00)
Auction: £96.00 (7 bids) · 2d 4h left
Best Offer available — lever to push the landed cost lower
```

Landed cost below `MINI_PC_STRONG_DEAL_MAX` (£160) is flagged a **strong deal**,
below `MINI_PC_EXCEPTIONAL_DEAL_MAX` (£130) an **exceptional deal**. Auctions are
priced on the *current bid* — a listing whose bid eBay does not return is dropped
rather than compared on a placeholder — and the time remaining is shown. Every
threshold above is tunable in `.env`.

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
sources/   base Source + normaliser + ebay + ebay_mini_pc + scraper_base + gumtree + fb
oracle/    ebay_client, keepa_client, pricing (with SQLite cache + TTL)
engine/    deals (fee model, channel selection, thresholds, scam filter)
alerts/    null (no-op; notifications disabled) + mini_pc message formatting
profiles/  mini_pc (search profile: CPU tiers, spec parsing, landed cost, ranking)
tests/     offline unit + integration tests (fixtures, respx, in-memory sqlite)
```
