"""eBay Browse source for the mini-PC profile.

Differs from the generic `EbaySource` in three ways the profile depends on:

  * each search term is a **separate** API call — eBay's relevance ranking
    truncates the long tail of an OR-ed query, and the whole point of the
    profile is comparing eight distinct machines side by side;
  * auctions are included, priced off the **current bid** (a listing whose bid
    eBay does not return is dropped rather than guessed at);
  * delivery is always resolved to a number and a provenance, so downstream
    code can quote a real landed cost instead of an item price.

Parsing is split out into pure functions so the whole mapping is tested against
saved fixtures with no network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from arb.config import Settings
from arb.logging_conf import get_logger
from arb.models import DeliveryKind, Listing, ListingType
from oracle.ebay_client import BROWSE_URL, EbayClient
from profiles.mini_pc import SEARCH_TERMS
from sources.base import Source
from sources.normalise import clean_title, extract_brand, normalise_condition

log = get_logger("sources.ebay_mini_pc")

# Used + refurbished grades. 7000 ("for parts or not working") is added only
# when the caller explicitly asks for broken units.
USED_CONDITION_IDS = ["2000", "2010", "2020", "2030", "3000", "4000", "5000", "6000"]
FOR_PARTS_CONDITION_ID = "7000"

# eBay condition ids -> raw condition strings (fed through the shared normaliser).
_CONDITION_IDS = {
    "1000": "new",
    "1500": "new other",
    "1750": "new other",
    "2000": "manufacturer refurbished",
    "2010": "manufacturer refurbished",
    "2020": "seller refurbished",
    "2030": "seller refurbished",
    "3000": "used",
    "4000": "used",
    "5000": "used",
    "6000": "used",
    FOR_PARTS_CONDITION_ID: "for parts or not working",
}


def _money(obj: dict[str, Any] | None) -> float | None:
    if not obj:
        return None
    try:
        return float(obj["value"])
    except (KeyError, TypeError, ValueError):
        return None


def parse_delivery(item: dict[str, Any], assumed: float) -> tuple[float, DeliveryKind]:
    """Resolve a listing's delivery cost and where that number came from.

    "Free delivery" and an itemised cost are exact. A calculated-at-checkout
    quote falls back to `assumed` so a landed cost can still be shown — flagged
    as an estimate rather than passed off as the seller's price.
    """
    options = item.get("shippingOptions") or []
    for opt in options:
        cost = _money(opt.get("shippingCost"))
        if cost is None:
            continue
        if cost == 0.0:
            return 0.0, DeliveryKind.FREE
        return round(cost, 2), DeliveryKind.FIXED

    # No usable quote. Collection-only listings genuinely cost nothing to ship.
    if item.get("pickupOptions") and not options:
        return 0.0, DeliveryKind.COLLECTION
    return round(assumed, 2), DeliveryKind.ESTIMATED


def parse_listing_type(buying_options: list[str]) -> ListingType:
    auction = "AUCTION" in buying_options
    fixed = "FIXED_PRICE" in buying_options
    if auction and fixed:
        return ListingType.AUCTION_WITH_BIN
    if auction:
        return ListingType.AUCTION
    return ListingType.FIXED_PRICE


def _parse_end_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _description(item: dict[str, Any]) -> str | None:
    parts = [item.get("subtitle"), item.get("shortDescription")]
    text = " ".join(p for p in parts if p)
    return clean_title(text) or None


def parse_mini_pc_item(item: dict[str, Any], assumed_delivery: float) -> Listing | None:
    """Map an eBay Browse item summary onto a Listing. Pure; returns None if unusable."""
    item_id = item.get("itemId") or item.get("legacyItemId")
    title = item.get("title")
    if not (item_id and title):
        return None

    buying_options = item.get("buyingOptions") or []
    listing_type = parse_listing_type(buying_options)

    if listing_type in (ListingType.AUCTION, ListingType.AUCTION_WITH_BIN):
        # Auctions are priced on the live bid. `price` on an auction summary is
        # the BIN/starting figure, so using it would be exactly the placeholder
        # the profile must not compare on.
        price = _money(item.get("currentBidPrice"))
        if price is None:
            log.debug("auction_without_bid", item_id=str(item_id))
            return None
    else:
        price = _money(item.get("price"))
        if price is None:
            return None

    delivery, delivery_kind = parse_delivery(item, assumed_delivery)

    raw_condition = item.get("condition") or _CONDITION_IDS.get(str(item.get("conditionId", "")))

    location = None
    loc = item.get("itemLocation") or {}
    if loc:
        location = ", ".join(
            filter(None, [loc.get("city"), loc.get("postalCode"), loc.get("country")])
        ) or None

    title = clean_title(title)
    return Listing(
        source="ebay_mini_pc",
        source_listing_id=str(item_id),
        title=title,
        description=_description(item),
        brand=extract_brand(title),
        price=round(price, 2),
        shipping=delivery,
        delivery_kind=delivery_kind,
        listing_type=listing_type,
        best_offer="BEST_OFFER" in buying_options,
        bid_count=item.get("bidCount"),
        ends_at=_parse_end_date(item.get("itemEndDate")),
        condition=normalise_condition(raw_condition),
        url=item.get("itemWebUrl") or item.get("itemHref") or "",
        image_url=(item.get("image") or {}).get("imageUrl"),
        location=location,
    )


class MiniPcEbaySource(Source):
    name = "ebay_mini_pc"

    def __init__(
        self,
        settings: Settings,
        queries: list[str] | None = None,
        include_broken: bool = False,
        client: EbayClient | None = None,
    ):
        self.settings = settings
        self.queries = queries or settings.mini_pc_query_list or SEARCH_TERMS
        self.include_broken = include_broken
        self._client = client or EbayClient(
            client_id=settings.ebay_client_id or "",
            client_secret=settings.ebay_client_secret or "",
            marketplace=settings.ebay_marketplace,
            has_insights=settings.ebay_has_insights,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _build_filter(self) -> str:
        condition_ids = list(USED_CONDITION_IDS)
        if self.include_broken:
            condition_ids.append(FOR_PARTS_CONDITION_ID)
        parts = [
            # Both buying routes: a cheap auction is as good as a cheap BIN.
            "buyingOptions:{FIXED_PRICE|AUCTION}",
            "conditionIds:{" + "|".join(condition_ids) + "}",
        ]
        if self.settings.mini_pc_max_price is not None:
            parts.append(f"price:[..{self.settings.mini_pc_max_price}]")
            parts.append("priceCurrency:GBP")
        return ",".join(parts)

    async def fetch(self) -> AsyncIterator[Listing]:
        headers = await self._client._headers()  # reuse token + marketplace headers
        for query in self.queries:
            params = {
                "q": query,
                "limit": str(self.settings.mini_pc_limit),
                "filter": self._build_filter(),
            }
            try:
                resp = await self._client._client.get(BROWSE_URL, headers=headers, params=params)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("mini_pc_fetch_error", query=query, error=str(exc))
                continue
            items = resp.json().get("itemSummaries") or []
            log.info("mini_pc_fetched", query=query, count=len(items))
            for raw in items:
                listing = parse_mini_pc_item(raw, self.settings.mini_pc_assumed_delivery)
                if listing is not None:
                    yield listing
