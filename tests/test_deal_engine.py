from __future__ import annotations

from arb.models import Condition, SellChannel
from engine.deals import estimate_p_sale, evaluate, score_deal
from tests.conftest import make_listing, make_valuation


def test_profitable_ebay_deal(settings):
    # Buy 200, resale 350. Fees: 350*0.128 + 0.30 = 45.10, postage 3.50, pkg 2.50.
    # profit = 350 - 200 - 51.10 = 98.90; ROI = 49.45%.
    listing = make_listing(price=200.0)
    deal = evaluate(listing, make_valuation(), settings)
    assert deal is not None
    assert deal.is_scam_flag is False
    assert deal.sell_channel == SellChannel.EBAY
    assert deal.est_profit == 98.90
    assert deal.roi_pct == 49.45


def test_expected_profit_sits_below_headline_profit(settings):
    """Risk adjustment must cost something — otherwise it is decoration."""
    listing = make_listing(price=200.0)
    deal = evaluate(listing, make_valuation(), settings)
    assert deal is not None
    assert deal.expected_profit < deal.est_profit
    assert deal.holding_cost > 0
    # Downside case is priced off the p10, so it is worse but still reported.
    assert deal.worst_case_profit < deal.est_profit


def test_unprofitable_below_min_profit(settings):
    listing = make_listing(price=330.0)
    assert evaluate(listing, make_valuation(), settings) is None


def test_low_confidence_valuation_rejected(settings):
    """A huge paper margin must not survive a valuation we do not believe."""
    listing = make_listing(price=100.0)
    val = make_valuation(confidence=0.10)
    assert evaluate(listing, val, settings) is None


def test_low_confidence_allowed_when_gate_lowered(settings):
    settings.min_confidence = 0.05
    listing = make_listing(price=100.0)
    deal = evaluate(listing, make_valuation(confidence=0.10), settings)
    assert deal is not None


def test_scam_priced_flagged_separately(settings):
    # Buy cost 50 vs resale 350 -> 0.14 < TGTBT_RATIO 0.20 -> scam flag.
    listing = make_listing(price=50.0)
    deal = evaluate(listing, make_valuation(), settings)
    assert deal is not None
    assert deal.is_scam_flag is True
    assert deal.score == 0.0
    assert deal.reasons


def test_scam_flag_reports_the_channel_it_was_computed_from(settings):
    """The reported resale must belong to the channel that triggered the flag."""
    listing = make_listing(price=40.0)
    val = make_valuation(resale_price=300.0, amazon_price=500.0, amazon_rank=900)
    deal = evaluate(listing, val, settings)
    assert deal is not None and deal.is_scam_flag
    expected = 500.0 if deal.sell_channel == SellChannel.AMAZON else 300.0
    assert deal.est_resale == expected


def test_parts_only_rejected_by_default(settings):
    listing = make_listing(price=100.0, condition=Condition.FOR_PARTS)
    assert evaluate(listing, make_valuation(), settings) is None


def test_parts_only_allowed_when_enabled(settings):
    settings.allow_for_parts = True
    listing = make_listing(price=100.0, condition=Condition.FOR_PARTS)
    deal = evaluate(listing, make_valuation(), settings)
    assert deal is not None
    assert deal.is_scam_flag is False


def test_amazon_channel_selected_when_more_profitable(settings):
    listing = make_listing(price=150.0)
    val = make_valuation(resale_price=300.0, amazon_price=400.0, amazon_rank=1000)
    deal = evaluate(listing, val, settings)
    assert deal is not None
    assert deal.sell_channel == SellChannel.AMAZON


def test_amazon_rejected_when_rank_too_high(settings):
    listing = make_listing(price=150.0)
    val = make_valuation(
        resale_price=None, comp_count=0, amazon_price=400.0, amazon_rank=99_999
    )
    assert evaluate(listing, val, settings) is None


def test_no_valuation_data_returns_none(settings):
    listing = make_listing(price=150.0)
    val = make_valuation(resale_price=None, comp_count=0, confidence=0.0)
    assert evaluate(listing, val, settings) is None


def test_below_min_roi_rejected(settings):
    settings.min_roi = 200.0
    listing = make_listing(price=200.0)
    assert evaluate(listing, make_valuation(), settings) is None


def test_below_min_expected_profit_rejected(settings):
    """Passes raw profit and ROI, fails once risk and holding cost are priced."""
    settings.min_expected_profit = 95.0
    listing = make_listing(price=200.0)
    assert evaluate(listing, make_valuation(), settings) is None


# ------------------------------------------------------------------ sub-models
def test_p_sale_shrinks_towards_the_prior_on_thin_evidence(settings):
    thin = make_valuation(sell_through_pct=100.0, comp_count=1)
    thick = make_valuation(sell_through_pct=100.0, comp_count=100)
    base = settings.base_sell_probability

    # One observation barely moves off the prior; a hundred nearly replaces it.
    assert base < estimate_p_sale(thin, settings) < 0.85
    assert estimate_p_sale(thick, settings) > 0.95


def test_p_sale_falls_back_to_base_without_data(settings):
    assert estimate_p_sale(make_valuation(), settings) == settings.base_sell_probability


def test_score_rewards_confidence(settings):
    """Same money, better evidence — the confident deal must rank higher."""
    listing = make_listing(price=200.0)
    confident = evaluate(listing, make_valuation(confidence=0.9), settings)
    shaky = evaluate(listing, make_valuation(confidence=0.4), settings)
    assert confident.score > shaky.score


def test_score_is_bounded(settings):
    listing = make_listing(price=10.0)
    val = make_valuation(resale_price=5000.0, confidence=1.0, est_days_to_sell=1)
    from engine.deals import _candidate_channels

    result = max(
        _candidate_channels(listing, val, settings), key=lambda c: c.expected_profit
    )
    assert 0.0 <= score_deal(result, val, settings) <= 100.0
