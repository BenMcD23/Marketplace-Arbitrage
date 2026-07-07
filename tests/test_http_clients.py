"""Client-level tests using respx so no live network calls are made."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from oracle.ebay_client import EbayClient
from oracle.keepa_client import KeepaClient

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.asyncio
@respx.mock
async def test_ebay_sold_stats_with_oauth():
    respx.post("https://api.ebay.com/identity/v1/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
    )
    respx.get("https://api.ebay.com/buy/marketplace_insights/v1_beta/item_sales/search").mock(
        return_value=httpx.Response(200, json=load("ebay_sold.json"))
    )
    client = EbayClient("id", "secret", has_insights=True)
    try:
        med, count = await client.sold_stats("iphone 12 A2403")
        assert med == 350.0
        assert count == 5
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_ebay_returns_none_without_insights():
    client = EbayClient("id", "secret", has_insights=False)
    try:
        med, count = await client.sold_stats("anything")
        assert (med, count) == (None, 0)
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_keepa_price_and_rank_by_asin():
    respx.get("https://api.keepa.com/product").mock(
        return_value=httpx.Response(200, json=load("keepa_product.json"))
    )
    client = KeepaClient("key", domain=2)
    try:
        price, rank = await client.get_price_and_rank("B08L5TNJHG")  # looks like an ASIN
        assert price == 429.99
        assert rank == 1200
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_keepa_searches_when_not_asin():
    respx.get("https://api.keepa.com/search").mock(
        return_value=httpx.Response(200, json={"asinList": ["B08L5TNJHG"]})
    )
    respx.get("https://api.keepa.com/product").mock(
        return_value=httpx.Response(200, json=load("keepa_product.json"))
    )
    client = KeepaClient("key", domain=2)
    try:
        price, rank = await client.get_price_and_rank("iPhone 12 128GB")
        assert price == 429.99
        assert rank == 1200
    finally:
        await client.aclose()
