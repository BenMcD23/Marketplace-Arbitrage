"""Deal Engine — turns a Listing + Valuation into a Deal (or nothing).

The previous engine asked one question: "if this sells at the median, does the
profit clear a threshold?" That question quietly assumes the item sells, sells
at the median, and sells immediately. In resale, none of those are free.

This engine prices the assumptions:

  * **It might not sell at the median.** Every deal is evaluated twice — once at
    the estimated resale price, once at the pessimistic p10 — and the downside
    case is reported alongside the headline number.
  * **It might not sell soon.** Observed sell-through and time-to-sell drive a
    probability of sale and a holding cost on the capital tied up.
  * **The estimate might be wrong.** Valuation confidence is a gate *and* a
    ranking input, so a thin, disagreeing comp set cannot produce a top-ranked
    deal no matter how good the paper margin looks.

The output is a `Deal` carrying an `expected_profit` to rank on, a `score` for
sorting a list at a glance, and `reasons` explaining itself in English.
"""

from __future__ import annotations

from dataclasses import dataclass

from arb.config import Settings
from arb.logging_conf import get_logger
from arb.models import Condition, Deal, Listing, PriceBasis, SellChannel, Valuation
from engine.fees import ProfitBreakdown, profit_at

log = get_logger("engine.deals")

# How strongly to shrink an observed sell-through rate towards the configured
# base rate. Expressed as "this many pseudo-observations of the base rate", so a
# product with 2 recorded sales barely moves off the prior and one with 40 moves
# almost entirely onto its own evidence.
_SELL_THROUGH_PRIOR_WEIGHT = 8.0


@dataclass
class ChannelResult:
    """A fully-costed way of selling one listing."""

    channel: SellChannel
    expected: ProfitBreakdown
    downside: ProfitBreakdown
    p_sale: float
    days_to_sell: int
    holding_cost: float
    expected_profit: float

    @property
    def est_resale(self) -> float:
        return self.expected.resale


def estimate_p_sale(valuation: Valuation, settings: Settings) -> float:
    """Probability the item sells within the observed window.

    Observed sell-through is the best evidence we have, but it is computed from
    however many sales we happen to have recorded, which early on is very few.
    Shrinking it towards the configured base rate stops a single lucky sale from
    reading as "100% sell-through".
    """
    base = settings.base_sell_probability
    if valuation.sell_through_pct is None:
        return round(base, 3)

    observed = max(0.0, min(1.0, valuation.sell_through_pct / 100.0))
    n = float(max(0, valuation.comp_count))
    weight = n / (n + _SELL_THROUGH_PRIOR_WEIGHT)
    blended = weight * observed + (1 - weight) * base
    return round(max(0.05, min(0.98, blended)), 3)


def estimate_days_to_sell(valuation: Valuation, settings: Settings) -> int:
    if valuation.est_days_to_sell and valuation.est_days_to_sell > 0:
        return int(valuation.est_days_to_sell)
    return settings.default_days_to_sell


def score_deal(result: ChannelResult, valuation: Valuation, settings: Settings) -> float:
    """A 0-100 composite for ranking a list of deals at a glance.

    Each component saturates, so a single spectacular input cannot dominate — a
    £900 profit on a valuation built from four comps should not outrank a solid,
    well-evidenced £120. Confidence gets real weight for exactly that reason.
    """
    # £150 expected profit is treated as "excellent"; beyond that, diminishing.
    profit_component = min(1.0, max(0.0, result.expected_profit / 150.0))
    # 100% ROI is "excellent".
    roi_component = min(1.0, max(0.0, result.expected.roi_pct / 100.0))
    confidence_component = min(1.0, max(0.0, valuation.confidence))
    # Selling in a week is excellent; 60 days is not.
    velocity_component = min(1.0, max(0.0, 1.0 - (result.days_to_sell - 7) / 53.0))

    raw = (
        0.30 * profit_component
        + 0.25 * roi_component
        + 0.30 * confidence_component
        + 0.15 * velocity_component
    )
    return round(100.0 * max(0.0, min(1.0, raw)), 1)


