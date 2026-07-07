"""Gumtree scraper (collection-only classifieds).

Searches electronics by keyword + location + max price and parses listing cards
into `Listing` objects. Most Gumtree items are collection-only, so shipping is
set to a configurable default (usually 0).

The price/field parsing is factored into pure functions so it can be unit
tested without a live browser. Selectors are best-effort and may need updating
if Gumtree changes its markup.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from urllib.parse import quote_plus

from arb.config import Settings
from arb.logging_conf import get_logger
from arb.models import Listing
from sources.normalise import clean_title, extract_brand, extract_model_number
from sources.scraper_base import ScraperSource

log = get_logger("sources.gumtree")

BASE = "https://www.gumtree.com"


def parse_price(text: str | None) -> float | None:
    """Extract a numeric price from strings like '£120.00' or 'Â£1,250'."""
    if not text:
        return None
    m = re.search(r"[\d,]+(?:\.\d{1,2})?", text.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def build_listing(
    settings: Settings,
    *,
    listing_id: str,
    title: str,
    price: float,
    url: str,
    image_url: str | None,
    location: str | None,
) -> Listing:
    title = clean_title(title)
    return Listing(
        source="gumtree",
        source_listing_id=str(listing_id),
        title=title,
        model_number=extract_model_number(title),
        brand=extract_brand(title),
        price=price,
        shipping=settings.scrape_default_shipping,
        condition="unknown",  # Gumtree rarely states a standard condition
        url=url if url.startswith("http") else f"{BASE}{url}",
        image_url=image_url,
        location=location,
    )


class GumtreeSource(ScraperSource):
    name = "gumtree"

    @property
    def _enabled_flag(self) -> bool:
        return self.settings.enable_gumtree

    def _search_url(self, query: str) -> str:
        params = [f"q={quote_plus(query)}"]
        if self.settings.scrape_location:
            params.append(f"search_location={quote_plus(self.settings.scrape_location)}")
        if self.settings.scrape_max_price is not None:
            params.append(f"max_price={int(self.settings.scrape_max_price)}")
        return f"{BASE}/search?{'&'.join(params)}"

    async def _scrape(self) -> AsyncIterator[Listing]:
        page = await self._context.new_page()
        try:
            for query in self.queries:
                url = self._search_url(query)
                if not await self._goto_with_retry(page, url):
                    continue
                await self._delay()

                cards = await page.query_selector_all("article[data-q='search-result']")
                log.info("gumtree_cards", query=query, count=len(cards))
                for card in cards:
                    listing = await self._parse_card(card)
                    if listing is not None:
                        yield listing
        finally:
            await page.close()

    async def _parse_card(self, card) -> Listing | None:
        try:
            link = await card.query_selector("a")
            href = await link.get_attribute("href") if link else None
            title_el = await card.query_selector("[data-q='naturalinf-title'], h2, .listing-title")
            title = (await title_el.inner_text()).strip() if title_el else None
            price_el = await card.query_selector("[data-q='listing-price'], .listing-price, [class*='price']")
            price = parse_price(await price_el.inner_text()) if price_el else None
            loc_el = await card.query_selector("[data-q='listing-location'], .listing-location")
            location = (await loc_el.inner_text()).strip() if loc_el else None
            img_el = await card.query_selector("img")
            image_url = await img_el.get_attribute("src") if img_el else None

            if not (href and title and price):
                return None
            listing_id = href.rstrip("/").split("/")[-1]
            return build_listing(
                self.settings,
                listing_id=listing_id,
                title=title,
                price=price,
                url=href,
                image_url=image_url,
                location=location,
            )
        except Exception as exc:
            log.debug("gumtree_card_parse_failed", error=str(exc))
            return None
