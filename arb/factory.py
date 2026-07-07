"""Wiring: build the oracle, alerter, and the enabled source list from config.

Kept separate from the pipeline so tests can inject fakes and the CLI stays thin.
"""

from __future__ import annotations

from alerts.telegram import TelegramAlerter
from arb.config import Settings
from arb.db import Database
from arb.logging_conf import get_logger
from oracle.ebay_client import EbayClient
from oracle.keepa_client import KeepaClient
from oracle.pricing import PricingOracle
from sources.base import Source
from sources.ebay import EbaySource

log = get_logger("factory")


def build_oracle(settings: Settings, db: Database) -> PricingOracle:
    ebay = None
    if settings.ebay_client_id and settings.ebay_client_secret:
        ebay = EbayClient(
            client_id=settings.ebay_client_id,
            client_secret=settings.ebay_client_secret,
            marketplace=settings.ebay_marketplace,
            has_insights=settings.ebay_has_insights,
        )
    keepa = KeepaClient(settings.keepa_api_key, domain=settings.keepa_domain) if settings.keepa_api_key else None
    return PricingOracle(settings, db, ebay=ebay, keepa=keepa)


def build_sources(settings: Settings) -> list[Source]:
    sources: list[Source] = []

    if settings.ebay_client_id and settings.ebay_client_secret and settings.ebay_query_list:
        sources.append(
            EbaySource(
                settings,
                queries=settings.ebay_query_list,
                category_id=settings.ebay_category_id,
                max_price=settings.ebay_max_price,
                limit=settings.ebay_limit,
            )
        )

    # Scraper sources are imported lazily so Playwright isn't required unless
    # they are actually switched on.
    if settings.enable_gumtree and settings.scrape_query_list:
        from sources.gumtree import GumtreeSource

        sources.append(GumtreeSource(settings, queries=settings.scrape_query_list))

    if settings.enable_fb_marketplace and settings.scrape_query_list:
        from sources.fb_marketplace import FacebookMarketplaceSource

        sources.append(FacebookMarketplaceSource(settings, queries=settings.scrape_query_list))

    return sources


def build_alerter(settings: Settings) -> TelegramAlerter:
    return TelegramAlerter(settings)
