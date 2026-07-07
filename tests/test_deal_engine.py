from __future__ import annotations

from arb.models import Condition, SellChannel
from engine.deals import evaluate
from tests.conftest import make_listing, make_valuation


def test_profitable_ebay_deal(settings):
    # Buy 200, resale 350 (10 comps). Fees ~ 350*0.128 + 0.30 = 45.10, pkg 2.50.
    # profit = 350 - 200 - 45.10 - 2.50 = 102.40; ROI = 51.2%.
    listing = make_listing(price=200.0)
    val = make_valuation(ebay_sold_median=350.0, ebay_sold_count=10)
    deal = evaluate(listing, val, settings)
    assert deal is not None
    assert deal.is_scam_flag is False
    assert deal.sell_channel == SellChannel.EBAY
    assert deal.est_profit == 102.40
    assert deal.roi_pct == 51.2


def test_unprofitable_below_min_profit(settings):
    # Resale barely above buy cost -> profit below MIN_PROFIT.
    listing = make_listing(price=330.0)
    val = make_valuation(ebay_sold_median=350.0, ebay_sold_count=10)
    assert evaluate(listing, val, settings) is None


def test_thin_data_rejected(settings):
    # Great margin but only 2 sold comps (< MIN_SOLD_COUNT=3) -> not trusted.
    listing = make_listing(price=100.0)
    val = make_valuation(ebay_sold_median=350.0, ebay_sold_count=2, amazon_price=None)
    assert evaluate(listing, val, settings) is None


def test_scam_priced_flagged_separately(settings):
    # Buy cost 50 vs resale 350 -> 0.14 < TGTBT_RATIO 0.20 -> scam flag.
    listing = make_listing(price=50.0)
    val = make_valuation(ebay_sold_median=350.0, ebay_sold_count=10)
    deal = evaluate(listing, val, settings)
    assert deal is not None
    assert deal.is_scam_flag is True


def test_parts_only_rejected_by_default(settings):
    listing = make_listing(price=100.0, condition=Condition.FOR_PARTS)
    val = make_valuation(ebay_sold_median=350.0, ebay_sold_count=10)
    assert evaluate(listing, val, settings) is None


def test_parts_only_allowed_when_enabled(settings):
    settings.allow_for_parts = True
    listing = make_listing(price=100.0, condition=Condition.FOR_PARTS)
    val = make_valuation(ebay_sold_median=350.0, ebay_sold_count=10)
    deal = evaluate(listing, val, settings)
    assert deal is not None
    assert deal.is_scam_flag is False


def test_amazon_channel_selected_when_more_profitable(settings):
    # eBay resale 300, Amazon resale 400 rank ok. Amazon should win on profit.
    listing = make_listing(price=150.0)
    val = make_valuation(
        ebay_sold_median=300.0,
        ebay_sold_count=10,
        amazon_price=400.0,
        amazon_rank=1000,
    )
    deal = evaluate(listing, val, settings)
    assert deal is not None
    assert deal.sell_channel == SellChannel.AMAZON


def test_amazon_rejected_when_rank_too_high(settings):
    # Amazon price attractive but rank worse than MAX_AMAZON_RANK -> unsellable.
    # eBay has no data, so nothing should be flagged.
    listing = make_listing(price=150.0)
    val = make_valuation(
        ebay_sold_median=None,
        ebay_sold_count=0,
        amazon_price=400.0,
        amazon_rank=99_999,
    )
    assert evaluate(listing, val, settings) is None


def test_no_valuation_data_returns_none(settings):
    listing = make_listing(price=150.0)
    val = make_valuation(ebay_sold_median=None, ebay_sold_count=0, amazon_price=None, amazon_rank=None)
    assert evaluate(listing, val, settings) is None


def test_below_min_roi_rejected(settings):
    # Set a very high ROI floor so a modestly profitable deal fails ROI but not profit.
    settings.min_roi = 200.0
    listing = make_listing(price=200.0)
    val = make_valuation(ebay_sold_median=350.0, ebay_sold_count=10)
    assert evaluate(listing, val, settings) is None
