"""Telegram alerting.

Formats a Deal + Listing into a clean message and pushes it to Telegram. Real
deals go to the main chat; "too good to be true" scam flags go to an optional
separate low-priority chat so they don't pollute the real signal. Sends are
queued and paced to stay under Telegram's rate limits.
"""

from __future__ import annotations

import asyncio

import httpx

from arb.config import Settings
from arb.logging_conf import get_logger
from arb.models import Deal, Listing

log = get_logger("alerts.telegram")

API_BASE = "https://api.telegram.org"

# Telegram allows ~30 messages/sec globally and ~1 msg/sec to a single chat.
# We pace conservatively so a big scan never trips the limit.
_MIN_INTERVAL_SEC = 1.1


def _esc(text: str) -> str:
    """Escape the handful of characters that matter for Telegram HTML parse mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_deal(deal: Deal, listing: Listing) -> str:
    header = "⚠️ <b>TOO GOOD TO BE TRUE</b>" if deal.is_scam_flag else "💰 <b>DEAL</b>"
    lines = [
        f"{header} — {_esc(listing.source)}",
        f"<b>{_esc(listing.title)}</b>",
        f"Condition: {_esc(listing.condition.value)}",
        "",
        f"Buy cost: £{deal.buy_cost:.2f}  (price £{listing.price:.2f} + ship £{listing.shipping:.2f})",
        f"Est. resale: £{deal.est_resale:.2f}  via {deal.sell_channel.value}",
        f"Est. fees: £{deal.est_fees:.2f}",
        f"<b>Est. profit: £{deal.est_profit:.2f}</b>",
        f"ROI: {deal.roi_pct:.1f}%   Margin: {deal.margin_pct:.1f}%",
    ]
    if listing.location:
        lines.append(f"Location: {_esc(listing.location)}")
    lines.append("")
    lines.append(f'<a href="{_esc(listing.url)}">View listing</a>')
    return "\n".join(lines)


class TelegramAlerter:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.token = settings.telegram_bot_token
        self.chat_id = settings.telegram_chat_id
        self.scam_chat_id = settings.telegram_scam_chat_id or settings.telegram_chat_id
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._owns_client = client is None
        self._lock = asyncio.Lock()
        self._last_send = 0.0

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    async def _throttle(self) -> None:
        now = asyncio.get_event_loop().time()
        wait = max(0.0, self._last_send + _MIN_INTERVAL_SEC - now)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_send = asyncio.get_event_loop().time()

    async def send_deal(self, deal: Deal, listing: Listing) -> bool:
        if self.settings.dry_run:
            log.info("dry_run_skip_alert", listing_id=listing.id)
            return False
        if not self.configured:
            log.warning("telegram_not_configured")
            return False

        chat_id = self.scam_chat_id if deal.is_scam_flag else self.chat_id
        text = format_deal(deal, listing)

        async with self._lock:
            await self._throttle()
            ok = await self._send(chat_id, text, listing.image_url)
        return ok

    async def _send(self, chat_id: str, text: str, image_url: str | None) -> bool:
        try:
            if image_url:
                resp = await self._client.post(
                    f"{API_BASE}/bot{self.token}/sendPhoto",
                    json={
                        "chat_id": chat_id,
                        "photo": image_url,
                        "caption": text,
                        "parse_mode": "HTML",
                    },
                )
                if resp.status_code >= 400:
                    # Photo can fail if the image URL is unreachable; fall back to text.
                    log.warning("telegram_photo_failed", status=resp.status_code)
                    return await self._send(chat_id, text, None)
            else:
                resp = await self._client.post(
                    f"{API_BASE}/bot{self.token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": False,
                    },
                )
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.error("telegram_send_error", error=str(exc))
            return False
