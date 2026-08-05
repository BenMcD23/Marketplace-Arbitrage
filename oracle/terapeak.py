"""Terapeak (eBay Product Research) — real sold data, via your own login.

eBay gives every seller account Terapeak for free, and it holds what this whole
project wants: **actual sold prices**, with a sell-through rate, over a far
longer window than any API exposes. What it does not have is an API. Marketplace
Insights is the API and it is closed to new applicants, so the only way to reach
Terapeak programmatically is to drive Seller Hub in a logged-in browser.

    ⚠️  READ THIS BEFORE ENABLING
    Automating the eBay site outside its published APIs is contrary to eBay's
    User Agreement. That is a civil matter, not a criminal one — but the account
    at risk is the one you sell on, which makes it a different bet from scraping
    a third-party site. It is off by default (`ENABLE_TERAPEAK=false`) and stays
    off unless you turn it on deliberately.

    Rate limiting and randomised delays are applied, and one query per product
    per valuation TTL is all this ever issues — but the honest summary is that
    you are accepting risk to your selling account in exchange for better data.

Design note: everything that parses lives in `parse_research_text`, a pure
function over the page's visible text. Terapeak's DOM is not a stable contract
and will change; keeping the parsing separate means it can be tested offline
against a fixture, and when eBay does move things the fix is one function rather
than a rewrite.
"""

from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from arb.config import Settings
from arb.logging_conf import get_logger
from oracle.comps import Comp

log = get_logger("oracle.terapeak")

RESEARCH_URL = "https://www.ebay.{tld}/sh/research"
SESSION_DIR = Path("data/sessions")
SESSION_FILE = "terapeak.json"

#: Marketplace id -> (domain suffix, eBay marketplace param).
_MARKETPLACES = {
    "EBAY_GB": ("co.uk", "EBAY-GB"),
    "EBAY_US": ("com", "EBAY-US"),
    "EBAY_DE": ("de", "EBAY-DE"),
    "EBAY_IE": ("ie", "EBAY-IE"),
    "EBAY_AU": ("com.au", "EBAY-AU"),
    "EBAY_CA": ("ca", "EBAY-CA"),
}

_MONEY = r"[£$€]\s*([\d,]+(?:\.\d+)?)"
_NUMBER = r"([\d,]+(?:\.\d+)?)"

# Gap between a label and its value. Counts use the stricter one: "sold" also
# occurs inside "Avg sold price £168.42", and a gap that could cross a currency
# symbol would happily read the price as the count.
_GAP = r"\D{0,40}?"
_COUNT_GAP = r"[^\d£$€]{0,40}?"

# Terapeak renders "label then value". Labels have been renamed before (the tool
# was rebranded to "Product Research"), so each metric accepts several spellings.
_PATTERNS: dict[str, list[str]] = {
    "avg_sold_price": [
        rf"avg(?:erage)?\.?\s*sold\s*price{_GAP}{_MONEY}",
        rf"average\s*sale\s*price{_GAP}{_MONEY}",
    ],
    "avg_shipping": [rf"avg(?:erage)?\.?\s*shipping{_GAP}{_MONEY}"],
    "total_sold": [
        rf"total\s*sold{_COUNT_GAP}{_NUMBER}",
        rf"items?\s*sold{_COUNT_GAP}{_NUMBER}",
        rf"sold\s*(?:items|count){_COUNT_GAP}{_NUMBER}",
    ],
    "total_sellers": [rf"(?:total\s*)?sellers{_COUNT_GAP}{_NUMBER}"],
    "sell_through_pct": [rf"sell[-\s]*through(?:\s*rate)?{_GAP}{_NUMBER}\s*%"],
}


@dataclass
class TerapeakStats:
    """Aggregate sold statistics for one search term."""

    query: str
    avg_sold_price: float | None = None
    avg_shipping: float | None = None
    total_sold: int | None = None
    total_sellers: int | None = None
    sell_through_pct: float | None = None
    day_range: int = 90

    @property
    def is_usable(self) -> bool:
        return bool(self.avg_sold_price and self.total_sold)

    @property
    def delivered_price(self) -> float | None:
        """Sold price including postage — what the buyer actually paid."""
        if self.avg_sold_price is None:
            return None
        return round(self.avg_sold_price + (self.avg_shipping or 0.0), 2)


def _search(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1)
    return None


def parse_research_text(text: str, query: str = "", day_range: int = 90) -> TerapeakStats:
    """Pull the aggregate metrics out of a Product Research page's text.

    Deliberately label-driven rather than position-driven: it looks for the
    metric's name and takes the number near it, so a layout change does not
    silently shift every value by one column — which is the failure mode that
    would quietly poison valuations rather than obviously breaking.
    """
    flat = re.sub(r"\s+", " ", text or "")
    stats = TerapeakStats(query=query, day_range=day_range)

    price = _search(_PATTERNS["avg_sold_price"], flat)
    if price:
        stats.avg_sold_price = _to_float(price)

    shipping = _search(_PATTERNS["avg_shipping"], flat)
    if shipping:
        stats.avg_shipping = _to_float(shipping)

    sold = _search(_PATTERNS["total_sold"], flat)
    if sold:
        stats.total_sold = _to_int(sold)

    sellers = _search(_PATTERNS["total_sellers"], flat)
    if sellers:
        stats.total_sellers = _to_int(sellers)

    rate = _search(_PATTERNS["sell_through_pct"], flat)
    if rate:
        value = _to_float(rate)
        # Terapeak can report sell-through above 100% (more sold than currently
        # listed). Keep it, but cap what the rest of the system sees.
        stats.sell_through_pct = min(value, 100.0) if value is not None else None

    return stats


