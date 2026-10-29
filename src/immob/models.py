"""The normalized data model shared by every source.

Each portal describes an apartment differently. Sources translate their own
format into a `Listing`, so storage, filters and notifications only ever deal
with this one shape.

Convention used throughout the project: `None` means "the ad doesn't say".
That is a different fact from `False` ("no garage") or `0` ("ground floor"),
and the filters treat it differently.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class BuildingType(StrEnum):
    # Austrian convention: "Altbau" is a building from before 1945.
    ALTBAU = "altbau"
    NEUBAU = "neubau"


class Listing(BaseModel):
    """One apartment ad, normalized."""

    source: str
    source_id: str
    url: str
    title: str

    price_eur: int | None = None
    area_m2: float | None = None
    rooms: float | None = None  # Austrian ads use half rooms, e.g. 2.5
    floor: int | None = None  # 0 = ground floor (EG), 1 = 1. OG, ...
    floor_label: str | None = None  # as written, e.g. "DG" (attic) has no number
    year_built: int | None = None
    building_type: BuildingType | None = None
    has_parking: bool | None = None
    has_elevator: bool | None = None
    commission_free: bool | None = None
    postal_code: str | None = None
    location: str | None = None
    description: str | None = None
    is_private: bool | None = None  # private seller rather than an agent

    # True once the (slower) detail page has been parsed, not just the search result.
    details_fetched: bool = False

    # The source's original data, stored so listings can be re-parsed later
    # when the parser improves. Excluded from model_dump().
    raw: dict[str, Any] | None = Field(default=None, exclude=True, repr=False)

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.source_id)

    @property
    def price_per_m2(self) -> float | None:
        if self.price_eur is None or not self.area_m2:
            return None
        return self.price_eur / self.area_m2

    @property
    def floor_display(self) -> str | None:
        if self.floor_label:
            return self.floor_label
        if self.floor is None:
            return None
        return "EG" if self.floor == 0 else f"{self.floor}. OG"

    @property
    def search_text(self) -> str:
        """Title and description together, for keyword matching."""
        return f"{self.title}\n{self.description or ''}"


class StoredListing(Listing):
    """A listing as kept in the database, with tracking metadata."""

    first_seen: datetime
    last_seen: datetime
    # Price at the time we last sent a notification; used to detect price drops.
    notified_price: int | None = None
    notified_at: datetime | None = None


def format_eur(amount: float | None) -> str:
    """Austrian style: 189000 -> '€ 189.000'."""
    if amount is None:
        return "–"
    return "€ " + f"{amount:,.0f}".replace(",", ".")
