from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from arb.models import Listing, Valuation
from arb.pipeline import Pipeline
from oracle.ebay_client import BudgetExhausted
from oracle.pricing import PricingOracle
from sources.base import Source
from tests.conftest import make_valuation


class FakeSource(Source):
    name = "fake"

    def __init__(self, listings: list[Listing]):
        self._listings = listings

    async def fetch(self) -> AsyncIterator[Listing]:
        for listing in self._listings:
            yield listing


class BudgetBlownSource(Source):
    name = "blown"

    async def fetch(self) -> AsyncIterator[Listing]:
        raise BudgetExhausted("daily limit reached")
        yield  # pragma: no cover


class ExplodingSource(Source):
    name = "broken"

    async def fetch(self) -> AsyncIterator[Listing]:
        raise RuntimeError("source is down")
        yield  # pragma: no cover


class FakeOracle(PricingOracle):
    def __init__(self, settings, db, valuation: Valuation):
        super().__init__(settings, db)
        self._valuation = valuation

    async def get_valuation(self, listing) -> Valuation:
        return self._valuation


class RecordingAlerter:
    def __init__(self):
        self.sent = []

    async def send_deal(self, deal, listing):
        self.sent.append((deal, listing))
        return True

    async def aclose(self):
        pass


def _listing() -> Listing:
    return Listing(
        source="ebay",
        source_listing_id="1",
        title="Apple iPhone 12 A2403 128GB",
        model_number="A2403",
        price=200.0,
        url="https://example.com/1",
    )


@pytest.mark.asyncio
async def test_pipeline_end_to_end_flags_and_alerts(settings, db):
    listing = _listing()
    pipeline = Pipeline(
        settings, db, FakeOracle(settings, db, make_valuation()), RecordingAlerter()
    )

    stats = await pipeline.run([FakeSource([listing])])

    assert stats.new_listings == 1
    assert stats.deals_found == 1
    assert stats.alerts_sent == 1
    assert db.deal_exists(listing.id)
    assert db.was_alerted(listing.id)


@pytest.mark.asyncio
async def test_pipeline_dedup_across_runs(settings, db):
    listing = _listing()
    alerter = RecordingAlerter()
    pipeline = Pipeline(settings, db, FakeOracle(settings, db, make_valuation()), alerter)

    await pipeline.run([FakeSource([listing])])
    second = await pipeline.run([FakeSource([listing])])

    assert second.new_listings == 0
    assert second.alerts_sent == 0
    assert len(alerter.sent) == 1


@pytest.mark.asyncio
async def test_pipeline_records_the_run(settings, db):
    run_id = db.start_run()
    pipeline = Pipeline(
        settings, db, FakeOracle(settings, db, make_valuation()), RecordingAlerter()
    )

    await pipeline.run([FakeSource([_listing()])], run_id=run_id)

    run = db.get_run(run_id)
    assert run.status.value == "complete"
    assert run.deals_found == 1
    assert run.finished_at is not None
    assert run.by_source == {"ebay": 1}


@pytest.mark.asyncio
async def test_a_broken_source_does_not_kill_the_run(settings, db):
    pipeline = Pipeline(
        settings, db, FakeOracle(settings, db, make_valuation()), RecordingAlerter()
    )

    stats = await pipeline.run([ExplodingSource(), FakeSource([_listing()])])

    # The healthy source still ran.
    assert stats.deals_found == 1


@pytest.mark.asyncio
async def test_exhausted_budget_stops_the_run_cleanly(settings, db):
    """Running out of free API calls is a stop condition, not a crash."""
    pipeline = Pipeline(
        settings, db, FakeOracle(settings, db, make_valuation()), RecordingAlerter()
    )

    stats = await pipeline.run([BudgetBlownSource(), FakeSource([_listing()])])

    assert stats.budget_exhausted is True
    # Nothing after the exhausted source is attempted.
    assert stats.listings_scanned == 0
