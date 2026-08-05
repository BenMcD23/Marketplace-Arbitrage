"""Pydantic domain models shared across the whole pipeline.

Sources emit `Listing`. The oracle produces `Valuation`. The deal engine
combines the two into a `Deal`. Nothing downstream of a source ever needs to
know which site a listing came from.

Valuations and deals both carry their own uncertainty. A resale estimate built
from four scruffy comps and one built from forty tight ones are not the same
number even when they happen to be equal, and the deal engine is allowed to
know the difference.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Condition(str, Enum):
    NEW = "new"
    USED = "used"
    FOR_PARTS = "for_parts"
    UNKNOWN = "unknown"


class SellChannel(str, Enum):
    EBAY = "ebay"
    AMAZON = "amazon"


class PriceBasis(str, Enum):
    """Where a resale estimate came from, best first.

    SOLD is our own observed sale prices (see `oracle.sold_tracker`) and is the
    only basis that reflects what buyers actually paid. ACTIVE is asking prices
    discounted towards a realistic sale price. AMAZON comes from Keepa when a
    key is configured — optional, since it is a paid service.
    """

    SOLD = "sold"
    #: CeX's used-retail price, discounted to a private-sale level. Weaker than
    #: observed sales but available from the very first scan, which is what
    #: makes it useful before the sold history has built up.
    CEX = "cex"
    ACTIVE = "active"
    AMAZON = "amazon"
    NONE = "none"


def make_listing_id(source: str, source_listing_id: str) -> str:
    """Stable id = short hash of source + source listing id (dedup key)."""
    digest = hashlib.sha256(f"{source}:{source_listing_id}".encode()).hexdigest()
    return digest[:16]


class Listing(BaseModel):
    id: str = ""
    source: str
    source_listing_id: str
    title: str
    model_number: str | None = None
    brand: str | None = None
    price: float
    shipping: float = 0.0
    condition: Condition = Condition.UNKNOWN
    url: str
    image_url: str | None = None
    location: str | None = None
    seen_at: datetime = Field(default_factory=_utcnow)

    def model_post_init(self, __context) -> None:  # noqa: D401
        # `id` is derived from source + source_listing_id so the same real-world
        # listing always hashes to the same dedup key.
        if not self.id:
            self.id = make_listing_id(self.source, self.source_listing_id)

    @property
    def buy_cost(self) -> float:
        return round(self.price + self.shipping, 2)


class CompRef(BaseModel):
    """A comp retained in a valuation, kept so the number can be audited."""

    title: str
    price: float
    condition: Condition = Condition.UNKNOWN
    url: str | None = None
    sold: bool = False
    relevance: float = 0.0


class Valuation(BaseModel):
    """What something is worth, and how much to believe that."""

    product_key: str
    resale_price: float | None = None
    basis: PriceBasis = PriceBasis.NONE

    # --- sample quality ---
    comp_count: int = 0
    comps_rejected: int = 0
    #: Robust dispersion (sigma / median). Above ~0.35 the market disagrees
    #: with itself and the point estimate deserves little weight.
    dispersion_cv: float = 0.0
    price_p10: float | None = None
    price_p90: float | None = None

    #: 0..1. Folds sample size, dispersion, basis and recency into one number.
    confidence: float = 0.0

    # --- liquidity ---
    #: Observed sold / (sold + active) for this product key, when known.
    sell_through_pct: float | None = None
    est_days_to_sell: int | None = None

    # --- CeX (free, no key) -------------------------------------------
    #: What CeX charges for the used item — a real used-retail price.
    cex_sell_price: float | None = None
    #: What CeX will pay you in cash. Not a prediction: an offer, and therefore
    #: a floor under the downside rather than another estimate.
    cex_cash_price: float | None = None
    #: The CeX product this was matched against, so the figure can be audited.
    cex_match: str | None = None

    # --- optional Amazon leg (Keepa, paid — off unless a key is configured) ---
    amazon_price: float | None = None
    amazon_rank: int | None = None

    #: A handful of retained comps, for the UI's "why is it worth this" panel.
    sample: list[CompRef] = Field(default_factory=list)
    #: Counts keyed by rejection reason, e.g. {"accessory_or_lot": 12}.
    reject_reasons: dict[str, int] = Field(default_factory=dict)

    updated_at: datetime = Field(default_factory=_utcnow)

    @property
    def has_price(self) -> bool:
        return self.resale_price is not None and self.resale_price > 0


class Deal(BaseModel):
    """A listing worth buying — or at least worth a human's attention."""

    listing_id: str
    buy_cost: float
    est_resale: float
    est_fees: float
    est_profit: float
    margin_pct: float
    roi_pct: float
    sell_channel: SellChannel

    # --- risk adjustment ---
    #: Probability the item sells inside the modelled horizon.
    p_sale: float = 1.0
    est_days_to_sell: int | None = None
    #: Cost of capital tied up while it sits unsold.
    holding_cost: float = 0.0
    #: p_sale-weighted profit net of holding cost. The number to rank on.
    expected_profit: float = 0.0
    #: Inherited from the valuation, 0..1.
    confidence: float = 0.0
    #: 0..100 composite ranking score.
    score: float = 0.0
    #: Downside estimate: profit if resale lands at the 10th percentile — or the
    #: guaranteed floor below, whichever is better, since you would take the
    #: better of the two in practice.
    worst_case_profit: float = 0.0
    #: Profit from selling to CeX at their cash offer. Not an estimate — an
    #: amount you could walk in and collect. None when CeX has no quote.
    floor_profit: float | None = None

    is_scam_flag: bool = False
    #: Human-readable notes explaining the verdict, shown in the UI.
    reasons: list[str] = Field(default_factory=list)
    flagged_at: datetime = Field(default_factory=_utcnow)


class SoldObservation(BaseModel):
    """A listing we watched disappear from search and confirmed as ended.

    This is how the system builds its own sold-price history without paying for
    eBay's Marketplace Insights API.
    """

    item_id: str
    product_key: str
    title: str
    price: float
    condition: Condition = Condition.UNKNOWN
    first_seen_at: datetime
    sold_at: datetime = Field(default_factory=_utcnow)
    #: Days between first sighting and disappearance — the velocity signal.
    days_listed: float = 0.0


class WatchQuery(BaseModel):
    """A search term the pipeline scans on every run."""

    id: int | None = None
    query: str
    category_id: str | None = None
    max_price: float | None = None
    min_price: float | None = None
    enabled: bool = True
    created_at: datetime = Field(default_factory=_utcnow)
    last_run_at: datetime | None = None


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class Run(BaseModel):
    """One execution of the scan pipeline."""

    id: int | None = None
    status: RunStatus = RunStatus.RUNNING
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime | None = None
    listings_scanned: int = 0
    new_listings: int = 0
    valuations_fetched: int = 0
    deals_found: int = 0
    scam_flags: int = 0
    sold_observed: int = 0
    api_calls: int = 0
    error: str | None = None
    by_source: dict[str, int] = Field(default_factory=dict)
