"""Base class for Playwright-based scrapers.

Responsible-scraping behaviour is baked in so individual site scrapers stay
small: rate limiting with randomised jitter, rotating user-agents, persistent
per-site sessions/cookies, graceful retry with backoff, and a global on/off
switch per source. It exposes the same `Source.fetch()` interface as API
sources, so the pipeline treats scrapers identically.

NOTE: scraping Gumtree / Facebook Marketplace violates their Terms of Service.
This is a civil matter (account/IP bans, cease-and-desist), not criminal. Every
scraper is modular and toggled off by default; drop any that become painful
without touching the core.
"""

from __future__ import annotations

import abc
import asyncio
import random
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from arb.config import Settings
from arb.logging_conf import get_logger
from arb.models import Listing
from sources.base import Source

log = get_logger("sources.scraper")

# A small pool of realistic desktop user-agents to rotate through.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
]

SESSION_DIR = Path("data/sessions")


class ScraperSource(Source, abc.ABC):
    #: Whether a persistent logged-in session is required (e.g. Facebook).
    requires_login: bool = False

    def __init__(self, settings: Settings, queries: list[str]):
        self.settings = settings
        self.queries = queries
        self._playwright = None
        self._browser = None
        self._context = None

    # ---- config-driven on/off switch -----------------------------------
    @property
    @abc.abstractmethod
    def _enabled_flag(self) -> bool:
        """Read the per-source enable flag from settings."""

    @property
    def enabled(self) -> bool:
        return self._enabled_flag

    # ---- responsible-scraping helpers -----------------------------------
    async def _delay(self) -> None:
        lo = self.settings.scrape_min_delay_sec
        hi = self.settings.scrape_max_delay_sec
        await asyncio.sleep(random.uniform(lo, hi))

    def _user_agent(self) -> str:
        return random.choice(USER_AGENTS)

    def _storage_state_path(self) -> Path:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        return SESSION_DIR / f"{self.name}.json"

    async def _ensure_browser(self) -> None:
        if self._context is not None:
            return
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)

        state_path = self._storage_state_path()
        context_kwargs: dict[str, Any] = {"user_agent": self._user_agent()}
        if state_path.exists():
            context_kwargs["storage_state"] = str(state_path)
        self._context = await self._browser.new_context(**context_kwargs)

    async def _save_session(self) -> None:
        if self._context is not None:
            await self._context.storage_state(path=str(self._storage_state_path()))

    async def _goto_with_retry(self, page, url: str, attempts: int = 3):
        for i in range(attempts):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                return True
            except Exception as exc:  # network hiccup / anti-bot interstitial
                backoff = 2 ** i
                log.warning("scrape_goto_retry", url=url, attempt=i + 1, backoff=backoff, error=str(exc))
                await asyncio.sleep(backoff)
        return False

    async def aclose(self) -> None:
        try:
            if self._context is not None:
                await self._save_session()
                await self._context.close()
            if self._browser is not None:
                await self._browser.close()
            if self._playwright is not None:
                await self._playwright.stop()
        finally:
            self._context = self._browser = self._playwright = None

    # ---- subclasses implement the site-specific scrape ------------------
    @abc.abstractmethod
    async def _scrape(self) -> AsyncIterator[Listing]:
        ...

    async def fetch(self) -> AsyncIterator[Listing]:
        if not self.enabled:
            return
        await self._ensure_browser()
        async for listing in self._scrape():
            yield listing
