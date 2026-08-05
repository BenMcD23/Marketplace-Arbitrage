from __future__ import annotations

from arb.models import SellChannel
from engine.fees import amazon_fees, breakeven_buy_price, ebay_fees, profit_at


def test_ebay_fee_lines_add_up(settings):
    fees = ebay_fees(350.0, settings)
    assert fees.final_value_fee == 350.0 * 0.128
    assert fees.fixed_fee == 0.30
    assert fees.postage == 3.50
    assert fees.packaging == 2.50
    assert fees.total == round(350.0 * 0.128 + 0.30 + 3.50 + 2.50, 2)


def test_ebay_fee_cap_is_applied(settings):
    settings.ebay_fvf_cap = 20.0
    fees = ebay_fees(1000.0, settings)
    assert fees.final_value_fee == 20.0


def test_ad_rate_costs_money(settings):
    without = ebay_fees(350.0, settings).total
    settings.ebay_ad_rate_pct = 5.0
    with_ads = ebay_fees(350.0, settings).total
    assert with_ads == round(without + 350.0 * 0.05, 2)


def test_amazon_fees_use_referral_and_fba(settings):
    fees = amazon_fees(400.0, settings)
    assert fees.referral_fee == 400.0 * 0.08
    assert fees.fulfilment_fee == 3.0
    # Amazon's FBA fee covers fulfilment, so no separate postage line.
    assert fees.postage == 0.0


def test_profit_at_computes_roi_and_margin(settings):
    result = profit_at(350.0, 200.0, SellChannel.EBAY, settings)
    assert result.profit == 98.90
    assert result.roi_pct == 49.45
    assert result.margin_pct == 28.26


def test_breakeven_is_the_price_that_yields_zero_profit(settings):
    breakeven = breakeven_buy_price(350.0, SellChannel.EBAY, settings)
    result = profit_at(350.0, breakeven, SellChannel.EBAY, settings)
    assert abs(result.profit) < 0.01


def test_as_dict_is_json_ready(settings):
    payload = ebay_fees(350.0, settings).as_dict()
    assert set(payload) == {
        "final_value_fee",
        "fixed_fee",
        "payment_fee",
        "ad_fee",
        "referral_fee",
        "fulfilment_fee",
        "postage",
        "packaging",
        "total",
    }
    assert all(isinstance(v, float) for v in payload.values())
