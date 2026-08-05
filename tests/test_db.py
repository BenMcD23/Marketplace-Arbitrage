from __future__ import annotations

from datetime import UTC, datetime, timedelta

from arb.models import (
    CompRef,
    Condition,
    Deal,
    PriceBasis,
    Run,
    RunStatus,
    SellChannel,
    SoldObservation,
    Valuation,
    WatchQuery,
)
from tests.conftest import make_listing, make_valuation


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
        expected_profit=88.0,
        confidence=0.7,
        score=64.0,
        reasons=["because"],
    )
    db.upsert_deal(deal)
    assert db.deal_exists(listing.id) is True

    stored = db.get_deal(listing.id)
    assert stored.expected_profit == 88.0
    assert stored.score == 64.0
    assert stored.reasons == ["because"]


def test_listing_round_trips(db):
    listing = make_listing()
    db.upsert_listing(listing)
    stored = db.get_listing(listing.id)
    assert stored.title == listing.title
    assert stored.condition == listing.condition
    assert stored.buy_cost == listing.buy_cost


def test_alerted_tracking(db):
    listing = make_listing()
    db.mark_seen(listing.id)
    assert db.was_alerted(listing.id) is False
    db.mark_alerted(listing.id)
    assert db.was_alerted(listing.id) is True


# ------------------------------------------------------------------ valuations
def test_valuation_round_trips_with_its_audit_trail(db):
    valuation = make_valuation(
        sample=[CompRef(title="a comp", price=300.0, sold=True, relevance=0.9)],
        reject_reasons={"accessory_or_lot": 4},
    )
    db.upsert_valuation(valuation)

    stored = db.get_valuation_any_age(valuation.product_key)
    assert stored.basis == PriceBasis.SOLD
    assert stored.confidence == valuation.confidence
    assert stored.sample[0].title == "a comp"
    assert stored.reject_reasons == {"accessory_or_lot": 4}


def test_valuation_cache_respects_ttl(db):
    valuation = make_valuation(
        updated_at=datetime.now(UTC) - timedelta(hours=48)
    )
    db.upsert_valuation(valuation)

    assert db.get_valuation(valuation.product_key, ttl_hours=24) is None
    assert db.get_valuation(valuation.product_key, ttl_hours=72) is not None


# ------------------------------------------------------------------ comp watch
def test_comp_sighting_is_idempotent(db):
    for _ in range(3):
        db.record_comp_sighting("i1", "k", "thing", 100.0, Condition.USED, None)
    assert db.count_live_comps("k") == 1


def test_stale_comps_only_returns_ones_that_dropped_out(db):
    db.record_comp_sighting("fresh", "k", "thing", 100.0, Condition.USED, None)
    db.record_comp_sighting(
        "old", "k", "thing", 100.0, Condition.USED, None,
        now=datetime.now(UTC) - timedelta(hours=72),
    )

    stale = db.stale_comps(not_seen_for_hours=36, limit=10)
    assert [row["item_id"] for row in stale] == ["old"]


def test_ended_comp_leaves_the_live_pool(db):
    db.record_comp_sighting("i1", "k", "thing", 100.0, Condition.USED, None)
    db.resolve_comp_ended("i1")
    assert db.count_live_comps("k") == 0


def test_confirmed_live_comp_stays_in_the_pool(db):
    db.record_comp_sighting(
        "i1", "k", "thing", 100.0, Condition.USED, None,
        now=datetime.now(UTC) - timedelta(hours=72),
    )
    db.mark_comp_still_live("i1")

    assert db.count_live_comps("k") == 1
    # Its clock restarted, so it is no longer considered stale.
    assert db.stale_comps(not_seen_for_hours=36, limit=10) == []


# ------------------------------------------------------------------ sold history
def test_sold_observations_are_deduped_and_windowed(db):
    now = datetime.now(UTC)
    obs = SoldObservation(
        item_id="i1", product_key="k", title="thing", price=100.0,
        first_seen_at=now, sold_at=now,
    )
    db.record_sold(obs)
    db.record_sold(obs)  # same item twice must not double-count
    assert len(db.sold_observations("k")) == 1
    assert db.sold_count() == 1

    old = SoldObservation(
        item_id="i2", product_key="k", title="thing", price=100.0,
        first_seen_at=now - timedelta(days=200), sold_at=now - timedelta(days=200),
    )
    db.record_sold(old)
    assert len(db.sold_observations("k", days=90)) == 1
    assert len(db.sold_observations("k", days=365)) == 2


# ------------------------------------------------------------------ watch queries
def test_watch_query_crud(db):
    created = db.add_query(WatchQuery(query="iphone 12", max_price=250.0))
    assert created.id is not None
    assert created.enabled is True

    updated = db.update_query(created.id, enabled=False, max_price=300.0)
    assert updated.enabled is False
    assert updated.max_price == 300.0

    assert len(db.list_queries(enabled_only=True)) == 0
    assert len(db.list_queries()) == 1

    assert db.delete_query(created.id) is True
    assert db.delete_query(created.id) is False


def test_adding_the_same_query_twice_updates_rather_than_duplicates(db):
    db.add_query(WatchQuery(query="ps5", max_price=200.0))
    db.add_query(WatchQuery(query="ps5", max_price=350.0))

    queries = db.list_queries()
    assert len(queries) == 1
    assert queries[0].max_price == 350.0


# ------------------------------------------------------------------ runs
def test_run_lifecycle(db):
    run_id = db.start_run()
    assert db.get_run(run_id).status == RunStatus.RUNNING

    db.finish_run(
        Run(
            id=run_id,
            status=RunStatus.COMPLETE,
            finished_at=datetime.now(UTC),
            deals_found=3,
            sold_observed=7,
            api_calls=412,
            by_source={"ebay": 120},
        )
    )

    run = db.get_run(run_id)
    assert run.status == RunStatus.COMPLETE
    assert run.deals_found == 3
    assert run.sold_observed == 7
    assert run.by_source == {"ebay": 120}
    assert db.list_runs()[0].id == run_id


def test_failed_run_keeps_its_error(db):
    run_id = db.start_run()
    db.finish_run(Run(id=run_id, status=RunStatus.FAILED, error="boom"))
    assert db.get_run(run_id).error == "boom"


# ------------------------------------------------------------------ migration
def test_incompatible_cache_tables_are_rebuilt(tmp_path):
    """An old database must not stop the app booting."""
    import sqlite3

    from arb.db import Database

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE valuations (model_number TEXT PRIMARY KEY, ebay_sold_median REAL);"
        "CREATE TABLE deals (listing_id TEXT PRIMARY KEY, est_profit REAL);"
    )
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        db.upsert_valuation(Valuation(product_key="k", resale_price=1.0))
        assert db.get_valuation_any_age("k").resale_price == 1.0
    finally:
        db.close()
