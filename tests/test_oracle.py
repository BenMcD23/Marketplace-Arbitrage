from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from arb.models import Valuation
from oracle.ebay_client import parse_sold_search
from oracle.keepa_client import parse_product
from oracle.pricing import PricingOracle, median

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ------------------------------------------------------------------ pure calc
def test_median_basic():
    assert median([300, 350, 400, 360, 340]) == 350.0
    assert median([]) is None
    assert median([None, -5, 100]) == 100.0


def test_parse_sold_search_median_and_count():
    med, count = parse_sold_search(load("ebay_sold.json"))
    assert med == 350.0
    assert count == 5


def test_parse_sold_search_empty():
    assert parse_sold_search({"itemSales": []}) == (None, 0)


def test_parse_keepa_product():
    price, rank = parse_product(load("keepa_product.json"))
    assert price == 429.99  # 42999 pence -> £429.99
    assert rank == 1200


def test_parse_keepa_no_data():
    assert parse_product({"products": []}) == (None, None)


# ------------------------------------------------------------------ cache behaviour
class FakeEbay:
    def __init__(self):
        self.calls = 0

    async def sold_stats(self, query, days=90, limit=100):
        self.calls += 1
        return 350.0, 7

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_oracle_caches_and_does_not_requery(settings, db):
    fake = FakeEbay()
    oracle = PricingOracle(settings, db, ebay=fake, keepa=None)

    v1 = await oracle.get_valuation("A2403", "Apple iPhone 12 A2403")
    assert v1.ebay_sold_median == 350.0
    assert fake.calls == 1

    # Second call within TTL must hit the cache, not the API.
    v2 = await oracle.get_valuation("A2403", "Apple iPhone 12 A2403")
    assert v2.ebay_sold_median == 350.0
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_oracle_requeries_after_ttl(settings, db):
    fake = FakeEbay()
    oracle = PricingOracle(settings, db, ebay=fake, keepa=None)

    await oracle.get_valuation("A2403", "Apple iPhone 12 A2403")
    assert fake.calls == 1

    # Age the cached record beyond the TTL.
    stale = Valuation(
        model_number="a2403",
        ebay_sold_median=350.0,
        ebay_sold_count=7,
        updated_at=datetime.now(UTC) - timedelta(hours=settings.valuation_ttl_hours + 1),
    )
    db.upsert_valuation(stale)

    await oracle.get_valuation("A2403", "Apple iPhone 12 A2403")
    assert fake.calls == 2
