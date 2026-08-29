"""End-to-end pipelines.

`Pipeline` is the resale-arbitrage run:

    for each source -> get listings -> for each NEW listing -> get valuation
    -> evaluate deal -> if Deal, store + alert

`MiniPcPipeline` runs a buy-to-keep search profile instead. It skips the pricing
oracle entirely and, because its output is a *ranking*, collects every candidate
before alerting rather than pushing as it streams.

Both dedup up front (the `seen` table) so a listing is never valued or alerted
twice, even across runs. A run summary is logged at the end.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from alerts.mini_pc import format_mini_pc_deal
from alerts.null import NullAlerter
from arb.config import Settings
from arb.db import Database
from arb.logging_conf import get_logger
from arb.models import Listing
from engine.deals import evaluate
from oracle.pricing import PricingOracle
from profiles.mini_pc import MiniPcCandidate, rank
from profiles.mini_pc import evaluate as evaluate_mini_pc
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


@dataclass
class MiniPcRunStats:
    listings_scanned: int = 0
    new_listings: int = 0
    candidates: int = 0
    alerts_sent: int = 0
    by_band: dict[str, int] = field(default_factory=dict)
    #: reason -> count, so it is obvious *why* a run surfaced nothing.
    rejected: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "listings_scanned": self.listings_scanned,
            "new_listings": self.new_listings,
            "candidates": self.candidates,
            "alerts_sent": self.alerts_sent,
            "by_band": self.by_band,
            "rejected": self.rejected,
        }


class MiniPcPipeline:
    """Runs a mini-PC search profile: scan -> filter -> rank -> push."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        alerter: NullAlerter,
        include_broken: bool = False,
    ):
        self.settings = settings
        self.db = db
        self.alerter = alerter
        self.include_broken = include_broken

    def _collect(self, listing: Listing, stats: MiniPcRunStats) -> MiniPcCandidate | None:
        stats.listings_scanned += 1

        # Dedup: only the first sighting of a listing proceeds.
        if not self.db.mark_seen(listing.id):
            return None
        stats.new_listings += 1
        self.db.upsert_listing(listing)

        verdict = evaluate_mini_pc(listing, self.settings, include_broken=self.include_broken)
        if verdict.candidate is None:
            reason = verdict.reason.value if verdict.reason else "unknown"
            stats.rejected[reason] = stats.rejected.get(reason, 0) + 1
            return None

        stats.candidates += 1
        band = verdict.candidate.band.value
        stats.by_band[band] = stats.by_band.get(band, 0) + 1
        return verdict.candidate

    async def run(self, sources: list[Source]) -> tuple[MiniPcRunStats, list[MiniPcCandidate]]:
        stats = MiniPcRunStats()
        candidates: list[MiniPcCandidate] = []

        for source in sources:
            if not source.enabled:
                log.info("source_disabled", source=source.name)
                continue
            log.info("source_start", source=source.name)
            try:
                async for listing in source.fetch():
                    candidate = self._collect(listing, stats)
                    if candidate is not None:
                        candidates.append(candidate)
            except Exception as exc:  # a broken source must not kill the run
                log.error("source_failed", source=source.name, error=str(exc))
            finally:
                await source.aclose()

        # Ranking needs the whole set, so alerting happens after the scan: best
        # CPU tier first, cheapest total landed cost within a tier.
        ranked = rank(candidates)
        for candidate in ranked:
            self.db.upsert_mini_pc_deal(candidate)
            if self.db.was_alerted(candidate.listing.id):
                continue
            sent = await self.alerter.send_text(
                format_mini_pc_deal(candidate), candidate.listing.image_url
            )
            if sent:
                self.db.mark_alerted(candidate.listing.id)
                stats.alerts_sent += 1

        log.info("mini_pc_run_complete", **stats.as_dict())
        return stats, ranked
