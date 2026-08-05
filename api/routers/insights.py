"""Stats, health, settings and valuation lookups — everything the dashboard
needs that isn't a deal or a run.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.deps import get_app_settings, get_db
from api.schemas import HealthOut, SettingsOut, ValuationOut
from arb import overrides
from arb.config import Settings
from arb.db import Database
from arb.factory import build_ebay_client
from arb.runner import is_running
from arb.stats import compute_stats

router = APIRouter(prefix="/api", tags=["insights"])


@router.get("/health", response_model=HealthOut)
def health(
    db: Database = Depends(get_db), settings: Settings = Depends(get_app_settings)
) -> HealthOut:
    # A throwaway client purely to read the budget's persisted counters; it
    # makes no network calls.
    client = build_ebay_client(settings)
    used = client.budget.used if client else 0
    remaining = client.budget.remaining if client else 0

    return HealthOut(
        status="ok",
        ebay_configured=bool(settings.ebay_client_id and settings.ebay_client_secret),
        keepa_configured=bool(settings.keepa_api_key),
        insights_available=settings.ebay_has_insights,
        marketplace=settings.ebay_marketplace,
        api_calls_used=used,
        api_calls_remaining=remaining,
        daily_call_limit=settings.ebay_daily_call_limit,
        scan_running=is_running(),
        watched_queries=len(db.list_queries(enabled_only=True)),
        sold_observations=db.sold_count(),
        db_path=settings.db_path,
    )


@router.get("/stats")
def stats(db: Database = Depends(get_db), days: int = 30) -> dict:
    return compute_stats(db, days=days)


@router.get("/valuations/{product_key:path}", response_model=ValuationOut)
def get_valuation(product_key: str, db: Database = Depends(get_db)) -> ValuationOut:
    valuation = db.get_valuation_any_age(product_key)
    if valuation is None:
        raise HTTPException(404, "no valuation for that product key")
    return ValuationOut.of(valuation)


@router.get("/settings", response_model=SettingsOut)
def read_settings(settings: Settings = Depends(get_app_settings)) -> SettingsOut:
    return SettingsOut(
        values=overrides.current(settings), editable=sorted(overrides.TUNABLE)
    )


@router.patch("/settings", response_model=SettingsOut)
def update_settings(
    payload: dict, settings: Settings = Depends(get_app_settings)
) -> SettingsOut:
    applied = overrides.apply(settings, payload)
    if not applied:
        raise HTTPException(400, "no recognised settings in payload")
    return SettingsOut(
        values=overrides.current(settings), editable=sorted(overrides.TUNABLE)
    )


@router.post("/settings/reset", response_model=SettingsOut)
def reset_settings(settings: Settings = Depends(get_app_settings)) -> SettingsOut:
    overrides.reset(settings)
    return SettingsOut(
        values=overrides.current(settings), editable=sorted(overrides.TUNABLE)
    )
