from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from arb.config import Settings
from arb.models import Condition, WatchQuery
from oracle.ebay_client import EbayClient
from sources.ebay import EbaySource, parse_browse_item
from sources.fb_marketplace import extract_item_id
from sources.gumtree import build_listing, parse_price

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_browse_item():
    payload = json.loads((FIXTURES / "ebay_browse.json").read_text())
    items = [parse_browse_item(i) for i in payload["itemSummaries"]]
    first = items[0]
    assert first.source == "ebay"
    assert first.price == 220.0
    assert first.shipping == 3.99
    assert first.condition == Condition.USED
    assert first.model_number == "A2403"
    assert first.brand == "Apple"
    assert "London" in first.location

    second = items[1]
    assert second.condition == Condition.NEW  # conditionId 1000
    assert second.model_number == "WH-1000XM4"
    assert second.brand == "Sony"


def test_parse_browse_item_missing_fields():
    assert parse_browse_item({"title": "no id or price"}) is None


def test_gumtree_parse_price():
    assert parse_price("£120.00") == 120.0
    assert parse_price("£1,250") == 1250.0
    assert parse_price("Free") is None
    assert parse_price(None) is None


def test_gumtree_build_listing():
    s = Settings(_env_file=None, scrape_default_shipping=0.0)
    listing = build_listing(
        s,
        listing_id="abc123",
        title="Samsung Galaxy SM-G991B 128GB",
        price=180.0,
        url="/p/phones/samsung/abc123",
        image_url=None,
        location="Leeds",
    )
    assert listing.source == "gumtree"
    assert listing.url.startswith("https://www.gumtree.com")
    assert listing.model_number == "SM-G991B"
    assert listing.brand == "Samsung"


def test_fb_extract_item_id():
    assert extract_item_id("/marketplace/item/1234567890/?ref=x") == "1234567890"
    assert extract_item_id("/marketplace/category/electronics") is None


# ------------------------------------------------------------------ eBay source
@pytest.mark.asyncio
@respx.mock
async def test_ebay_source_fetches_each_watched_query():
    """Searches come from the watch list, not from a hard-coded env var."""
    respx.post("https://api.ebay.com/identity/v1/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
    )
    route = respx.get("https://api.ebay.com/buy/browse/v1/item_summary/search").mock(
        return_value=httpx.Response(
            200, json=json.loads((FIXTURES / "ebay_browse.json").read_text())
        )
    )

    settings = Settings(_env_file=None)
    client = EbayClient("id", "secret")
    source = EbaySource(
        settings,
        queries=[WatchQuery(query="iphone 12"), WatchQuery(query="ps5")],
        client=client,
    )
    try:
        listings = [listing async for listing in source.fetch()]
    finally:
        await source.aclose()

    assert route.call_count == 2       # one search per watched query
    assert len(listings) == 4          # two items from each
    assert {listing.source for listing in listings} == {"ebay"}


@pytest.mark.asyncio
@respx.mock
async def test_ebay_source_skips_disabled_queries():
    respx.post("https://api.ebay.com/identity/v1/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
    )
    route = respx.get("https://api.ebay.com/buy/browse/v1/item_summary/search").mock(
        return_value=httpx.Response(200, json={"itemSummaries": []})
    )

    source = EbaySource(
        Settings(_env_file=None),
        queries=[WatchQuery(query="on"), WatchQuery(query="off", enabled=False)],
        client=EbayClient("id", "secret"),
    )
    try:
        [listing async for listing in source.fetch()]
    finally:
        await source.aclose()

    assert route.call_count == 1
