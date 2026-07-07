"""Facebook Marketplace scraper (highest edge, highest difficulty).

Private sellers who don't know resale value are the best source of underpriced
electronics — and Facebook fights automation hard. This scraper needs a
persistent logged-in session and human-like pacing. It is fully isolated behind
the Source interface and off by default, so it can be toggled off the moment it
becomes more trouble than it's worth without touching the rest of the pipeline.

NOTE: scraping Facebook Marketplace violates its Terms of Service (civil, not
criminal — risk of account/IP bans). Use a throwaway account and keep the
request rate low and randomised.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from urllib.parse import quote_plus

from arb.config import Settings
from arb.logging_conf import get_logger
from arb.models import Listing
from sources.gumtree import parse_price
from sources.normalise import clean_title, extract_brand, extract_model_number
from sources.scraper_base import ScraperSource

log = get_logger("sources.fb")

BASE = "https://www.facebook.com"

# Marketplace item urls look like /marketplace/item/<id>/
_ITEM_ID_RE = re.compile(r"/marketplace/item/(\d+)")


def extract_item_id(href: str) -> str | None:
    m = _ITEM_ID_RE.search(href)
    return m.group(1) if m else None


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
        source="fb_marketplace",
        source_listing_id=str(listing_id),
        title=title,
        model_number=extract_model_number(title),
        brand=extract_brand(title),
        price=price,
        shipping=settings.scrape_default_shipping,
        condition="unknown",
        url=url if url.startswith("http") else f"{BASE}{url}",
        image_url=image_url,
        location=location,
    )


class FacebookMarketplaceSource(ScraperSource):
    name = "fb_marketplace"
    requires_login = True

    @property
    def _enabled_flag(self) -> bool:
        return self.settings.enable_fb_marketplace

    def _search_url(self, query: str) -> str:
        # Facebook uses a location-scoped path; callers set FB_LOCATION_SLUG via
        # a persisted session/cookie, so we use the generic search here.
        parts = [f"query={quote_plus(query)}"]
        if self.settings.scrape_max_price is not None:
            parts.append(f"maxPrice={int(self.settings.scrape_max_price)}")
        parts.append("sortBy=creation_time_descend")
        return f"{BASE}/marketplace/search/?{'&'.join(parts)}"

    async def _is_logged_in(self, page) -> bool:
        # A logged-out session redirects to a login wall.
        return "login" not in page.url.lower()

    async def _scrape(self) -> AsyncIterator[Listing]:
        page = await self._context.new_page()
        try:
            for query in self.queries:
                url = self._search_url(query)
                if not await self._goto_with_retry(page, url):
                    continue
                await self._delay()

                if not await self._is_logged_in(page):
                    log.warning("fb_not_logged_in", hint="seed data/sessions/fb_marketplace.json")
                    return

                # Human-like scroll to trigger lazy loading.
                for _ in range(3):
                    await page.mouse.wheel(0, 2000)
                    await self._delay()

                anchors = await page.query_selector_all("a[href*='/marketplace/item/']")
                log.info("fb_cards", query=query, count=len(anchors))
                seen_ids: set[str] = set()
                for a in anchors:
                    listing = await self._parse_anchor(a)
                    if listing is not None and listing.source_listing_id not in seen_ids:
                        seen_ids.add(listing.source_listing_id)
                        yield listing
        finally:
            await page.close()

    async def _parse_anchor(self, a) -> Listing | None:
        try:
            href = await a.get_attribute("href") or ""
            item_id = extract_item_id(href)
            if not item_id:
                return None
            text = (await a.inner_text()).strip()
            # Marketplace anchor text is typically "£120\nTitle\nLocation".
            lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
            price = next((parse_price(ln) for ln in lines if parse_price(ln) is not None), None)
            title = next((ln for ln in lines if parse_price(ln) is None), None)
            location = lines[-1] if len(lines) > 2 else self.settings.scrape_location or None
            img_el = await a.query_selector("img")
            image_url = await img_el.get_attribute("src") if img_el else None

            if not (title and price):
                return None
            return build_listing(
                self.settings,
                listing_id=item_id,
                title=title,
                price=price,
                url=href.split("?")[0],
                image_url=image_url,
                location=location,
            )
        except Exception as exc:
            log.debug("fb_anchor_parse_failed", error=str(exc))
            return None
