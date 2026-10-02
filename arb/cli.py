"""Command-line entrypoint.

    arb run          run the pipeline once over all enabled sources
    arb run --dry    scan + evaluate but never send alerts (threshold tuning)
    arb schedule     run continuously on a configurable interval (APScheduler)
    arb stats        print performance stats from the deals table
    arb serve        start the FastAPI server for the dashboard
    arb watch        list / add / remove the searches that get scanned
    arb test-alert   post one fake deal to Discord to check the webhook
    arb terapeak-login  sign in to eBay once and save the session (optional)
    arb auctions backfill|report|scan   auction houses as a buy side
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


def cmd_test_alert(args: argparse.Namespace) -> None:
    """Post one made-up auction deal, to prove the webhook before a real run."""
    from datetime import UTC, datetime, timedelta

    from arb.factory import build_alerter
    from arb.models import Condition, Listing, PriceBasis, Valuation
    from engine.deals import evaluate, max_bid

    settings = get_settings()
    if not settings.discord_webhook_url:
        print("DISCORD_WEBHOOK_URL is not set in .env — nothing to test.")
        return
    settings.dry_run = False  # the whole point of this command is to send

    listing = Listing(
        source="ebay",
        source_listing_id="test-alert",
        title="TEST ALERT — Apple iPhone 12 128GB",
        brand="Apple",
        price=60.0,
        shipping=4.0,
        condition=Condition.USED,
        url="https://www.ebay.co.uk/",
        is_auction=True,
        bid_count=3,
        end_time=datetime.now(UTC) + timedelta(hours=6),
    )
    valuation = Valuation(
        product_key="test",
        resale_price=300.0,
        basis=PriceBasis.SOLD,
        comp_count=12,
        confidence=0.8,
    )
    deal = evaluate(listing, valuation, settings)
    if deal is None:
        print("The sample deal did not clear your thresholds — loosen them or check .env.")
        return

    async def go() -> bool:
        alerter = build_alerter(settings)
        try:
            return await alerter.send_deal(deal, listing, max_bid(listing, valuation, settings))
        finally:
            await alerter.aclose()

    print("sent — check Discord" if asyncio.run(go()) else "failed — see the log above")


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


def cmd_auctions(args: argparse.Namespace) -> None:
    from arb.factory import build_oracle
    from houses import analysis
    from houses.store import LotStore

    settings = get_settings()
    store = LotStore(settings.db_path)

    async def go() -> str:
        if args.action == "backfill":
            stats = await analysis.backfill(settings, store, auctions=args.auctions)
            return json.dumps(stats, indent=2)
        oracle = build_oracle(settings, Database(settings.db_path))
        try:
            if args.action == "report":
                return await analysis.report(settings, store, oracle)
            return await analysis.scan(settings, store, oracle, hours=args.hours)
        finally:
            await oracle.aclose()

    print(asyncio.run(go()))


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

    p_test = sub.add_parser("test-alert", help="Post one fake deal to Discord.")
    p_test.set_defaults(func=cmd_test_alert)

    p_terapeak = sub.add_parser(
        "terapeak-login", help="Sign in to eBay once and save a Terapeak session."
    )
    p_terapeak.set_defaults(func=cmd_terapeak_login)

    p_auc = sub.add_parser("auctions", help="Auction houses as a buy side (Simon Charles).")
    p_auc.add_argument("action", choices=["backfill", "report", "scan"])
    p_auc.add_argument("--auctions", type=int, default=30,
                       help="backfill: how many recent auction ids to walk.")
    p_auc.add_argument("--hours", type=float, default=24,
                       help="scan: only lots ending within this many hours.")
    p_auc.set_defaults(func=cmd_auctions)

    return parser


def main(argv: list[str] | None = None) -> None:
    settings = get_settings()
    configure_logging(env=settings.env, level=settings.log_level)
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
