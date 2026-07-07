"""eBay API client: OAuth application token + sold-comps via Marketplace Insights.

The parsing functions are deliberately separated from the HTTP calls so unit
tests can feed saved JSON fixtures without hitting the network.
"""

from __future__ import annotations

import base64
import statistics
import time
from datetime import UTC
from typing import Any

import httpx

from arb.logging_conf import get_logger

log = get_logger("oracle.ebay")

OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
INSIGHTS_URL = "https://api.ebay.com/buy/marketplace_insights/v1_beta/item_sales/search"

# Scope required for both Browse and Marketplace Insights read access.
SCOPE = "https://api.ebay.com/oauth/api_scope"


def _extract_price(item: dict[str, Any]) -> float | None:
    """Pull a numeric price out of an eBay item summary/sale record."""
    price = item.get("price") or {}
    # Marketplace Insights item_sales use lastSoldPrice; Browse uses price.
    if not price:
        price = item.get("lastSoldPrice") or {}
    value = price.get("value")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_sold_search(payload: dict[str, Any]) -> tuple[float | None, int]:
    """Given a Marketplace Insights item_sales response, return (median, count).

    Pure function — the core of the oracle's "resale truth". Tested offline.
    """
    items = payload.get("itemSales") or payload.get("itemSummaries") or []
    prices = [p for p in (_extract_price(it) for it in items) if p is not None and p > 0]
    if not prices:
        return None, 0
    return round(statistics.median(prices), 2), len(prices)


class EbayClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        marketplace: str = "EBAY_GB",
        has_insights: bool = False,
        client: httpx.AsyncClient | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.marketplace = marketplace
        self.has_insights = has_insights
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._owns_client = client is None
        self._token: str | None = None
        self._token_expiry: float = 0.0

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        resp = await self._client.post(
            OAUTH_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": SCOPE},
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 7200))
        return self._token

    async def _headers(self) -> dict[str, str]:
        token = await self._get_token()
        return {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
            "Content-Type": "application/json",
        }

    async def sold_stats(self, query: str, days: int = 90, limit: int = 100) -> tuple[float | None, int]:
        """Median sold price and count over the last `days` days.

        Uses Marketplace Insights if the account tier has it; otherwise returns
        (None, 0) — the deal engine treats missing sold data as untrustworthy.
        """
        if not self.has_insights:
            log.debug("insights_unavailable", query=query)
            return None, 0
        headers = await self._headers()
        params = {
            "q": query,
            "limit": str(limit),
            # Marketplace Insights supports a lastSoldDate filter window.
            "filter": f"lastSoldDate:[{_days_ago_iso(days)}..]",
        }
        try:
            resp = await self._client.get(INSIGHTS_URL, headers=headers, params=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.warning("ebay_insights_error", query=query, status=exc.response.status_code)
            return None, 0
        return parse_sold_search(resp.json())

    async def active_lowest(self, query: str, limit: int = 50) -> float | None:
        """Fallback signal when no sold data: lowest active BIN price."""
        headers = await self._headers()
        params = {"q": query, "limit": str(limit), "filter": "buyingOptions:{FIXED_PRICE}"}
        try:
            resp = await self._client.get(BROWSE_URL, headers=headers, params=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.warning("ebay_browse_error", query=query, status=exc.response.status_code)
            return None
        median_price, _ = parse_sold_search(resp.json())
        return median_price


def _days_ago_iso(days: int) -> str:
    from datetime import datetime, timedelta

    dt = datetime.now(UTC) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
