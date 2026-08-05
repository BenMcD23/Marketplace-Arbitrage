"""eBay API client: OAuth application token + Browse search.

Everything here is built against the **Browse API**, which any registered
developer gets for free at 5,000 calls/day. Sold-price data (Marketplace
Insights) is a limited-release API that eBay no longer grants to new
applicants, so it is supported but never assumed — see `oracle/sold_tracker.py`
for how sold prices are obtained without it.

Because the call budget is the binding constraint on how much of the market we
can watch, every request goes through a `CallBudget` that refuses to overspend
the day's allowance rather than letting eBay start returning errors mid-run.

Parsing is kept separate from the HTTP calls so unit tests can feed saved JSON
fixtures without touching the network.
"""

from __future__ import annotations

import base64
import statistics
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from arb.logging_conf import get_logger
from arb.models import Condition
from oracle.comps import Comp
from sources.normalise import normalise_condition

log = get_logger("oracle.ebay")

OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
ITEM_URL = "https://api.ebay.com/buy/browse/v1/item"
INSIGHTS_URL = "https://api.ebay.com/buy/marketplace_insights/v1_beta/item_sales/search"

# Scope required for both Browse and Marketplace Insights read access.
SCOPE = "https://api.ebay.com/oauth/api_scope"

# eBay condition ids -> our raw condition strings (fed through normalise).
CONDITION_IDS = {
    "1000": "new",
    "1500": "new other",
    "1750": "new other",
    "2000": "manufacturer refurbished",
    "2010": "manufacturer refurbished",
    "2020": "seller refurbished",
    "2030": "seller refurbished",
    "3000": "used",
    "4000": "used",
    "5000": "used",
    "6000": "used",
    "7000": "for parts or not working",
}


class BudgetExhausted(RuntimeError):
    """Raised when the day's API call allowance is spent."""


class CallBudget:
    """Tracks API calls against a daily cap, resetting at UTC midnight.

    The free Browse tier is 5,000 calls/day for the whole application. Blowing
    through it mid-scan means every later call fails, which silently corrupts a
    run's results; refusing the call instead makes the limit visible and lets
    the pipeline stop cleanly.
    """

    def __init__(self, daily_limit: int = 5000):
        self.daily_limit = daily_limit
        self._day = datetime.now(UTC).date()
        self._used = 0

    def _roll(self) -> None:
        today = datetime.now(UTC).date()
        if today != self._day:
            self._day = today
            self._used = 0

    @property
    def used(self) -> int:
        self._roll()
        return self._used

    @property
    def remaining(self) -> int:
        return max(0, self.daily_limit - self.used)

    def spend(self, n: int = 1) -> None:
        self._roll()
        if self._used + n > self.daily_limit:
            raise BudgetExhausted(
                f"eBay API daily limit reached ({self.daily_limit} calls)"
            )
        self._used += n


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


def _extract_shipping(item: dict[str, Any]) -> float:
    for opt in item.get("shippingOptions") or []:
        cost = (opt.get("shippingCost") or {}).get("value")
        if cost is not None:
            try:
                return float(cost)
            except (TypeError, ValueError):
                continue
    return 0.0


def item_condition(item: dict[str, Any]) -> Condition:
    raw = item.get("condition")
    if not raw:
        raw = CONDITION_IDS.get(str(item.get("conditionId", "")))
    return normalise_condition(raw)


def parse_comps(payload: dict[str, Any], sold: bool = False) -> list[Comp]:
    """Turn a Browse (or Insights) response into candidate comps.

    Comps are priced **delivered** — item price plus postage — because that is
    what a buyer compares, and therefore what our resale price has to beat.
    """
    items = payload.get("itemSummaries") or payload.get("itemSales") or []
    comps: list[Comp] = []
    for item in items:
        price = _extract_price(item)
        if price is None or price <= 0:
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        comps.append(
            Comp(
                title=title,
                price=round(price + _extract_shipping(item), 2),
                condition=item_condition(item),
                item_id=str(item.get("itemId") or item.get("legacyItemId") or ""),
                url=item.get("itemWebUrl"),
                sold=sold,
            )
        )
    return comps