def _score_channel(
    channel: SellChannel,
    resale: float,
    downside_resale: float,
    buy_cost: float,
    valuation: Valuation,
    settings: Settings,
) -> ChannelResult | None:
    if resale is None or resale <= 0:
        return None

    expected = profit_at(resale, buy_cost, channel, settings)
    downside = profit_at(max(downside_resale, 0.01), buy_cost, channel, settings)

    p_sale = estimate_p_sale(valuation, settings)
    days = estimate_days_to_sell(valuation, settings)
    holding = round(buy_cost * settings.daily_capital_cost_pct * days, 2)

    # Two-point expectation: it sells at the estimate, or it eventually clears at
    # the pessimistic end of the range. Not selling at all is not modelled as a
    # total loss, because in practice you drop the price until it moves.
    expected_profit = round(
        p_sale * expected.profit + (1 - p_sale) * downside.profit - holding, 2
    )

    return ChannelResult(
        channel=channel,
        expected=expected,
        downside=downside,
        p_sale=p_sale,
        days_to_sell=days,
        holding_cost=holding,
        expected_profit=expected_profit,
    )


def _candidate_channels(
    listing: Listing, valuation: Valuation, settings: Settings
) -> list[ChannelResult]:
    """Every channel we have usable resale data for, fully costed."""
    buy_cost = listing.buy_cost
    results: list[ChannelResult] = []

    if valuation.has_price:
        downside = valuation.price_p10 or (valuation.resale_price * 0.85)
        ebay = _score_channel(
            SellChannel.EBAY,
            valuation.resale_price,
            downside,
            buy_cost,
            valuation,
            settings,
        )
        if ebay is not None:
            results.append(ebay)

    if valuation.amazon_price is not None and valuation.amazon_price > 0:
        rank_ok = (
            valuation.amazon_rank is not None
            and valuation.amazon_rank <= settings.max_amazon_rank
        )
        if rank_ok:
            amazon = _score_channel(
                SellChannel.AMAZON,
                valuation.amazon_price,
                valuation.amazon_price * 0.9,
                buy_cost,
                valuation,
                settings,
            )
            if amazon is not None:
                results.append(amazon)

    return results


def _build_deal(
    listing: Listing,
    valuation: Valuation,
    result: ChannelResult,
    settings: Settings,
    *,
    is_scam: bool,
    reasons: list[str],
) -> Deal:
    return Deal(
        listing_id=listing.id,
        buy_cost=listing.buy_cost,
        est_resale=result.est_resale,
        est_fees=result.expected.fees.total,
        est_profit=result.expected.profit,
        margin_pct=result.expected.margin_pct,
        roi_pct=result.expected.roi_pct,
        sell_channel=result.channel,
        p_sale=result.p_sale,
        est_days_to_sell=result.days_to_sell,
        holding_cost=result.holding_cost,
        expected_profit=result.expected_profit,
        confidence=valuation.confidence,
        score=0.0 if is_scam else score_deal(result, valuation, settings),
        worst_case_profit=result.downside.profit,
        is_scam_flag=is_scam,
        reasons=reasons,
    )


