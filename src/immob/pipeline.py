"""The crawl and notify steps, independent of the command line."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from immob.dedup import find_original
from immob.filters import FilterProfile
from immob.models import StoredListing
from immob.notify import TelegramNotifier, format_listing
from immob.sources.base import ParseError, Source
from immob.storage import Change, Storage

log = logging.getLogger(__name__)


@dataclass
class CrawlStats:
    seen: int = 0
    new: int = 0
    price_changed: int = 0
    details_fetched: int = 0
    errors: int = 0
    total_found: int | None = None  # what the site reports for the whole search
    # The search itself failed (not just a detail page): the site may have changed.
    search_failed: bool = False
    # Every listing of the search was read, so missing ones can count as gone.
    complete: bool = False
    gone: int = 0
    duplicates: int = 0


def run_crawl(
    source: Source,
    store: Storage,
    profile: FilterProfile,
    *,
    max_pages: int,
    fetch_details: bool = True,
    max_details: int = 25,
    refresh_details: bool = False,
) -> CrawlStats:
    """Read the source's search pages into the store.

    `refresh_details` re-fetches detail pages already parsed before, e.g. after
    the parser learned new fields.
    """
    stats = CrawlStats()
    seen_ids: set[str] = set()
    try:
        for listing in source.search(max_pages):
            stats.seen += 1
            seen_ids.add(listing.source_id)
            stored = store.get(*listing.key)

            # Detail pages cost one slow request each, so only fetch them for
            # listings we haven't detailed before that could still match.
            # Unknown values must pass here (include_unknown=True): the detail
            # page is exactly where we hope to find them.
            if (
                fetch_details
                and stats.details_fetched < max_details
                and (refresh_details or not (stored and stored.details_fetched))
                and profile.matches(listing, include_unknown=True)
            ):
                try:
                    listing = source.fetch_details(listing)
                    stats.details_fetched += 1
                except (httpx.HTTPError, ParseError) as exc:
                    log.warning("could not fetch details for %s: %s", listing.url, exc)
                    stats.errors += 1

            change = store.upsert(listing)
            if change is Change.NEW:
                stats.new += 1
                if original := find_original(store, listing):
                    # Already known from another site: link it, and don't
                    # announce it as new. Its price drops are still notified.
                    store.set_duplicate_of(listing, original)
                    store.mark_notified(listing)
                    stats.duplicates += 1
            elif change is Change.PRICE_CHANGED:
                stats.price_changed += 1
    except (httpx.HTTPError, ParseError) as exc:
        # Keep what was collected so far; the next run picks up the rest.
        log.error("crawl of %s stopped early: %s", source.name, exc)
        stats.errors += 1
        stats.search_failed = True
    stats.total_found = source.total_found

    # Only a crawl that read the whole search can tell what's gone: after a
    # partial one, a missing listing may just be on a page that wasn't read.
    stats.complete = (
        not stats.search_failed
        and stats.total_found is not None
        and stats.seen >= stats.total_found
    )
    if stats.complete:
        stats.gone = store.mark_gone(source.name, seen_ids)
    return stats


def send_pending(
    store: Storage, profile: FilterProfile, notifier: TelegramNotifier, *, limit: int = 20
) -> int:
    """Notify about new matching listings and price drops. Returns messages sent.

    A listing is marked as notified only after its message went out, so a
    Telegram outage just delays the notification to the next run. Anything over
    `limit` also waits for the next run.
    """
    pending = [listing for listing in store.pending_notifications() if profile.matches(listing)]
    sent = 0
    for listing in pending[:limit]:
        notifier.send(format_listing(listing, also_on=store.duplicates_of(listing)))
        store.mark_notified(listing)
        sent += 1
    return sent


def current_matches(
    store: Storage, profile: FilterProfile, *, max_age_days: int = 7
) -> list[StoredListing]:
    """Stored listings that match the profile, cheapest first, one per flat.

    Left out: listings that are gone or haven't been seen for `max_age_days`
    (most likely sold), and copies of a flat whose original listing is still
    active. When the original is gone, the copy on the other site takes over.
    """
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    listings = store.all()
    active = {
        listing.ref
        for listing in listings
        if listing.gone_at is None and listing.last_seen >= cutoff
    }
    return [
        listing
        for listing in listings
        if listing.ref in active and listing.duplicate_of not in active and profile.matches(listing)
    ]


def resend(store: Storage, notifier: TelegramNotifier, listings: list[StoredListing]) -> int:
    """Send these listings again, whether or not they were notified before."""
    for listing in listings:
        notifier.send(format_listing(listing, resent=True, also_on=store.duplicates_of(listing)))
        store.mark_notified(listing)
    return len(listings)
