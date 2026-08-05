"""CeX (webuy) client — a free, day-one price source for used electronics.

The eBay sold-price tracker in `oracle/sold_tracker.py` is the better long-run
signal, but it has a cold start: it knows nothing until it has watched listings
end, which takes weeks. CeX closes that gap immediately.

CeX publishes an unauthenticated JSON API behind their store front. For every
product they trade they give three numbers:

  * ``sellPrice``     — what CeX charges for the used item. A real used-retail
                        price, not an asking price.
  * ``cashPrice``     — what CeX will pay **you**, in cash, today.
  * ``exchangePrice`` — the same in store credit.

``cashPrice`` is the interesting one, and it is why this is worth more than
another price estimate. It is not a prediction — it is an offer. If you can buy
something for less than CeX will pay for it, the downside is bounded by
something you can actually walk into a shop and collect, rather than by a
percentile of a comp distribution. The deal engine uses it exactly that way.

Caveats worth keeping in mind:

  * CeX is UK-centric (other regions exist via the country code) and only
    covers products they choose to trade.
  * ``cashPrice`` is deliberately well under market — it is a floor, never a
    target. ``sellPrice`` is retail with a shop's margin on top, so it sits
    above what a private seller gets on eBay; `CEX_TO_EBAY_RATIO` discounts it.
  * This is an unofficial API. It is not versioned for us and could change or
    disappear, so every failure here is non-fatal and simply yields no quote.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Any

import httpx

from arb.logging_conf import get_logger
from arb.models import Condition
from oracle.comps import contradicts_model, is_accessory, relevance, tokenize

log = get_logger("oracle.cex")

#: Country code goes in the middle: wss2.cex.<cc>.webuy.io
BASE_URL_TEMPLATE = "https://wss2.cex.{country}.webuy.io/v3"

# CeX grade their stock A (best) / B / C, and the grade is usually a trailing
# token on the box name: "Apple iPhone 12 128GB Blue, Unlocked A".
_GRADE_RE = re.compile(r"[\s,(\[-]+(?:grade\s*)?([ABC])\)?\]?\s*$", re.I)

#: Which CeX grade best represents a listing in each condition.
_CONDITION_GRADE = {
    Condition.NEW: "A",
    Condition.USED: "B",
}


@dataclass
class CexBox:
    """One CeX product line."""

    box_id: str
    name: str
    sell_price: float
    cash_price: float
    exchange_price: float
    category: str | None = None
    grade: str | None = None
    in_stock: bool = True
    relevance: float = 0.0


@dataclass
class CexQuote:
    """What CeX says a thing is worth, both directions."""

    #: Used-retail price CeX charges.
    sell_price: float
    #: Guaranteed cash they will pay you. The floor.
    cash_price: float
    exchange_price: float
    #: The box name this was matched against, so the number can be audited.
    matched_name: str
    box_id: str
    grade: str | None
    #: How many CeX products were considered a match.
    n_matched: int


def strip_grade(name: str) -> tuple[str, str | None]:
    """Split a CeX box name into (name without grade, grade letter or None)."""
    match = _GRADE_RE.search(name)
    if not match:
        return name.strip(), None
    return name[: match.start()].strip(" ,-([", ), match.group(1).upper()


def parse_boxes(payload: dict[str, Any]) -> list[CexBox]:
    """Turn a `/boxes` response into `CexBox` records. Pure, tested offline."""
    response = payload.get("response") or {}
    if (response.get("ack") or "").lower() not in {"success", ""}:
        return []
    boxes = ((response.get("data") or {}).get("boxes")) or []

    parsed: list[CexBox] = []
    for box in boxes:
        name = (box.get("boxName") or "").strip()
        sell = _as_price(box.get("sellPrice"))
        if not name or sell is None:
            continue
        clean_name, grade = strip_grade(name)
        parsed.append(
            CexBox(
                box_id=str(box.get("boxId") or ""),
                name=clean_name,
                sell_price=sell,
                cash_price=_as_price(box.get("cashPrice")) or 0.0,
                exchange_price=_as_price(box.get("exchangePrice")) or 0.0,
                category=box.get("categoryFriendlyName") or box.get("categoryName"),
                grade=grade,
                in_stock=not bool(box.get("outOfStock")),
            )
        )
    return parsed


def select_boxes(
    target_title: str,
    boxes: list[CexBox],
    min_relevance: float = 0.6,
) -> list[CexBox]:
    """Keep only CeX products that plausibly are the thing being valued.

    CeX search is keyword-based and returns near misses just like eBay does, so
    the same relevance test applies — otherwise an iPhone 12 gets priced off an
    iPhone 12 Pro Max.
    """
    target_tokens = tokenize(target_title)
    kept: list[CexBox] = []
    for box in boxes:
        if is_accessory(box.name) or contradicts_model(target_title, box.name):
            continue
        score = relevance(target_tokens, box.name)
        if score < min_relevance:
            continue
        box.relevance = score
        kept.append(box)
    kept.sort(key=lambda b: b.relevance, reverse=True)
    return kept


def quote_from_boxes(
    target_title: str,
    boxes: list[CexBox],
    condition: Condition = Condition.UNKNOWN,
    min_relevance: float = 0.6,
) -> CexQuote | None:
    """Best CeX quote for a listing. Pure, so it can be tested offline.

    Prefers products at the grade matching the listing's condition; when the
    grade is unknown or absent it takes the median across whatever matched,
    which spans CeX's condition range rather than assuming the best case.
    """
    matched = select_boxes(target_title, boxes, min_relevance=min_relevance)
    if not matched:
        return None

    preferred = _CONDITION_GRADE.get(condition)
    graded = [b for b in matched if b.grade == preferred] if preferred else []
    pool = graded or matched

    best = pool[0]
    return CexQuote(
        sell_price=round(statistics.median([b.sell_price for b in pool]), 2),
        cash_price=round(statistics.median([b.cash_price for b in pool]), 2),
        exchange_price=round(statistics.median([b.exchange_price for b in pool]), 2),
        matched_name=best.name,
        box_id=best.box_id,
        grade=best.grade,
        n_matched=len(pool),
    )


class CexClient:
    def __init__(
        self,
        country: str = "uk",
        client: httpx.AsyncClient | None = None,
        min_relevance: float = 0.6,
    ):
        self.country = country.lower()
        self.min_relevance = min_relevance
        self._client = client or httpx.AsyncClient(
            timeout=15.0,
            # The API sits behind the store front and is unhappy with a bare
            # client; a normal browser UA is enough.
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
            },
        )
        self._owns_client = client is None

    @property
    def base_url(self) -> str:
        return BASE_URL_TEMPLATE.format(country=self.country)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search(self, query: str, count: int = 50) -> list[CexBox]:
        """Search CeX. Returns [] on any failure — this source is optional."""
        params = {
            "q": query,
            "firstRecord": "1",
            "count": str(min(count, 50)),
            "sortBy": "relevance",
            "sortOrder": "desc",
        }
        try:
            resp = await self._client.get(f"{self.base_url}/boxes", params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("cex_search_error", query=query, error=str(exc))
            return []
        try:
            return parse_boxes(resp.json())
        except ValueError as exc:  # not JSON — the unofficial API changed shape
            log.warning("cex_parse_error", query=query, error=str(exc))
            return []

    async def quote(
        self, title: str, condition: Condition = Condition.UNKNOWN, query: str | None = None
    ) -> CexQuote | None:
        """Look up what CeX would sell this for, and pay for it."""
        boxes = await self.search(query or title)
        if not boxes:
            return None
        quote = quote_from_boxes(
            title, boxes, condition=condition, min_relevance=self.min_relevance
        )
        if quote is not None:
            log.debug(
                "cex_quote",
                title=title,
                matched=quote.matched_name,
                sell=quote.sell_price,
                cash=quote.cash_price,
            )
        return quote


def _as_price(value: Any) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return round(price, 2) if price > 0 else None
