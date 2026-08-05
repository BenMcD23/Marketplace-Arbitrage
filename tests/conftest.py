from __future__ import annotations

import pytest

from arb.config import Settings
from arb.db import Database
from arb.models import Condition, Listing, PriceBasis, Valuation
from oracle.comps import Comp


@pytest.fixture
def settings() -> Settings:
    # Explicit test settings — never read the developer's real .env.
    return Settings(
        _env_file=None,
        min_profit=25.0,
        min_roi=30.0,
        min_expected_profit=15.0,
        min_confidence=0.35,
        min_score=0.0,
        min_comps=4,
        min_sold_comps=5,
        min_condition_comps=3,
        min_comp_relevance=0.6,
        max_amazon_rank=50_000,
        tgtbt_ratio=0.20,
        active_to_sold_ratio=0.88,
        used_to_new_ratio=0.75,
        base_sell_probability=0.75,
        default_days_to_sell=21,
        capital_annual_cost_pct=12.0,
        ebay_fvf_pct=12.8,
        ebay_fixed_fee=0.30,
        ebay_ad_rate_pct=0.0,
        postage_cost=3.50,
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
    """A confident, well-evidenced valuation. Override to weaken it."""
    base = dict(
        product_key="apple|128gb|12|a2403",
        resale_price=350.0,
        basis=PriceBasis.SOLD,
        comp_count=12,
        comps_rejected=6,
        dispersion_cv=0.10,
        price_p10=320.0,
        price_p90=380.0,
        confidence=0.8,
        sell_through_pct=None,
        est_days_to_sell=None,
    )
    base.update(kwargs)
    return Valuation(**base)


def make_comp(title: str, price: float, condition: Condition = Condition.USED, **kwargs) -> Comp:
    return Comp(title=title, price=price, condition=condition, **kwargs)
