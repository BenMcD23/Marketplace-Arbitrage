from __future__ import annotations

from arb.models import Condition, Listing, make_listing_id
from sources.normalise import (
    extract_brand,
    extract_model_number,
    normalise_condition,
)


def test_listing_id_is_stable_and_derived():
    a = Listing(source="ebay", source_listing_id="42", title="x", price=1.0, url="u")
    b = Listing(source="ebay", source_listing_id="42", title="different", price=9.0, url="u2")
    assert a.id == b.id == make_listing_id("ebay", "42")


def test_listing_id_differs_by_source():
    a = Listing(source="ebay", source_listing_id="42", title="x", price=1.0, url="u")
    b = Listing(source="gumtree", source_listing_id="42", title="x", price=1.0, url="u")
    assert a.id != b.id


def test_buy_cost_sums_price_and_shipping():
    listing = Listing(source="ebay", source_listing_id="1", title="x", price=10.0, shipping=2.5, url="u")
    assert listing.buy_cost == 12.5


def test_extract_brand():
    assert extract_brand("Apple iPhone 13 Pro Max") == "Apple"
    assert extract_brand("Sony WH-1000XM4 Headphones") == "Sony"
    assert extract_brand("Some generic thing") is None


def test_extract_model_number():
    assert extract_model_number("Sony WH-1000XM4 Wireless") == "WH-1000XM4"
    assert extract_model_number("Samsung Galaxy SM-G991B 128GB") == "SM-G991B"
    assert extract_model_number("Apple iPad A2403 space grey") == "A2403"
    assert extract_model_number("just some words") is None


def test_normalise_condition():
    assert normalise_condition("Brand New") == Condition.NEW
    assert normalise_condition("Used") == Condition.USED
    assert normalise_condition("Spares or repair") == Condition.FOR_PARTS
    assert normalise_condition("For parts or not working") == Condition.FOR_PARTS
    assert normalise_condition(None) == Condition.UNKNOWN
    assert normalise_condition("weird custom text") == Condition.UNKNOWN
