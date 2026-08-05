from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from arb.db import Database
from arb.models import Condition, PriceBasis, SoldObservation, Valuation
from oracle.comps import Comp
from oracle.ebay_client import parse_comps, parse_sold_search
from oracle.keepa_client import parse_product
from oracle.pricing import (
    PricingOracle,
    calibrate_active_ratio,
    calibrate_condition_ratio,
    condition_adjusted_estimate,
    confidence_score,
)
from oracle.robust import robust_price
from sources.normalise import product_key
from tests.conftest import make_comp, make_listing

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ------------------------------------------------------------------ parsing
def test_parse_sold_search_median_and_count():
    med, count = parse_sold_search(load("ebay_sold.json"))
    assert med == 350.0
    assert count == 5


def test_parse_sold_search_empty():
    assert parse_sold_search({"itemSales": []}) == (None, 0)


def test_parse_comps_prices_are_delivered():
    """A buyer compares the delivered price, so comps must include postage."""
    comps = parse_comps(load("ebay_browse.json"))
    assert comps
    assert comps[0].price == 223.99  # 220.00 item + 3.99 postage
    assert comps[0].condition == Condition.USED


def test_parse_keepa_product():
    price, rank = parse_product(load("keepa_product.json"))
    assert price == 429.99  # 42999 pence -> £429.99
    assert rank == 1200


def test_parse_keepa_no_data():
    assert parse_product({"products": []}) == (None, None)


# ------------------------------------------------------------------ confidence
def _relevant_comps() -> list[Comp]:
    comp = make_comp("x", 300)
    comp.relevance = 0.9
    return [comp]


def test_confidence_rises_with_sample_size(settings):
    small = robust_price([300, 305, 310, 302])
    large = robust_price([300, 305, 310, 302] * 5)
    comps = _relevant_comps()
    assert confidence_score(large, PriceBasis.SOLD, comps, settings) > confidence_score(
        small, PriceBasis.SOLD, comps, settings
    )


def test_confidence_falls_as_comps_disagree(settings):
    tight = robust_price([300, 302, 298, 301, 299, 300])
    loose = robust_price([180, 300, 420, 240, 380, 300])
    comps = _relevant_comps()
    assert confidence_score(tight, PriceBasis.SOLD, comps, settings) > confidence_score(
        loose, PriceBasis.SOLD, comps, settings
    )


def test_sold_basis_is_trusted_more_than_asking_prices(settings):
    est = robust_price([300, 305, 310, 302, 308])
    comps = _relevant_comps()
    assert confidence_score(est, PriceBasis.SOLD, comps, settings) > confidence_score(
        est, PriceBasis.ACTIVE, comps, settings
    )


def test_no_basis_means_no_confidence(settings):
    est = robust_price([300, 305, 310, 302])
    assert confidence_score(est, PriceBasis.NONE, [], settings) == 0.0


# ------------------------------------------------------------------ condition
def test_prices_off_same_condition_comps_when_available(settings):
    comps = [
        make_comp("iPhone 12", 400, condition=Condition.NEW),
        make_comp("iPhone 12", 410, condition=Condition.NEW),
        make_comp("iPhone 12", 405, condition=Condition.NEW),
        make_comp("iPhone 12", 300, condition=Condition.USED),
        make_comp("iPhone 12", 305, condition=Condition.USED),
        make_comp("iPhone 12", 295, condition=Condition.USED),
    ]
    est, priced_from, note = condition_adjusted_estimate(comps, Condition.USED, settings)
    assert note == "same_condition_comps"
    assert est.value == 300.0
    assert all(c.condition == Condition.USED for c in priced_from)


def test_converts_from_the_other_condition_when_comps_are_one_sided(settings):
    """A used handset must not be valued off sealed-in-box comps."""
    comps = [make_comp("iPhone 12", 400, condition=Condition.NEW) for _ in range(4)]
    est, _, note = condition_adjusted_estimate(comps, Condition.USED, settings)
    assert note == "condition_ratio_default"
    assert est.value == round(400.0 * settings.used_to_new_ratio, 2)


