"""Pydantic domain models shared across the whole pipeline.

Sources emit `Listing`. The oracle produces `Valuation`. The deal engine
combines the two into a `Deal`. Nothing downstream of a source ever needs to
know which site a listing came from.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
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


class ListingType(str, Enum):
    """How the item is being sold. Auctions price off the current bid."""

    FIXED_PRICE = "fixed_price"
    AUCTION = "auction"
    AUCTION_WITH_BIN = "auction_with_bin"


class DeliveryKind(str, Enum):
    """Where a listing's delivery cost came from.

    `FREE` and `FIXED` are quoted by the seller and exact. `ESTIMATED` means the
    site quotes delivery only at checkout, so the cost is a configured
    assumption. `COLLECTION` means no delivery is offered at all.
    """

    FREE = "free"
    FIXED = "fixed"
    ESTIMATED = "estimated"
    COLLECTION = "collection"

    @property
    def is_exact(self) -> bool:
        return self in (DeliveryKind.FREE, DeliveryKind.FIXED, DeliveryKind.COLLECTION)


def make_listing_id(source: str, source_listing_id: str) -> str:
    """Stable id = short hash of source + source listing id (dedup key)."""
    digest = hashlib.sha256(f"{source}:{source_listing_id}".encode()).hexdigest()
    return digest[:16]


class Listing(BaseModel):
    id: str = ""
    source: str
    source_listing_id: str
    title: str
    # Extra free text (subtitle / short description) that specs are often
    # buried in when the title has run out of room.
    description: str | None = None
    model_number: str | None = None
    brand: str | None = None
    #: For auctions this is the *current bid*, never a starting-price placeholder.
    price: float
    shipping: float = 0.0
    delivery_kind: DeliveryKind = DeliveryKind.FIXED
    listing_type: ListingType = ListingType.FIXED_PRICE
    best_offer: bool = False
    bid_count: int | None = None
    ends_at: datetime | None = None
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
        """Total landed cost: item price + delivery. Never compare on price alone."""
        return round(self.price + self.shipping, 2)

    @property
    def is_auction(self) -> bool:
        return self.listing_type in (ListingType.AUCTION, ListingType.AUCTION_WITH_BIN)

    def time_remaining(self, now: datetime | None = None) -> timedelta | None:
        """Time left on an auction, or None if it has no end date / has ended."""
        if self.ends_at is None:
            return None
        remaining = self.ends_at - (now or _utcnow())
        return remaining if remaining.total_seconds() > 0 else None


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
