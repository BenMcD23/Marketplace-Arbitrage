from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from arb.models import Listing, Valuation
from arb.pipeline import Pipeline
from oracle.pricing import PricingOracle
from sources.base import Source


class FakeSource(Source):
    name = "fake"

    def __init__(self, listings: list[Listing]):
        self._listings = listings

    async def fetch(self) -> AsyncIterator[Listing]:
        for listing in self._listings:
            yield listing


class FakeOracle(PricingOracle):
    def __init__(self, settings, db, valuation: Valuation):
        super().__init__(settings, db)
        self._valuation = valuation

    async def get_valuation(self, model_number, title) -> Valuation:
        return self._valuation


class RecordingAlerter:
    def __init__(self):
        self.sent = []

    async def send_deal(self, deal, listing):
        self.sent.append((deal, listing))
        return True

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_pipeline_end_to_end_flags_and_alerts(settings, db):
    listing = Listing(
        source="ebay",
        source_listing_id="1",
        title="Apple iPhone 12 A2403 128GB",
        model_number="A2403",
        price=200.0,
        url="https://example.com/1",
    )
    valuation = Valuation(model_number="a2403", ebay_sold_median=350.0, ebay_sold_count=10)

    oracle = FakeOracle(settings, db, valuation)
    alerter = RecordingAlerter()
    pipeline = Pipeline(settings, db, oracle, alerter)

    stats = await pipeline.run([FakeSource([listing])])

    assert stats.new_listings == 1
    assert stats.deals_found == 1
    assert stats.alerts_sent == 1
    assert db.deal_exists(listing.id)
    assert db.was_alerted(listing.id)


@pytest.mark.asyncio
async def test_pipeline_dedup_across_runs(settings, db):
    listing = Listing(
        source="ebay",
        source_listing_id="1",
        title="Apple iPhone 12 A2403 128GB",
        model_number="A2403",
        price=200.0,
        url="https://example.com/1",
    )
    valuation = Valuation(model_number="a2403", ebay_sold_median=350.0, ebay_sold_count=10)
    oracle = FakeOracle(settings, db, valuation)
    alerter = RecordingAlerter()
    pipeline = Pipeline(settings, db, oracle, alerter)

    await pipeline.run([FakeSource([listing])])
    second = await pipeline.run([FakeSource([listing])])

    # Same listing on a second run must not alert again.
    assert second.new_listings == 0
    assert second.alerts_sent == 0
    assert len(alerter.sent) == 1
