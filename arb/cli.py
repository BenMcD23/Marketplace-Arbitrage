"""Command-line entrypoint.

    arb run          run the pipeline once over all enabled sources
    arb run --dry    scan + evaluate but never send alerts (threshold tuning)
    arb schedule     run continuously on a configurable interval (APScheduler)
    arb stats        print performance stats from the deals table
    arb serve        start the FastAPI server for the dashboard
    arb watch        list / add / remove the searches that get scanned
    arb terapeak-login  sign in to eBay once and save the session (optional)
"""

from __future__ import annotations

import argparse
import asyncio
import json

from arb.config import get_settings
from arb.db import Database
from arb.logging_conf import configure_logging, get_logger
from arb.models import WatchQuery
from arb.runner import run_once
from arb.stats import compute_stats, format_stats

log = get_logger("cli")


def cmd_run(args: argparse.Namespace) -> None:
    stats = asyncio.run(run_once(dry_run=args.dry))
    print(json.dumps(stats.as_dict(), indent=2))


def cmd_schedule(args: argparse.Namespace) -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler

    settings = get_settings()
    interval = args.interval or 15
    log.info("scheduler_start", interval_minutes=interval)

    scheduler = BlockingScheduler(timezone="UTC")

    def job() -> None:
        try:
            stats = asyncio.run(run_once(dry_run=settings.dry_run))
            log.info("scheduled_run_done", **stats.as_dict())
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
    print(json.dumps(stats, indent=2) if args.json else format_stats(stats))


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run(
        "api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_config=None,
    )


def cmd_watch(args: argparse.Namespace) -> None:
    settings = get_settings()
    settings.ensure_db_dir()
    db = Database(settings.db_path)
    try:
        if args.add:
            watch = db.add_query(
                WatchQuery(query=args.add, category_id=args.category, max_price=args.max_price)
            )
            print(f"watching #{watch.id}: {watch.query}")
        elif args.remove is not None:
            print("removed" if db.delete_query(args.remove) else "no such query")
        else:
            queries = db.list_queries()
            if not queries:
                print("No watched searches. Add one with:  arb watch --add 'iphone 12'")
            for q in queries:
                state = "on " if q.enabled else "off"
                cap = f"  <= £{q.max_price:g}" if q.max_price else ""
                print(f"  [{state}] #{q.id:<3} {q.query}{cap}")
    finally:
        db.close()


def cmd_terapeak_login(args: argparse.Namespace) -> None:
    from oracle.terapeak import interactive_login

    settings = get_settings()
    print(
        "\nNote: automating the eBay site outside its published APIs is contrary\n"
        "to eBay's User Agreement, and the account at risk is the one you sell on.\n"
        "You are signing in yourself — no credentials are asked for or stored here,\n"
        "only the resulting session cookies.\n"
    )
    asyncio.run(interactive_login(settings))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arb", description="Marketplace arbitrage pipeline.")
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

    p_serve = sub.add_parser("serve", help="Start the API server.")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true", help="Auto-reload on code changes.")
    p_serve.set_defaults(func=cmd_serve)

    p_watch = sub.add_parser("watch", help="Manage watched searches.")
    p_watch.add_argument("--add", metavar="QUERY", help="Add a search term.")
    p_watch.add_argument("--remove", type=int, metavar="ID", help="Remove a search by id.")
    p_watch.add_argument("--category", help="eBay category id for the added search.")
    p_watch.add_argument("--max-price", type=float, dest="max_price", help="Max buy price.")
    p_watch.set_defaults(func=cmd_watch)

    p_terapeak = sub.add_parser(
        "terapeak-login", help="Sign in to eBay once and save a Terapeak session."
    )
    p_terapeak.set_defaults(func=cmd_terapeak_login)

    return parser


def main(argv: list[str] | None = None) -> None:
    settings = get_settings()
    configure_logging(env=settings.env, level=settings.log_level)
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
