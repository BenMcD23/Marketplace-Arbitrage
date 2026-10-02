"""Is there money in buying IT at auction and selling it on eBay?

Three jobs:

* `backfill` — walk recent closed auctions and store every IT lot with its
  final hammer price. This is the evidence: what these lots actually went for.
* `report` — value each sold lot on eBay, cost it with the house's full fee
  schedule, and show what you would have made had you won it.
* `scan` — the same maths on open lots, turned round into a max bid.

The fee schedule is the part that is easy to get wrong. At Simon Charles a £60
hammer is ~£92 landed: VAT on the hammer, a 20% premium and a 5% internet fee
(both plus VAT), and a £2 lot fee (plus VAT). A bid that ignores that overpays
every time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from arb.config import Settings
from arb.logging_conf import get_logger
from arb.models import Condition, Listing, PriceBasis, SellChannel, Valuation
from engine.fees import ProfitBreakdown, profit_at
from houses.simon_charles import HOUSE, Lot, SimonCharlesClient
from houses.store import LotStore
from oracle.pricing import PricingOracle

log = get_logger("houses.analysis")
UK = ZoneInfo("Europe/London")

LOT_FEE = 2.0  # Simon Charles' per-lot processing fee, before VAT

# What makes a lot "IT" for this purpose: business kit and the consumer
# computing that sells alongside it. Deliberately name-based — the house's own
# categories cannot be filtered on closed auctions.
_IT_RE = re.compile(
    r"\b(laptop|notebook|macbook|chromebook|ultrabook|thinkpad|latitude|elitebook|"
    r"probook|zbook|dell precision|precision \d{4}|vostro|optiplex|elitedesk|prodesk|thinkcentre|"
    r"surface (?:pro|laptop|go|book)|imac|mac mini|ipad|monitor|docking station|"
    r"poweredge|proliant|synology|qnap|mini pc|desktop pc|all[- ]in[- ]one pc|"
    r"graphics card|geforce|radeon rx)\b",
    re.I,
)

# Stated faults. "Not tested" is not a fault — that is the normal state of an
# auction lot and the thing you are buying the chance to find out.
_FAULT_RE = re.compile(
    r"\b(damaged|cracked|smashed|broken|faulty|spares|for parts|not working|"
    r"no power|does not power|won'?t power|missing (?:battery|keys?|screen|"
    r"touchpad|stand|hard ?drive)|bios lock|password lock|icloud|activation lock|"
    r"hinge damage|water damage|burn[- ]?in|lines? on (?:the )?screen)\b",
    re.I,
)

# Words that describe the lot rather than identify the product. Left in, eBay
# ANDs them into the search ("unboxed ... in silver") and returns nothing.
_SEARCH_NOISE = {
    "boxed", "unboxed", "new", "used", "laptop", "tablet", "computer", "notebook",
    "in", "with", "and", "the", "uk", "keyboard", "black", "silver", "grey", "gray",
    "blue", "red", "white", "pink", "gold", "green", "purple", "rose", "space",
    "midnight", "starlight", "graphite", "abyssal", "colour", "color", "intel",
    "core", "amd", "ryzen", "ram", "ssd", "hdd", "emmc", "storage", "windows",
    "win", "os", "chrome", "gen", "generation", "wi", "fi", "wifi", "only",
    "untested", "tested", "camera", "display", "screen", "inch", "inches",
}
_CAPACITY_RE = re.compile(r"\b\d+\s?(gb|tb)\b", re.I)
_SIZE_RE = re.compile(r"\b\d{2}(\.\d)?[-\s]?(inch|in|\")", re.I)
MAX_SEARCH_TOKENS = 6


# Things sold *for* a device. Narrower than `oracle.comps.is_accessory`, whose
# multi-pack pattern reads "EliteBook x360" as 360 units.
_ACCESSORY_RE = re.compile(
    r"\b(case|cases|sleeve|bag|backpack|cover|stand|mount|bracket|charger|adapter|"
    r"cable|screen protector|stylus|pen|keyboard case|box only|empty box|skin)\b",
    re.I,
)


# "Monitor" is also a blood-pressure monitor, a baby monitor and a camera field
# monitor; mixed pallets name a monitor among the kettles.
_NOT_IT_RE = re.compile(
    r"\b(blood pressure|sphygmomanometer|baby|on-camera|camera monitor|heart rate|"
    r"pallet|tote|box of|cage of|assorted|desk|shelf|refrigerator|fridge|cross trainer|riser|monitor arm|light bar|vtech|studio monitor|smart keyboard|carplay|in-car)\b",
    re.I,
)


def is_it_lot(title: str) -> bool:
    if not _IT_RE.search(title) or _NOT_IT_RE.search(title):
        return False
    # "MONITOR STAND", "LAPTOP BAG": the accessory word follows the device word.
    return not _ACCESSORY_RE.search(title)


def is_faulty(lot: Lot) -> bool:
    return bool(_FAULT_RE.search(" ".join([lot.title, lot.description, lot.condition_report])))


def search_title(title: str) -> str:
    """Reduce an auction title to the words that identify the product.

    "UNBOXED HP ELITEBOOK X360 1040 6G INTEL I-7 8TH GEN LAPTOP IN SILVER"
    -> "hp elitebook x360 1040 6g i-7"
    """
    t = _SIZE_RE.sub(" ", _CAPACITY_RE.sub(" ", title.lower()))
    tokens = re.findall(r"[a-z0-9][a-z0-9\-]*", t)
    kept = [w for w in tokens if w not in _SEARCH_NOISE and not re.fullmatch(r"\d+th", w)]
    return " ".join(kept[:MAX_SEARCH_TOKENS])


def landed_cost(hammer: float, lot: Lot, settings: Settings) -> float:
    """Everything a winning bid of `hammer` costs, delivered to your door."""
    vat = lot.vat_pct / 100.0
    fees = hammer * (lot.premium_pct + lot.internet_pct) / 100.0 + LOT_FEE
    # Premiums and the lot fee always carry VAT; the hammer only when the lot does.
    total = hammer * (1 + vat) + fees * 1.2
    total += settings.auction_postage_estimate if lot.postal else settings.auction_collection_cost
    return round(total, 2)


def listing_for(lot: Lot, cost: float) -> Listing:
    return Listing(
        source=HOUSE,
        source_listing_id=str(lot.id),
        title=search_title(lot.title),
        # No model number on purpose: the oracle searches eBay for the model
        # number alone when it has one, and the extractor mistakes "256GB" or
        # "X360" for one. The cleaned title is the better query.
        model_number=None,
        price=cost,
        url=lot.url,
        condition=Condition.USED,
    )


@dataclass
class LotVerdict:
    lot: Lot
    valuation: Valuation
    buy_hammer: float
    landed: float
    expected: ProfitBreakdown | None
    downside: ProfitBreakdown | None
    faulty: bool

    @property
    def trusted(self) -> bool:
        return self.expected is not None


async def judge(
    lot: Lot, hammer: float, oracle: PricingOracle, settings: Settings
) -> LotVerdict:
    landed = landed_cost(hammer, lot, settings)
    faulty = is_faulty(lot)
    expected = downside = None
    if faulty:
        # Stated faults are out of scope: not worth an eBay call to value.
        return LotVerdict(lot, Valuation(product_key="", basis=PriceBasis.NONE), hammer,
                          landed, None, None, True)
    valuation = await oracle.get_valuation(listing_for(lot, landed))
    if valuation.has_price and valuation.confidence >= settings.min_confidence:
        expected = profit_at(valuation.resale_price, landed, SellChannel.EBAY, settings)
        low = valuation.price_p10 or valuation.resale_price * 0.85
        downside = profit_at(low, landed, SellChannel.EBAY, settings)
    return LotVerdict(lot, valuation, hammer, landed, expected, downside, False)


def max_hammer(lot: Lot, valuation: Valuation, settings: Settings) -> float:
    """Highest hammer that still clears MIN_PROFIT and MIN_ROI after all fees."""
    if not valuation.has_price or valuation.confidence < settings.min_confidence:
        return 0.0

    def ok(h: float) -> bool:
        p = profit_at(valuation.resale_price, landed_cost(h, lot, settings), SellChannel.EBAY,
                      settings)
        return p.profit >= settings.min_profit and p.roi_pct >= settings.min_roi

    lo, hi = 0.0, valuation.resale_price
    if not ok(lo):
        return 0.0
    while hi - lo > 0.5:
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if ok(mid) else (lo, mid)
    # Bids go in whole pounds: finish on the highest whole pound that still clears.
    bid = int(lo)
    while ok(bid + 1):
        bid += 1
    return float(bid)


# --- commands ----------------------------------------------------------------


async def backfill(settings: Settings, store: LotStore, auctions: int) -> dict[str, int]:
    """Store IT lots from the most recent `auctions` auction ids, open or closed."""
    sc = SimonCharlesClient(delay_sec=settings.auction_request_delay_sec)
    stats = {"auctions": 0, "lots": 0, "it_lots": 0}
    try:
        current = await sc.current_auction_ids()
        if not current:
            log.warning("sc_no_auctions_found")
            return stats
        # Ids are shared by every sale the house runs, and the newest are often
        # scheduled but not yet populated. Count only auctions that have lots,
        # and give up after a long run of empty ids.
        empty_run = 0
        for auction_id in range(max(current), 0, -1):
            if stats["auctions"] >= auctions or empty_run >= 25:
                break
            if store.auction_done(HOUSE, auction_id):
                stats["auctions"] += 1
                continue
            lots = await sc.auction_lots(auction_id)
            if not lots:
                empty_run += 1
                continue
            empty_run = 0
            stats["auctions"] += 1
            stats["lots"] += len(lots)
            it = [lot for lot in lots if is_it_lot(lot.title)]
            for lot in it:
                if not store.has_detail(HOUSE, lot.id):
                    lot = await sc.lot_detail(lot.id) or lot
                store.upsert(HOUSE, lot)
            stats["it_lots"] += len(it)
            closed = bool(lots) and all(not lot.is_open for lot in lots)
            store.mark_auction(HOUSE, auction_id, closed)
            log.info("sc_auction_scanned", auction=auction_id, lots=len(lots), it=len(it),
                     closed=closed)
    finally:
        await sc.aclose()
    return stats


async def refresh_current(settings: Settings, store: LotStore) -> int:
    """Re-read every auction the house currently lists, storing its IT lots."""
    sc = SimonCharlesClient(delay_sec=settings.auction_request_delay_sec)
    count = 0
    try:
        for auction_id in await sc.current_auction_ids():
            for lot in await sc.auction_lots(auction_id):
                if not is_it_lot(lot.title):
                    continue
                if not store.has_detail(HOUSE, lot.id):
                    lot = await sc.lot_detail(lot.id) or lot
                store.upsert(HOUSE, lot)
                count += 1
    finally:
        await sc.aclose()
    return count


async def settle_ended(settings: Settings, store: LotStore) -> int:
    """Re-read auctions whose stored lots were open but have since ended.

    A lot stored mid-auction still holds its standing bid. Once the sale closes
    its page carries the final hammer, and that is the number the report needs.
    """
    now = datetime.now(UTC)
    stale = {
        lot.auction_id
        for lot in store.lots(HOUSE, open_only=True)
        if lot.end_time is not None and lot.end_time < now
    }
    if not stale:
        return 0
    sc = SimonCharlesClient(delay_sec=settings.auction_request_delay_sec)
    settled = 0
    try:
        for auction_id in sorted(stale):
            for lot in await sc.auction_lots(auction_id):
                if is_it_lot(lot.title):
                    store.upsert(HOUSE, lot)
                    settled += not lot.is_open
            log.info("sc_auction_settled", auction=auction_id)
    finally:
        await sc.aclose()
    return settled


async def report(settings: Settings, store: LotStore, oracle: PricingOracle) -> str:
    """What you would have made on every sold IT lot, had you won it."""
    await settle_ended(settings, store)
    verdicts: list[LotVerdict] = []
    for lot in store.lots(HOUSE, sold=True):
        # To win you must beat the winner's hammer; the site's next increment is
        # the least that could have done it. The winner's real ceiling may have
        # been higher, so this flatters the result — treat it as the best case.
        buy = lot.next_bid or (lot.hammer or 0) + 1
        verdicts.append(await judge(lot, buy, oracle, settings))

    valued = [v for v in verdicts if v.trusted]
    working = [v for v in valued if not v.faulty]
    good = [
        v for v in working
        if v.expected.profit >= settings.min_profit and v.expected.roi_pct >= settings.min_roi
    ]

    lines = [
        f"Simon Charles — sold IT lots: {len(verdicts)}",
        f"  valued on eBay with confidence >= {settings.min_confidence}: {len(valued)}",
        f"  of those, no stated fault: {len(working)}",
    ]
    if working:
        lines.append(
            f"  median landed cost / eBay resale: "
            f"{median(v.landed / v.valuation.resale_price for v in working):.0%}"
        )
        lines.append(f"  median profit if won at next bid: "
                     f"£{median(v.expected.profit for v in working):.0f}")
    lines.append(
        f"  would clear £{settings.min_profit:.0f} and {settings.min_roi:.0f}% ROI: "
        f"{len(good)}  (total £{sum(v.expected.profit for v in good):.0f}, "
        f"downside total £{sum(v.downside.profit for v in good):.0f})"
    )
    lines.append("")
    lines.append(f"{'hammer':>7} {'landed':>7} {'resale':>7} {'profit':>7} {'p10':>6} "
                 f"{'conf':>4}  lot")
    for v in sorted(working, key=lambda v: v.expected.profit, reverse=True)[:25]:
        lines.append(
            f"£{v.lot.hammer:6.0f} £{v.landed:6.0f} £{v.valuation.resale_price:6.0f} "
            f"£{v.expected.profit:6.0f} £{v.downside.profit:5.0f} {v.valuation.confidence:4.2f}"
            f"  {v.lot.title[:70]}"
        )
    unvalued = len(verdicts) - len(valued)
    if unvalued:
        lines.append(f"\n{unvalued} lots could not be valued with enough confidence (skipped).")
    return "\n".join(lines)


async def scan(settings: Settings, store: LotStore, oracle: PricingOracle, hours: float) -> str:
    """Refresh current auctions, then price every open IT lot ending soon."""
    await refresh_current(settings, store)
    horizon = datetime.now(UTC) + timedelta(hours=hours)
    rows = []
    for lot in store.lots(HOUSE, open_only=True):
        if lot.end_time is None or lot.end_time > horizon or lot.end_time < datetime.now(UTC):
            continue
        current = lot.next_bid or 1.0
        v = await judge(lot, current, oracle, settings)
        ceiling = 0.0 if v.faulty else max_hammer(lot, v.valuation, settings)
        if ceiling >= current:
            rows.append((ceiling - current, ceiling, v))
    if not rows:
        return f"No open IT lots ending in the next {hours:.0f}h are worth bidding on."
    lines = [f"{'bid now':>7} {'max bid':>7} {'resale':>7} {'profit@max':>10}  ends   lot"]
    for _, ceiling, v in sorted(rows, key=lambda r: r[0], reverse=True):
        at_max = profit_at(v.valuation.resale_price, landed_cost(ceiling, v.lot, settings),
                           SellChannel.EBAY, settings)
        ends = v.lot.end_time.astimezone(UK).strftime("%a %H:%M")
        lines.append(
            f"£{v.buy_hammer:6.0f} £{ceiling:6.0f} £{v.valuation.resale_price:6.0f} "
            f"£{at_max.profit:9.0f}  {ends}  {v.lot.title[:60]}\n"
            f"{'':44}{v.lot.url}"
        )
    return "\n".join(lines)
