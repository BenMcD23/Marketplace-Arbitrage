"""Tests for the free sold-price history built from listing lifecycles."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arb.models import Condition
from oracle.comps import Comp
from oracle.ebay_client import CallBudget
from oracle.sold_tracker import SOLD_INFERENCE_HAIRCUT, SoldTracker


class FakeEbay:
    """Reports item liveness from a lookup table. None means 'could not tell'."""

    def __init__(self, liveness: dict[str, bool | None], daily_limit: int = 5000):
        self.liveness = liveness
        self.checks: list[str] = []
        self.budget = CallBudget(daily_limit=daily_limit)

    async def item_is_live(self, item_id: str):
        self.checks.append(item_id)
        return self.liveness.get(item_id)


def _comps(n: int = 3) -> list[Comp]:
    return [
        Comp(
            title=f"Apple iPhone 12 128GB #{i}",
            price=300.0 + i,
            condition=Condition.USED,
            item_id=f"item-{i}",
        )
        for i in range(n)
    ]


def _age_sightings(db, hours: int, listed_days: float = 0.0) -> None:
    """Backdate watched comps so the sweep considers them stale.

    `listed_days` pushes first-seen further back than last-seen, simulating a
    comp that was visible for a while before disappearing.
    """
    last_seen = datetime.now(UTC) - timedelta(hours=hours)
    first_seen = last_seen - timedelta(days=listed_days)
    db.query(
        "UPDATE comp_watch SET last_seen_at = ?, first_seen_at = ?",
        (last_seen.isoformat(), first_seen.isoformat()),
    )


def test_record_active_starts_watching_comps(settings, db):
    tracker = SoldTracker(settings, db)
    tracker.record_active("iphone|12|128gb", _comps())
    assert db.count_live_comps("iphone|12|128gb") == 3


def test_record_active_ignores_comps_without_an_id(settings, db):
    tracker = SoldTracker(settings, db)
    tracker.record_active("k", [Comp(title="no id", price=100.0)])
    assert db.count_live_comps("k") == 0


@pytest.mark.asyncio
async def test_sweep_records_ended_listings_as_sales(settings, db):
    ebay = FakeEbay({"item-0": False, "item-1": True, "item-2": False})
    tracker = SoldTracker(settings, db, ebay)
    tracker.record_active("k", _comps())
    _age_sightings(db, settings.comp_stale_hours + 1)

    recorded = await tracker.sweep()

    assert recorded == 2
    observations = db.sold_observations("k")
    assert {o.item_id for o in observations} == {"item-0", "item-2"}
    # The one still listed stays out of the sold history.
    assert all(o.item_id != "item-1" for o in observations)


@pytest.mark.asyncio
async def test_sweep_leaves_inconclusive_checks_watched(settings, db):
    """A network failure must not be recorded as a sale."""
    ebay = FakeEbay({"item-0": None})
    tracker = SoldTracker(settings, db, ebay)
    tracker.record_active("k", _comps(1))
    _age_sightings(db, settings.comp_stale_hours + 1)

    assert await tracker.sweep() == 0
    assert db.count_live_comps("k") == 1  # still being watched


@pytest.mark.asyncio
async def test_sweep_ignores_comps_that_are_still_in_search_results(settings, db):
    ebay = FakeEbay({"item-0": False})
    tracker = SoldTracker(settings, db, ebay)
    tracker.record_active("k", _comps(1))
    # Not backdated, so it is not stale yet.

    assert await tracker.sweep() == 0
    assert ebay.checks == []


@pytest.mark.asyncio
async def test_sweep_respects_its_call_budget(settings, db):
    ebay = FakeEbay({f"item-{i}": False for i in range(10)})
    tracker = SoldTracker(settings, db, ebay)
    tracker.record_active("k", _comps(10))
    _age_sightings(db, settings.comp_stale_hours + 1)

    await tracker.sweep(max_checks=3)
    assert len(ebay.checks) == 3


@pytest.mark.asyncio
async def test_sweep_is_a_no_op_without_a_client(settings, db):
    tracker = SoldTracker(settings, db, ebay=None)
    assert await tracker.sweep() == 0


def test_sold_comps_apply_the_inference_haircut(settings, db):
    """Ended is not the same as sold, so observed prices are discounted."""
    from arb.models import SoldObservation

    now = datetime.now(UTC)
    db.record_sold(
        SoldObservation(
            item_id="i1",
            product_key="k",
            title="thing",
            price=100.0,
            first_seen_at=now,
            sold_at=now,
        )
    )
    comps = SoldTracker(settings, db).sold_comps("k")
    assert comps[0].price == round(100.0 * SOLD_INFERENCE_HAIRCUT, 2)
    assert comps[0].sold is True


@pytest.mark.asyncio
async def test_liquidity_reports_sell_through_and_duration(settings, db):
    ebay = FakeEbay({"item-0": False, "item-1": False, "item-2": True})
    tracker = SoldTracker(settings, db, ebay)
    tracker.record_active("k", _comps(3))
    _age_sightings(db, settings.comp_stale_hours + 1, listed_days=4)

    await tracker.sweep()
    sell_through, days = tracker.liquidity("k")

    # Two of three ended, and the third is confirmed still live — so it stays
    # in the denominator rather than being retired.
    assert sell_through == pytest.approx(66.7, abs=0.1)
    assert days == 4


def test_liquidity_is_unknown_without_observations(settings, db):
    assert SoldTracker(settings, db).liquidity("nothing-here") == (None, None)


# ------------------------------------------------------------------ budget
def test_sweep_runs_hard_while_history_is_thin(settings, db):
    """Idle API calls are days added to the cold start, so bootstrap spends more."""
    tracker = SoldTracker(settings, db, FakeEbay({}))
    assert tracker.sweep_budget() == settings.sold_sweep_bootstrap_checks


def test_sweep_settles_once_there_is_enough_history(settings, db):
    from arb.models import SoldObservation

    settings.sold_bootstrap_threshold = 3
    now = datetime.now(UTC)
    for i in range(4):
        db.record_sold(
            SoldObservation(
                item_id=f"obs-{i}",
                product_key="k",
                title="thing",
                price=100.0,
                first_seen_at=now,
                sold_at=now,
            )
        )

    tracker = SoldTracker(settings, db, FakeEbay({}))
    assert tracker.sweep_budget() == settings.sold_sweep_max_checks


def test_sweep_never_exceeds_the_remaining_daily_allowance(settings, db):
    """The scan comes first; the sweep only gets what is actually left."""
    ebay = FakeEbay({}, daily_limit=40)
    ebay.budget.spend(35)
    tracker = SoldTracker(settings, db, ebay)
    assert tracker.sweep_budget() == 5


def test_sweep_budget_is_zero_when_the_allowance_is_gone(settings, db):
    ebay = FakeEbay({}, daily_limit=10)
    ebay.budget.spend(10)
    assert SoldTracker(settings, db, ebay).sweep_budget() == 0
