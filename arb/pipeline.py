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
from engine.deals import evaluate, max_bid
from oracle.ebay_client import BudgetExhausted
from oracle.pricing import PricingOracle
from sources.base import Source
from sources.ebay import parse_browse_item

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
    auctions_deferred: int = 0
    auctions_priced: int = 0
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
            "auctions_deferred": self.auctions_deferred,
            "auctions_priced": self.auctions_priced,
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

        # Dedup: only the first sighting of a listing proceeds. A dry run is
        # exempt — it exists to tune thresholds, which means running the same
        # listings again after each change, so it must not consume the dedup.
        if not self.settings.dry_run and not self.db.mark_seen(listing.id):
            return
        stats.new_listings += 1
        self.db.upsert_listing(listing)

        # An auction's price today says nothing about its price at the hammer.
        # Store it and let `_sweep_auctions` price it once it is near the end —
        # including one already inside the window, which that sweep picks up
        # later in this same run.
        if listing.is_auction:
            stats.auctions_deferred += 1
            return

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
            # Recorded for review in the dashboard, never pushed. A scam flag is
            # a reason to distrust the valuation, so alerting on one is just
            # noise with a big number attached.
            stats.scam_flags += 1
            return
        stats.deals_found += 1

        sent = await self.alerter.send_deal(deal, listing)
        if sent:
            self.db.mark_alerted(listing.id)
            stats.alerts_sent += 1

    async def _sweep_auctions(self, stats: RunStats) -> None:
        """Price the auctions now inside the lead window and alert with a bid cap.

        The stored price is whatever the bid was when we first saw the listing,
        so each one is re-read from eBay first — one API call, and the only
        moment in the auction's life where the standing price is worth acting
        on.
        """
        ebay = self.oracle.ebay
        if ebay is None:
            return

        due = self.db.due_auctions(
            self.settings.auction_bid_lead_hours, self.settings.auction_sweep_max_checks
        )
        for stored in due:
            payload = await ebay.get_item(stored.source_listing_id)
            if payload is None:
                continue
            fresh = parse_browse_item(payload)
            if fresh is None or not fresh.is_auction:
                # Ended, pulled, or converted to a straight sale — either way
                # there is nothing left to bid on.
                self.db.mark_alerted(stored.id)
                continue
            self.db.upsert_listing(fresh)

            valuation = await self.oracle.get_valuation(fresh)
            stats.valuations_fetched += 1
            stats.auctions_priced += 1

            cap = max_bid(fresh, valuation, self.settings)
            if cap <= fresh.buy_cost:
                # Bidding is already past the point where it pays. Marked so it
                # is not re-checked every 15 minutes for the rest of its life.
                log.info(
                    "auction_already_too_expensive",
                    listing_id=fresh.id,
                    bid=fresh.buy_cost,
                    max_bid=cap,
                )
                self.db.mark_alerted(fresh.id)
                continue

            deal = evaluate(fresh, valuation, self.settings)
            if deal is None:
                self.db.mark_alerted(fresh.id)
                continue

            self.db.upsert_deal(deal)
            if deal.is_scam_flag:
                stats.scam_flags += 1
                self.db.mark_alerted(fresh.id)
                continue
            stats.deals_found += 1

            if await self.alerter.send_deal(deal, fresh, max_bid=cap):
                self.db.mark_alerted(fresh.id)
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

        # Auctions first: their window closes, the sold sweep's does not.
        if self.settings.enable_auctions and not stats.budget_exhausted:
            try:
                await self._sweep_auctions(stats)
            except BudgetExhausted:
                stats.budget_exhausted = True
            except Exception as exc:
                log.error("auction_sweep_failed", error=str(exc))

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
