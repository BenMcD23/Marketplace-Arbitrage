"""Abstract Source interface.

Every source — API or scraper — is a `Source` that yields `Listing` objects.
The pipeline only ever sees `Listing`s, so downstream code never changes when a
new source is added or an old one is dropped.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator

from arb.models import Listing


class Source(abc.ABC):
    #: short stable identifier, e.g. "ebay", "gumtree", "fb_marketplace"
    name: str = "base"

    @property
    def enabled(self) -> bool:
        """Whether this source should run. Overridden per-source via config."""
        return True

    @abc.abstractmethod
    async def fetch(self) -> AsyncIterator[Listing]:
        """Yield normalised Listing objects for the configured search(es)."""
        raise NotImplementedError
        yield  # pragma: no cover  (marks this as an async generator)

    async def aclose(self) -> None:
        """Release any resources (browser, http client). Default: no-op."""
        return None