def evaluate(listing: Listing, valuation: Valuation, settings: Settings) -> Deal | None:
    """Evaluate a listing against its valuation. Returns a Deal or None.

    A returned Deal may carry `is_scam_flag=True` — those are recorded for
    review rather than treated as buys.
    """
    if listing.condition == Condition.FOR_PARTS and not settings.allow_for_parts:
        log.debug("reject_for_parts", listing_id=listing.id)
        return None

    candidates = _candidate_channels(listing, valuation, settings)
    if not candidates:
        return None

    best = max(candidates, key=lambda c: c.expected_profit)

    # --- Too-good-to-be-true ---------------------------------------------
    # Checked against the best resale figure available, on the channel that
    # produced it, so the reported numbers always describe the same channel the
    # ratio was computed from.
    if listing.buy_cost < settings.tgtbt_ratio * best.est_resale:
        log.info(
            "scam_flag",
            listing_id=listing.id,
            buy_cost=listing.buy_cost,
            est_resale=best.est_resale,
        )
        return _build_deal(
            listing,
            valuation,
            best,
            settings,
            is_scam=True,
            reasons=[
                f"Asking £{listing.buy_cost:.2f} against an estimated "
                f"£{best.est_resale:.2f} resale — below the "
                f"{settings.tgtbt_ratio:.0%} plausibility floor."
            ],
        )

    # --- Gates ------------------------------------------------------------
    if valuation.confidence < settings.min_confidence:
        log.debug(
            "reject_low_confidence", listing_id=listing.id, confidence=valuation.confidence
        )
        return None
    if best.expected.profit < settings.min_profit:
        log.debug("reject_below_min_profit", listing_id=listing.id, profit=best.expected.profit)
        return None
    if best.expected.roi_pct < settings.min_roi:
        log.debug("reject_below_min_roi", listing_id=listing.id, roi=best.expected.roi_pct)
        return None
    if best.expected_profit < settings.min_expected_profit:
        log.debug(
            "reject_below_min_expected_profit",
            listing_id=listing.id,
            expected=best.expected_profit,
        )
        return None

    score = score_deal(best, valuation, settings)
    if score < settings.min_score:
        log.debug("reject_below_min_score", listing_id=listing.id, score=score)
        return None

    log.info(
        "deal_flagged",
        listing_id=listing.id,
        channel=best.channel.value,
        profit=best.expected.profit,
        expected=best.expected_profit,
        roi=best.expected.roi_pct,
        score=score,
    )
    return _build_deal(
        listing,
        valuation,
        best,
        settings,
        is_scam=False,
        reasons=explain(listing, valuation, best, settings),
    )


def explain(
    listing: Listing,
    valuation: Valuation,
    result: ChannelResult,
    settings: Settings,
) -> list[str]:
    """Plain-English notes on why this is a deal — and what could go wrong."""
    notes: list[str] = []

    basis_text = {
        PriceBasis.SOLD: f"{valuation.comp_count} observed sales",
        PriceBasis.ACTIVE: f"{valuation.comp_count} active listings (discounted to a sale price)",
        PriceBasis.AMAZON: "Amazon pricing via Keepa",
        PriceBasis.NONE: "no comparable data",
    }[valuation.basis]
    notes.append(f"Valued at £{result.est_resale:.2f} from {basis_text}.")

    notes.append(
        f"Buy £{listing.buy_cost:.2f}, sell on {result.channel.value} for "
        f"£{result.est_resale:.2f}, £{result.expected.fees.total:.2f} of costs "
        f"→ £{result.expected.profit:.2f} profit ({result.expected.roi_pct:.0f}% ROI)."
    )

    notes.append(
        f"Risk-adjusted: {result.p_sale:.0%} chance of selling in ~{result.days_to_sell} days, "
        f"£{result.holding_cost:.2f} of held capital → £{result.expected_profit:.2f} expected."
    )

    if result.downside.profit < 0:
        notes.append(
            f"Downside: at the pessimistic £{result.downside.resale:.2f} resale you would "
            f"lose £{abs(result.downside.profit):.2f}."
        )
    else:
        notes.append(
            f"Downside: even at £{result.downside.resale:.2f} you clear "
            f"£{result.downside.profit:.2f}."
        )

    if valuation.dispersion_cv > 0.3:
        notes.append(
            f"Caution: comps disagree a lot (spread {valuation.dispersion_cv:.0%} of the median) — "
            "the resale estimate is soft."
        )
    if valuation.confidence < 0.5:
        notes.append(
            f"Caution: valuation confidence is only {valuation.confidence:.0%}; "
            "verify the comps before committing."
        )
    if valuation.comps_rejected > valuation.comp_count:
        notes.append(
            f"{valuation.comps_rejected} candidate comps were discarded as accessories, "
            "wrong variants or outliers."
        )
    return notes
