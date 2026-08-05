"""Runtime-tunable settings, editable from the dashboard.

Thresholds and fee rates are the settings you actually want to fiddle with —
you tighten the ROI floor, watch the deal list get shorter, and tighten it
again. Making that a redeploy is how it stops happening.

So a whitelisted subset of `Settings` can be overridden at runtime and is
persisted to a small JSON file next to the database. Everything not on the
whitelist — API credentials above all — stays in the environment where it
belongs and can never be written or read back through the API.
"""

from __future__ import annotations

import json
from pathlib import Path

from arb.config import Settings
from arb.logging_conf import get_logger

log = get_logger("overrides")

#: Settings the UI is allowed to change. Anything absent is immutable at
#: runtime, and no secret is ever on this list.
TUNABLE: dict[str, type] = {
    # deal thresholds
    "min_profit": float,
    "min_roi": float,
    "min_expected_profit": float,
    "min_confidence": float,
    "min_score": float,
    "tgtbt_ratio": float,
    "allow_for_parts": bool,
    "max_amazon_rank": int,
    # valuation model
    "min_comps": int,
    "min_sold_comps": int,
    "min_comp_relevance": float,
    "min_condition_comps": int,
    "active_to_sold_ratio": float,
    "used_to_new_ratio": float,
    "sold_window_days": int,
    "confidence_target_comps": int,
    "confidence_max_cv": float,
    "valuation_ttl_hours": int,
    "comp_search_limit": int,
    # liquidity / risk
    "base_sell_probability": float,
    "default_days_to_sell": int,
    "capital_annual_cost_pct": float,
    # fees
    "ebay_fvf_pct": float,
    "ebay_fixed_fee": float,
    "ebay_payment_pct": float,
    "ebay_ad_rate_pct": float,
    "postage_cost": float,
    "packaging_cost": float,
    "amazon_referral_pct": float,
    "amazon_fba_fee": float,
    # scanning
    "ebay_limit": int,
    "comp_stale_hours": int,
    "sold_sweep_max_checks": int,
    "ebay_daily_call_limit": int,
}


def overrides_path(settings: Settings) -> Path:
    return settings.db_file.parent / "settings_override.json"


def load(settings: Settings) -> Settings:
    """Apply any persisted overrides onto a Settings instance, in place."""
    path = overrides_path(settings)
    if not path.exists():
        return settings
    try:
        stored = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("overrides_unreadable", path=str(path), error=str(exc))
        return settings
    applied = apply(settings, stored, persist=False)
    if applied:
        log.info("overrides_loaded", count=len(applied))
    return settings


def apply(settings: Settings, values: dict, persist: bool = True) -> dict:
    """Validate and apply `values`. Returns the subset actually applied.

    Unknown keys are ignored rather than rejected, so a stored override file
    written by an older version cannot stop the app booting.
    """
    applied: dict = {}
    for key, raw in values.items():
        caster = TUNABLE.get(key)
        if caster is None:
            continue
        try:
            value = bool(raw) if caster is bool else caster(raw)
        except (TypeError, ValueError):
            log.warning("override_bad_value", key=key, value=raw)
            continue
        try:
            setattr(settings, key, value)
        except Exception as exc:  # pydantic validation on assignment
            log.warning("override_rejected", key=key, value=value, error=str(exc))
            continue
        applied[key] = value

    if persist and applied:
        save(settings, applied)
    return applied


def save(settings: Settings, values: dict) -> None:
    """Merge `values` into the persisted override file."""
    path = overrides_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            existing = {}
    existing.update(values)
    path.write_text(json.dumps(existing, indent=2, sort_keys=True))


def current(settings: Settings) -> dict:
    """The current value of every tunable setting."""
    return {key: getattr(settings, key) for key in TUNABLE}


def reset(settings: Settings) -> None:
    """Drop all overrides. Values revert to the environment on next start."""
    path = overrides_path(settings)
    if path.exists():
        path.unlink()
