"""End-to-end pipeline.

    for each source -> get listings -> for each NEW listing -> get valuation
    -> evaluate deal -> if Deal, store + alert

Dedup happens up front (the `seen` table) so a listing is never valued or
alerted twice, even across runs. A run summary is logged at the end.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from alerts.null import NullAlerter
from arb.config import Settings
from arb.db import Database
from arb.logging_conf import get_logger
from arb.models import Listing
from engine.deals import evaluate
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
    by_source: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "listings_scanned": self.listings_scanned,
            "new_listings": self.new_listings,
            "valuations_fetched": self.valuations_fetched,
            "deals_found": self.deals_found,
            "scam_flags": self.scam_flags,
            "alerts_sent": self.alerts_sent,
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

        valuation = await self.oracle.get_valuation(listing.model_number, listing.title)
        stats.valuations_fetched += 1

        deal = evaluate(listing, valuation, self.settings)
        if deal is None:
            return

        # Guard against double-alerting across concurrent/overlapping runs.
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

    async def run(self, sources: list[Source]) -> RunStats:
        stats = RunStats()
        for source in sources:
            if not source.enabled:
                log.info("source_disabled", source=source.name)
                continue
            log.info("source_start", source=source.name)
            try:
                async for listing in source.fetch():
                    await self._process_listing(listing, stats)
            except Exception as exc:  # a broken source must not kill the run
                log.error("source_failed", source=source.name, error=str(exc))
            finally:
                await source.aclose()

        log.info("run_complete", **stats.as_dict())
        return stats
