"""Client-level tests using respx so no live network calls are made."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from oracle.ebay_client import BudgetExhausted, CallBudget, EbayClient
from oracle.keepa_client import KeepaClient

FIXTURES = Path(__file__).parent / "fixtures"

OAUTH = "https://api.ebay.com/identity/v1/oauth2/token"
BROWSE = "https://api.ebay.com/buy/browse/v1/item_summary/search"
INSIGHTS = "https://api.ebay.com/buy/marketplace_insights/v1_beta/item_sales/search"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _mock_oauth() -> None:
    respx.post(OAUTH).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
    )


# ------------------------------------------------------------------ budget
def test_budget_counts_and_refuses_when_spent():
    budget = CallBudget(daily_limit=3)
    for _ in range(3):
        budget.spend()
    assert budget.used == 3
    assert budget.remaining == 0
    with pytest.raises(BudgetExhausted):
        budget.spend()


def test_budget_reports_remaining():
    budget = CallBudget(daily_limit=5000)
    budget.spend(10)
    assert budget.remaining == 4990


# ------------------------------------------------------------------ eBay
@pytest.mark.asyncio
@respx.mock
async def test_browse_search_returns_comps():
    _mock_oauth()
    respx.get(BROWSE).mock(return_value=httpx.Response(200, json=load("ebay_browse.json")))

    client = EbayClient("id", "secret")
    try:
        comps = await client.search_comps("iphone 12")
        assert len(comps) == 2
        assert comps[0].item_id
        assert comps[0].price > 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_search_spends_from_the_budget():
    _mock_oauth()
    respx.get(BROWSE).mock(return_value=httpx.Response(200, json=load("ebay_browse.json")))

    client = EbayClient("id", "secret", budget=CallBudget(daily_limit=10))
    try:
        await client.search_comps("iphone 12")
        assert client.budget.used == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_search_raises_once_the_budget_is_gone():
    _mock_oauth()
    respx.get(BROWSE).mock(return_value=httpx.Response(200, json=load("ebay_browse.json")))

    client = EbayClient("id", "secret", budget=CallBudget(daily_limit=1))
    try:
        await client.search_comps("first")
        with pytest.raises(BudgetExhausted):
            await client.search_comps("second")
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_search_errors_yield_no_comps_rather_than_raising():
    _mock_oauth()
    respx.get(BROWSE).mock(return_value=httpx.Response(500, json={}))

    client = EbayClient("id", "secret")
    try:
        assert await client.search_comps("iphone 12") == []
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_item_is_live_reads_a_404_as_ended():
    _mock_oauth()
    respx.get(url__startswith="https://api.ebay.com/buy/browse/v1/item/").mock(
        return_value=httpx.Response(404, json={})
    )

    client = EbayClient("id", "secret")
    try:
        assert await client.item_is_live("v1|123|0") is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_item_is_live_reads_out_of_stock_as_ended():
    _mock_oauth()
    respx.get(url__startswith="https://api.ebay.com/buy/browse/v1/item/").mock(
        return_value=httpx.Response(
            200,
            json={
                "estimatedAvailabilities": [
                    {"estimatedAvailabilityStatus": "OUT_OF_STOCK"}
                ]
            },
        )
    )

    client = EbayClient("id", "secret")
    try:
        assert await client.item_is_live("v1|123|0") is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_item_is_live_returns_none_when_it_cannot_tell():
    """A 500 must not be mistaken for a sale."""
    _mock_oauth()
    respx.get(url__startswith="https://api.ebay.com/buy/browse/v1/item/").mock(
        return_value=httpx.Response(503, json={})
    )

    client = EbayClient("id", "secret")
    try:
        assert await client.item_is_live("v1|123|0") is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_item_is_live_returns_true_for_an_available_item():
    _mock_oauth()
    respx.get(url__startswith="https://api.ebay.com/buy/browse/v1/item/").mock(
        return_value=httpx.Response(
            200,
            json={
                "estimatedAvailabilities": [
                    {"estimatedAvailabilityStatus": "IN_STOCK"}
                ]
            },
        )
    )

    client = EbayClient("id", "secret")
    try:
        assert await client.item_is_live("v1|123|0") is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_ebay_sold_stats_with_oauth():
    _mock_oauth()
    respx.get(INSIGHTS).mock(return_value=httpx.Response(200, json=load("ebay_sold.json")))

    client = EbayClient("id", "secret", has_insights=True)
    try:
        med, count = await client.sold_stats("iphone 12 A2403")
        assert med == 350.0
        assert count == 5
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_ebay_returns_none_without_insights():
    """Marketplace Insights is closed to new applicants — this is the norm."""
    client = EbayClient("id", "secret", has_insights=False)
    try:
        assert await client.sold_stats("anything") == (None, 0)
        assert await client.sold_comps("anything") == []
    finally:
        await client.aclose()


# ------------------------------------------------------------------ Keepa
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
