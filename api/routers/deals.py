"""Deal browsing endpoints.

Sorting defaults to `score` rather than raw profit. A £900 paper profit derived
from four disagreeing comps is not a better lead than a well-evidenced £120, and
the default ordering should not pretend otherwise.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_app_settings, get_db
from api.schemas import DealDetailOut, DealOut, DealPage, FeeLineOut, ValuationOut
from arb.config import Settings
from arb.db import Database, _row_to_deal, _row_to_listing
from engine.fees import breakeven_buy_price, fees_for

router = APIRouter(prefix="/api/deals", tags=["deals"])

SORTABLE = {
    "score": "d.score",
    "expected_profit": "d.expected_profit",
    "profit": "d.est_profit",
    "roi": "d.roi_pct",
    "confidence": "d.confidence",
    "flagged_at": "d.flagged_at",
    "buy_cost": "d.buy_cost",
}


@router.get("", response_model=DealPage)
def list_deals(
    db: Database = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sort: str = Query("score"),
    order: Literal["asc", "desc"] = "desc",
    min_score: float | None = None,
    min_profit: float | None = None,
    min_confidence: float | None = None,
    channel: str | None = None,
    source: str | None = None,
    search: str | None = None,
    include_scams: bool = False,
    days: int | None = None,
) -> DealPage:
    if sort not in SORTABLE:
        raise HTTPException(400, f"sort must be one of {sorted(SORTABLE)}")

    where = ["1=1"]
    params: list = []

    if not include_scams:
        where.append("d.is_scam_flag = 0")
    if min_score is not None:
        where.append("d.score >= ?")
        params.append(min_score)
    if min_profit is not None:
        where.append("d.est_profit >= ?")
        params.append(min_profit)
    if min_confidence is not None:
        where.append("d.confidence >= ?")
        params.append(min_confidence)
    if channel:
        where.append("d.sell_channel = ?")
        params.append(channel)
    if source:
        where.append("l.source = ?")
        params.append(source)
    if search:
        where.append("l.title LIKE ?")
        params.append(f"%{search}%")
    if days is not None:
        where.append("d.flagged_at >= datetime('now', ?)")
        params.append(f"-{days} days")

    clause = " AND ".join(where)
    total = db.query(
        f"SELECT COUNT(*) AS n FROM deals d JOIN listings l ON l.id = d.listing_id WHERE {clause}",
        tuple(params),
    )[0]["n"]

    rows = db.query(
        f"""
        SELECT d.*, l.id AS l_id, l.source AS l_source, l.source_listing_id AS l_source_listing_id,
               l.title AS l_title, l.model_number AS l_model_number, l.brand AS l_brand,
               l.price AS l_price, l.shipping AS l_shipping, l.condition AS l_condition,
               l.url AS l_url, l.image_url AS l_image_url, l.location AS l_location,
               l.seen_at AS l_seen_at
        FROM deals d JOIN listings l ON l.id = d.listing_id
        WHERE {clause}
        ORDER BY {SORTABLE[sort]} {order.upper()}
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    )

    items = [DealOut.of(_row_to_deal(r), _listing_from_joined(r)) for r in rows]
    return DealPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/{listing_id}", response_model=DealDetailOut)
def get_deal(
    listing_id: str,
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> DealDetailOut:
    deal = db.get_deal(listing_id)
    if deal is None:
        raise HTTPException(404, "deal not found")
    listing = db.get_listing(listing_id)

    valuation = None
    if listing is not None:
        from sources.normalise import product_key

        key = product_key(listing.title, listing.brand, listing.model_number)
        valuation = db.get_valuation_any_age(key)

    fees = fees_for(deal.sell_channel, deal.est_resale, settings)
    detail = DealDetailOut.of(deal, listing)
    detail.valuation = ValuationOut.of(valuation) if valuation else None
    detail.fee_breakdown = FeeLineOut(**fees.as_dict())
    detail.breakeven_buy_price = breakeven_buy_price(
        deal.est_resale, deal.sell_channel, settings
    )
    return detail


def _listing_from_joined(row):
    """Rebuild a Listing from the l_-prefixed columns of a joined row."""
    return _row_to_listing(
        {
            "id": row["l_id"],
            "source": row["l_source"],
            "source_listing_id": row["l_source_listing_id"],
            "title": row["l_title"],
            "model_number": row["l_model_number"],
            "brand": row["l_brand"],
            "price": row["l_price"],
            "shipping": row["l_shipping"],
            "condition": row["l_condition"],
            "url": row["l_url"],
            "image_url": row["l_image_url"],
            "location": row["l_location"],
            "seen_at": row["l_seen_at"],
        }
    )
