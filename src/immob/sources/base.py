"""The interface every listing source implements.

Adding a portal means writing one new class with these two methods; the
crawl pipeline, storage, filters and notifications don't change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import ClassVar

from immob.extract import fill_from_text
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


def merge_details(
    summary: Listing, summary_text: str, detail: Listing, detail_text: str
) -> Listing:
    """Combine a search-page listing with its detail page.

    Structured fields are merged first (the detail page wins over the search
    page), and only then are gaps filled from text, so a text guess never
    overrides real data: "Fahrradraum im EG" in a description mustn't replace
    the floor a structured field stated.
    """
    known = {k: v for k, v in detail.model_dump().items() if v not in (None, "")}
    merged = summary.model_copy(update=known | {"details_fetched": True})
    merged = fill_from_text(merged, f"{detail_text}\n{summary_text}")
    merged.raw = detail.raw
    return merged
