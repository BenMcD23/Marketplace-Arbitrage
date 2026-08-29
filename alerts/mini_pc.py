"""Push-message formatting for the mini-PC profile.

Same shape as the rest of the pipeline's push format (Telegram-flavoured HTML:
`<b>` for emphasis, an `<a>` link at the foot), with one addition the profile
insists on — a **Total landed** line that spells out item price *and* delivery.
Delivery hidden behind a listing link is how a £120 machine turns into a £150
machine, so the number is never left implicit.
"""

from __future__ import annotations

from datetime import timedelta

from arb.models import DeliveryKind, Listing, ListingType
from profiles.mini_pc import MAX_TIER, DealBand, MiniPcCandidate

_BAND_HEADERS = {
    DealBand.EXCEPTIONAL: "🔥 <b>EXCEPTIONAL MINI PC DEAL</b>",
    DealBand.STRONG: "💰 <b>STRONG MINI PC DEAL</b>",
    DealBand.STANDARD: "🖥️ <b>MINI PC</b>",
}

_DELIVERY_LABELS = {
    DeliveryKind.FREE: "free delivery",
    DeliveryKind.FIXED: "delivery",
    DeliveryKind.ESTIMATED: "delivery est.",
    DeliveryKind.COLLECTION: "collection only",
}


#: Notes the message already renders as their own line, so they are not
#: repeated in the bullet list underneath.
_NOTES_RENDERED_ELSEWHERE = ("Needs RAM upgrade", "Best Offer accepted")


def format_storage(gb: int | None) -> str:
    if gb is None:
        return "not stated"
    if gb >= 1024 and gb % 1024 == 0:
        return f"{gb // 1024}TB"
    return f"{gb}GB"


def _esc(text: str) -> str:
    """Escape the handful of characters that matter for HTML parse mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_time_remaining(remaining: timedelta | None) -> str:
    if remaining is None:
        return "ended"
    total_minutes = int(remaining.total_seconds() // 60)
    days, rem = divmod(total_minutes, 60 * 24)
    hours, minutes = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_landed_line(candidate: MiniPcCandidate) -> str:
    """The line that must always be present: item + delivery = total landed."""
    listing = candidate.listing
    label = _DELIVERY_LABELS[listing.delivery_kind]
    return (
        f"<b>Total landed: £{candidate.total_landed:.2f}</b> "
        f"(item £{candidate.item_price:.2f} + {label} £{candidate.delivery_cost:.2f})"
    )


def _buying_line(listing: Listing) -> str:
    if listing.is_auction:
        bids = f"{listing.bid_count} bid{'s' if listing.bid_count != 1 else ''}" \
            if listing.bid_count is not None else "current bid"
        route = "Auction + Buy It Now" if listing.listing_type == ListingType.AUCTION_WITH_BIN \
            else "Auction"
        left = format_time_remaining(listing.time_remaining())
        return f"{route}: £{listing.price:.2f} ({bids}) · {left} left"
    return f"Buy It Now: £{listing.price:.2f}"


def format_mini_pc_deal(candidate: MiniPcCandidate) -> str:
    """Render one ranked candidate as a push message."""
    listing = candidate.listing
    ram = f"{candidate.ram_gb}GB" if candidate.ram_gb is not None else "?"
    storage = format_storage(candidate.storage_gb)

    lines = [
        f"{_BAND_HEADERS[candidate.band]} — {_esc(listing.source)}",
        f"<b>{_esc(listing.title)}</b>",
        f"CPU: {candidate.cpu.name} ({candidate.cpu.tdp_w}W, "
        f"tier {candidate.cpu.tier}/{MAX_TIER})",
        f"RAM: {ram}   Storage: {storage}",
        f"Condition: {_esc(listing.condition.value)}",
        "",
        format_landed_line(candidate),
        _buying_line(listing),
    ]

    if listing.best_offer:
        lines.append("Best Offer available — lever to push the landed cost lower")
    if candidate.needs_ram_upgrade and candidate.ram_upgrade_cost is not None:
        lines.append(
            f"⚠️ Needs RAM upgrade: +£{candidate.ram_upgrade_cost:.2f} "
            f"→ £{candidate.all_in_cost:.2f} all-in"
        )
    for note in candidate.notes:
        if note.startswith(_NOTES_RENDERED_ELSEWHERE):
            continue
        lines.append(f"• {_esc(note)}")
    if listing.location:
        lines.append(f"Location: {_esc(listing.location)}")

    lines.append("")
    lines.append(f'<a href="{_esc(listing.url)}">View listing</a>')
    return "\n".join(lines)
