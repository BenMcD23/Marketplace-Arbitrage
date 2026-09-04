"""Discord alerting via an incoming webhook.

A webhook is a single unauthenticated POST, so there is no bot, no gateway
connection and no discord.py dependency — httpx, which is already here, does
the whole job.

Rate limits are respected the cheap way: a 429 is retried once after the
`retry_after` Discord hands back. Deals arrive a handful at a time, so anything
more elaborate would be machinery for a problem this never has.
"""

from __future__ import annotations

import asyncio

import httpx

from arb.config import Settings
from arb.logging_conf import get_logger
from arb.models import Deal, Listing

log = get_logger("alerts.discord")

_GREEN = 0x2ECC71
_AMBER = 0xE67E22
_RED = 0xE74C3C


def _embed(deal: Deal, listing: Listing, max_bid: float | None) -> dict:
    """One Discord embed describing a deal."""
    if deal.is_scam_flag:
        colour, headline = _RED, "⚠️ Too good to be true"
    elif listing.is_auction:
        colour, headline = _AMBER, "🔨 Auction"
    else:
        colour, headline = _GREEN, "💰 Deal"

    fields = [
        {"name": "Price", "value": f"£{listing.buy_cost:.2f}", "inline": True},
        {"name": "Est. resale", "value": f"£{deal.est_resale:.2f}", "inline": True},
        {
            "name": "Profit",
            "value": f"£{deal.est_profit:.2f} ({deal.roi_pct:.0f}% ROI)",
            "inline": True,
        },
        {"name": "Expected", "value": f"£{deal.expected_profit:.2f}", "inline": True},
        {"name": "Confidence", "value": f"{deal.confidence:.0%}", "inline": True},
        {"name": "Score", "value": f"{deal.score:.0f}/100", "inline": True},
    ]

    if listing.is_auction:
        # The bid ceiling is the whole point of an auction alert, so it leads,
        # full-width, above the supporting numbers.
        bid = f"£{max_bid:.2f}" if max_bid else "do not bid"
        fields.insert(0, {"name": "🎯 Max bid", "value": bid, "inline": False})
        if listing.end_time is not None:
            stamp = int(listing.end_time.timestamp())
            fields.append(
                {"name": "Ends", "value": f"<t:{stamp}:R> ({listing.bid_count} bids)",
                 "inline": False}
            )

    embed = {
        "title": listing.title[:250],
        "url": listing.url or None,
        "description": f"**{headline}** · {listing.source}\n" + "\n".join(
            f"• {r}" for r in deal.reasons[:5]
        ),
        "color": colour,
        "fields": fields,
    }
    if listing.image_url:
        embed["thumbnail"] = {"url": listing.image_url}
    return embed


class DiscordAlerter:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.webhook_url = settings.discord_webhook_url or ""
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send_deal(self, deal: Deal, listing: Listing, max_bid: float | None = None) -> bool:
        if self.settings.dry_run:
            log.info("dry_run_skip_alert", listing_id=listing.id)
            return False
        if not self.webhook_url:
            log.warning("discord_no_webhook", listing_id=listing.id)
            return False

        payload = {"embeds": [_embed(deal, listing, max_bid)]}
        for attempt in (1, 2):
            try:
                resp = await self._client.post(self.webhook_url, json=payload)
            except httpx.HTTPError as exc:
                log.warning("discord_post_failed", listing_id=listing.id, error=str(exc))
                return False
            if resp.status_code == 429 and attempt == 1:
                wait = float(resp.json().get("retry_after", 1.0))
                await asyncio.sleep(min(wait, 30.0))
                continue
            if resp.status_code >= 400:
                log.warning(
                    "discord_post_rejected",
                    listing_id=listing.id,
                    status=resp.status_code,
                    body=resp.text[:200],
                )
                return False
            log.info("discord_alert_sent", listing_id=listing.id, title=listing.title)
            return True
        return False
