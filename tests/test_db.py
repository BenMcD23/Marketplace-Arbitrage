from __future__ import annotations

from arb.db import Database
from arb.models import (
    Condition,
    Deal,
    DeliveryKind,
    Listing,
    ListingType,
    SellChannel,
)
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


def test_migrate_adds_columns_to_a_pre_existing_database(tmp_path):
    """An `arb.db` from before the mini-PC profile must keep working."""
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE listings (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, source_listing_id TEXT NOT NULL,
            title TEXT NOT NULL, model_number TEXT, brand TEXT, price REAL NOT NULL,
            shipping REAL NOT NULL DEFAULT 0, condition TEXT NOT NULL, url TEXT NOT NULL,
            image_url TEXT, location TEXT, seen_at TEXT NOT NULL);
        INSERT INTO listings VALUES
            ('old1','ebay','9','Old listing',NULL,NULL,200,0,'used','http://x',NULL,NULL,
             '2026-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    database = Database(path)
    try:
        columns = {row["name"] for row in database.query("PRAGMA table_info(listings)")}
        assert {"description", "delivery_kind", "listing_type", "best_offer"} <= columns

        # The old row survives and new-shape writes succeed against the same file.
        database.upsert_listing(
            Listing(
                source="ebay_mini_pc",
                source_listing_id="1",
                title="OptiPlex 7060 Micro i5-8500T 16GB RAM 256GB SSD",
                price=134.0,
                shipping=8.99,
                delivery_kind=DeliveryKind.FREE,
                listing_type=ListingType.AUCTION,
                best_offer=True,
                bid_count=3,
                condition=Condition.USED,
                url="http://y",
            )
        )
        assert database.query("SELECT id FROM listings WHERE id='old1'")
        row = database.query("SELECT * FROM listings WHERE source='ebay_mini_pc'")[0]
        assert row["best_offer"] == 1
        assert row["listing_type"] == "auction"
        assert row["bid_count"] == 3
    finally:
        database.close()
