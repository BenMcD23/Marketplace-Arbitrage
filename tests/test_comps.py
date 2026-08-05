from __future__ import annotations

from arb.models import Condition
from oracle.comps import (
    extract_capacity,
    is_accessory,
    relevance,
    select_comps,
    tokenize,
)
from tests.conftest import make_comp


# ------------------------------------------------------------------ accessories
def test_accessory_detection():
    assert is_accessory("iPhone 12 Case Clear Silicone")
    assert is_accessory("Screen Protector for iPhone 12")
    assert is_accessory("USB C Charger Cable for iPhone")
    assert is_accessory("iPhone 12 Empty Box only")
    assert is_accessory("Job lot of broken phones")
    assert is_accessory("Lot of 5 iPhone 12")


def test_genuine_device_is_not_an_accessory():
    assert not is_accessory("Apple iPhone 12 128GB Blue Unlocked")
    assert not is_accessory("Sony WH-1000XM4 Wireless Headphones")


# ------------------------------------------------------------------ capacity
def test_extract_capacity_normalises_units():
    assert extract_capacity("iPhone 12 128GB") == "128gb"
    assert extract_capacity("MacBook Pro 1TB SSD") == "1024gb"
    assert extract_capacity("iPhone 12 256 GB") == "256gb"
    assert extract_capacity("Sony headphones") is None


# ------------------------------------------------------------------ relevance
def test_tokenize_drops_sales_noise():
    tokens = tokenize("Apple iPhone 12 128GB Unlocked Excellent Condition Fast Free Postage")
    assert "unlocked" not in tokens
    assert "excellent" not in tokens
    assert "iphone" in tokens
    assert "12" in tokens


def test_relevance_is_asymmetric():
    """Extra words in a comp are fine; missing the target's words is not."""
    target = tokenize("Apple iPhone 12 128GB")
    assert relevance(target, "Apple iPhone 12 128GB with charger and case") == 1.0
    assert relevance(target, "Apple iPhone 11 128GB") < 1.0


def test_relevance_weights_numbers_heavily():
    """An iPhone 12 and an iPhone 13 differ by one character and about £150."""
    target = tokenize("Apple iPhone 12 128GB")
    right = relevance(target, "Apple iPhone 12 128GB")
    wrong_model = relevance(target, "Apple iPhone 13 128GB")
    assert wrong_model < right - 0.2


# ------------------------------------------------------------------ selection
def test_select_comps_filters_the_junk():
    candidates = [
        make_comp("Apple iPhone 12 128GB Blue Unlocked", 340),
        make_comp("Apple iPhone 12 128GB Black", 360),
        make_comp("iPhone 12 Case Clear Silicone", 6),
        make_comp("Screen Protector for iPhone 12", 3),
        make_comp("Apple iPhone 12 64GB", 300),
        make_comp("Apple iPhone 12 128GB spares", 90, condition=Condition.FOR_PARTS),
        make_comp("Samsung Galaxy S21 128GB", 280),
    ]
    result = select_comps("Apple iPhone 12 128GB", candidates, min_relevance=0.6)

    kept_titles = [c.title for c in result.kept]
    assert len(result.kept) == 2
    assert all("iPhone 12 128GB" in t for t in kept_titles)

    reasons = result.reject_counts()
    assert reasons["accessory_or_lot"] == 2
    assert reasons["capacity_mismatch"] == 1
    assert reasons["for_parts"] == 1
    assert reasons["low_relevance"] == 1


def test_select_comps_allows_unstated_capacity():
    """Plenty of genuine listings omit the capacity; do not punish them."""
    candidates = [make_comp("Apple iPhone 12 Blue Unlocked", 340)]
    result = select_comps("Apple iPhone 12 128GB", candidates, min_relevance=0.5)
    assert len(result.kept) == 1


def test_select_comps_rejects_priceless_entries():
    result = select_comps("Apple iPhone 12", [make_comp("Apple iPhone 12", 0)])
    assert result.reject_counts() == {"no_price": 1}


def test_kept_comps_are_sorted_by_relevance():
    candidates = [
        make_comp("Apple iPhone 12", 300),
        make_comp("Apple iPhone 12 128GB Blue", 340),
    ]
    result = select_comps("Apple iPhone 12 128GB", candidates, min_relevance=0.4)
    assert result.kept[0].relevance >= result.kept[-1].relevance
