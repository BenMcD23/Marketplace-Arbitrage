"""Keepa API client: current Amazon price + sales rank for a model/ASIN.

Keepa returns prices as integers in the currency's minor unit (pence/cents) and
uses -1 to mean "no data". Parsing is separated from HTTP for offline tests.
"""

from __future__ import annotations

from typing import Any

import httpx

from arb.logging_conf import get_logger

log = get_logger("oracle.keepa")

PRODUCT_URL = "https://api.keepa.com/product"
SEARCH_URL = "https://api.keepa.com/search"


def _cents_to_pounds(value: Any) -> float | None:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if v < 0:  # Keepa uses -1 for "no data"
        return None
    return round(v / 100.0, 2)


def parse_product(payload: dict[str, Any]) -> tuple[float | None, int | None]:
    """Return (amazon_price, sales_rank) from a Keepa product response."""
    products = payload.get("products") or []
    if not products:
        return None, None
    product = products[0]
    stats = product.get("stats") or {}
    current = stats.get("current") or []

    # Keepa CSV index 0 = Amazon price, 1 = New price, 3 = Sales Rank.
    price = None
    if len(current) > 0 and current[0] is not None and current[0] >= 0:
        price = _cents_to_pounds(current[0])
    if price is None and len(current) > 1:
        price = _cents_to_pounds(current[1])

    rank = None
    if len(current) > 3 and current[3] is not None and current[3] >= 0:
        rank = int(current[3])
    return price, rank


class KeepaClient:
    def __init__(
        self,
        api_key: str,
        domain: int = 2,
        client: httpx.AsyncClient | None = None,
    ):
        self.api_key = api_key
        self.domain = domain
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _search_asin(self, term: str) -> str | None:
        params = {"key": self.api_key, "domain": str(self.domain), "type": "product", "term": term}
        try:
            resp = await self._client.get(SEARCH_URL, params=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.warning("keepa_search_error", term=term, status=exc.response.status_code)
            return None
        data = resp.json()
        asins = data.get("asinList") or []
        return asins[0] if asins else None

    async def get_price_and_rank(self, model_number: str) -> tuple[float | None, int | None]:
        """Look up an Amazon price + sales rank for a model number.

        Treats the model number as an ASIN if it looks like one, otherwise
        searches for a matching product first.
        """
        asin = model_number if _looks_like_asin(model_number) else await self._search_asin(model_number)
        if not asin:
            return None, None
        params = {
            "key": self.api_key,
            "domain": str(self.domain),
            "asin": asin,
            "stats": "1",
        }
        try:
            resp = await self._client.get(PRODUCT_URL, params=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.warning("keepa_product_error", asin=asin, status=exc.response.status_code)
            return None, None
        return parse_product(resp.json())


def _looks_like_asin(s: str) -> bool:
    return len(s) == 10 and s.isalnum() and s.upper() == s and any(c.isalpha() for c in s)
