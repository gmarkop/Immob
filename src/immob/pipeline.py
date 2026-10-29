"""The crawl and notify steps, independent of the command line."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from immob.filters import FilterProfile
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


def run_crawl(
    source: Source,
    store: Storage,
    profile: FilterProfile,
    *,
    max_pages: int,
    fetch_details: bool = True,
    max_details: int = 25,
) -> CrawlStats:
    stats = CrawlStats()
    try:
        for listing in source.search(max_pages):
            stats.seen += 1
            stored = store.get(*listing.key)

            # Detail pages cost one slow request each, so only fetch them for
            # listings we haven't detailed before that could still match.
            # Unknown values must pass here (include_unknown=True): the detail
            # page is exactly where we hope to find them.
            if (
                fetch_details
                and stats.details_fetched < max_details
                and not (stored and stored.details_fetched)
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
            elif change is Change.PRICE_CHANGED:
                stats.price_changed += 1
    except (httpx.HTTPError, ParseError) as exc:
        # Keep what was collected so far; the next run picks up the rest.
        log.error("crawl of %s stopped early: %s", source.name, exc)
        stats.errors += 1
    stats.total_found = source.total_found
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
        notifier.send(format_listing(listing))
        store.mark_notified(listing)
        sent += 1
    return sent
