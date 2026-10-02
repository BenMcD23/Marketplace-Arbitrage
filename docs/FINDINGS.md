# Findings: is there money in it?

Research and live tests run 30 September – 2 October 2026, UK. Every figure
below came from a live run of this repo against real data, not from estimates,
unless it says otherwise.

## Summary

- **eBay → eBay on popular electronics: no.** A live scan of 500 Buy It Now
  listings across ten searches flagged 14 "deals", and every one was a broken
  device, a part or an accessory priced as if it were the whole device.
- **eBay bulk lots → eBay singles: thin.** Other resellers have already priced
  most lots. The exception was docks (below).
- **Auction houses → eBay: buy prices are genuinely low, but the stock decides
  everything.** At Simon Charles's 1 October sale, business laptops sold for
  £7–£32. Most lots, though, were old school laptops worth less than the fees
  to buy and post them. The best case across 144 sold IT lots was about £229,
  and the realistic case is closer to £100–150 per sale.
- **Next step:** the same tooling, pointed at auction houses that sell 8th-gen-or-newer
  corporate kit.

## 1. eBay Buy It Now scanning (the original pipeline)

Ten searches (iPhone 12, PS5, Switch OLED, WH-1000XM4, Steam Deck, AirPods
Pro 2, MacBook Air M1, DJI Mini 2, RTX 3070, iPad Air 4), with 500 listings
scanned and 14 flagged.

| What was flagged | Examples |
|---|---|
| Parts priced as whole devices | MacBook Air M1 logic board (£88 vs £334), WH-1000XM4 charging board |
| Accessories | WH-1000XM4 case, AirPods Pro 2 charging cases (×4), PS5 stand |
| Services | "Switch OLED micro-soldering service" (×2) |
| Faulty devices | iPhone 12 (passcode-locked, cracked, damaged port), iPad Air 4 (cracked internal screen, ×2) |

Accessory filtering on the buy side has since been added (`d534886`). The
underlying finding stands: liquid consumer electronics on eBay Buy It Now are
priced efficiently, so anything cheap is cheap for a reason.

**CeX is unusable.** Its API returns 403 to every request, from the bot and
from curl with browser headers alike. The "guaranteed floor" the deal engine
was designed around never shows up, so CeX is now off (`ENABLE_CEX=false`).

## 2. eBay bulk lots vs single units

Prices are used Buy It Now asking prices, including postage, on 30 September.
These are asking prices, not sold prices.

| Product | Singles, median | Lots, per unit |
|---|---|---|
| OptiPlex 3060 Micro, i5 | £150 | £100 (5× 3060 i5-8500, 8GB, 256GB) |
| OptiPlex 3050/5050 Tiny | £137 | £28, barebones (no CPU, RAM, drive or power supply) |
| 24" monitors | £59 (Dell P2419H) | £9–17, mixed brands and condition |
| Dell WD15 dock | £20 | £7 |
| Dell TB16 dock | £35 | £4 (a lot of 50; power supplies unknown) |

Ex-corporate PC lots on eBay leave roughly £15–25 a unit once fees, postage and
missing parts are counted. Docks were the only clear gap.

## 3. Where ex-corporate IT is sold, and whether it can be scraped

| Source | Stock | Access |
|---|---|---|
| Trade-only sellers: RPC Components (weekly trade auction), Surfurb, 1st Technologies, Foxway | Ex-corporate PCs, laptops and monitors in bulk | Trade account and login needed; best checked by hand |
| Simon Charles | Mostly consumer returns; occasional IT sales | Open: `robots.txt` allows all, and lot data is embedded as JSON |
| John Pye | Tech and government-surplus sales | The bidding site is behind a Cloudflare challenge, so it needs a real browser |
| BidSpotter, i-bidder | Hosts catalogues for most UK auction houses | Bot challenge (an empty 202 response) |
| easyLive Auction | Hosts many auction houses | Loaded without a challenge |
| Eddisons, William George | Insolvency and clearance sales | Cloudflare |

**Fees decide the bid.** At Simon Charles you pay VAT on the hammer, a 20%
buyer's premium and a 5% internet fee (both plus VAT), a £2 lot fee (plus VAT)
and postage from £4.99 per invoice. A £60 hammer is about £100 delivered. John
Pye charges a 25% premium plus VAT.

