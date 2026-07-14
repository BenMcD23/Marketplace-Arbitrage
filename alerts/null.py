"""No-op alerting.

Notifications are turned off for now. Deals are still evaluated and stored in
the database by the pipeline; this alerter simply logs each deal instead of
pushing it anywhere. Swap in a real alerter (Discord, email, ...) here when
notifications are wanted again.
"""

from __future__ import annotations

from arb.config import Settings
from arb.logging_conf import get_logger
from arb.models import Deal, Listing

log = get_logger("alerts.null")


class NullAlerter:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def aclose(self) -> None:
        return None

    async def send_deal(self, deal: Deal, listing: Listing) -> bool:
        if self.settings.dry_run:
            log.info("dry_run_skip_alert", listing_id=listing.id)
            return False
        # No outbound notification — just record that a deal was found so it can
        # be marked handled and not re-processed on the next run.
        log.info(
            "deal_found",
            listing_id=listing.id,
            source=listing.source,
            title=listing.title,
            est_profit=round(deal.est_profit, 2),
            roi_pct=round(deal.roi_pct, 1),
            is_scam_flag=deal.is_scam_flag,
            url=listing.url,
        )
        return True
