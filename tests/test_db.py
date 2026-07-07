from __future__ import annotations

from arb.models import Deal, SellChannel
from tests.conftest import make_listing


def test_mark_seen_dedup(db):
    listing = make_listing()
    assert db.mark_seen(listing.id) is True   # first sighting
    assert db.mark_seen(listing.id) is False  # already seen
    assert db.is_seen(listing.id) is True


def test_upsert_listing_and_deal(db):
    listing = make_listing()
    db.upsert_listing(listing)
    deal = Deal(
        listing_id=listing.id,
        buy_cost=200.0,
        est_resale=350.0,
        est_fees=45.0,
        est_profit=100.0,
        margin_pct=28.5,
        roi_pct=50.0,
        sell_channel=SellChannel.EBAY,
    )
    db.upsert_deal(deal)
    assert db.deal_exists(listing.id) is True


def test_alerted_tracking(db):
    listing = make_listing()
    db.mark_seen(listing.id)
    assert db.was_alerted(listing.id) is False
    db.mark_alerted(listing.id)
    assert db.was_alerted(listing.id) is True
