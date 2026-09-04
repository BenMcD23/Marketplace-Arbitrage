"""eBay Browse API source.

Fetches Buy-It-Now listings for each watched search term and normalises them
into `Listing` objects. This is the only source that matters right now: it is
free, it is within eBay's terms, and it is where the buying happens.

Searches come from the `watch_queries` table (managed in the UI) so the scan
can be retargeted without a redeploy.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from arb.config import Settings
from arb.logging_conf import get_logger
from arb.models import Listing, WatchQuery
from oracle.ebay_client import CONDITION_IDS, BudgetExhausted, EbayClient, item_category
from sources.base import Source
from sources.normalise import clean_title, extract_brand, extract_model_number, normalise_condition

log = get_logger("sources.ebay")


def parse_browse_item(item: dict[str, Any]) -> Listing | None:
    """Map a single eBay Browse item_summary into a Listing (pure, tested offline)."""
    item_id = item.get("itemId") or item.get("legacyItemId")
    title = item.get("title")
    is_auction = "AUCTION" in (item.get("buyingOptions") or [])
    # On an auction the standing bid is the price that matters; `price` is the
    # Buy-It-Now figure, which many auctions do not have at all.
    price_obj = (item.get("currentBidPrice") if is_auction else None) or item.get("price") or {}
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
        raw_condition = CONDITION_IDS.get(str(item.get("conditionId", "")), None)

    title = clean_title(title)
    category_id, category_name = item_category(item)
    location = None
    loc = item.get("itemLocation") or {}
    if loc:
        location = ", ".join(
            filter(None, [loc.get("city"), loc.get("postalCode"), loc.get("country")])
        ) or None

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
        category_id=category_id,
        category_name=category_name,
        is_auction=is_auction,
        end_time=_parse_end(item.get("itemEndDate")),
        bid_count=int(item.get("bidCount") or 0),
    )


def _parse_end(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


class EbaySource(Source):
    name = "ebay"

    def __init__(
        self,
        settings: Settings,
        queries: list[WatchQuery],
        limit: int = 50,
        client: EbayClient | None = None,
        on_query_done: callable | None = None,
    ):
        self.settings = settings
        self.queries = queries
        self.limit = limit
        self._on_query_done = on_query_done
        # The factory hands the same client to the oracle so both draw on one
        # daily budget. Closing a client we were given would take the oracle's
        # post-scan sweeps down with it.
        self._owns_client = client is None
        self._client = client or EbayClient(
            client_id=settings.ebay_client_id or "",
            client_secret=settings.ebay_client_secret or "",
            marketplace=settings.ebay_marketplace,
            has_insights=settings.ebay_has_insights,
        )

    @property
    def client(self) -> EbayClient:
        return self._client

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self) -> AsyncIterator[Listing]:
        # Buy-it-now and auctions are fetched separately, one call each. Best
        # Match buries auctions under buy-it-now, so asking for both at once
        # returns a full page of BIN and no auctions — the second call is the
        # only way to see them at all.
        options = ["FIXED_PRICE"]
        if self.settings.enable_auctions:
            options.append("AUCTION")

        for watch in self.queries:
            if not watch.enabled:
                continue
            for buying_option in options:
                try:
                    payload = await self._client.search(
                        watch.query,
                        limit=self.limit,
                        category_id=watch.category_id or self.settings.ebay_category_id,
                        max_price=watch.max_price
                        if watch.max_price is not None
                        else self.settings.ebay_max_price,
                        min_price=watch.min_price,
                        buying_options=buying_option,
                    )
                except BudgetExhausted:
                    # Let the pipeline decide what to do about the day's allowance.
                    raise
                except httpx.HTTPError as exc:
                    log.warning("ebay_fetch_error", query=watch.query, error=str(exc))
                    continue

                items = payload.get("itemSummaries") or []
                log.info(
                    "ebay_fetched",
                    query=watch.query,
                    buying_option=buying_option,
                    count=len(items),
                )
                for raw in items:
                    listing = parse_browse_item(raw)
                    if listing is not None:
                        yield listing

            if self._on_query_done is not None:
                self._on_query_done(watch.query)