def parse_sold_search(payload: dict[str, Any]) -> tuple[float | None, int]:
    """Median and count from an Insights item_sales response. Pure, tested offline."""
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
        budget: CallBudget | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.marketplace = marketplace
        self.has_insights = has_insights
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._owns_client = client is None
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self.budget = budget or CallBudget()

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

    def _currency(self) -> str:
        return {
            "EBAY_GB": "GBP",
            "EBAY_US": "USD",
            "EBAY_DE": "EUR",
            "EBAY_FR": "EUR",
            "EBAY_IT": "EUR",
            "EBAY_ES": "EUR",
            "EBAY_AU": "AUD",
            "EBAY_CA": "CAD",
        }.get(self.marketplace, "GBP")

    # ---------------------------------------------------------------- search
    async def search(
        self,
        query: str,
        limit: int = 50,
        category_id: str | None = None,
        max_price: float | None = None,
        min_price: float | None = None,
        conditions: list[str] | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Raw Browse item_summary/search call. Returns the decoded payload."""
        filters = ["buyingOptions:{FIXED_PRICE}"]
        if max_price is not None or min_price is not None:
            lo = "" if min_price is None else str(min_price)
            hi = "" if max_price is None else str(max_price)
            filters.append(f"price:[{lo}..{hi}]")
            filters.append(f"priceCurrency:{self._currency()}")
        if conditions:
            joined = "|".join(conditions)
            filters.append(f"conditions:{{{joined}}}")

        params: dict[str, str] = {
            "q": query,
            "limit": str(min(limit, 200)),
            "filter": ",".join(filters),
        }
        if offset:
            params["offset"] = str(offset)
        if category_id:
            params["category_ids"] = category_id

        headers = await self._headers()
        self.budget.spend()
        resp = await self._client.get(BROWSE_URL, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()

    async def search_comps(
        self,
        query: str,
        limit: int = 100,
        category_id: str | None = None,
    ) -> list[Comp]:
        """Active listings for `query`, as candidate comps.

        Deliberately unfiltered by condition: the oracle wants the whole spread
        so it can price new and used separately.
        """
        try:
            payload = await self.search(query, limit=limit, category_id=category_id)
        except BudgetExhausted:
            raise
        except httpx.HTTPError as exc:
            log.warning("ebay_comp_search_error", query=query, error=str(exc))
            return []
        return parse_comps(payload)

    async def item_is_live(self, item_id: str) -> bool | None:
        """Whether a Browse item is still available.

        Returns True (live), False (ended/removed) or None (couldn't tell —
        network trouble or budget exhausted, so the caller must not conclude
        anything from it).
        """
        try:
            headers = await self._headers()
            self.budget.spend()
            resp = await self._client.get(f"{ITEM_URL}/{item_id}", headers=headers)
        except BudgetExhausted:
            return None
        except httpx.HTTPError as exc:
            log.warning("ebay_item_check_error", item_id=item_id, error=str(exc))
            return None

        if resp.status_code == 404:
            return False
        if resp.status_code >= 400:
            log.warning("ebay_item_check_status", item_id=item_id, status=resp.status_code)
            return None

        payload = resp.json()
        # eBay keeps serving ended items for a while with an availability
        # marker rather than a 404, so check both.
        end_date = payload.get("itemEndDate") or ""
        if end_date:
            try:
                ends = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                if ends <= datetime.now(UTC):
                    return False
            except ValueError:
                pass
        estimate = (payload.get("estimatedAvailabilities") or [{}])[0]
        if estimate.get("estimatedAvailabilityStatus") == "OUT_OF_STOCK":
            return False
        return True

    # -------------------------------------------------- Marketplace Insights
    async def sold_stats(
        self, query: str, days: int = 90, limit: int = 100
    ) -> tuple[float | None, int]:
        """Median sold price and count, via Marketplace Insights.

        Only usable by the handful of applications eBay has granted the limited
        release to. Returns (None, 0) otherwise, and the oracle falls back to
        its own observed sales.
        """
        if not self.has_insights:
            log.debug("insights_unavailable", query=query)
            return None, 0
        headers = await self._headers()
        params = {
            "q": query,
            "limit": str(limit),
            "filter": f"lastSoldDate:[{_days_ago_iso(days)}..]",
        }
        try:
            self.budget.spend()
            resp = await self._client.get(INSIGHTS_URL, headers=headers, params=params)
            resp.raise_for_status()
        except BudgetExhausted:
            return None, 0
        except httpx.HTTPStatusError as exc:
            log.warning("ebay_insights_error", query=query, status=exc.response.status_code)
            return None, 0
        return parse_sold_search(resp.json())

    async def sold_comps(self, query: str, days: int = 90, limit: int = 100) -> list[Comp]:
        """Sold comps via Insights, when available. Empty list otherwise."""
        if not self.has_insights:
            return []
        headers = await self._headers()
        params = {
            "q": query,
            "limit": str(limit),
            "filter": f"lastSoldDate:[{_days_ago_iso(days)}..]",
        }
        try:
            self.budget.spend()
            resp = await self._client.get(INSIGHTS_URL, headers=headers, params=params)
            resp.raise_for_status()
        except (BudgetExhausted, httpx.HTTPError):
            return []
        return parse_comps(resp.json(), sold=True)


def _days_ago_iso(days: int) -> str:
    dt = datetime.now(UTC) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
