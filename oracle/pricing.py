"""Pricing Oracle — determines resale value for a model.

Combines eBay sold-comps and Keepa (Amazon) data into a single `Valuation`,
caches it in SQLite, and re-queries only when the cached record is older than
the configured TTL. This is the module that decides "what is this worth".
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime

from arb.config import Settings
from arb.db import Database
from arb.logging_conf import get_logger
from arb.models import Valuation
from oracle.ebay_client import EbayClient
from oracle.keepa_client import KeepaClient

log = get_logger("oracle.pricing")


def median(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None and v > 0]
    if not clean:
        return None
    return round(statistics.median(clean), 2)


class PricingOracle:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        ebay: EbayClient | None = None,
        keepa: KeepaClient | None = None,
    ):
        self.settings = settings
        self.db = db
        self.ebay = ebay
        self.keepa = keepa

    async def aclose(self) -> None:
        if self.ebay:
            await self.ebay.aclose()
        if self.keepa:
            await self.keepa.aclose()

    def _cache_key(self, model_number: str | None, title: str) -> str:
        # Prefer model number; fall back to a normalised title so titles without
        # an extractable model still get cached (and don't hammer the APIs).
        return (model_number or title).strip().lower()

    async def get_valuation(self, model_number: str | None, title: str) -> Valuation:
        key = self._cache_key(model_number, title)

        cached = self.db.get_valuation(key, ttl_hours=self.settings.valuation_ttl_hours)
        if cached is not None:
            log.debug("valuation_cache_hit", key=key)
            return cached

        query = model_number or title

        ebay_median: float | None = None
        ebay_count = 0
        if self.ebay is not None:
            ebay_median, ebay_count = await self.ebay.sold_stats(query)

        amazon_price: float | None = None
        amazon_rank: int | None = None
        if self.keepa is not None and model_number:
            amazon_price, amazon_rank = await self.keepa.get_price_and_rank(model_number)

        valuation = Valuation(
            model_number=key,
            ebay_sold_median=ebay_median,
            ebay_sold_count=ebay_count,
            amazon_price=amazon_price,
            amazon_rank=amazon_rank,
            updated_at=datetime.now(UTC),
        )
        self.db.upsert_valuation(valuation)
        log.info(
            "valuation_fetched",
            key=key,
            ebay_median=ebay_median,
            ebay_count=ebay_count,
            amazon_price=amazon_price,
            amazon_rank=amazon_rank,
        )
        return valuation