def stats_to_comps(stats: TerapeakStats) -> list[Comp]:
    """Represent Terapeak's aggregate as comps the oracle can price from.

    Terapeak gives a mean and a count, not the individual sales. Rather than
    invent a distribution, this emits one comp per sale up to a cap, all at the
    average — which is honest about the centre and makes no claim about spread.
    The oracle's dispersion measure will read as zero, and the confidence model
    treats that as agreement, so the count is capped to stop a 500-sale search
    manufacturing more certainty than an average deserves.
    """
    price = stats.delivered_price
    if not stats.is_usable or price is None:
        return []
    count = min(stats.total_sold or 0, 20)
    return [
        Comp(title=stats.query, price=price, item_id=f"terapeak:{stats.query}:{i}", sold=True)
        for i in range(count)
    ]


class TerapeakClient:
    """Drives a logged-in Seller Hub session to read Product Research."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._playwright = None
        self._browser = None
        self._context = None
        self._logged_out_warned = False

    @property
    def enabled(self) -> bool:
        return self.settings.enable_terapeak

    @property
    def session_path(self) -> Path:
        return SESSION_DIR / SESSION_FILE

    def has_session(self) -> bool:
        return self.session_path.exists()

    def _urls(self) -> tuple[str, str]:
        tld, marketplace = _MARKETPLACES.get(
            self.settings.ebay_marketplace, ("co.uk", "EBAY-GB")
        )
        return RESEARCH_URL.format(tld=tld), marketplace

    def research_url(self, query: str) -> str:
        base, marketplace = self._urls()
        return (
            f"{base}?marketplace={marketplace}&keywords={quote_plus(query)}"
            f"&dayRange={self.settings.terapeak_day_range}"
            f"&categoryId=0&offset=0&limit=50&tabName=SOLD"
        )

    async def _ensure_browser(self) -> bool:
        if self._context is not None:
            return True
        if not self.has_session():
            if not self._logged_out_warned:
                log.warning(
                    "terapeak_no_session",
                    hint="run `arb terapeak-login` once to sign in and save a session",
                )
                self._logged_out_warned = True
            return False

        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        self._context = await self._browser.new_context(
            storage_state=str(self.session_path),
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        return True

    async def _delay(self) -> None:
        await asyncio.sleep(
            random.uniform(self.settings.scrape_min_delay_sec, self.settings.scrape_max_delay_sec)
        )

    async def stats(self, query: str) -> TerapeakStats | None:
        """Aggregate sold stats for a search term, or None if unavailable."""
        if not self.enabled:
            return None
        if not await self._ensure_browser():
            return None

        page = await self._context.new_page()
        try:
            await self._delay()
            await page.goto(self.research_url(query), wait_until="domcontentloaded", timeout=45000)
            # The metrics render client-side after the search resolves.
            await page.wait_for_timeout(self.settings.terapeak_render_wait_ms)
            text = await page.inner_text("body")
        except Exception as exc:
            log.warning("terapeak_fetch_failed", query=query, error=str(exc))
            return None
        finally:
            await page.close()

        if _looks_logged_out(text):
            log.warning(
                "terapeak_session_expired",
                hint="run `arb terapeak-login` again to refresh the saved session",
            )
            return None

        stats = parse_research_text(
            text, query=query, day_range=self.settings.terapeak_day_range
        )
        if not stats.is_usable:
            log.info("terapeak_no_data", query=query)
            return None

        log.info(
            "terapeak_stats",
            query=query,
            avg=stats.avg_sold_price,
            sold=stats.total_sold,
            sell_through=stats.sell_through_pct,
        )
        return stats

    async def aclose(self) -> None:
        try:
            if self._context is not None:
                await self._context.close()
            if self._browser is not None:
                await self._browser.close()
            if self._playwright is not None:
                await self._playwright.stop()
        finally:
            self._context = self._browser = self._playwright = None


async def interactive_login(settings: Settings) -> bool:
    """Open a real browser so you can sign in once; saves the session.

    Deliberately headed and manual — no credentials are ever asked for, typed or
    stored by this project. You log in yourself, including any 2FA, and only the
    resulting cookies are written to `data/sessions/terapeak.json`.
    """
    from playwright.async_api import async_playwright

    tld, _ = _MARKETPLACES.get(settings.ebay_marketplace, ("co.uk", "EBAY-GB"))
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(f"https://www.ebay.{tld}/sh/research", wait_until="domcontentloaded")

        print("\nA browser window has opened.")
        print("Sign in to eBay, then navigate to Seller Hub → Research.")
        print("When Product Research has loaded, come back here and press Enter.\n")
        await asyncio.get_event_loop().run_in_executor(None, input)

        text = await page.inner_text("body")
        if _looks_logged_out(text):
            print("Still signed out — nothing saved. Try again.")
            await browser.close()
            return False

        await context.storage_state(path=str(SESSION_DIR / SESSION_FILE))
        await browser.close()

    print(f"Session saved to {SESSION_DIR / SESSION_FILE}")
    print("Set ENABLE_TERAPEAK=true to start using it.")
    return True


def _looks_logged_out(text: str) -> bool:
    lowered = (text or "").lower()
    signed_out_markers = ("sign in to your account", "sign in or register", "please sign in")
    return any(marker in lowered for marker in signed_out_markers)


def _to_float(value: str) -> float | None:
    try:
        return float(value.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _to_int(value: str) -> int | None:
    try:
        return int(float(value.replace(",", "")))
    except (TypeError, ValueError):
        return None