def test_converts_using_a_calibrated_ratio_when_one_is_supplied(settings):
    comps = [make_comp("iPhone 12", 400, condition=Condition.NEW) for _ in range(4)]
    est, _, note = condition_adjusted_estimate(
        comps, Condition.USED, settings, condition_ratio=0.7
    )
    assert note == "condition_ratio_calibrated"
    assert est.value == 280.0


def test_condition_ratio_calibration_learns_from_observed_sales(settings, db):
    """Used selling at 280 against new at 400 should learn 0.7, not assume 0.75."""
    settings.calibration_min_keys = 3
    now = datetime.now(UTC)
    for i in range(4):
        key = f"product-{i}"
        for suffix, price, cond in (
            ("new", 400.0, Condition.NEW),
            ("used", 280.0, Condition.USED),
        ):
            db.record_sold(
                SoldObservation(
                    item_id=f"{key}-{suffix}",
                    product_key=key,
                    title="thing",
                    price=price,
                    condition=cond,
                    first_seen_at=now,
                    sold_at=now,
                )
            )

    assert calibrate_condition_ratio(db, settings) == 0.7


def test_condition_ratio_falls_back_without_enough_paired_keys(settings, db):
    assert calibrate_condition_ratio(db, settings) == settings.used_to_new_ratio


def test_unknown_condition_uses_everything(settings):
    comps = [make_comp("iPhone 12", 300, condition=Condition.UNKNOWN) for _ in range(5)]
    _, _, note = condition_adjusted_estimate(comps, Condition.UNKNOWN, settings)
    assert note == "mixed_condition_comps"


# ------------------------------------------------------------------ calibration
def test_calibration_falls_back_to_the_default_without_data(settings, db):
    assert calibrate_active_ratio(db, settings) == settings.active_to_sold_ratio


def test_calibration_learns_from_paired_observations(settings, db):
    """Sold at 90 against 100 asking, across enough keys, should learn 0.9."""
    settings.calibration_min_keys = 3
    now = datetime.now(UTC)
    for i in range(5):
        key = f"product-{i}"
        db.record_sold(
            SoldObservation(
                item_id=f"sold-{i}",
                product_key=key,
                title="thing",
                price=90.0,
                first_seen_at=now,
                sold_at=now,
            )
        )
        db.record_comp_sighting(f"live-{i}", key, "thing", 100.0, Condition.USED, None)

    assert calibrate_active_ratio(db, settings) == 0.9


def test_calibration_ignores_implausible_ratios(settings, db):
    """A 10x ratio is a product-key collision, not a market signal."""
    settings.calibration_min_keys = 2
    now = datetime.now(UTC)
    for i in range(3):
        key = f"product-{i}"
        db.record_sold(
            SoldObservation(
                item_id=f"sold-{i}",
                product_key=key,
                title="thing",
                price=1000.0,
                first_seen_at=now,
                sold_at=now,
            )
        )
        db.record_comp_sighting(f"live-{i}", key, "thing", 100.0, Condition.USED, None)

    assert calibrate_active_ratio(db, settings) == settings.active_to_sold_ratio


# ------------------------------------------------------------------ oracle flow
class FakeEbay:
    """Stands in for the Browse API. Counts calls so caching can be asserted."""

    has_insights = False

    def __init__(self, comps: list[Comp] | None = None):
        self.calls = 0
        self._comps = comps or []

    async def search_comps(self, query, limit=100, category_id=None):
        self.calls += 1
        return [
            Comp(
                title=c.title,
                price=c.price,
                condition=c.condition,
                item_id=c.item_id,
                url=c.url,
                sold=c.sold,
            )
            for c in self._comps
        ]

    async def sold_comps(self, query, days=90, limit=100):
        return []

    async def aclose(self):
        pass


