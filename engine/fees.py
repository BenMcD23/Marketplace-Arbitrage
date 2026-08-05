"""Selling fees, itemised.

The deal engine is only as honest as this module. Optimistic fees turn losers
into "deals", and the failure is silent — you find out weeks later when the
payouts do not match the spreadsheet. So fees are broken out line by line
rather than folded into one number, which means the UI can show a seller
exactly where a £102 profit went and they can check it against a real payout.

Every rate comes from config. The defaults are UK eBay at the time of writing;
they are a starting point, not a source of truth. Replace them with whatever
your own account actually charges.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from arb.config import Settings
from arb.models import SellChannel


@dataclass
class FeeBreakdown:
    """Itemised selling costs for one channel, all in £."""

    channel: SellChannel
    final_value_fee: float = 0.0
    fixed_fee: float = 0.0
    payment_fee: float = 0.0
    ad_fee: float = 0.0
    referral_fee: float = 0.0
    fulfilment_fee: float = 0.0
    postage: float = 0.0
    packaging: float = 0.0

    @property
    def total(self) -> float:
        return round(
            self.final_value_fee
            + self.fixed_fee
            + self.payment_fee
            + self.ad_fee
            + self.referral_fee
            + self.fulfilment_fee
            + self.postage
            + self.packaging,
            2,
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "final_value_fee": round(self.final_value_fee, 2),
            "fixed_fee": round(self.fixed_fee, 2),
            "payment_fee": round(self.payment_fee, 2),
            "ad_fee": round(self.ad_fee, 2),
            "referral_fee": round(self.referral_fee, 2),
            "fulfilment_fee": round(self.fulfilment_fee, 2),
            "postage": round(self.postage, 2),
            "packaging": round(self.packaging, 2),
            "total": self.total,
        }


def ebay_fees(resale: float, s: Settings) -> FeeBreakdown:
    """eBay selling costs on a `resale` sale price.

    Note the fee base: eBay charges its final value fee on the **total** the
    buyer pays, postage included. Modelling it on the item price alone
    understates the fee on every postage-paid sale.
    """
    fvf = resale * (s.ebay_fvf_pct / 100.0)
    if s.ebay_fvf_cap is not None:
        fvf = min(fvf, s.ebay_fvf_cap)
    return FeeBreakdown(
        channel=SellChannel.EBAY,
        final_value_fee=fvf,
        fixed_fee=s.ebay_fixed_fee,
        payment_fee=resale * (s.ebay_payment_pct / 100.0),
        ad_fee=resale * (s.ebay_ad_rate_pct / 100.0),
        postage=s.postage_cost,
        packaging=s.packaging_cost,
    )


def amazon_fees(resale: float, s: Settings) -> FeeBreakdown:
    """Amazon selling costs. FBA fulfilment covers the postage leg."""
    return FeeBreakdown(
        channel=SellChannel.AMAZON,
        referral_fee=resale * (s.amazon_referral_pct / 100.0),
        fulfilment_fee=s.amazon_fba_fee,
        packaging=s.packaging_cost,
    )


def fees_for(channel: SellChannel, resale: float, s: Settings) -> FeeBreakdown:
    return ebay_fees(resale, s) if channel == SellChannel.EBAY else amazon_fees(resale, s)


@dataclass
class ProfitBreakdown:
    """What a sale at `resale` actually leaves you with."""

    resale: float
    buy_cost: float
    fees: FeeBreakdown
    profit: float = 0.0
    roi_pct: float = 0.0
    margin_pct: float = 0.0
    notes: list[str] = field(default_factory=list)


def profit_at(resale: float, buy_cost: float, channel: SellChannel, s: Settings) -> ProfitBreakdown:
    """Profit, ROI and margin for selling at `resale` on `channel`."""
    fees = fees_for(channel, resale, s)
    profit = round(resale - buy_cost - fees.total, 2)
    return ProfitBreakdown(
        resale=round(resale, 2),
        buy_cost=round(buy_cost, 2),
        fees=fees,
        profit=profit,
        roi_pct=round(profit / buy_cost * 100, 2) if buy_cost > 0 else 0.0,
        margin_pct=round(profit / resale * 100, 2) if resale > 0 else 0.0,
    )


def breakeven_buy_price(resale: float, channel: SellChannel, s: Settings) -> float:
    """The most you could pay and still break even. Useful for offer-making."""
    fees = fees_for(channel, resale, s)
    return round(resale - fees.total, 2)