## 4. The Simon Charles test

Built as `houses/` and run with `arb auctions backfill|report|scan`.

**Backfill:** the 40 most recent auctions held about 19,700 lots, of which
3 were genuine IT items. IT doesn't arrive steadily; it comes in occasional
dedicated sales.

**The 1 October sale:** 144 IT lots sold, and 39 of them could be valued on
eBay with enough confidence. Each lot is priced at the site's next bid above
the winning hammer, the least that could have won it:

| | |
|---|---|
| Median landed cost / eBay resale | 140% |
| Median profit per lot | −£13 |
| Lots clearing £25 profit and 30% ROI | 4 |
| Their total profit, at the median eBay price | £229 |
| The same four sold at the low end of the eBay range (p10) | £22 |

The best-value lots:

| Lot | Hammer | Landed | eBay resale | Profit |
|---|---|---|---|---|
| HP EliteBook 840 | £13 | £31 | £176* | £115* |
| Fujitsu Lifebook A514, i5 | £7 | £22 | £86 | £46 |
| HP Chromebook | £8 | £24 | £79 | £38 |
| Dell Latitude 5480 | £19 | £40 | £87 | £29 |
| iPad Air 4 (A2316) | £92 | £151 | £208 | £23 |
| Surface Pro 5, i5, 8GB, 256GB | £32 | £61 | £97 | £17 |

\* The EliteBook's generation isn't stated. A G1–G3, which its "Refurb PCs"
licence sticker suggests, sells for about £60, which puts the profit near £15.

Most of the remaining lots were 2nd–4th gen school laptops (Dell Vostro 1540,
RM Notebook 320, HP ProBook 4530s, Fujitsu A514 i3) worth £8–40 on eBay. At
that level the fixed costs of £2.40 in lot fees, about £8 postage in and about
£6 postage out wipe out any margin.

## 5. Why the real numbers would differ from the report

**Makes it look better than reality:**
- **Bidding at one above the hammer.** The report assumes you'd win at the
  next bid. The winner may have been willing to pay more, so you wouldn't
  necessarily have won.
- **Raw returns.** Every lot is an unprocessed raw return, untested by the
  house. Screens are only shown switched off.
- **Chargers.** Most lots have none, and a replacement costs about £8–10. The
  model doesn't subtract it.
- **Locks.** An iPad could be activation-locked. A Surface or laptop with an
  asset tag could still be enrolled in a company's device management or have
  a BIOS password.

**Makes it look worse than reality:**
- **Postage.** Simon Charles charges postage per invoice, so several wins in
  one sale share it. The report charges £8 on every lot.

**Valuation gaps:**
- **Unvalued lots.** 105 of the 144 lots couldn't be valued with enough
  confidence and are left out of the figures.
- **Vague titles.** A title like "EliteBook 840" with no generation gets
  valued at the newer models' price.

## 6. Verdict and next steps

The approach works: scrape the lots, cost every fee, value on eBay, and bid only
up to a ceiling. The buy prices are real. A Latitude 5480 for £19 or a Surface
Pro 5 for £32 is a good buy for someone who can test and refurbish. What
limits it is the stock: Simon Charles sells too little modern business kit to
be worth running on its own.

1. Add auction houses with better stock: John Pye's tech sales (needs
   `uv run playwright install chromium`) and the IT auctioneers on easyLive.
2. Use Simon Charles as an occasional source: run `scan` before each IT sale,
   and bid only on 8th-gen-or-newer business machines at the max bid shown.
3. Value vague titles at the low end, or skip them, and subtract a charger
   allowance from every laptop lot.
4. Apply for trade accounts with RPC and one or two wholesalers, as the
   comparison point for whatever the auctions turn up.

## Known code issues

- **Bad eBay searches.** The oracle searches eBay by `model_number` alone
  whenever one is extracted, and the extractor picks tokens like "256GB" or
  "X360". The auction code works around this; the main eBay pipeline still has
  it.
- **Generation-blind valuations.** Valuations don't distinguish product
  generations unless the title states one.
