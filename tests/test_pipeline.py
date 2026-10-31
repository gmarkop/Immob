"""The crawl and notification steps together, with a fake source."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from immob.filters import FilterProfile
from immob.models import Listing
from immob.notify import TelegramNotifier
from immob.pipeline import current_matches, resend, run_crawl, send_pending
from immob.sources.base import ParseError, Source
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


def test_refresh_details_refetches_known_listings(store):
    source = FakeSource([make("a", 150000)])
    run_crawl(source, store, FilterProfile(), max_pages=1)
    source.detail_requests.clear()

    run_crawl(source, store, FilterProfile(), max_pages=1, refresh_details=True)
    assert source.detail_requests == ["a"]


def test_failed_search_is_reported(store):
    class BrokenSource(FakeSource):
        def search(self, max_pages: int) -> Iterator[Listing]:
            yield make("a", 150000)
            raise ParseError("no __NEXT_DATA__ script in page")

    stats = run_crawl(BrokenSource([]), store, FilterProfile(), max_pages=1)

    assert stats.search_failed
    assert store.get("fake", "a") is not None  # what arrived before the failure is kept


def test_current_matches_skip_stale_and_non_matching(store):
    now = datetime.now(UTC)
    store.upsert(make("recent", 150000), now=now)
    store.upsert(make("stale", 150000), now=now - timedelta(days=30))  # likely sold
    store.upsert(make("too-expensive", 400000), now=now)

    matches = current_matches(store, FilterProfile(price_max=200000), max_age_days=7)

    assert [listing.source_id for listing in matches] == ["recent"]


def test_resend_includes_listings_notified_before(store):
    store.upsert(make("a", 150000))
    store.mark_all_notified()  # e.g. after `crawl --seed`
    sent_texts = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_texts.append(request.content.decode())
        return httpx.Response(200, json={"ok": True})

    notifier = TelegramNotifier(
        "t", "1", transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    assert send_pending(store, FilterProfile(), notifier) == 0  # nothing pending

    matches = current_matches(store, FilterProfile())
    assert resend(store, notifier, matches) == 1
    assert "Current match" in sent_texts[0]


def test_complete_crawl_marks_missing_listings_gone(store):
    run_crawl(FakeSource([make("a", 1), make("b", 1)]), store, FilterProfile(), max_pages=1)

    source = FakeSource([make("a", 1)])
    source.total_found = 1  # the site reports one listing, and we read it
    stats = run_crawl(source, store, FilterProfile(), max_pages=1)

    assert (stats.complete, stats.gone) == (True, 1)
    assert store.get("fake", "b").gone_at is not None
    assert [m.source_id for m in current_matches(store, FilterProfile())] == ["a"]


def test_partial_crawl_marks_nothing_gone(store):
    run_crawl(FakeSource([make("a", 1), make("b", 1)]), store, FilterProfile(), max_pages=1)

    source = FakeSource([make("a", 1)])
    source.total_found = 2  # one listing is on a page we didn't read
    stats = run_crawl(source, store, FilterProfile(), max_pages=1)

    assert (stats.complete, stats.gone) == (False, 0)
    assert store.get("fake", "b").gone_at is None


def _flat(source_name: str, source_id: str) -> Listing:
    return Listing(
        source=source_name,
        source_id=source_id,
        url=f"https://x/{source_name}/{source_id}",
        title="2-Zimmer-Wohnung",
        price_eur=199000,
        area_m2=54,
        rooms=2,
        postal_code="9500",
    )


def test_same_flat_on_second_site_is_linked_not_announced(store):
    willhaben = FakeSource([_flat("willhaben", "1")])
    willhaben.name = "willhaben"
    run_crawl(willhaben, store, FilterProfile(), max_pages=1)

    immoscout = FakeSource([_flat("immoscout24", "a")])
    immoscout.name = "immoscout24"
    stats = run_crawl(immoscout, store, FilterProfile(), max_pages=1)

    assert stats.duplicates == 1
    copy = store.get("immoscout24", "a")
    assert copy.duplicate_of == "willhaben:1"
    assert [p.ref for p in store.pending_notifications()] == ["willhaben:1"]  # one flat
    assert [m.ref for m in current_matches(store, FilterProfile())] == ["willhaben:1"]

    # The willhaben listing goes; the flat is still on ImmoScout24.
    store.mark_gone("willhaben", seen_ids=set())
    assert [m.ref for m in current_matches(store, FilterProfile())] == ["immoscout24:a"]
