from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from arb.models import Condition, Listing, Valuation
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


class RecordingTextAlerter(RecordingAlerter):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    async def send_text(self, text, image_url=None):
        self.messages.append(text)
        return True


def _mini_settings():
    from arb.config import Settings

    return Settings(_env_file=None)


def _mini_listing(listing_id: str, title: str, price: float, shipping: float = 0.0) -> Listing:
    return Listing(
        source="ebay_mini_pc",
        source_listing_id=listing_id,
        title=title,
        price=price,
        shipping=shipping,
        condition=Condition.USED,
        url=f"https://example.com/mini/{listing_id}",
    )


@pytest.mark.asyncio
async def test_mini_pc_pipeline_ranks_before_alerting(db):
    from arb.pipeline import MiniPcPipeline

    settings = _mini_settings()
    listings = [
        _mini_listing("a", "EliteDesk 800 G4 i5-8500T 16GB RAM 256GB SSD", 110.0),
        _mini_listing("b", "ProDesk 400 G6 i7-10700T 16GB RAM 512GB SSD", 175.0),
        # Rejected: 65W desktop part, not a T-series chip.
        _mini_listing("c", "OptiPlex 3070 Micro i5-9500 16GB RAM 256GB SSD", 90.0),
    ]
    alerter = RecordingTextAlerter()
    pipeline = MiniPcPipeline(settings, db, alerter)

    stats, ranked = await pipeline.run([FakeSource(listings)])

    assert stats.listings_scanned == 3
    assert stats.candidates == 2
    assert stats.alerts_sent == 2
    assert stats.rejected["cpu_not_low_power"] == 1
    # Higher CPU tier is pushed first even though it costs more.
    assert [c.cpu.name for c in ranked] == ["i7-10700T", "i5-8500T"]
    assert "Total landed: £175.00" in alerter.messages[0]
    assert db.mini_pc_deal_exists(ranked[0].listing.id)


@pytest.mark.asyncio
async def test_mini_pc_pipeline_dedups_across_runs(db):
    from arb.pipeline import MiniPcPipeline

    settings = _mini_settings()
    listings = [_mini_listing("a", "ThinkCentre M70q i5-10400T 16GB RAM 256GB SSD", 120.0)]
    alerter = RecordingTextAlerter()
    pipeline = MiniPcPipeline(settings, db, alerter)

    await pipeline.run([FakeSource(listings)])
    second, _ = await pipeline.run([FakeSource(listings)])

    assert second.new_listings == 0
    assert second.alerts_sent == 0
    assert len(alerter.messages) == 1
