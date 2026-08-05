"""One place that knows how to execute a scan.

The CLI, the scheduler and the API's "Run scan" button all go through here, so
a run means exactly the same thing however it was triggered — same wiring, same
run record, same cleanup.

A module-level lock prevents two scans overlapping. Overlapping runs would
double-spend the daily API budget and race each other through the `seen` table,
and there is never a good reason to want one.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from arb.config import Settings, get_settings
from arb.db import Database
from arb.factory import build_alerter, build_ebay_client, build_oracle, build_sources
from arb.logging_conf import get_logger
from arb.models import Run, RunStatus
from arb.pipeline import Pipeline, RunStats

log = get_logger("runner")

_run_lock = asyncio.Lock()


def is_running() -> bool:
    return _run_lock.locked()


async def run_once(
    settings: Settings | None = None,
    db: Database | None = None,
    dry_run: bool = False,
) -> RunStats:
    """Execute a single scan. Raises RuntimeError if one is already in flight."""
    if _run_lock.locked():
        raise RuntimeError("a scan is already running")

    async with _run_lock:
        settings = settings or get_settings()
        if dry_run:
            settings.dry_run = True
        settings.ensure_db_dir()

        owns_db = db is None
        db = db or Database(settings.db_path)

        run_id = db.start_run()
        oracle = None
        alerter = None

        # Wiring lives inside the try as well as the run itself. A failure while
        # building sources would otherwise leave the run row stuck on "running"
        # forever, and the dashboard polling it would spin indefinitely.
        try:
            ebay = build_ebay_client(settings)
            oracle = build_oracle(settings, db, ebay=ebay)
            alerter = build_alerter(settings)
            sources = build_sources(settings, db, ebay=ebay)

            if not sources:
                log.warning("no_sources_enabled")

            pipeline = Pipeline(settings, db, oracle, alerter)
            stats = await pipeline.run(sources, run_id=run_id)
        except Exception as exc:
            db.finish_run(
                Run(
                    id=run_id,
                    status=RunStatus.FAILED,
                    finished_at=datetime.now(UTC),
                    error=str(exc),
                )
            )
            log.error("run_failed", error=str(exc))
            raise
        finally:
            if oracle is not None:
                await oracle.aclose()
            if alerter is not None:
                await alerter.aclose()
            if owns_db:
                db.close()

        return stats
