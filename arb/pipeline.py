"""End-to-end pipeline.

    for each source -> get listings -> for each NEW listing -> get valuation
    -> evaluate deal -> store -> then sweep ended comps into sold history

Dedup happens up front (the `seen` table) so a listing is never valued twice,
even across runs. Each run is recorded in the `runs` table so the API and UI can
show history and progress.

The sold sweep runs last, deliberately: the scan is what earns money today, and
whatever API budget it leaves over is spent building the sold-price history that
makes tomorrow's valuations better.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from alerts.null import NullAlerter
from arb.config import Settings
from arb.db import Database
from arb.logging_conf import get_logger
from arb.models import Listing, Run, RunStatus
from engine.deals import evaluate
from oracle.ebay_client import BudgetExhausted
from oracle.pricing import PricingOracle
from sources.base import Source

log = get_logger("pipeline")


@dataclass
class RunStats:
    listings_scanned: int = 0
    new_listings: int = 0
    valuations_fetched: int = 0
    deals_found: int = 0
    scam_flags: int = 0
    alerts_sent: int = 0
    sold_observed: int = 0
    api_calls: int = 0
    budget_exhausted: bool = False
    by_source: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "listings_scanned": self.listings_scanned,
            "new_listings": self.new_listings,
            "valuations_fetched": self.valuations_fetched,
            "deals_found": self.deals_found,
            "scam_flags": self.scam_flags,
            "alerts_sent": self.alerts_sent,
            "sold_observed": self.sold_observed,
            "api_calls": self.api_calls,
            "budget_exhausted": self.budget_exhausted,
            "by_source": self.by_source,
        }


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        oracle: PricingOracle,
        alerter: NullAlerter,
    ):
        self.settings = settings
        self.db = db
        self.oracle = oracle
        self.alerter = alerter

    async def _process_listing(self, listing: Listing, stats: RunStats) -> None:
        stats.listings_scanned += 1
        stats.by_source[listing.source] = stats.by_source.get(listing.source, 0) + 1

        # Dedup: only the first sighting of a listing proceeds.
        if not self.db.mark_seen(listing.id):
            return
        stats.new_listings += 1
        self.db.upsert_listing(listing)

        valuation = await self.oracle.get_valuation(listing)
        stats.valuations_fetched += 1

        deal = evaluate(listing, valuation, self.settings)
        if deal is None:
            return

        # Guard against double-recording across overlapping runs.
        if self.db.was_alerted(listing.id):
            return

        self.db.upsert_deal(deal)
        if deal.is_scam_flag:
            stats.scam_flags += 1
        else:
            stats.deals_found += 1

        sent = await self.alerter.send_deal(deal, listing)
        if sent:
            self.db.mark_alerted(listing.id)
            stats.alerts_sent += 1

    async def run(self, sources: list[Source], run_id: int | None = None) -> RunStats:
        stats = RunStats()
        # A fresh run re-derives the asking->sold calibration from whatever sold
        # data has accumulated since last time.
        self.oracle.reset_calibration()

        for source in sources:
            if not source.enabled:
                log.info("source_disabled", source=source.name)
                continue
            log.info("source_start", source=source.name)
            try:
                async for listing in source.fetch():
                    await self._process_listing(listing, stats)
            except BudgetExhausted as exc:
                # Not an error: the day's free allowance is simply spent.
                stats.budget_exhausted = True
                log.warning("budget_exhausted", source=source.name, error=str(exc))
                break
            except Exception as exc:  # a broken source must not kill the run
                log.error("source_failed", source=source.name, error=str(exc))
            finally:
                await source.aclose()

        # Spend leftover budget building the free sold-price history.
        if not stats.budget_exhausted:
            try:
                stats.sold_observed = await self.oracle.tracker.sweep()
            except BudgetExhausted:
                stats.budget_exhausted = True
            except Exception as exc:
                log.error("sold_sweep_failed", error=str(exc))

        if self.oracle.ebay is not None:
            stats.api_calls = self.oracle.ebay.budget.used

        if run_id is not None:
            self.db.finish_run(
                Run(
                    id=run_id,
                    status=RunStatus.COMPLETE,
                    finished_at=datetime.now(UTC),
                    listings_scanned=stats.listings_scanned,
                    new_listings=stats.new_listings,
                    valuations_fetched=stats.valuations_fetched,
                    deals_found=stats.deals_found,
                    scam_flags=stats.scam_flags,
                    sold_observed=stats.sold_observed,
                    api_calls=stats.api_calls,
                    by_source=stats.by_source,
                )
            )

        log.info("run_complete", **stats.as_dict())
        return stats
