"""Pricing Oracle — decides what something is worth, and how sure it is.

The old oracle took a median of whatever eBay handed back. This one treats
valuation as the estimation problem it actually is:

  * **Comps are earned, not given** (`oracle.comps`) — accessories, spares, job
    lots and near-miss models are thrown out before any arithmetic happens.
  * **Robust statistics** (`oracle.robust`) — MAD-based outlier rejection, so a
    single £2,000 typo cannot drag a £300 valuation upwards.
  * **Sold beats asking.** Observed sales (`oracle.sold_tracker`) are used when
    there are enough of them. Otherwise asking prices are discounted towards a
    realistic sale price by a ratio the system *calibrates from its own data*.
  * **Condition is priced, not ignored.** A used handset is not valued off
    sealed-in-box comps.
  * **Every number carries a confidence**, so the deal engine can size its
    conviction instead of treating four scruffy comps like forty good ones.

The result is a `Valuation` that can be audited in the UI: which comps counted,
which were rejected and why, how wide the spread was, and how much to trust it.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime

from arb.config import Settings
from arb.db import Database
from arb.logging_conf import get_logger
from arb.models import CompRef, Condition, Listing, PriceBasis, Valuation
from oracle.cex_client import CexClient, CexQuote
from oracle.comps import Comp, CompSelection, select_comps
from oracle.ebay_client import BudgetExhausted, EbayClient
from oracle.keepa_client import KeepaClient
from oracle.robust import PriceEstimate, robust_price
from oracle.sold_tracker import SoldTracker
from oracle.terapeak import TerapeakClient, stats_to_comps
from sources.normalise import product_key

log = get_logger("oracle.pricing")

_BASIS_TRUST = {
    PriceBasis.SOLD: 1.0,
    PriceBasis.AMAZON: 0.8,
    #: CeX is a real used-retail price but for a single matched product on a
    #: different venue, so it is trusted below the marketplace we actually sell
    #: on — but above nothing, which is what the cold start used to have.
    PriceBasis.CEX: 0.65,
    PriceBasis.ACTIVE: 0.7,
    PriceBasis.NONE: 0.0,
}

#: When two independent sources disagree by more than this, the estimate is
#: softer than either one alone suggests, and confidence is cut accordingly.
_DISAGREEMENT_TOLERANCE = 0.35
_DISAGREEMENT_PENALTY = 0.7


def median(values: list[float]) -> float | None:
    """Plain median of positive values. Retained for callers that want one."""
    clean = [v for v in values if v is not None and v > 0]
    if not clean:
        return None
    return round(statistics.median(clean), 2)


def confidence_score(
    estimate: PriceEstimate,
    basis: PriceBasis,
    kept: list[Comp],
    settings: Settings,
) -> float:
    """Fold sample size, agreement, comp quality and data basis into 0..1.

    Kept deliberately simple and monotonic — every input moves confidence in the
    direction you would expect, which matters more for a number a human has to
    trust than any extra sophistication would.
    """
    target_n = max(1, settings.confidence_target_comps)
    sample = min(1.0, estimate.n_used / target_n)

    max_cv = max(0.01, settings.confidence_max_cv)
    spread = max(0.0, min(1.0, 1.0 - (estimate.cv / max_cv)))

    relevances = [c.relevance for c in kept if c.relevance > 0]
    quality = statistics.fmean(relevances) if relevances else 0.5

    raw = 0.40 * sample + 0.30 * spread + 0.30 * quality
    return round(_BASIS_TRUST.get(basis, 0.0) * raw, 3)


def calibrate_active_ratio(db: Database, settings: Settings) -> float:
    """Learn how far asking prices sit above realised prices.

    For every product key where we have both observed sales and live comps, the
    ratio of the two medians says what the discount from "asking" to "sold"
    really is for the kind of stock being scanned. Averaged across keys it is a
    far better constant than a guess — and it improves on its own as the sold
    history grows.

    Falls back to the configured default until there is enough paired data to
    beat it.
    """
    rows = db.query(
        """
        SELECT s.product_key AS k,
               AVG(s.price)  AS sold_avg,
               (SELECT AVG(w.price) FROM comp_watch w
                 WHERE w.product_key = s.product_key AND w.resolved = 0) AS active_avg
        FROM sold_observations s
        GROUP BY s.product_key
        """
    )
    ratios = [
        r["sold_avg"] / r["active_avg"]
        for r in rows
        if r["sold_avg"] and r["active_avg"] and r["active_avg"] > 0
    ]
    # Ignore implausible ratios — those are key collisions, not market signal.
    ratios = [r for r in ratios if 0.4 <= r <= 1.2]

    if len(ratios) < settings.calibration_min_keys:
        return settings.active_to_sold_ratio
    learned = round(statistics.median(ratios), 3)
    log.info("active_ratio_calibrated", ratio=learned, keys=len(ratios))
    return learned


def _split_by_condition(comps: list[Comp]) -> tuple[list[Comp], list[Comp], list[Comp]]:
    new = [c for c in comps if c.condition == Condition.NEW]
    used = [c for c in comps if c.condition == Condition.USED]
    unknown = [c for c in comps if c.condition == Condition.UNKNOWN]
    return new, used, unknown


def calibrate_condition_ratio(db: Database, settings: Settings) -> float:
    """Learn what "used" is worth as a fraction of "new", from observed sales.

    The ratio cannot be learned from a single product's comp set — if that set
    held enough of both conditions we would simply price off the same-condition
    half and never need a ratio at all. So it is learned across the corpus:
    every product key that has recorded both new and used sales contributes one
    ratio, and the median of those is the constant to convert with.

    Falls back to the configured default until enough keys have both.
    """
    rows = db.query(
        """
        SELECT product_key,
               AVG(CASE WHEN condition = 'new'  THEN price END) AS new_avg,
               AVG(CASE WHEN condition = 'used' THEN price END) AS used_avg
        FROM sold_observations
        GROUP BY product_key
        HAVING new_avg IS NOT NULL AND used_avg IS NOT NULL
        """
    )
    ratios = [
        r["used_avg"] / r["new_avg"]
        for r in rows
        if r["new_avg"] and r["new_avg"] > 0
    ]
    ratios = [r for r in ratios if 0.3 <= r <= 1.0]

    if len(ratios) < settings.calibration_min_keys:
        return settings.used_to_new_ratio
    learned = round(statistics.median(ratios), 3)
    log.info("condition_ratio_calibrated", ratio=learned, keys=len(ratios))
    return learned


def condition_adjusted_estimate(
    kept: list[Comp],
    target_condition: Condition,
    settings: Settings,
    condition_ratio: float | None = None,
) -> tuple[PriceEstimate | None, list[Comp], str]:
    """Price a listing against the comps that describe *its* condition.

    Prefers a same-condition sample. When that is too thin it prices off the
    other condition and converts using `condition_ratio` (see
    `calibrate_condition_ratio`), falling back to the configured default.
    Returns the estimate, the comps it was built from, and a note for the audit
    trail.
    """
    new, used, unknown = _split_by_condition(kept)
    min_n = settings.min_condition_comps

    same = new if target_condition == Condition.NEW else used
    other = used if target_condition == Condition.NEW else new

    # Unknown-condition comps sit with whichever group is being used; they are
    # mostly genuine listings whose condition string we failed to parse.
    same_pool = same + unknown

    if target_condition in (Condition.NEW, Condition.USED) and len(same_pool) >= min_n:
        return robust_price([c.price for c in same_pool]), same_pool, "same_condition_comps"

    # Not enough same-condition comps. If the other side is well populated,
    # price off it and convert.
    if target_condition in (Condition.NEW, Condition.USED) and len(other) >= min_n:
        est = robust_price([c.price for c in other])
        if est is None:
            return None, [], "no_usable_comps"

        if condition_ratio is None:
            ratio, note = settings.used_to_new_ratio, "condition_ratio_default"
        else:
            ratio, note = condition_ratio, "condition_ratio_calibrated"
        ratio = min(1.0, max(0.3, ratio))

        factor = ratio if target_condition == Condition.USED else (1 / ratio if ratio else 1.0)
        adjusted = PriceEstimate(
            value=round(est.value * factor, 2),
            n_used=est.n_used,
            n_total=est.n_total,
            spread=round(est.spread * factor, 2),
            cv=est.cv,
            p10=round(est.p10 * factor, 2),
            p90=round(est.p90 * factor, 2),
        )
        return adjusted, other, note

    # Condition unknown, or both groups thin — use everything we kept.
    return robust_price([c.price for c in kept]), kept, "mixed_condition_comps"


class PricingOracle:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        ebay: EbayClient | None = None,
        keepa: KeepaClient | None = None,
        tracker: SoldTracker | None = None,
        cex: CexClient | None = None,
        terapeak: TerapeakClient | None = None,
    ):
        self.settings = settings
        self.db = db
        self.ebay = ebay
        self.keepa = keepa
        self.cex = cex
        self.terapeak = terapeak
        self.tracker = tracker or SoldTracker(settings, db, ebay)
        self._active_ratio: float | None = None
        self._condition_ratio: float | None = None

    async def aclose(self) -> None:
        if self.ebay:
            await self.ebay.aclose()
        if self.keepa:
            await self.keepa.aclose()
        if self.cex:
            await self.cex.aclose()
        if self.terapeak:
            await self.terapeak.aclose()

    @property
    def active_ratio(self) -> float:
        """Calibrated asking-price -> sale-price ratio, computed once per run."""
        if self._active_ratio is None:
            self._active_ratio = calibrate_active_ratio(self.db, self.settings)
        return self._active_ratio

    @property
    def condition_ratio(self) -> float:
        """Calibrated used/new price ratio, computed once per run."""
        if self._condition_ratio is None:
            self._condition_ratio = calibrate_condition_ratio(self.db, self.settings)
        return self._condition_ratio

    def reset_calibration(self) -> None:
        self._active_ratio = None
        self._condition_ratio = None

    async def get_valuation(self, listing: Listing) -> Valuation:
        """Value a listing, using the cache when it is still fresh."""
        key = product_key(listing.title, listing.brand, listing.model_number)

        cached = self.db.get_valuation(key, ttl_hours=self.settings.valuation_ttl_hours)
        if cached is not None:
            log.debug("valuation_cache_hit", key=key)
            return cached

        valuation = await self._build_valuation(key, listing)
        self.db.upsert_valuation(valuation)
        log.info(
            "valuation_built",
            key=key,
            price=valuation.resale_price,
            basis=valuation.basis.value,
            comps=valuation.comp_count,
            confidence=valuation.confidence,
        )
        return valuation

    async def _build_valuation(self, key: str, listing: Listing) -> Valuation:
        query = listing.model_number or listing.title
        empty = Valuation(product_key=key, basis=PriceBasis.NONE, updated_at=datetime.now(UTC))

        # --- gather candidate comps -------------------------------------
        active_candidates: list[Comp] = []
        if self.ebay is not None:
            try:
                active_candidates = await self.ebay.search_comps(
                    query, limit=self.settings.comp_search_limit
                )
            except BudgetExhausted:
                log.warning("comp_search_budget_exhausted", key=key)
                # A stale valuation beats no valuation when the budget runs out.
                stale = self.db.get_valuation_any_age(key)
                return stale or empty

        # Marketplace Insights sold comps, for the rare account that has them.
        insights_sold: list[Comp] = []
        if self.ebay is not None and self.ebay.has_insights:
            insights_sold = await self.ebay.sold_comps(
                query, days=self.settings.sold_window_days
            )

        # --- filter both sets against the listing being valued -----------
        active_sel = select_comps(
            listing.title,
            active_candidates,
            min_relevance=self.settings.min_comp_relevance,
            target_condition=listing.condition,
        )
        # Watch the surviving comps so their endings become tomorrow's sold data.
        self.tracker.record_active(key, active_sel.kept)

        # Terapeak, when enabled, contributes genuine sold data — the strongest
        # signal available, and unlike the tracker it works from the first scan.
        terapeak_comps: list[Comp] = []
        terapeak_sell_through: float | None = None
        if self.terapeak is not None and self.terapeak.enabled:
            stats = await self.terapeak.stats(query)
            if stats is not None:
                terapeak_comps = stats_to_comps(stats)
                terapeak_sell_through = stats.sell_through_pct

        own_sold = self.tracker.sold_comps(key)
        sold_sel = select_comps(
            listing.title,
            insights_sold + own_sold + terapeak_comps,
            min_relevance=self.settings.min_comp_relevance,
            target_condition=listing.condition,
        )

        # CeX is queried regardless of which basis wins: even when it does not
        # set the price, its cash offer bounds the downside, and its sell price
        # is an independent second opinion on the estimate.
        cex_quote = await self._cex_quote(listing, query)

        # --- choose a basis ----------------------------------------------
        if len(sold_sel.kept) >= self.settings.min_sold_comps:
            selection, basis, scale = sold_sel, PriceBasis.SOLD, 1.0
        elif len(active_sel.kept) >= self.settings.min_comps:
            selection, basis, scale = active_sel, PriceBasis.ACTIVE, self.active_ratio
        else:
            selection, basis, scale = active_sel, PriceBasis.NONE, self.active_ratio

        estimate, priced_from, note = condition_adjusted_estimate(
            selection.kept,
            listing.condition,
            self.settings,
            condition_ratio=self.condition_ratio,
        )

        if estimate is None or basis == PriceBasis.NONE:
            # eBay could not price it. CeX often still can, which is exactly the
            # cold-start case the tracker cannot help with yet.
            valuation = self._cex_only_valuation(key, cex_quote) or empty
            valuation.comps_rejected = len(active_sel.rejected)
            valuation.reject_reasons = active_sel.reject_counts()
            return await self._attach_amazon(valuation, listing)

        resale = round(estimate.value * scale, 2)
        sell_through, days_to_sell = self.tracker.liquidity(key)
        if sell_through is None:
            sell_through = terapeak_sell_through

        confidence = confidence_score(estimate, basis, priced_from, self.settings)
        reject_reasons = _merge_counts(selection.reject_counts(), note)

        # Two independent sources disagreeing is precisely when to be less sure.
        if cex_quote is not None and _disagrees(resale, cex_quote, self.settings):
            confidence = round(confidence * _DISAGREEMENT_PENALTY, 3)
            reject_reasons["note:cex_disagrees"] = 1

        valuation = Valuation(
            product_key=key,
            resale_price=resale,
            basis=basis,
            comp_count=estimate.n_used,
            comps_rejected=len(selection.rejected) + estimate.n_rejected,
            dispersion_cv=estimate.cv,
            price_p10=round(estimate.p10 * scale, 2),
            price_p90=round(estimate.p90 * scale, 2),
            confidence=confidence,
            sell_through_pct=sell_through,
            est_days_to_sell=days_to_sell,
            sample=[
                CompRef(
                    title=c.title,
                    price=c.price,
                    condition=c.condition,
                    url=c.url,
                    sold=c.sold,
                    relevance=c.relevance,
                )
                for c in priced_from[: self.settings.valuation_sample_size]
            ],
            reject_reasons=reject_reasons,
            updated_at=datetime.now(UTC),
        )
        _attach_cex(valuation, cex_quote)
        return await self._attach_amazon(valuation, listing)

    async def _cex_quote(self, listing: Listing, query: str) -> CexQuote | None:
        if self.cex is None or not self.settings.enable_cex:
            return None
        try:
            return await self.cex.quote(listing.title, listing.condition, query=query)
        except Exception as exc:  # an optional source must never break a run
            log.warning("cex_quote_failed", title=listing.title, error=str(exc))
            return None

    def _cex_only_valuation(self, key: str, quote: CexQuote | None) -> Valuation | None:
        """Price purely from CeX, when eBay comps were too thin to use."""
        if quote is None:
            return None
        resale = round(quote.sell_price * self.settings.cex_to_ebay_ratio, 2)
        valuation = Valuation(
            product_key=key,
            resale_price=resale,
            basis=PriceBasis.CEX,
            comp_count=quote.n_matched,
            # A single retail price carries no distribution, so the range is
            # widened by hand rather than implied to be tight.
            price_p10=round(resale * 0.85, 2),
            price_p90=round(resale * 1.15, 2),
            confidence=round(_BASIS_TRUST[PriceBasis.CEX] * 0.6, 3),
            updated_at=datetime.now(UTC),
        )
        _attach_cex(valuation, quote)
        return valuation

    async def _attach_amazon(self, valuation: Valuation, listing: Listing) -> Valuation:
        """Optional Keepa leg. Off entirely unless an API key is configured."""
        if self.keepa is None or not listing.model_number:
            return valuation
        price, rank = await self.keepa.get_price_and_rank(listing.model_number)
        valuation.amazon_price = price
        valuation.amazon_rank = rank
        # Amazon is a usable basis only when eBay produced nothing.
        if valuation.basis == PriceBasis.NONE and price:
            valuation.resale_price = price
            valuation.basis = PriceBasis.AMAZON
            valuation.confidence = round(_BASIS_TRUST[PriceBasis.AMAZON] * 0.6, 3)
        return valuation


def _attach_cex(valuation: Valuation, quote: CexQuote | None) -> Valuation:
    """Record CeX's two numbers on a valuation, whatever set the price."""
    if quote is None:
        return valuation
    valuation.cex_sell_price = quote.sell_price
    valuation.cex_cash_price = quote.cash_price
    valuation.cex_match = quote.matched_name
    return valuation


def _disagrees(resale: float, quote: CexQuote, settings: Settings) -> bool:
    """True when CeX's view of the price is far from ours.

    Compared like for like: CeX's retail price is first discounted to the
    private-sale level our estimate is expressed in.
    """
    cex_equivalent = quote.sell_price * settings.cex_to_ebay_ratio
    if cex_equivalent <= 0 or resale <= 0:
        return False
    return abs(resale - cex_equivalent) / cex_equivalent > _DISAGREEMENT_TOLERANCE


def _merge_counts(counts: dict[str, int], note: str) -> dict[str, int]:
    merged = dict(counts)
    merged[f"note:{note}"] = 1
    return merged


def selection_summary(selection: CompSelection) -> dict[str, int]:
    """Convenience for logging/tests: kept count plus rejection reasons."""
    summary = {"kept": len(selection.kept)}
    summary.update(selection.reject_counts())
    return summary
