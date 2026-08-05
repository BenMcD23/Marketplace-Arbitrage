# Marketplace Arbitrage

A private, self-hosted pipeline that scans eBay for underpriced electronics,
values them against real resale data, and ranks what is actually worth buying.

```
Sources → Normaliser → Pricing Oracle → Deal Engine → API → Dashboard
```

The dashboard lives in a separate repo:
[Marketplace-Arbitrage-UI](https://github.com/BenMcD23/Marketplace-Arbitrage-UI).

**It runs entirely on free data.** No Keepa subscription, no sold-comps
reseller, no scraping. See [Where the price data comes from](#where-the-price-data-comes-from)
— it is the most important design decision in the project.

## Tech stack

Python 3.12 · `uv` · FastAPI · SQLite · httpx · Pydantic · structlog ·
APScheduler · Docker.

## Quick start

```bash
# 1. Install
uv venv --python 3.12
uv pip install -e ".[dev]"

# 2. Configure — only EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are required.
cp .env.example .env

# 3. Run the tests (fully offline — no live API calls)
uv run pytest

# 4. Tell it what to look for
uv run arb watch --add "sony wh-1000xm4" --max-price 120

# 5. Scan
uv run arb run

# 6. Start the API, then the dashboard from the UI repo
uv run arb serve                 # http://127.0.0.1:8000  (docs at /docs)
```

Getting eBay credentials: register a free application at
[developer.ebay.com](https://developer.ebay.com), take the **production** App ID
and Cert ID. That is the whole setup — the free tier allows 5,000 API calls a
day, which is plenty for a few dozen watched searches.

## Where the price data comes from

This is the question the whole project turns on, because **an arbitrage bot is
only as good as its answer to "what is this actually worth?"** — and the honest
sources of that answer are the ones you have to pay for.

**The problem.** eBay's sold-price data lives behind the
[Marketplace Insights API](https://developer.ebay.com/api-docs/buy/marketplace-insights/overview.html),
which is a Limited Release that eBay
[no longer grants to new applicants](https://developer.ebay.com/api-docs/buy/marketplace-insights/overview.html).
The old Finding API's `findCompletedItems` was retired. Keepa (Amazon) and the
various sold-comp resellers are monthly subscriptions. So the data everyone
tells you to use is, in practice, unavailable or expensive.

**What is actually free.** The eBay **Browse API** — 5,000 calls/day, no card,
within eBay's terms. It returns *active* listings, not sold ones. Plus, for used
electronics, **CeX publishes real used prices with no key at all**.

The system closes the gap four ways.

### 1. Active asking prices, honestly discounted

Active listings tell you what sellers *want*, which is systematically higher
than what buyers *pay*. So asking prices are discounted by a ratio before they
are treated as a resale estimate — and rather than leaving that ratio as a
guess, the system **calibrates it from its own accumulated data** (see below).

### 2. CeX — real used prices, and a guaranteed floor

`oracle/cex_client.py`. CeX publish an unauthenticated JSON API behind their
store front, giving three numbers for every product they trade: what they
**sell** the used item for, what they will **pay you** in cash, and the same in
credit.

The cash price is the one that matters, and it is worth more than another
estimate because **it is not a prediction — it is an offer**. If you can buy
something for less than CeX will pay for it, your downside is bounded by money
you can walk into a shop and collect. The deal engine uses it exactly that way:
the modelled bad case becomes the *better* of "sells poorly on eBay" and "sell
it to CeX today", and a deal already in profit at the cash price is flagged as
guaranteed.

It also works from the very first scan, which is what covers the cold start
while the sold-price tracker below builds up history — and it doubles as an
independent second opinion, so when CeX and eBay disagree sharply the valuation
loses confidence rather than quietly picking one.

Caveats: UK-centric, limited to products CeX choose to trade, and the cash price
is deliberately under market — a floor, never a target. It is also an unofficial
API, so every failure there is non-fatal.

### 3. Building a sold-price history for free

`oracle/sold_tracker.py`. A listing that was there yesterday and is gone today
has told you something. So:

1. Every comp returned by a search is recorded with a first-seen timestamp.
2. Comps that stop appearing in results become "stale".
3. A budgeted handful of stale comps are checked directly. Ones eBay reports as
   ended are recorded as observed sales — a price someone plausibly paid, and a
   duration telling you how long it took.

After a few weeks of scanning this is a private sold-comp database worth more
than the one you could have bought, because it covers exactly the searches you
run. Once a product has enough observed sales, they replace asking prices as the
valuation basis entirely.

**The honest caveat:** an ended listing is not *certainly* a sold one. Sellers
cancel, and fixed-duration listings lapse. So observed prices take a small
haircut, and valuations built on inferred sales never reach the confidence that
genuine sold data would earn. If your account *does* have Marketplace Insights,
set `EBAY_HAS_INSIGHTS=true` and its real sold comps are used instead, with no
haircut.

### 4. Refusing to price badly

The largest accuracy win is not a data source at all — it is throwing out comps
that should never have counted. A search for "iPhone 12 128GB" returns genuine
handsets, but also cases, screen protectors, cracked-screen salvage, "empty box"
listings and job lots of ten. Averaging those in produces a resale figure that
is confidently wrong, which is exactly how a bot talks itself into losing money.

`oracle/comps.py` makes every candidate earn its place: accessory and multi-pack
rejection, capacity matching (a 128GB phone is not a 256GB phone), and a
relevance score that weights numeric tokens double — in electronics the numbers
*are* the product, and the gap between an iPhone 12 and a 13 is one character
and about £150. Then `oracle/robust.py` applies MAD-based outlier rejection, so
one £2,000 typo cannot drag a £300 valuation upwards.

### Terapeak — the best data, if you accept the trade

eBay gives every seller account **Terapeak** free, with three years of real sold
prices and a sell-through rate. There is no API for it — Marketplace Insights
*is* the API and it is closed — so `oracle/terapeak.py` drives Seller Hub in a
browser using a session you create yourself:

```bash
uv run arb terapeak-login   # opens a browser; you sign in, only cookies are saved
```

> ⚠️ **Automating the eBay site outside its published APIs is contrary to eBay's
> User Agreement, and the account at risk is the one you sell on.** This is off
> by default (`ENABLE_TERAPEAK=false`) and stays off unless you deliberately turn
> it on. No credentials are ever asked for, typed or stored by this project.

When enabled, its sold data feeds the oracle as the strongest available basis.
The parsing is a pure function over the page text, so it is tested offline and a
Terapeak redesign is a one-function fix rather than a rewrite.

### Keepa is optional

If you do have a Keepa key, set it and Amazon becomes a second sell channel.
Everything works without it.

## The valuation model

Every `Valuation` carries a resale price **and how much to believe it**:

| | |
|---|---|
| **Basis** | observed sales > discounted asking prices > CeX used price > Amazon |
| **Comp quality** | how many survived filtering, how many were discarded and why |
| **Dispersion** | robust spread as a fraction of the median — high means the market disagrees with itself |
| **Confidence** | 0–1, folding sample size, agreement, comp relevance and basis into one number |
| **Liquidity** | observed sell-through and median days to sell |
| **Floor** | CeX's cash offer, when they trade the product — the one number that is not an estimate |

Two things calibrate themselves as data accumulates, rather than staying fixed
constants:

- **Asking → sold ratio.** For every product with both observed sales and live
  comps, the ratio of the two medians says what the real discount is. The median
  across products beats any guess.
- **Used → new ratio.** Learned the same way, from products that have recorded
  sales in both conditions.

Valuations are also **condition-adjusted**: a used handset is priced against
used comps where there are enough of them, never against sealed-in-box ones.

## The deal engine

The naive question is "if this sells at the median, does the profit clear a
threshold?" — which quietly assumes the item sells, sells at the median, and
sells immediately. None of those are free, so `engine/deals.py` prices them:

- **It might not sell at the median.** Every deal is evaluated twice: once at
  the estimate, once at the pessimistic p10. The downside is reported alongside
  the headline number — and where CeX will pay more than that, the floor
  replaces it, because that is what you would actually do.
- **It might not sell soon.** Observed sell-through drives a probability of
  sale (shrunk towards a base rate so one lucky sale does not read as 100%), and
  the capital tied up accrues a holding cost.
- **The estimate might be wrong.** Confidence is both a gate and a ranking
  input, so a thin, disagreeing comp set cannot produce a top-ranked deal
  however good the paper margin looks.

The output is an **expected profit** to rank on, a **0–100 score** for sorting at
a glance, and **plain-English reasons** explaining the verdict and what could go
wrong.

Fees are itemised in `engine/fees.py` and charged on the delivered price, since
that is how eBay actually charges. **Tune them to your real seller fees** —
optimistic fees turn losers into false "deals", and you find out weeks later
from the payouts.

## Commands

```bash
arb run                    # scan once
arb run --dry              # scan + evaluate, change nothing
arb schedule --interval 15 # run continuously
arb stats --days 30        # performance and data-health summary
arb serve                  # start the API for the dashboard
arb watch                  # list watched searches
arb watch --add "ps5" --max-price 250
arb watch --remove 3
arb terapeak-login         # optional: save an eBay session for Terapeak
```

## Configuration

Everything tunable lives in `.env` (see `.env.example` for the full annotated
list). Most of it is also editable live in the dashboard's Settings page, which
persists overrides to `data/settings_override.json`. Credentials are deliberately
**not** on that list — they stay in the environment and are never readable or
writable through the API.

The two things to get right before trusting any profit number:

1. **Fee accuracy** — `EBAY_FVF_PCT`, `POSTAGE_COST`, `PACKAGING_COST`, and your
   ad rate if you run promoted listings.
2. **Thresholds** — start strict (`MIN_CONFIDENCE` especially; raising it is the
   single most effective way to cut false positives) and loosen from there.

## Docker

```bash
docker compose up -d --build     # scanner on a schedule + the API on :8000
docker compose run --rm arb stats
docker compose run --rm arb run --dry
```

Both services share one SQLite file on a mounted volume. Build the dashboard
separately (`npm run build` in the UI repo) and serve its `dist/` behind the
same host as the API.

## Sources & responsible scraping

The eBay source uses the official Browse API and stays within eBay's terms.
Gumtree and Facebook Marketplace scrapers exist behind the same `Source`
interface but are **off by default** (`ENABLE_GUMTREE`, `ENABLE_FB_MARKETPLACE`)
— scraping those sites violates their Terms of Service (a civil matter: account
and IP bans, cease-and-desist; not criminal). They are fully modular, so any
that become painful can be dropped without touching the core.

## Layout

```
arb/       config, models, db, pipeline, factory, runner, stats, overrides, cli
sources/   base Source + normaliser + ebay + scraper_base + gumtree + fb
oracle/    ebay_client, cex_client, terapeak, comps, robust, sold_tracker,
           pricing, keepa_client
engine/    fees (itemised) + deals (risk-adjusted scoring)
api/       FastAPI app, routers, response schemas
tests/     offline unit + integration tests (fixtures, respx, in-memory sqlite)
```

Core principle, unchanged: sources are swappable, everything downstream never
changes.
