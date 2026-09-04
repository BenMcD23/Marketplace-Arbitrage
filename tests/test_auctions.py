"""Auction handling: parsing, the bid ceiling, and the sweep's dedup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from arb.models import PriceBasis
from engine.deals import evaluate, max_bid
from sources.ebay import parse_browse_item
from tests.conftest import make_listing, make_valuation


def test_auction_price_is_the_standing_bid_not_the_bin():
    listing = parse_browse_item(
        {
            "itemId": "v1|123|0",
            "title": "Apple iPhone 12 128GB",
            "buyingOptions": ["AUCTION"],
            "currentBidPrice": {"value": "85.00"},
            "price": {"value": "400.00"},
            "bidCount": 7,
            "itemEndDate": "2026-09-05T18:00:00.000Z",
            "itemWebUrl": "https://ebay.co.uk/itm/123",
        }
    )
    assert listing.is_auction
    assert listing.price == 85.0
    assert listing.bid_count == 7
    assert listing.end_time == datetime(2026, 9, 5, 18, 0, tzinfo=UTC)


def test_fixed_price_listings_are_not_auctions():
    listing = parse_browse_item(
        {
            "itemId": "v1|9|0",
            "title": "Apple iPhone 12 128GB",
            "buyingOptions": ["FIXED_PRICE"],
            "price": {"value": "220.00"},
            "itemWebUrl": "https://ebay.co.uk/itm/9",
        }
    )
    assert not listing.is_auction
    assert listing.price == 220.0
    assert listing.end_time is None


def test_max_bid_is_the_last_bid_that_still_qualifies(settings):
    valuation = make_valuation(resale_price=300.0, basis=PriceBasis.SOLD, confidence=0.8)
    listing = make_listing(price=50.0, shipping=5.0, is_auction=True,
                           end_time=datetime.now(UTC) + timedelta(hours=6))

    cap = max_bid(listing, valuation, settings)
    assert cap > 0

    # Bidding the cap still clears every gate; a penny more does not.
    assert evaluate(listing.model_copy(update={"price": cap}), valuation, settings) is not None
    assert evaluate(listing.model_copy(update={"price": cap + 1.0}), valuation, settings) is None


def test_max_bid_is_zero_when_the_valuation_is_untrustworthy(settings):
    valuation = make_valuation(resale_price=300.0, basis=PriceBasis.ACTIVE, confidence=0.1)
    listing = make_listing(price=50.0, is_auction=True)
    assert max_bid(listing, valuation, settings) == 0.0


def test_due_auctions_only_returns_unalerted_ones_inside_the_window(db):
    now = datetime.now(UTC)
    soon = make_listing(source_listing_id="soon", is_auction=True,
                        end_time=now + timedelta(hours=6))
    later = make_listing(source_listing_id="later", is_auction=True,
                         end_time=now + timedelta(hours=40))
    bin_item = make_listing(source_listing_id="bin", is_auction=False)
    for listing in (soon, later, bin_item):
        db.upsert_listing(listing)
        db.mark_seen(listing.id)

    assert [x.id for x in db.due_auctions(12, 50)] == [soon.id]

    db.mark_alerted(soon.id)
    assert db.due_auctions(12, 50) == []


class _StubEbay:
    """Just enough eBay for the sweep: one item, re-read at its live price."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def get_item(self, item_id):
        self.calls += 1
        return self.payload


async def test_sweep_alerts_once_with_a_bid_cap(settings, db):
    from arb.pipeline import Pipeline, RunStats
    from tests.test_pipeline import FakeOracle, RecordingAlerter

    ends = datetime.now(UTC) + timedelta(hours=6)
    payload = {
        "itemId": "v1|55|0",
        "title": "Apple iPhone 12 A2403 128GB",
        "buyingOptions": ["AUCTION"],
        "currentBidPrice": {"value": "60.00"},
        "bidCount": 3,
        "itemEndDate": ends.isoformat().replace("+00:00", "Z"),
        "itemWebUrl": "https://ebay.co.uk/itm/55",
        "conditionId": "3000",
    }
    listing = parse_browse_item(payload)

    oracle = FakeOracle(settings, db, make_valuation(resale_price=300.0, confidence=0.8))
    oracle.ebay = _StubEbay(payload)
    alerter = RecordingAlerter()
    pipeline = Pipeline(settings, db, oracle, alerter)

    # Scanning defers the auction rather than pricing it at first sight.
    await pipeline._process_listing(listing, RunStats())
    assert alerter.sent == []

    stats = RunStats()
    await pipeline._sweep_auctions(stats)
    assert len(alerter.sent) == 1
    _deal, alerted, cap = alerter.sent[0]
    assert alerted.price == 60.0
    assert cap > 60.0

    # A second sweep must not alert again.
    await pipeline._sweep_auctions(stats)
    assert len(alerter.sent) == 1
