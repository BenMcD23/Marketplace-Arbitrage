"""eBay Browse API source.

Fetches Buy-It-Now electronics listings by category + keyword, filtered by max
price and condition, and normalises each into a `Listing`. This is the first
"real" source and the milestone target: the whole pipeline must work on
legitimate eBay API data before any scraping is added.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from arb.config import Settings
from arb.logging_conf import get_logger
from arb.models import Listing
from oracle.ebay_client import BROWSE_URL, EbayClient
from sources.base import Source
from sources.normalise import clean_title, extract_brand, extract_model_number, normalise_condition

log = get_logger("sources.ebay")

# eBay condition ids -> our raw condition strings (fed through normalise).
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
    "7000": "for parts or not working",
}


def parse_browse_item(item: dict[str, Any]) -> Listing | None:
    """Map a single eBay Browse item_summary into a Listing (pure, tested offline)."""
    item_id = item.get("itemId") or item.get("legacyItemId")
    title = item.get("title")
    price_obj = item.get("price") or {}
    price = price_obj.get("value")
    if not (item_id and title and price is not None):
        return None

    shipping = 0.0
    for opt in item.get("shippingOptions") or []:
        cost = (opt.get("shippingCost") or {}).get("value")
        if cost is not None:
            shipping = float(cost)
            break

    raw_condition = item.get("condition")
    if not raw_condition:
        raw_condition = _CONDITION_IDS.get(str(item.get("conditionId", "")), None)

    title = clean_title(title)
    location = None
    loc = item.get("itemLocation") or {}
    if loc:
        location = ", ".join(filter(None, [loc.get("city"), loc.get("postalCode"), loc.get("country")])) or None

    return Listing(
        source="ebay",
        source_listing_id=str(item_id),
        title=title,
        model_number=extract_model_number(title),
        brand=extract_brand(title),
        price=float(price),
        shipping=shipping,
        condition=normalise_condition(raw_condition),
        url=item.get("itemWebUrl") or item.get("itemHref") or "",
        image_url=(item.get("image") or {}).get("imageUrl"),
        location=location,
    )


class EbaySource(Source):
    name = "ebay"

    def __init__(
        self,
        settings: Settings,
        queries: list[str],
        category_id: str | None = None,
        max_price: float | None = None,
        limit: int = 50,
        client: EbayClient | None = None,
    ):
        self.settings = settings
        self.queries = queries
        self.category_id = category_id
        self.max_price = max_price
        self.limit = limit
        self._client = client or EbayClient(
            client_id=settings.ebay_client_id or "",
            client_secret=settings.ebay_client_secret or "",
            marketplace=settings.ebay_marketplace,
            has_insights=settings.ebay_has_insights,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _build_filter(self) -> str:
        parts = ["buyingOptions:{FIXED_PRICE}"]
        if self.max_price is not None:
            parts.append(f"price:[..{self.max_price}]")
            parts.append("priceCurrency:GBP")
        return ",".join(parts)

    async def fetch(self) -> AsyncIterator[Listing]:
        headers = await self._client._headers()  # reuse token + marketplace headers
        for query in self.queries:
            params = {
                "q": query,
                "limit": str(self.limit),
                "filter": self._build_filter(),
            }
            if self.category_id:
                params["category_ids"] = self.category_id
            try:
                resp = await self._client._client.get(BROWSE_URL, headers=headers, params=params)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("ebay_fetch_error", query=query, error=str(exc))
                continue
            payload = resp.json()
            items = payload.get("itemSummaries") or []
            log.info("ebay_fetched", query=query, count=len(items))
            for raw in items:
                listing = parse_browse_item(raw)
                if listing is not None:
                    yield listing
