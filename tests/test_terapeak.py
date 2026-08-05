"""Tests for the Terapeak parser.

Only the parsing is tested, and deliberately so: the browser half needs a real
logged-in eBay session, which no test should ever depend on. Splitting the two
means the fragile part (eBay's DOM) is the *only* part that is unverified here,
and when it changes the fix is one pure function.
"""

from __future__ import annotations

from arb.config import Settings
from oracle.terapeak import TerapeakClient, parse_research_text, stats_to_comps

# Roughly how Product Research reads once the metrics have rendered. Label
# order and wording vary, which is why parsing is label-driven.
SAMPLE_PAGE = """
    Product research
    Sony WH-1000XM4
    Last 90 days · United Kingdom
    Avg sold price
    £168.42
    Avg shipping
    £3.49
    Total sold
    1,284
    Total sales
    £216,231.28
    Sell-through rate
    82.5%
    Total sellers
    412
    Date range
    90 days
"""

RENAMED_LABELS_PAGE = """
    Average sale price £249.99
    Items sold 63
    Sell through rate 45%
"""

EMPTY_PAGE = """
    Product research
    No results found for your search.
"""


def test_parses_the_metrics():
    stats = parse_research_text(SAMPLE_PAGE, query="Sony WH-1000XM4")
    assert stats.avg_sold_price == 168.42
    assert stats.avg_shipping == 3.49
    assert stats.total_sold == 1284
    assert stats.total_sellers == 412
    assert stats.sell_through_pct == 82.5
    assert stats.is_usable


def test_delivered_price_includes_postage():
    """The buyer pays both, so the resale estimate has to clear both."""
    stats = parse_research_text(SAMPLE_PAGE)
    assert stats.delivered_price == 171.91


def test_handles_alternative_label_wording():
    """Terapeak has been renamed and relabelled before; it will be again."""
    stats = parse_research_text(RENAMED_LABELS_PAGE, query="thing")
    assert stats.avg_sold_price == 249.99
    assert stats.total_sold == 63
    assert stats.sell_through_pct == 45.0


def test_empty_results_are_not_usable():
    stats = parse_research_text(EMPTY_PAGE, query="nonsense")
    assert stats.avg_sold_price is None
    assert not stats.is_usable


def test_sell_through_is_capped_at_100():
    """Terapeak reports over 100% when more sold than are currently listed."""
    stats = parse_research_text("Sell-through rate 340%")
    assert stats.sell_through_pct == 100.0


def test_thousands_separators_are_handled():
    stats = parse_research_text("Avg sold price £1,299.50 Total sold 12,004")
    assert stats.avg_sold_price == 1299.50
    assert stats.total_sold == 12004


def test_other_currencies_parse():
    assert parse_research_text("Avg sold price $249.99").avg_sold_price == 249.99
    assert parse_research_text("Avg sold price €199.00").avg_sold_price == 199.00


# ------------------------------------------------------------------ comps
def test_stats_become_comps_at_the_delivered_price():
    stats = parse_research_text(SAMPLE_PAGE, query="Sony WH-1000XM4")
    comps = stats_to_comps(stats)
    assert comps
    assert all(c.sold for c in comps)
    assert all(c.price == stats.delivered_price for c in comps)


def test_comp_count_is_capped():
    """1,284 identical comps would manufacture certainty the average lacks."""
    stats = parse_research_text(SAMPLE_PAGE, query="Sony WH-1000XM4")
    assert stats.total_sold == 1284
    assert len(stats_to_comps(stats)) == 20


def test_unusable_stats_produce_no_comps():
    assert stats_to_comps(parse_research_text(EMPTY_PAGE)) == []


# ------------------------------------------------------------------ client
def test_client_is_disabled_by_default():
    """The ToS risk is the user's to accept, so it is never on implicitly."""
    settings = Settings(_env_file=None)
    assert settings.enable_terapeak is False
    assert TerapeakClient(settings).enabled is False


def test_research_url_targets_the_configured_marketplace():
    client = TerapeakClient(Settings(_env_file=None, ebay_marketplace="EBAY_GB"))
    url = client.research_url("sony wh-1000xm4")
    assert url.startswith("https://www.ebay.co.uk/sh/research")
    assert "marketplace=EBAY-GB" in url
    assert "keywords=sony+wh-1000xm4" in url
    assert "tabName=SOLD" in url


def test_research_url_follows_the_marketplace_setting():
    client = TerapeakClient(Settings(_env_file=None, ebay_marketplace="EBAY_US"))
    assert client.research_url("x").startswith("https://www.ebay.com/sh/research")


def test_unknown_marketplace_falls_back_to_uk():
    client = TerapeakClient(Settings(_env_file=None, ebay_marketplace="EBAY_XX"))
    assert "ebay.co.uk" in client.research_url("x")
