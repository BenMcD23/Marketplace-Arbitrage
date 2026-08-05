"""Shared FastAPI dependencies.

The database is a single long-lived SQLite connection rather than a per-request
one. SQLite is happy with that (the connection is opened with
`check_same_thread=False` and WAL journaling), it keeps the scheduler, the API
and a background scan all looking at the same file, and it avoids paying
connection setup on every request for a single-user tool.
"""

from __future__ import annotations

from functools import lru_cache

from arb.config import Settings, get_settings
from arb.db import Database
from arb.overrides import load as load_overrides


@lru_cache
def get_app_settings() -> Settings:
    """Settings with any dashboard overrides applied."""
    settings = get_settings()
    load_overrides(settings)
    return settings


@lru_cache
def get_db() -> Database:
    settings = get_app_settings()
    settings.ensure_db_dir()
    return Database(settings.db_path)


def close_db() -> None:
    if get_db.cache_info().currsize:
        get_db().close()
        get_db.cache_clear()
