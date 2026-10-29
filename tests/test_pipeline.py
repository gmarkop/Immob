"""The crawl and notification steps together, with a fake source."""

from collections.abc import Iterator

import httpx
import pytest

from immob.filters import FilterProfile
from immob.models import Listing
from immob.notify import TelegramNotifier
from immob.pipeline import run_crawl, send_pending
from immob.sources.base import Source
from immob.storage import Storage


class FakeSource(Source):
    name = "fake"

    def __init__(self, listings: list[Listing], fail_details: bool = False) -> None:
        self.listings = listings
        self.fail_details = fail_details
        self.detail_requests: list[str] = []

    def search(self, max_pages: int) -> Iterator[Listing]:
        yield from self.listings

    def fetch_details(self, listing: Listing) -> Listing:
        self.detail_requests.append(listing.source_id)
        if self.fail_details:
            raise httpx.ConnectError("offline")
        return listing.model_copy(update={"floor": 2, "details_fetched": True})


def make(source_id: str, price: int) -> Listing:
    return Listing(
        source="fake", source_id=source_id, url=f"https://x/{source_id}", title="W", price_eur=price
    )


@pytest.fixture
def store():
    with Storage(":memory:") as storage:
        yield storage


def test_crawl_fetches_details_only_for_candidates(store):
    source = FakeSource([make("cheap", 150000), make("expensive", 400000)])
    profile = FilterProfile(price_max=200000, floor_min=1, include_unknown=False)

    stats = run_crawl(source, store, profile, max_pages=1)

    assert source.detail_requests == ["cheap"]
    assert (stats.seen, stats.new, stats.details_fetched) == (2, 2, 1)
    assert store.get("fake", "cheap").floor == 2

    # Second run: already detailed, so no new detail request.
    source.detail_requests.clear()
    stats = run_crawl(source, store, profile, max_pages=1)
    assert source.detail_requests == []
    assert stats.new == 0


def test_crawl_keeps_listing_when_details_fail(store):
    source = FakeSource([make("a", 150000)], fail_details=True)
    stats = run_crawl(source, store, FilterProfile(), max_pages=1)
    assert stats.errors == 1
    assert store.get("fake", "a") is not None


def test_send_pending_only_matching_and_marks_them(store):
    store.upsert(make("match", 150000))
    store.upsert(make("too-expensive", 400000))
    sent_texts = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_texts.append(request.content.decode())
        return httpx.Response(200, json={"ok": True})

    notifier = TelegramNotifier(
        "t", "1", transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    profile = FilterProfile(price_max=200000)

    assert send_pending(store, profile, notifier) == 1
    assert len(sent_texts) == 1
    assert send_pending(store, profile, notifier) == 0  # not sent twice
