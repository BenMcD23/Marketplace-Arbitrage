"""Mini / micro PC profile: spec parsing, filters, ranking and push format.

Everything here is offline — the eBay mapping runs against a saved fixture.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from alerts.mini_pc import format_landed_line, format_mini_pc_deal, format_time_remaining
from arb.config import Settings
from arb.models import Condition, DeliveryKind, Listing, ListingType
from profiles.mini_pc import (
    CPU_TIERS,
    DealBand,
    Reject,
    evaluate,
    find_cpus,
    parse_ram_gb,
    parse_storage_gb,
    rank,
    select_cpu,
)
from sources.ebay_mini_pc import (
    MiniPcEbaySource,
    parse_delivery,
    parse_listing_type,
    parse_mini_pc_item,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def mini_settings() -> Settings:
    return Settings(
        _env_file=None,
        mini_pc_max_cpu_tdp_w=35,
        mini_pc_min_ram_gb=16,
        mini_pc_hard_min_ram_gb=8,
        mini_pc_min_storage_gb=128,
        mini_pc_ram_upgrade_cost=25.0,
        mini_pc_assumed_delivery=8.0,
        mini_pc_strong_deal_max=160.0,
        mini_pc_exceptional_deal_max=130.0,
    )


def make_mini_listing(**kwargs) -> Listing:
    base = dict(
        source="ebay_mini_pc",
        source_listing_id="1",
        title="Dell OptiPlex 7060 Micro i5-8500T 16GB RAM 256GB SSD",
        price=130.0,
        shipping=8.99,
        delivery_kind=DeliveryKind.FIXED,
        condition=Condition.USED,
        url="https://example.com/mini/1",
    )
    base.update(kwargs)
    return Listing(**base)


# ------------------------------------------------------------------ CPU parsing
def test_find_cpus_reads_family_model_and_suffix():
    matches = find_cpus("Dell OptiPlex Micro i5-10500T and a spare i7 10700t board")
    assert [m.key for m in matches] == ["I5-10500T", "I7-10700T"]
    assert all(m.is_low_power for m in matches)


def test_non_t_desktop_part_is_not_low_power():
    (match,) = find_cpus("OptiPlex 3070 Micro i5-9400 16GB")
    assert match.key == "I5-9400"
    assert not match.is_low_power


@pytest.mark.parametrize(
    "title,expected",
    [
        ("HP ProDesk 400 G6 i7-10700T 16GB", "i7-10700T"),
        ("Dell OptiPlex Micro i5-10500T 16GB", "i5-10500T"),
        ("Lenovo ThinkCentre M70q i5-10400T 16GB", "i5-10400T"),
        ("Dell OptiPlex 7070 i5-9500T 16GB", "i5-9500T"),
        ("HP EliteDesk 800 G4 i5-8500T 16GB", "i5-8500T"),
    ],
)
def test_select_cpu_matches_every_tier(title, expected, mini_settings):
    cpu, reason = select_cpu(title, mini_settings)
    assert reason is None
    assert cpu.name == expected


def test_select_cpu_prefers_the_highest_tier_mentioned(mini_settings):
    cpu, _ = select_cpu("Job lot: i5-8500T and i7-10700T micro PCs", mini_settings)
    assert cpu.name == "i7-10700T"


def test_select_cpu_rejects_non_t_variants(mini_settings):
    cpu, reason = select_cpu("Dell OptiPlex Micro i5-10500 16GB 256GB", mini_settings)
    assert cpu is None
    assert reason is Reject.CPU_NOT_LOW_POWER


def test_select_cpu_rejects_untiered_and_missing(mini_settings):
    assert select_cpu("Dell OptiPlex i3-8100T 16GB", mini_settings)[1] is Reject.CPU_NOT_IN_TIER_TABLE
    assert select_cpu("Raspberry Pi 4 8GB RAM", mini_settings)[1] is Reject.CPU_MISSING


def test_tier_order_matches_the_stated_ranking():
    order = sorted(CPU_TIERS.values(), key=lambda c: -c.tier)
    assert [c.name for c in order] == [
        "i7-10700T",
        "i5-10500T",
        "i5-10400T",
        "i5-9500T",
        "i5-8500T",
    ]
    assert all(c.tdp_w <= 35 for c in CPU_TIERS.values())


def test_tdp_ceiling_is_enforced_not_assumed(mini_settings):
    strict = mini_settings.model_copy(update={"mini_pc_max_cpu_tdp_w": 15})
    cpu, reason = select_cpu("HP ProDesk 400 G6 i7-10700T", strict)
    assert cpu is None
    assert reason is Reject.CPU_TDP_TOO_HIGH


# ---------------------------------------------------------------- spec parsing
@pytest.mark.parametrize(
    "text,expected",
    [
        ("i5-10500T 16GB RAM 256GB SSD", 16),
        ("i5-10500T 16 GB DDR4 512GB NVMe", 16),
        ("Micro PC RAM: 32GB, 1TB SSD", 32),
        ("2x8GB DDR4 SO-DIMM", 16),
        ("4 x 8GB DDR4 memory", 32),
        ("8GB memory 128GB SSD", 8),
    ],
)
def test_parse_ram_gb(text, expected):
    assert parse_ram_gb(text) == expected


def test_ram_parsing_never_reads_storage_as_memory():
    # No memory word next to the figure -> unknown, not 256GB of RAM.
    assert parse_ram_gb("OptiPlex Micro i5-10500T 256GB SSD") is None
    assert parse_ram_gb("OptiPlex Micro i5-10500T 512GB NVMe 16GB DDR4") == 16


@pytest.mark.parametrize(
    "text,expected",
    [
        ("16GB RAM 256GB SSD", 256),
        ("16GB RAM 1TB HDD", 1024),
        ("16GB RAM 512 GB NVMe", 512),
        ("16GB DDR4, storage: 128GB", 128),
        ("16GB RAM No HDD", 0),
        ("16GB RAM 128GB SSD + 1TB HDD", 1024),
    ],
)
def test_parse_storage_gb(text, expected):
    assert parse_storage_gb(text) == expected


def test_storage_unknown_when_unstated():
    assert parse_storage_gb("Dell OptiPlex Micro i5-10500T 16GB RAM") is None


# ------------------------------------------------------------------ evaluation
def test_accepts_a_clean_used_listing(mini_settings):
    verdict = evaluate(make_mini_listing(), mini_settings)
    assert verdict.accepted
    candidate = verdict.candidate
    assert candidate.cpu.name == "i5-8500T"
    assert candidate.ram_gb == 16
    assert candidate.storage_gb == 256
    # Landed cost is item + delivery, never the item price alone.
    assert candidate.item_price == 130.0
    assert candidate.delivery_cost == 8.99
    assert candidate.total_landed == 138.99
    assert candidate.band is DealBand.STRONG


def test_bands_key_off_total_landed_not_item_price(mini_settings):
    # £129 item looks exceptional until £20 of delivery is added.
    verdict = evaluate(make_mini_listing(price=129.0, shipping=20.0), mini_settings)
    assert verdict.candidate.total_landed == 149.0
    assert verdict.candidate.band is DealBand.STRONG

    cheap = evaluate(make_mini_listing(price=120.0, shipping=0.0), mini_settings)
    assert cheap.candidate.band is DealBand.EXCEPTIONAL

    dear = evaluate(make_mini_listing(price=180.0, shipping=0.0), mini_settings)
    assert dear.candidate.band is DealBand.STANDARD


def test_for_parts_rejected_unless_include_broken(mini_settings):
    listing = make_mini_listing(condition=Condition.FOR_PARTS)
    assert evaluate(listing, mini_settings).reason is Reject.CONDITION_FOR_PARTS
    assert evaluate(listing, mini_settings, include_broken=True).accepted


def test_new_condition_is_out_of_profile(mini_settings):
    verdict = evaluate(make_mini_listing(condition=Condition.NEW), mini_settings)
    assert verdict.reason is Reject.CONDITION_NOT_USED


def test_ram_under_the_hard_floor_is_always_rejected(mini_settings):
    listing = make_mini_listing(
        title="Dell OptiPlex 7060 Micro i5-8500T 4GB RAM 256GB SSD", price=50.0, shipping=0.0
    )
    assert evaluate(listing, mini_settings).reason is Reject.RAM_BELOW_FLOOR


def test_ram_unknown_is_rejected_because_16gb_cannot_be_proven(mini_settings):
    listing = make_mini_listing(title="Dell OptiPlex 7060 Micro i5-8500T 256GB SSD")
    assert evaluate(listing, mini_settings).reason is Reject.RAM_UNKNOWN


def test_8gb_rejected_at_a_normal_price_but_flagged_when_exceptional(mini_settings):
    title = "Dell OptiPlex 7060 Micro i5-8500T 8GB RAM 256GB SSD"
    normal = evaluate(make_mini_listing(title=title, price=150.0, shipping=0.0), mini_settings)
    assert normal.reason is Reject.RAM_BELOW_TARGET

    bargain = evaluate(make_mini_listing(title=title, price=99.0, shipping=5.0), mini_settings)
    assert bargain.accepted
    assert bargain.candidate.needs_ram_upgrade
    assert bargain.candidate.ram_upgrade_cost == 25.0
    assert bargain.candidate.all_in_cost == 129.0


def test_soldered_ram_kills_the_upgrade_escape_hatch(mini_settings):
    listing = make_mini_listing(
        title="Mini PC i5-8500T 8GB soldered RAM 256GB SSD", price=99.0, shipping=5.0
    )
    assert evaluate(listing, mini_settings).reason is Reject.RAM_BELOW_TARGET


def test_small_storage_rejected_unless_price_is_exceptional(mini_settings):
    title = "Dell OptiPlex 7060 Micro i5-8500T 16GB RAM 64GB SSD"
    normal = evaluate(make_mini_listing(title=title, price=150.0, shipping=0.0), mini_settings)
    assert normal.reason is Reject.STORAGE_BELOW_FLOOR

    bargain = evaluate(make_mini_listing(title=title, price=100.0, shipping=0.0), mini_settings)
    assert bargain.accepted
    assert any("64GB storage" in n for n in bargain.candidate.notes)


def test_best_offer_and_estimated_delivery_are_noted(mini_settings):
    listing = make_mini_listing(best_offer=True, delivery_kind=DeliveryKind.ESTIMATED)
    notes = evaluate(listing, mini_settings).candidate.notes
    assert any("Best Offer" in n for n in notes)
    assert any("Delivery quoted at checkout" in n for n in notes)


def test_specs_are_read_from_the_description_too(mini_settings):
    listing = make_mini_listing(
        title="Dell OptiPlex 7060 Micro i5-8500T Mini PC",
        description="16GB DDR4 RAM, 256GB NVMe SSD, Windows 11 Pro",
    )
    verdict = evaluate(listing, mini_settings)
    assert verdict.accepted
    assert verdict.candidate.ram_gb == 16
    assert verdict.candidate.storage_gb == 256


# --------------------------------------------------------------------- ranking
def test_rank_is_cpu_tier_then_total_landed(mini_settings):
    cheap_slow = evaluate(
        make_mini_listing(
            source_listing_id="a",
            title="EliteDesk 800 G4 i5-8500T 16GB RAM 256GB SSD",
            price=100.0,
            shipping=0.0,
        ),
        mini_settings,
    ).candidate
    dear_fast = evaluate(
        make_mini_listing(
            source_listing_id="b",
            title="ProDesk 400 G6 i7-10700T 16GB RAM 256GB SSD",
            price=200.0,
            shipping=0.0,
        ),
        mini_settings,
    ).candidate
    cheap_fast = evaluate(
        make_mini_listing(
            source_listing_id="c",
            title="ProDesk 400 G6 i7-10700T 32GB RAM 512GB SSD",
            price=150.0,
            shipping=10.0,
        ),
        mini_settings,
    ).candidate

    ordered = rank([cheap_slow, dear_fast, cheap_fast])
    # Tier wins first; inside a tier the cheaper landed cost wins.
    assert [c.listing.source_listing_id for c in ordered] == ["c", "b", "a"]


def test_rank_compares_landed_cost_not_item_price(mini_settings):
    pricey_item_free_post = evaluate(
        make_mini_listing(source_listing_id="a", price=140.0, shipping=0.0), mini_settings
    ).candidate
    cheap_item_costly_post = evaluate(
        make_mini_listing(source_listing_id="b", price=135.0, shipping=25.0), mini_settings
    ).candidate
    ordered = rank([cheap_item_costly_post, pricey_item_free_post])
    assert [c.listing.source_listing_id for c in ordered] == ["a", "b"]


# ------------------------------------------------------------- eBay item mapping
def test_parse_delivery_free_fixed_estimated_and_collection():
    free = {"shippingOptions": [{"shippingCost": {"value": "0.00"}}]}
    assert parse_delivery(free, 8.0) == (0.0, DeliveryKind.FREE)

    fixed = {"shippingOptions": [{"shippingCost": {"value": "4.95"}}]}
    assert parse_delivery(fixed, 8.0) == (4.95, DeliveryKind.FIXED)

    calculated = {"shippingOptions": [{"shippingCostType": "CALCULATED"}]}
    assert parse_delivery(calculated, 8.0) == (8.0, DeliveryKind.ESTIMATED)

    collection = {"pickupOptions": [{"pickupLocationType": "STORE"}]}
    assert parse_delivery(collection, 8.0) == (0.0, DeliveryKind.COLLECTION)


def test_parse_listing_type():
    assert parse_listing_type(["FIXED_PRICE"]) is ListingType.FIXED_PRICE
    assert parse_listing_type(["AUCTION"]) is ListingType.AUCTION
    assert parse_listing_type(["AUCTION", "FIXED_PRICE"]) is ListingType.AUCTION_WITH_BIN


def test_parse_fixture_items():
    payload = json.loads((FIXTURES / "ebay_mini_pc.json").read_text())
    items = [parse_mini_pc_item(i, assumed_delivery=8.0) for i in payload["itemSummaries"]]

    optiplex = items[0]
    assert optiplex.price == 134.0
    assert optiplex.shipping == 8.99
    assert optiplex.buy_cost == 142.99
    assert optiplex.delivery_kind is DeliveryKind.FIXED
    assert optiplex.best_offer is True
    assert optiplex.condition is Condition.USED

    prodesk = items[1]
    assert prodesk.delivery_kind is DeliveryKind.FREE
    assert prodesk.shipping == 0.0
    assert prodesk.buy_cost == 245.0
    assert "free next-day delivery" in prodesk.description

    thinkcentre = items[2]
    # Auction: priced on the live bid, not the £300 BIN placeholder.
    assert thinkcentre.price == 96.0
    assert thinkcentre.listing_type is ListingType.AUCTION
    assert thinkcentre.bid_count == 7
    assert thinkcentre.delivery_kind is DeliveryKind.ESTIMATED
    assert thinkcentre.buy_cost == 104.0
    assert thinkcentre.ends_at is not None

    # Auction with no bid figure at all is dropped rather than guessed at.
    assert items[5] is None


def test_fixture_items_through_the_profile(mini_settings):
    payload = json.loads((FIXTURES / "ebay_mini_pc.json").read_text())
    listings = [
        listing
        for listing in (parse_mini_pc_item(i, 8.0) for i in payload["itemSummaries"])
        if listing is not None
    ]
    verdicts = [evaluate(listing, mini_settings) for listing in listings]
    accepted = rank([v.candidate for v in verdicts if v.accepted])

    assert [c.cpu.name for c in accepted] == ["i7-10700T", "i5-10400T", "i5-8500T"]
    # i5-9500 (no T suffix) and the for-parts 4GB unit are both filtered out.
    reasons = {v.reason for v in verdicts if not v.accepted}
    assert Reject.CPU_NOT_LOW_POWER in reasons
    assert Reject.CONDITION_FOR_PARTS in reasons


def test_source_filter_includes_auctions_and_used_conditions(mini_settings):
    source = MiniPcEbaySource(mini_settings, queries=["OptiPlex Micro i5-10500T"], client=object())
    built = source._build_filter()
    assert "buyingOptions:{FIXED_PRICE|AUCTION}" in built
    assert "7000" not in built

    broken = MiniPcEbaySource(mini_settings, include_broken=True, client=object())._build_filter()
    assert "7000" in broken


def test_source_defaults_to_the_profile_search_terms(mini_settings):
    source = MiniPcEbaySource(mini_settings, client=object())
    assert source.queries == [
        "OptiPlex Micro i5-10500T",
        "OptiPlex Micro i5-10400T",
        "ProDesk 400 G6 i5-10500T",
        "ProDesk 400 G6 i7-10700T",
        "EliteDesk 800 G4 i5-8500T",
        "OptiPlex 7060 Micro i5-8500T",
        "ThinkCentre M70q i5-10400T",
        "ThinkCentre Tiny i5-8500T",
    ]


# ---------------------------------------------------------------- push format
def test_landed_line_always_breaks_out_item_and_delivery(mini_settings):
    candidate = evaluate(make_mini_listing(), mini_settings).candidate
    assert format_landed_line(candidate) == (
        "<b>Total landed: £138.99</b> (item £130.00 + delivery £8.99)"
    )


def test_landed_line_labels_free_delivery(mini_settings):
    listing = make_mini_listing(shipping=0.0, delivery_kind=DeliveryKind.FREE)
    candidate = evaluate(listing, mini_settings).candidate
    assert "free delivery £0.00" in format_landed_line(candidate)


def test_message_carries_the_landed_line_and_specs(mini_settings):
    candidate = evaluate(make_mini_listing(best_offer=True), mini_settings).candidate
    message = format_mini_pc_deal(candidate)
    assert "STRONG MINI PC DEAL" in message
    assert "Total landed: £138.99</b> (item £130.00 + delivery £8.99)" in message
    assert "CPU: i5-8500T (35W, tier 1/5)" in message
    assert "RAM: 16GB   Storage: 256GB" in message
    assert "Best Offer available" in message
    assert '<a href="https://example.com/mini/1">View listing</a>' in message


def test_auction_message_shows_current_bid_and_time_left(mini_settings):
    listing = make_mini_listing(
        price=96.0,
        shipping=8.0,
        listing_type=ListingType.AUCTION,
        bid_count=7,
        ends_at=datetime.now(UTC) + timedelta(days=1, hours=4),
    )
    message = format_mini_pc_deal(evaluate(listing, mini_settings).candidate)
    assert "Auction: £96.00 (7 bids)" in message
    assert "1d 3h left" in message or "1d 4h left" in message
    assert "Total landed: £104.00</b> (item £96.00 + delivery £8.00)" in message


def test_exceptional_header_and_ram_upgrade_cost(mini_settings):
    listing = make_mini_listing(
        title="Dell OptiPlex 7060 Micro i5-8500T 8GB RAM 256GB SSD", price=99.0, shipping=5.0
    )
    message = format_mini_pc_deal(evaluate(listing, mini_settings).candidate)
    assert "EXCEPTIONAL MINI PC DEAL" in message
    assert "Needs RAM upgrade: +£25.00 → £129.00 all-in" in message


def test_format_time_remaining():
    assert format_time_remaining(timedelta(days=2, hours=4)) == "2d 4h"
    assert format_time_remaining(timedelta(hours=3, minutes=12)) == "3h 12m"
    assert format_time_remaining(timedelta(minutes=48)) == "48m"
    assert format_time_remaining(None) == "ended"


def test_titles_are_html_escaped(mini_settings):
    listing = make_mini_listing(
        title="OptiPlex 7060 Micro i5-8500T 16GB RAM 256GB SSD <b>WOW</b> & more"
    )
    message = format_mini_pc_deal(evaluate(listing, mini_settings).candidate)
    assert "&lt;b&gt;WOW&lt;/b&gt; &amp; more" in message


def test_storage_is_rendered_in_tb_when_it_is_a_whole_terabyte():
    from alerts.mini_pc import format_storage

    assert format_storage(1024) == "1TB"
    assert format_storage(2048) == "2TB"
    assert format_storage(512) == "512GB"
    assert format_storage(None) == "not stated"


def test_best_offer_is_not_printed_twice(mini_settings):
    message = format_mini_pc_deal(
        evaluate(make_mini_listing(best_offer=True), mini_settings).candidate
    )
    assert message.count("Best Offer") == 1
