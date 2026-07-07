from __future__ import annotations

import pytest

from arb.config import Settings
from arb.db import Database
from arb.models import Condition, Listing, Valuation


@pytest.fixture
def settings() -> Settings:
    # Explicit test settings — never read the developer's real .env.
    return Settings(
        _env_file=None,
        min_profit=25.0,
        min_roi=30.0,
        min_sold_count=3,
        max_amazon_rank=50_000,
        tgtbt_ratio=0.20,
        ebay_fvf_pct=12.8,
        ebay_fixed_fee=0.30,
        amazon_referral_pct=8.0,
        amazon_fba_fee=3.0,
        packaging_cost=2.50,
        valuation_ttl_hours=24,
    )


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    yield database
    database.close()


def make_listing(**kwargs) -> Listing:
    base = dict(
        source="ebay",
        source_listing_id="123",
        title="Apple iPhone 12 A2403 128GB",
        model_number="A2403",
        brand="Apple",
        price=200.0,
        shipping=0.0,
        condition=Condition.USED,
        url="https://example.com/item/123",
    )
    base.update(kwargs)
    return Listing(**base)


def make_valuation(**kwargs) -> Valuation:
    base = dict(
        model_number="a2403",
        ebay_sold_median=350.0,
        ebay_sold_count=10,
        amazon_price=None,
        amazon_rank=None,
    )
    base.update(kwargs)
    return Valuation(**base)
