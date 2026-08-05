"""Building a sold-price history without paying for one.

eBay's sold data lives behind the Marketplace Insights API, which is a limited
release that is closed to new applicants — and the third-party services that
resell it charge monthly. But the Browse API is free, and a listing that was
there yesterday and is gone today has told you something.

So the tracker does this, once per scan:

  1. Every comp returned by a search is recorded in `comp_watch` with a
     first-seen timestamp.
  2. Comps that stop appearing in search results become "stale".
  3. A budgeted handful of stale comps are checked directly. Ones eBay reports
     as ended become `sold_observations` — a price someone plausibly paid, and
     a duration telling us how long it took.

Over a few weeks of scanning this accumulates into a private sold-comp database
that is worth more than the one we could have bought, because it is specific to
the exact searches being run.

**The honest caveat**, which the confidence model is built around: an ended
listing is not necessarily a *sold* listing. Sellers cancel, run out of
patience, or let fixed-duration listings lapse. The signal is good but noisy, so
`SOLD_INFERENCE_HAIRCUT` discounts observed prices slightly and the resulting
valuations never reach full confidence on inferred data alone. Where the
Marketplace Insights API *is* available, its genuinely-sold comps are used
instead and skip the haircut entirely.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime

from arb.config import Settings
from arb.db import Database
from arb.logging_conf import get_logger
from arb.models import Condition, SoldObservation
from oracle.comps import Comp
from oracle.ebay_client import EbayClient

log = get_logger("oracle.sold_tracker")

# Ended-but-not-necessarily-sold listings skew high: an item that failed to sell
# at its asking price is over-represented among endings. A small haircut keeps
# the estimate honest without throwing the signal away.
SOLD_INFERENCE_HAIRCUT = 0.97


class SoldTracker:
    def __init__(self, settings: Settings, db: Database, ebay: EbayClient | None = None):
        self.settings = settings
        self.db = db
        self.ebay = ebay

    # ------------------------------------------------------------------ record
    def record_active(self, product_key: str, comps: list[Comp]) -> None:
        """Note every comp currently visible for a product key."""
        now = datetime.now(UTC)
        for comp in comps:
            if not comp.item_id:
                continue
            self.db.record_comp_sighting(
                item_id=comp.item_id,
                product_key=product_key,
                title=comp.title,
                price=comp.price,
                condition=comp.condition,
                url=comp.url,
                now=now,
            )

    # ------------------------------------------------------------------ sweep
    def sweep_budget(self) -> int:
        """How many listing checks this run may spend.

        While the sold history is thin the daily API allowance is mostly idle,
        and every idle call is another day added to the cold start — so the
        sweep runs much harder until there is enough history to price from, then
        settles back to a maintenance rate. Never more than the day's remaining
        allowance, whichever mode it is in.
        """
        observed = self.db.sold_count()
        if observed < self.settings.sold_bootstrap_threshold:
            limit = self.settings.sold_sweep_bootstrap_checks
        else:
            limit = self.settings.sold_sweep_max_checks

        if self.ebay is not None:
            # Leave the scan's own needs alone: only spend what is actually left.
            limit = min(limit, self.ebay.budget.remaining)
        return max(0, limit)

    async def sweep(self, max_checks: int | None = None) -> int:
        """Resolve stale watched comps into sold observations.

        Returns the number of new sold observations recorded. Costs at most
        `max_checks` API calls, so it can be given whatever budget is left over
        after the scan itself.
        """
        if self.ebay is None:
            return 0
        limit = max_checks if max_checks is not None else self.sweep_budget()
        if limit <= 0:
            return 0

        stale = self.db.stale_comps(
            not_seen_for_hours=self.settings.comp_stale_hours, limit=limit
        )
        if not stale:
            return 0

        recorded = 0
        for row in stale:
            live = await self.ebay.item_is_live(row["item_id"])
            if live is None:
                # Inconclusive — leave it watched and try again next run.
                self.db.touch_comp_check(row["item_id"])
                continue
            if live:
                # Still listed; it simply fell out of the search ranking. Keep
                # watching it — it is live inventory and may yet sell.
                self.db.mark_comp_still_live(row["item_id"])
                continue

            first_seen = _parse(row["first_seen_at"])
            last_seen = _parse(row["last_seen_at"])
            days = max(0.0, (last_seen - first_seen).total_seconds() / 86400.0)
            self.db.record_sold(
                SoldObservation(
                    item_id=row["item_id"],
                    product_key=row["product_key"],
                    title=row["title"],
                    price=row["price"],
                    condition=Condition(row["condition"]),
                    first_seen_at=first_seen,
                    sold_at=last_seen,
                    days_listed=round(days, 2),
                )
            )
            self.db.resolve_comp_ended(row["item_id"])
            recorded += 1

        log.info("sold_sweep", checked=len(stale), recorded=recorded)
        return recorded

    # ------------------------------------------------------------------ read
    def sold_comps(self, product_key: str, days: int | None = None) -> list[Comp]:
        """Observed sales for a product key, as comps the oracle can price from."""
        window = days if days is not None else self.settings.sold_window_days
        observations = self.db.sold_observations(product_key, days=window)
        return [
            Comp(
                title=obs.title,
                price=round(obs.price * SOLD_INFERENCE_HAIRCUT, 2),
                condition=obs.condition,
                item_id=obs.item_id,
                sold=True,
            )
            for obs in observations
        ]

    def liquidity(self, product_key: str, days: int | None = None) -> tuple[float | None, int | None]:
        """(sell-through %, median days to sell) for a product key.

        Sell-through is observed sales over sales-plus-still-listed. Days to
        sell is measured from *our first sighting*, not from when the seller
        listed it, so it under-states the true duration — fine for ranking one
        product against another, which is all it is used for.
        """
        window = days if days is not None else self.settings.sold_window_days
        observations = self.db.sold_observations(product_key, days=window)
        if not observations:
            return None, None

        live = self.db.count_live_comps(product_key)
        total = len(observations) + live
        sell_through = round(len(observations) / total * 100, 1) if total else None

        durations = [o.days_listed for o in observations if o.days_listed > 0]
        median_days = int(round(statistics.median(durations))) if durations else None
        return sell_through, median_days


def _parse(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.now(UTC)