def _active_comps(n: int = 8) -> list[Comp]:
    return [
        Comp(
            title="Apple iPhone 12 A2403 128GB Blue",
            price=400.0 + i,
            condition=Condition.USED,
            item_id=f"item-{i}",
        )
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_oracle_values_from_active_comps_and_discounts_them(settings, db):
    fake = FakeEbay(_active_comps())
    oracle = PricingOracle(settings, db, ebay=fake, keepa=None)

    valuation = await oracle.get_valuation(make_listing())

    assert valuation.basis == PriceBasis.ACTIVE
    # Asking prices are discounted towards a realistic sale price.
    assert valuation.resale_price < 403.5
    assert valuation.comp_count >= settings.min_comps
    assert valuation.confidence > 0


@pytest.mark.asyncio
async def test_oracle_caches_and_does_not_requery(settings, db):
    fake = FakeEbay(_active_comps())
    oracle = PricingOracle(settings, db, ebay=fake, keepa=None)

    await oracle.get_valuation(make_listing())
    assert fake.calls == 1

    await oracle.get_valuation(make_listing())
    assert fake.calls == 1  # served from cache


@pytest.mark.asyncio
async def test_oracle_requeries_after_ttl(settings, db):
    fake = FakeEbay(_active_comps())
    oracle = PricingOracle(settings, db, ebay=fake, keepa=None)

    listing = make_listing()
    first = await oracle.get_valuation(listing)
    assert fake.calls == 1

    aged = first.model_dump()
    aged["updated_at"] = datetime.now(UTC) - timedelta(
        hours=settings.valuation_ttl_hours + 1
    )
    db.upsert_valuation(Valuation(**aged))

    await oracle.get_valuation(listing)
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_oracle_prefers_observed_sales_over_asking_prices(settings, db):
    """Once enough sales are observed, they replace asking prices as the basis."""
    fake = FakeEbay(_active_comps())
    oracle = PricingOracle(settings, db, ebay=fake, keepa=None)
    listing = make_listing()
    key = product_key(listing.title, listing.brand, listing.model_number)

    now = datetime.now(UTC)
    for i in range(6):
        db.record_sold(
            SoldObservation(
                item_id=f"sold-{i}",
                product_key=key,
                title="Apple iPhone 12 A2403 128GB Blue",
                price=330.0,
                condition=Condition.USED,
                first_seen_at=now - timedelta(days=5),
                sold_at=now,
                days_listed=5.0,
            )
        )

    valuation = await oracle.get_valuation(listing)
    assert valuation.basis == PriceBasis.SOLD
    # The sold basis is used as-is, not discounted like asking prices are.
    assert 315 <= valuation.resale_price <= 330


@pytest.mark.asyncio
async def test_oracle_returns_empty_valuation_without_enough_comps(settings, db):
    fake = FakeEbay(_active_comps(n=2))  # below min_comps
    oracle = PricingOracle(settings, db, ebay=fake, keepa=None)

    valuation = await oracle.get_valuation(make_listing())
    assert valuation.basis == PriceBasis.NONE
    assert valuation.resale_price is None
    assert valuation.confidence == 0.0


@pytest.mark.asyncio
async def test_oracle_records_rejected_comps_for_auditing(settings, db):
    comps = _active_comps(6) + [
        Comp(title="iPhone 12 Case Silicone", price=5.0, item_id="junk-1"),
        Comp(title="Screen Protector iPhone 12", price=3.0, item_id="junk-2"),
    ]
    oracle = PricingOracle(settings, db, ebay=FakeEbay(comps), keepa=None)

    valuation = await oracle.get_valuation(make_listing())
    assert valuation.reject_reasons.get("accessory_or_lot") == 2
    assert valuation.sample  # the comps actually used are retained for the UI


@pytest.mark.asyncio
async def test_oracle_watches_comps_so_their_endings_become_sold_data(settings, db):
    oracle = PricingOracle(settings, db, ebay=FakeEbay(_active_comps()), keepa=None)
    listing = make_listing()

    await oracle.get_valuation(listing)

    key = product_key(listing.title, listing.brand, listing.model_number)
    assert db.count_live_comps(key) == 8


# ------------------------------------------------------------------ CeX
class FakeCex:
    """Stands in for the CeX API."""

    def __init__(self, quote):
        self._quote = quote
        self.calls = 0

    async def quote(self, title, condition=Condition.UNKNOWN, query=None):
        self.calls += 1
        return self._quote

    async def aclose(self):
        pass


def _cex_quote(sell: float, cash: float, name: str = "Apple iPhone 12 128GB"):
    from oracle.cex_client import CexQuote

    return CexQuote(
        sell_price=sell,
        cash_price=cash,
        exchange_price=cash * 1.2,
        matched_name=name,
        box_id="X1",
        grade="B",
        n_matched=2,
    )


@pytest.mark.asyncio
async def test_cex_prices_a_listing_ebay_could_not(settings, db):
    """The cold-start case: too few eBay comps, but CeX knows the product."""
    fake = FakeEbay(_active_comps(n=2))  # below min_comps
    oracle = PricingOracle(
        settings, db, ebay=fake, keepa=None, cex=FakeCex(_cex_quote(400.0, 240.0))
    )

    valuation = await oracle.get_valuation(make_listing())

    assert valuation.basis == PriceBasis.CEX
    assert valuation.resale_price == round(400.0 * settings.cex_to_ebay_ratio, 2)
    assert valuation.cex_cash_price == 240.0
    assert valuation.confidence > 0


@pytest.mark.asyncio
async def test_cex_cash_price_is_attached_even_when_ebay_sets_the_price(settings, db):
    """The floor is useful regardless of which source won the basis."""
    oracle = PricingOracle(
        settings,
        db,
        ebay=FakeEbay(_active_comps()),
        keepa=None,
        cex=FakeCex(_cex_quote(460.0, 250.0)),
    )

    valuation = await oracle.get_valuation(make_listing())

    assert valuation.basis == PriceBasis.ACTIVE
    assert valuation.cex_cash_price == 250.0
    assert valuation.cex_match == "Apple iPhone 12 128GB"


@pytest.mark.asyncio
async def test_disagreement_with_cex_costs_confidence(settings, db):
    """Two independent sources disagreeing is exactly when to be less sure."""
    agreeing = PricingOracle(
        settings,
        db,
        ebay=FakeEbay(_active_comps()),
        keepa=None,
        cex=FakeCex(_cex_quote(410.0, 250.0)),
    )
    confident = await agreeing.get_valuation(make_listing())

    db2 = Database(":memory:")
    try:
        disagreeing = PricingOracle(
            settings,
            db2,
            ebay=FakeEbay(_active_comps()),
            keepa=None,
            cex=FakeCex(_cex_quote(900.0, 500.0)),
        )
        doubtful = await disagreeing.get_valuation(make_listing())
    finally:
        db2.close()

    assert doubtful.confidence < confident.confidence


@pytest.mark.asyncio
async def test_a_broken_cex_never_breaks_a_valuation(settings, db):
    class ExplodingCex:
        async def quote(self, *args, **kwargs):
            raise RuntimeError("cex is down")

        async def aclose(self):
            pass

    oracle = PricingOracle(
        settings, db, ebay=FakeEbay(_active_comps()), keepa=None, cex=ExplodingCex()
    )
    valuation = await oracle.get_valuation(make_listing())

    assert valuation.basis == PriceBasis.ACTIVE
    assert valuation.cex_cash_price is None


@pytest.mark.asyncio
async def test_cex_is_skipped_when_disabled(settings, db):
    settings.enable_cex = False
    fake_cex = FakeCex(_cex_quote(400.0, 240.0))
    oracle = PricingOracle(
        settings, db, ebay=FakeEbay(_active_comps()), keepa=None, cex=fake_cex
    )

    await oracle.get_valuation(make_listing())
    assert fake_cex.calls == 0
