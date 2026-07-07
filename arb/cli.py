"""Command-line entrypoint.

    arb run          run the pipeline once over all enabled sources
    arb run --dry    scan + evaluate but never send alerts (threshold tuning)
    arb schedule     run continuously on a configurable interval (APScheduler)
    arb stats        print performance stats from the deals table
"""

from __future__ import annotations

import argparse
import asyncio
import json

from arb.config import get_settings
from arb.db import Database
from arb.factory import build_alerter, build_oracle, build_sources
from arb.logging_conf import configure_logging, get_logger
from arb.pipeline import Pipeline
from arb.stats import compute_stats, format_stats

log = get_logger("cli")


async def _run_once(dry_run: bool = False) -> dict:
    settings = get_settings()
    if dry_run:
        settings.dry_run = True
    settings.ensure_db_dir()

    db = Database(settings.db_path)
    oracle = build_oracle(settings, db)
    alerter = build_alerter(settings)
    sources = build_sources(settings)

    if not sources:
        log.warning("no_sources_enabled")

    pipeline = Pipeline(settings, db, oracle, alerter)
    try:
        stats = await pipeline.run(sources)
    finally:
        await oracle.aclose()
        await alerter.aclose()
        db.close()
    return stats.as_dict()


def cmd_run(args: argparse.Namespace) -> None:
    result = asyncio.run(_run_once(dry_run=args.dry))
    print(json.dumps(result, indent=2))


def cmd_schedule(args: argparse.Namespace) -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler

    settings = get_settings()
    interval = args.interval or 15
    log.info("scheduler_start", interval_minutes=interval)

    scheduler = BlockingScheduler(timezone="UTC")

    def job() -> None:
        try:
            result = asyncio.run(_run_once(dry_run=settings.dry_run))
            log.info("scheduled_run_done", **result)
        except Exception as exc:
            log.error("scheduled_run_failed", error=str(exc))

    scheduler.add_job(job, "interval", minutes=interval, next_run_time=None)
    # Kick off immediately, then on the interval.
    job()
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler_stop")


def cmd_stats(args: argparse.Namespace) -> None:
    settings = get_settings()
    db = Database(settings.db_path)
    try:
        stats = compute_stats(db, days=args.days)
    finally:
        db.close()
    if args.json:
        print(json.dumps(stats, indent=2))
    else:
        print(format_stats(stats))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arb", description="Electronics arbitrage pipeline.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run the pipeline once.")
    p_run.add_argument("--dry", action="store_true", help="Dry run: scan + evaluate, no alerts.")
    p_run.set_defaults(func=cmd_run)

    p_sched = sub.add_parser("schedule", help="Run continuously on an interval.")
    p_sched.add_argument("--interval", type=int, default=15, help="Minutes between runs.")
    p_sched.set_defaults(func=cmd_schedule)

    p_stats = sub.add_parser("stats", help="Print deal stats.")
    p_stats.add_argument("--days", type=int, default=30, help="Look-back window in days.")
    p_stats.add_argument("--json", action="store_true", help="Emit JSON.")
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> None:
    settings = get_settings()
    configure_logging(env=settings.env, level=settings.log_level)
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
