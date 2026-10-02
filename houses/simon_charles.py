"""Simon Charles Auctioneers (simoncharles.com) — weekly online auctions.

The site is a Next.js app that streams its page data as React Server Component
chunks (`self.__next_f.push(...)`). Every lot on a page is in there as plain
JSON — title, hammer price, next bid, end time, VAT and the fee percentages —
so there is no HTML to scrape, just JSON to pull out of the stream.

Closed auctions stay browsable with their final hammer prices, which is what
makes this house useful for answering "is there money in it": the history is
there to backfill, not just the live sale.

robots.txt allows everything except /account/. Requests are still spaced out.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from arb.logging_conf import get_logger

log = get_logger("houses.simon_charles")

BASE = "https://simoncharles.com"
HOUSE = "simoncharles"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

_CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', re.S)
_LOT_ARRAY_RE = re.compile(r'"_?lots":\[')
_LOT_OBJ_RE = re.compile(r'"_?lot":\{')
_AUCTION_LINK_RE = re.compile(r'href="/auctions/(\d+)/')

# Lot status as the site reports it: 0 while bidding is open, non-zero once the
# lot has closed. A closed lot with a winner and a hammer price sold.
_OPEN = 0


@dataclass
class Lot:
    id: int
    auction_id: int
    title: str
    hammer: float | None  # current bid while open, final hammer once closed
    next_bid: float | None  # the site's asking price: what you would have to bid
    end_time: datetime | None
    status: int
    has_winner: bool
    vat_pct: float
    premium_pct: float
    internet_pct: float
    postal: bool
    description: str = ""
    condition_report: str = ""

    @property
    def url(self) -> str:
        return f"{BASE}/lots/{self.id}/x"

    @property
    def is_open(self) -> bool:
        return self.status == _OPEN

    @property
    def sold(self) -> bool:
        return not self.is_open and self.has_winner and bool(self.hammer)


def _rsc_payload(html: str) -> str:
    """Concatenate the page's RSC chunks back into one decoded string."""
    return "".join(json.loads(f'"{c}"') for c in _CHUNK_RE.findall(html))


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Some embedded times carry no offset; the site's canonical ones are UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _to_lot(o: dict) -> Lot | None:
    try:
        return Lot(
            id=int(o["id"]),
            auction_id=int(o.get("auctionId") or 0),
            title=re.sub(r"\s+", " ", o.get("title") or "").strip(),
            hammer=float(o["hammerPrice"]) if o.get("hammerPrice") is not None else None,
            next_bid=float(o["askingPrice"]) if o.get("askingPrice") is not None else None,
            end_time=_parse_time(o.get("endTime")),
            status=int(o.get("status") or 0),
            has_winner=o.get("winningUserId") is not None,
            vat_pct=float(o.get("vat") if o.get("vat") is not None else 20),
            premium_pct=float(o.get("auctionBuyersPremium") or o.get("buyersPremium") or 20),
            internet_pct=float(o.get("auctionInternetPremium") or 5),
            postal=o.get("postalBinId") is not None,
            description=o.get("description") or "",
            condition_report=o.get("conditionReport") or "",
        )
    except (KeyError, TypeError, ValueError):
        return None


def parse_lots(html: str) -> dict[int, Lot]:
    """Every lot object embedded in a page, keyed by id. Pure; tested offline."""
    s = _rsc_payload(html)
    dec = json.JSONDecoder()
    raw: dict[int, dict] = {}
    for m in _LOT_ARRAY_RE.finditer(s):
        try:
            arr, _ = dec.raw_decode(s, m.end() - 1)
        except ValueError:
            continue
        for o in arr:
            if isinstance(o, dict) and "id" in o:
                raw.setdefault(o["id"], {}).update(o)
    for m in _LOT_OBJ_RE.finditer(s):
        try:
            o, _ = dec.raw_decode(s, m.end() - 1)
        except ValueError:
            continue
        if isinstance(o, dict) and "id" in o:
            # The detail page's own lot object is the fullest one (it carries the
            # description and condition report), so it wins over list entries.
            raw.setdefault(o["id"], {}).update(o)
    lots = {}
    for lot_id, o in raw.items():
        lot = _to_lot(o)
        if lot is not None:
            lots[lot_id] = lot
    return lots


def parse_auction_ids(html: str) -> list[int]:
    return sorted({int(x) for x in _AUCTION_LINK_RE.findall(html)})


class SimonCharlesClient:
    def __init__(self, delay_sec: float = 1.0, client: httpx.AsyncClient | None = None):
        self.delay = delay_sec
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": _UA}, timeout=30, follow_redirects=True
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **params) -> str:
        await asyncio.sleep(self.delay)
        r = await self._client.get(f"{BASE}{path}", params=params or None)
        r.raise_for_status()
        return r.text

    async def current_auction_ids(self) -> list[int]:
        return parse_auction_ids(await self._get("/auctions"))

    async def auction_lots(self, auction_id: int, max_pages: int = 60) -> list[Lot]:
        """Every lot in one auction, open or closed, walking pages until empty."""
        seen: dict[int, Lot] = {}
        for page in range(1, max_pages + 1):
            try:
                html = await self._get(f"/auctions/{auction_id}/x", page=page)
            except httpx.HTTPStatusError as exc:
                log.warning("sc_page_error", auction=auction_id, page=page, error=str(exc))
                break
            new = {k: v for k, v in parse_lots(html).items() if k not in seen}
            # Pages also embed a few "popular lots" from other auctions.
            new = {k: v for k, v in new.items() if v.auction_id == auction_id}
            if not new:
                break
            seen.update(new)
        return list(seen.values())

    async def lot_detail(self, lot_id: int) -> Lot | None:
        try:
            html = await self._get(f"/lots/{lot_id}/x")
        except httpx.HTTPStatusError:
            return None
        return parse_lots(html).get(lot_id)
