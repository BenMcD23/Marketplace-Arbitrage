"""API response shapes.

The database stores deals, listings and valuations separately because that is
the right shape for writing. The UI always wants them together — a deal is
meaningless without the item it refers to and the evidence behind its price — so
the API joins them here rather than making the client do three round trips and
stitch the result.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from arb.models import (
    CompRef,
    Condition,
    Deal,
    Listing,
    PriceBasis,
    SellChannel,
    Valuation,
)


class ListingOut(BaseModel):
    id: str
    source: str
    title: str
    brand: str | None = None
    model_number: str | None = None
    price: float
    shipping: float
    buy_cost: float
    condition: Condition
    url: str
    image_url: str | None = None
    location: str | None = None
    seen_at: datetime

    @classmethod
    def of(cls, listing: Listing) -> ListingOut:
        return cls(**listing.model_dump(), buy_cost=listing.buy_cost)


class ValuationOut(BaseModel):
    product_key: str
    resale_price: float | None
    basis: PriceBasis
    comp_count: int
    comps_rejected: int
    dispersion_cv: float
    price_p10: float | None
    price_p90: float | None
    confidence: float
    sell_through_pct: float | None
    est_days_to_sell: int | None
    cex_sell_price: float | None
    cex_cash_price: float | None
    cex_match: str | None
    amazon_price: float | None
    amazon_rank: int | None
    sample: list[CompRef]
    reject_reasons: dict[str, int]
    updated_at: datetime

    @classmethod
    def of(cls, valuation: Valuation) -> ValuationOut:
        return cls(**valuation.model_dump())


class DealOut(BaseModel):
    """A deal with the listing it refers to — the row shape of the deals table."""

    listing_id: str
    buy_cost: float
    est_resale: float
    est_fees: float
    est_profit: float
    margin_pct: float
    roi_pct: float
    sell_channel: SellChannel
    p_sale: float
    est_days_to_sell: int | None
    holding_cost: float
    expected_profit: float
    confidence: float
    score: float
    worst_case_profit: float
    floor_profit: float | None
    is_scam_flag: bool
    reasons: list[str]
    flagged_at: datetime
    listing: ListingOut | None = None

    @classmethod
    def of(cls, deal: Deal, listing: Listing | None = None) -> DealOut:
        return cls(
            **deal.model_dump(),
            listing=ListingOut.of(listing) if listing else None,
        )


class FeeLineOut(BaseModel):
    final_value_fee: float
    fixed_fee: float
    payment_fee: float
    ad_fee: float
    referral_fee: float
    fulfilment_fee: float
    postage: float
    packaging: float
    total: float


class DealDetailOut(DealOut):
    """Everything needed to justify a deal on one screen."""

    valuation: ValuationOut | None = None
    fee_breakdown: FeeLineOut | None = None
    breakeven_buy_price: float | None = None


class DealPage(BaseModel):
    items: list[DealOut]
    total: int
    limit: int
    offset: int


class RunOut(BaseModel):
    id: int | None
    status: str
    started_at: datetime
    finished_at: datetime | None
    listings_scanned: int
    new_listings: int
    valuations_fetched: int
    deals_found: int
    scam_flags: int
    sold_observed: int
    api_calls: int
    error: str | None
    by_source: dict[str, int]


class WatchQueryIn(BaseModel):
    query: str
    category_id: str | None = None
    max_price: float | None = None
    min_price: float | None = None
    enabled: bool = True


class WatchQueryPatch(BaseModel):
    query: str | None = None
    category_id: str | None = None
    max_price: float | None = None
    min_price: float | None = None
    enabled: bool | None = None


class HealthOut(BaseModel):
    status: str
    ebay_configured: bool
    keepa_configured: bool
    insights_available: bool
    marketplace: str
    api_calls_used: int
    api_calls_remaining: int
    daily_call_limit: int
    scan_running: bool
    watched_queries: int
    sold_observations: int
    db_path: str


class SettingsOut(BaseModel):
    values: dict
    editable: list[str]
