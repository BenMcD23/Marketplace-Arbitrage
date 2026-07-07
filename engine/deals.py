"""Deal Engine — turns a Listing + Valuation into a Deal (or nothing).

The engine is only as honest as its fee model, so every fee and threshold is
pulled from config. It evaluates each viable sell channel, picks the most
profitable, and flags a Deal only when the profit/ROI/data-quality gates all
pass. A separately-flagged "too good to be true" path catches likely scams.
"""

from __future__ import annotations

from dataclasses import dataclass

from arb.config import Settings
from arb.logging_conf import get_logger
from arb.models import Condition, Deal, Listing, SellChannel, Valuation

log = get_logger("engine.deals")


@dataclass
class _ChannelResult:
    channel: SellChannel
    est_resale: float
    est_fees: float
    est_profit: float
    roi_pct: float
    margin_pct: float


def _ebay_fees(resale: float, s: Settings) -> float:
    fvf = resale * (s.ebay_fvf_pct / 100.0)
    payment = resale * (s.ebay_payment_pct / 100.0)
    return round(fvf + payment + s.ebay_fixed_fee, 2)


def _amazon_fees(resale: float, s: Settings) -> float:
    referral = resale * (s.amazon_referral_pct / 100.0)
    return round(referral + s.amazon_fba_fee, 2)


def _score_channel(
    channel: SellChannel, resale: float, buy_cost: float, s: Settings
) -> _ChannelResult | None:
    if resale is None or resale <= 0:
        return None
    fees = _ebay_fees(resale, s) if channel == SellChannel.EBAY else _amazon_fees(resale, s)
    profit = round(resale - buy_cost - fees - s.packaging_cost, 2)
    roi = round(profit / buy_cost * 100, 2) if buy_cost > 0 else 0.0
    margin = round(profit / resale * 100, 2) if resale > 0 else 0.0
    return _ChannelResult(channel, resale, fees, profit, roi, margin)


def evaluate(listing: Listing, valuation: Valuation, settings: Settings) -> Deal | None:
    """Evaluate a listing against its valuation. Returns a Deal or None.

    A returned Deal may carry `is_scam_flag=True` — those are surfaced on a
    separate low-priority channel rather than treated as real buys.
    """
    buy_cost = listing.buy_cost

    # --- Safety: reject parts-only unless explicitly allowed --------------
    if listing.condition == Condition.FOR_PARTS and not settings.allow_for_parts:
        log.debug("reject_for_parts", listing_id=listing.id)
        return None

    # --- Score each channel where we have resale data ---------------------
    candidates: list[_ChannelResult] = []

    ebay_result = None
    if valuation.ebay_sold_median is not None:
        ebay_result = _score_channel(SellChannel.EBAY, valuation.ebay_sold_median, buy_cost, settings)
        # eBay channel needs enough sold comps to be trustworthy.
        if ebay_result is not None and valuation.ebay_sold_count >= settings.min_sold_count:
            candidates.append(ebay_result)

    amazon_result = None
    if valuation.amazon_price is not None:
        amazon_result = _score_channel(SellChannel.AMAZON, valuation.amazon_price, buy_cost, settings)
        # Amazon channel needs a sellable rank.
        rank_ok = valuation.amazon_rank is not None and valuation.amazon_rank <= settings.max_amazon_rank
        if amazon_result is not None and rank_ok:
            candidates.append(amazon_result)

    # Best available resale estimate (even from a channel that failed its data
    # gate) — used only for the scam check so we still catch obvious fakes.
    resale_for_scam = _best_resale(ebay_result, amazon_result)

    # --- Too-good-to-be-true scam check -----------------------------------
    if resale_for_scam is not None and buy_cost < settings.tgtbt_ratio * resale_for_scam:
        # Use whichever channel produced the resale estimate for reporting.
        ref = ebay_result or amazon_result
        log.info("scam_flag", listing_id=listing.id, buy_cost=buy_cost, est_resale=resale_for_scam)
        return Deal(
            listing_id=listing.id,
            buy_cost=buy_cost,
            est_resale=ref.est_resale,
            est_fees=ref.est_fees,
            est_profit=ref.est_profit,
            margin_pct=ref.margin_pct,
            roi_pct=ref.roi_pct,
            sell_channel=ref.channel,
            is_scam_flag=True,
        )

    if not candidates:
        return None

    # --- Pick the best channel by profit ----------------------------------
    best = max(candidates, key=lambda c: c.est_profit)

    if best.est_profit < settings.min_profit:
        log.debug("reject_below_min_profit", listing_id=listing.id, profit=best.est_profit)
        return None
    if best.roi_pct < settings.min_roi:
        log.debug("reject_below_min_roi", listing_id=listing.id, roi=best.roi_pct)
        return None

    log.info(
        "deal_flagged",
        listing_id=listing.id,
        channel=best.channel.value,
        profit=best.est_profit,
        roi=best.roi_pct,
    )
    return Deal(
        listing_id=listing.id,
        buy_cost=buy_cost,
        est_resale=best.est_resale,
        est_fees=best.est_fees,
        est_profit=best.est_profit,
        margin_pct=best.margin_pct,
        roi_pct=best.roi_pct,
        sell_channel=best.channel,
        is_scam_flag=False,
    )


def _best_resale(*results: _ChannelResult | None) -> float | None:
    resales = [r.est_resale for r in results if r is not None]
    return max(resales) if resales else None
