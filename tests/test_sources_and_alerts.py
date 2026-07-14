from __future__ import annotations

import json
from pathlib import Path

from arb.config import Settings
from arb.models import Condition
from sources.ebay import parse_browse_item
from sources.fb_marketplace import extract_item_id
from sources.gumtree import build_listing, parse_price

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_browse_item():
    payload = json.loads((FIXTURES / "ebay_browse.json").read_text())
    items = [parse_browse_item(i) for i in payload["itemSummaries"]]
    first = items[0]
    assert first.source == "ebay"
    assert first.price == 220.0
    assert first.shipping == 3.99
    assert first.condition == Condition.USED
    assert first.model_number == "A2403"
    assert first.brand == "Apple"
    assert "London" in first.location

    second = items[1]
    assert second.condition == Condition.NEW  # conditionId 1000
    assert second.model_number == "WH-1000XM4"
    assert second.brand == "Sony"


def test_parse_browse_item_missing_fields():
    assert parse_browse_item({"title": "no id or price"}) is None


def test_gumtree_parse_price():
    assert parse_price("£120.00") == 120.0
    assert parse_price("£1,250") == 1250.0
    assert parse_price("Free") is None
    assert parse_price(None) is None


def test_gumtree_build_listing():
    s = Settings(_env_file=None, scrape_default_shipping=0.0)
    listing = build_listing(
        s,
        listing_id="abc123",
        title="Samsung Galaxy SM-G991B 128GB",
        price=180.0,
        url="/p/phones/samsung/abc123",
        image_url=None,
        location="Leeds",
    )
    assert listing.source == "gumtree"
    assert listing.url.startswith("https://www.gumtree.com")
    assert listing.model_number == "SM-G991B"
    assert listing.brand == "Samsung"


def test_fb_extract_item_id():
    assert extract_item_id("/marketplace/item/1234567890/?ref=x") == "1234567890"
    assert extract_item_id("/marketplace/category/electronics") is None
