"""Tests for the shared scan runner used by the CLI, scheduler and API."""

from __future__ import annotations

import asyncio

import pytest

from arb import runner
from arb.models import RunStatus


@pytest.mark.asyncio
async def test_run_once_records_a_completed_run(settings, db, monkeypatch):
    monkeypatch.setattr('arb.runner.build_sources', lambda *a, **k: [])

    stats = await runner.run_once(settings=settings, db=db)

    assert stats.listings_scanned == 0
    runs = db.list_runs()
    assert len(runs) == 1
    assert runs[0].status == RunStatus.COMPLETE
    assert runs[0].finished_at is not None


@pytest.mark.asyncio
async def test_a_failing_run_is_recorded_as_failed(settings, db, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("wiring is broken")

    monkeypatch.setattr('arb.runner.build_sources', explode)

    with pytest.raises(RuntimeError):
        await runner.run_once(settings=settings, db=db)

    run = db.list_runs()[0]
    assert run.status == RunStatus.FAILED
    assert "wiring is broken" in run.error


@pytest.mark.asyncio
async def test_concurrent_scans_are_refused(settings, db, monkeypatch):
    """Two overlapping scans would double-spend the daily API budget."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_run(self, sources, run_id=None):
        started.set()
        await release.wait()
        from arb.pipeline import RunStats

        return RunStats()

    monkeypatch.setattr('arb.runner.build_sources', lambda *a, **k: [])
    monkeypatch.setattr('arb.pipeline.Pipeline.run', slow_run)

    first = asyncio.create_task(runner.run_once(settings=settings, db=db))
    await started.wait()

    assert runner.is_running() is True
    with pytest.raises(RuntimeError, match="already running"):
        await runner.run_once(settings=settings, db=db)

    release.set()
    await first
    assert runner.is_running() is False
