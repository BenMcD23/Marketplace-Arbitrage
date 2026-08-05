"""API tests.

The app is built against a temporary database rather than the developer's real
one, and no test in here touches the network.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from arb.models import Condition, Listing, PriceBasis
from engine.deals import evaluate
from sources.normalise import product_key
from tests.conftest import make_valuation


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "arb.db"))
    monkeypatch.setenv("EBAY_QUERIES", "")

    from api import deps
    from arb.config import get_settings

    get_settings.cache_clear()
    deps.get_app_settings.cache_clear()
    deps.get_db.cache_clear()

    from api.main import app

    with TestClient(app) as test_client:
        yield test_client

    deps.get_db.cache_clear()
    deps.get_app_settings.cache_clear()
    get_settings.cache_clear()


@pytest.fixture
def seeded(client):
    """A database with one listing, its valuation, and the resulting deal."""
    from api.deps import get_app_settings, get_db

    db = get_db()
    listing = Listing(
        source="ebay",
        source_listing_id="1",
        title="Apple iPhone 12 A2403 128GB Blue",
        model_number="A2403",
        brand="Apple",
        price=200.0,
        condition=Condition.USED,
        url="https://www.ebay.co.uk/itm/1",
    )
    db.upsert_listing(listing)

    key = product_key(listing.title, listing.brand, listing.model_number)
    valuation = make_valuation(product_key=key)
    db.upsert_valuation(valuation)

    deal = evaluate(listing, valuation, get_app_settings())
    assert deal is not None
    db.upsert_deal(deal)
    return client, listing, deal


# ------------------------------------------------------------------ meta
def test_root_and_health(client):
    assert client.get("/").status_code == 200

    health = client.get("/api/health").json()
    assert health["status"] == "ok"
    assert health["scan_running"] is False
    assert health["daily_call_limit"] == 5000


# ------------------------------------------------------------------ deals
def test_list_deals_returns_the_listing_alongside_the_deal(seeded):
    client, listing, _ = seeded
    payload = client.get("/api/deals").json()

    assert payload["total"] == 1
    row = payload["items"][0]
    assert row["listing_id"] == listing.id
    assert row["listing"]["title"] == listing.title
    assert row["listing"]["buy_cost"] == 200.0
    assert row["score"] > 0


def test_deal_detail_carries_its_evidence(seeded):
    client, listing, _ = seeded
    detail = client.get(f"/api/deals/{listing.id}").json()

    assert detail["valuation"]["basis"] == PriceBasis.SOLD.value
    assert detail["fee_breakdown"]["total"] > 0
    assert detail["breakeven_buy_price"] > detail["buy_cost"]
    assert detail["reasons"]


def test_deal_detail_404s_for_unknown_listing(client):
    assert client.get("/api/deals/nope").status_code == 404


def test_deal_filters(seeded):
    client, _, deal = seeded
    assert client.get("/api/deals", params={"min_score": 999}).json()["total"] == 0
    assert client.get("/api/deals", params={"min_score": 0}).json()["total"] == 1
    assert client.get("/api/deals", params={"search": "iPhone"}).json()["total"] == 1
    assert client.get("/api/deals", params={"search": "Galaxy"}).json()["total"] == 0
    assert client.get("/api/deals", params={"channel": "amazon"}).json()["total"] == 0
    assert (
        client.get("/api/deals", params={"channel": deal.sell_channel.value}).json()["total"]
        == 1
    )


def test_deal_sort_is_validated(seeded):
    client, _, _ = seeded
    assert client.get("/api/deals", params={"sort": "score"}).status_code == 200
    assert client.get("/api/deals", params={"sort": "; DROP TABLE"}).status_code == 400


def test_scam_flagged_deals_are_hidden_by_default(client):
    from api.deps import get_app_settings, get_db

    db = get_db()
    listing = Listing(
        source="ebay",
        source_listing_id="scam",
        title="Apple iPhone 12 A2403 128GB",
        model_number="A2403",
        brand="Apple",
        price=20.0,
        condition=Condition.USED,
        url="https://www.ebay.co.uk/itm/scam",
    )
    db.upsert_listing(listing)
    key = product_key(listing.title, listing.brand, listing.model_number)
    deal = evaluate(listing, make_valuation(product_key=key), get_app_settings())
    assert deal.is_scam_flag
    db.upsert_deal(deal)

    assert client.get("/api/deals").json()["total"] == 0
    assert client.get("/api/deals", params={"include_scams": True}).json()["total"] == 1


# ------------------------------------------------------------------ watchlist
def test_watchlist_crud(client):
    created = client.post("/api/watchlist", json={"query": "ps5", "max_price": 300}).json()
    assert created["query"] == "ps5"

    listed = client.get("/api/watchlist").json()
    assert len(listed) == 1

    patched = client.patch(
        f"/api/watchlist/{created['id']}", json={"enabled": False}
    ).json()
    assert patched["enabled"] is False

    assert client.delete(f"/api/watchlist/{created['id']}").status_code == 204
    assert client.get("/api/watchlist").json() == []


def test_watchlist_rejects_an_empty_query(client):
    assert client.post("/api/watchlist", json={"query": "   "}).status_code == 400


def test_watchlist_404s_on_unknown_id(client):
    assert client.patch("/api/watchlist/999", json={"enabled": False}).status_code == 404
    assert client.delete("/api/watchlist/999").status_code == 404


# ------------------------------------------------------------------ settings
def test_settings_round_trip(client):
    original = client.get("/api/settings").json()
    assert "min_roi" in original["values"]
    assert "ebay_client_secret" not in original["values"]

    updated = client.patch("/api/settings", json={"min_roi": 55.0}).json()
    assert updated["values"]["min_roi"] == 55.0

    # The change persists across reads.
    assert client.get("/api/settings").json()["values"]["min_roi"] == 55.0

    client.post("/api/settings/reset")


def test_settings_ignores_unknown_and_protected_keys(client):
    response = client.patch("/api/settings", json={"ebay_client_secret": "leak"})
    assert response.status_code == 400  # nothing recognised was applied


# ------------------------------------------------------------------ stats & runs
def test_stats_includes_data_health(seeded):
    client, _, _ = seeded
    stats = client.get("/api/stats").json()
    assert stats["total_deals"] == 1
    assert "data_health" in stats
    assert stats["data_health"]["listings"] == 1


def test_runs_start_empty(client):
    assert client.get("/api/runs").json() == []
    assert client.get("/api/runs/1").status_code == 404


def test_valuation_lookup(seeded):
    client, listing, _ = seeded
    key = product_key(listing.title, listing.brand, listing.model_number)

    valuation = client.get(f"/api/valuations/{key}").json()
    assert valuation["product_key"] == key
    assert valuation["confidence"] > 0

    assert client.get("/api/valuations/not-a-real-key").status_code == 404
