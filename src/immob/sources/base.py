"""The interface every listing source implements.

Adding a portal means writing one new class with these two methods; the
crawl pipeline, storage, filters and notifications don't change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import ClassVar

from immob.models import Listing


class ParseError(Exception):
    """A page didn't have the structure the parser expects."""


class Source(ABC):
    name: ClassVar[str]

    # Set by search(): how many listings the site reports for the search, if known.
    total_found: int | None = None

    @abstractmethod
    def search(self, max_pages: int) -> Iterator[Listing]:
        """Yield listings from the search result pages (fast, fewer fields)."""

    @abstractmethod
    def fetch_details(self, listing: Listing) -> Listing:
        """Return the listing completed with data from its detail page (slow)."""
