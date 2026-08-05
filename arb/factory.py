"""Wiring: build the oracle, alerter, and the enabled source list from config.

Kept separate from the pipeline so tests can inject fakes and both the CLI and
the API stay thin.

The eBay client is built once and shared between the source (which finds things
to buy) and the oracle (which values them), so both draw on a single daily call
budget instead of racing each other through it.
"""

from __future__ import annotations

from alerts.null import NullAlerter
from arb.config import Settings
from arb.db import Database
from arb.logging_conf import get_logger
from arb.models import WatchQuery
from oracle.cex_client import CexClient
from oracle.ebay_client import CallBudget, EbayClient
from oracle.keepa_client import KeepaClient
from oracle.pricing import PricingOracle
from oracle.sold_tracker import SoldTracker
from oracle.terapeak import TerapeakClient
from sources.base import Source
from sources.ebay import EbaySource

log = get_logger("factory")


def build_ebay_client(settings: Settings) -> EbayClient | None:
    if not (settings.ebay_client_id and settings.ebay_client_secret):
        return None
    return EbayClient(
        client_id=settings.ebay_client_id,
        client_secret=settings.ebay_client_secret,
        marketplace=settings.ebay_marketplace,
        has_insights=settings.ebay_has_insights,
        budget=CallBudget(daily_limit=settings.ebay_daily_call_limit),
    )


def build_oracle(
    settings: Settings, db: Database, ebay: EbayClient | None = None
) -> PricingOracle:
    ebay = ebay or build_ebay_client(settings)
    keepa = (
        KeepaClient(settings.keepa_api_key, domain=settings.keepa_domain)
        if settings.keepa_api_key
        else None
    )
    # CeX needs no key and works from the first scan, so it is on unless turned
    # off. Terapeak needs a saved login and carries ToS risk, so it is the
    # opposite: off unless deliberately enabled and a session actually exists.
    cex = (
        CexClient(country=settings.cex_country, min_relevance=settings.cex_min_relevance)
        if settings.enable_cex
        else None
    )
    terapeak = None
    if settings.enable_terapeak:
        terapeak = TerapeakClient(settings)
        if not terapeak.has_session():
            log.warning(
                "terapeak_enabled_without_session",
                hint="run `arb terapeak-login` once, or set ENABLE_TERAPEAK=false",
            )

    return PricingOracle(
        settings,
        db,
        ebay=ebay,
        keepa=keepa,
        tracker=SoldTracker(settings, db, ebay),
        cex=cex,
        terapeak=terapeak,
    )


def seed_queries(settings: Settings, db: Database) -> None:
    """Populate an empty watch list from EBAY_QUERIES, once.

    After the first run the list lives in the database and is edited in the UI;
    the env var is only a convenience for a fresh install.
    """
    if db.list_queries():
        return
    for term in settings.ebay_query_list:
        db.add_query(
            WatchQuery(
                query=term,
                category_id=settings.ebay_category_id,
                max_price=settings.ebay_max_price,
            )
        )
    if settings.ebay_query_list:
        log.info("watch_queries_seeded", count=len(settings.ebay_query_list))


def build_sources(
    settings: Settings, db: Database, ebay: EbayClient | None = None
) -> list[Source]:
    sources: list[Source] = []

    ebay = ebay or build_ebay_client(settings)
    if ebay is not None:
        seed_queries(settings, db)
        watches = db.list_queries(enabled_only=True)
        if watches:
            sources.append(
                EbaySource(
                    settings,
                    queries=watches,
                    limit=settings.ebay_limit,
                    client=ebay,
                    on_query_done=db.mark_query_run,
                )
            )
        else:
            log.warning("no_watch_queries")

    # Scraper sources are imported lazily so Playwright isn't required unless
    # they are actually switched on.
    if settings.enable_gumtree and settings.scrape_query_list:
        from sources.gumtree import GumtreeSource

        sources.append(GumtreeSource(settings, queries=settings.scrape_query_list))

    if settings.enable_fb_marketplace and settings.scrape_query_list:
        from sources.fb_marketplace import FacebookMarketplaceSource

        sources.append(FacebookMarketplaceSource(settings, queries=settings.scrape_query_list))

    return sources


def build_alerter(settings: Settings) -> NullAlerter:
    # Notifications are off — deals are stored and surfaced in the UI.
    return NullAlerter(settings)
