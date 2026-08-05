"""Managing the searches the scanner runs.

The watch list is the single highest-leverage control in the whole system:
change what you search for and everything downstream changes with it. Keeping
it editable at runtime is why it lives in the database rather than in `.env`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.deps import get_db
from api.schemas import WatchQueryIn, WatchQueryPatch
from arb.db import Database
from arb.models import WatchQuery

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


@router.get("", response_model=list[WatchQuery])
def list_queries(db: Database = Depends(get_db), enabled_only: bool = False) -> list[WatchQuery]:
    return db.list_queries(enabled_only=enabled_only)


@router.post("", response_model=WatchQuery, status_code=201)
def add_query(payload: WatchQueryIn, db: Database = Depends(get_db)) -> WatchQuery:
    term = payload.query.strip()
    if not term:
        raise HTTPException(400, "query cannot be empty")
    return db.add_query(WatchQuery(**{**payload.model_dump(), "query": term}))


@router.patch("/{query_id}", response_model=WatchQuery)
def update_query(
    query_id: int, payload: WatchQueryPatch, db: Database = Depends(get_db)
) -> WatchQuery:
    updated = db.update_query(query_id, **payload.model_dump(exclude_unset=True))
    if updated is None:
        raise HTTPException(404, "query not found")
    return updated


@router.delete("/{query_id}", status_code=204)
def delete_query(query_id: int, db: Database = Depends(get_db)) -> None:
    if not db.delete_query(query_id):
        raise HTTPException(404, "query not found")
