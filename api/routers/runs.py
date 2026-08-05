"""Scan runs: history, and triggering one from the dashboard.

A scan takes minutes, so the trigger endpoint starts it as a background task
and returns immediately with the run id. The UI polls `GET /api/runs/{id}` for
progress. `arb.runner` refuses concurrent scans, and this endpoint surfaces that
refusal as a 409 rather than silently queuing a second one.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from api.deps import get_app_settings, get_db
from api.schemas import RunOut
from arb.config import Settings
from arb.db import Database
from arb.logging_conf import get_logger
from arb.runner import is_running, run_once

log = get_logger("api.runs")

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("", response_model=list[RunOut])
def list_runs(db: Database = Depends(get_db), limit: int = 20) -> list[RunOut]:
    return [RunOut(**run.model_dump()) for run in db.list_runs(limit=limit)]


@router.get("/{run_id}", response_model=RunOut)
def get_run(run_id: int, db: Database = Depends(get_db)) -> RunOut:
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return RunOut(**run.model_dump())


@router.post("", response_model=RunOut, status_code=202)
async def trigger_run(
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> RunOut:
    if is_running():
        raise HTTPException(409, "a scan is already running")

    async def _go() -> None:
        try:
            await run_once(settings=settings, db=db)
        except Exception as exc:
            log.error("triggered_run_failed", error=str(exc))

    # Fire and forget: the run records its own progress and outcome in the
    # `runs` table, which is what the UI polls.
    task = asyncio.create_task(_go())
    _background.add(task)
    task.add_done_callback(_background.discard)

    # Give the run a moment to insert its row so the client gets a real id.
    await asyncio.sleep(0.15)
    runs = db.list_runs(limit=1)
    if not runs:
        raise HTTPException(500, "run did not start")
    return RunOut(**runs[0].model_dump())


#: Strong references to in-flight tasks, so they are not garbage collected
#: mid-run — asyncio only keeps weak references to running tasks.
_background: set[asyncio.Task] = set()
