"""Recognize the same flat listed on several sites.

Agents often advertise one flat on willhaben and ImmoScout24 at once. Listing
IDs differ between sites, so a match has to come from the facts: same postal
code, (almost) the same area, the same number of rooms and a similar price.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from immob.models import Listing, StoredListing
from immob.storage import Storage

AREA_TOLERANCE = 0.03  # 3 %: sites round areas differently (54 vs 54.3 m²)
PRICE_TOLERANCE = 0.05  # 5 %: an agent may update the price on one site first


def same_flat(a: Listing, b: Listing) -> bool:
    """Whether two listings from different sites are probably the same flat.

    Area and price must be known on both: without them a match would be a guess.
    """
    if a.source == b.source:
        return False  # one site's listings are told apart by their IDs
    if a.area_m2 is None or b.area_m2 is None or a.price_eur is None or b.price_eur is None:
        return False
    if a.postal_code and b.postal_code:
        if a.postal_code != b.postal_code:
            return False
    elif not _same_place(a.location, b.location):
        return False
    if a.rooms is not None and b.rooms is not None and a.rooms != b.rooms:
        return False
    return _close(a.area_m2, b.area_m2, AREA_TOLERANCE) and _close(
        a.price_eur, b.price_eur, PRICE_TOLERANCE
    )


def find_original(store: Storage, listing: Listing) -> StoredListing | None:
    """The earlier listing of the same flat on another site, if there is one.

    With several candidates, the one with the most similar title wins.
    """
    candidates = [
        other
        for other in store.duplicate_candidates(listing, AREA_TOLERANCE)
        if same_flat(listing, other)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda other: _similarity(listing.title, other.title))


def _close(a: float, b: float, tolerance: float) -> bool:
    return abs(a - b) <= tolerance * max(a, b)


def _same_place(a: str | None, b: str | None) -> bool:
    return bool(a and b) and a.strip().lower() == b.strip().lower()


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()
