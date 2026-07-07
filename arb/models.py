"""Pydantic domain models shared across the whole pipeline.

Sources emit `Listing`. The oracle produces `Valuation`. The deal engine
combines the two into a `Deal`. Nothing downstream of a source ever needs to
know which site a listing came from.
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


class Valuation(BaseModel):
    model_number: str
    ebay_sold_median: float | None = None
    ebay_sold_count: int = 0
    amazon_price: float | None = None
    amazon_rank: int | None = None  # sell rank — low = sells fast
    updated_at: datetime = Field(default_factory=_utcnow)


class Deal(BaseModel):
    listing_id: str
    buy_cost: float
    est_resale: float
    est_fees: float
    est_profit: float
    margin_pct: float
    roi_pct: float
    sell_channel: SellChannel
    is_scam_flag: bool = False
    flagged_at: datetime = Field(default_factory=_utcnow)
