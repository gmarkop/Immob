"""Filter profiles: your search criteria, saved as a YAML file.

Example: profiles/kaernten.yaml. Every criterion is optional.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from immob.models import BuildingType, Listing

# "required": must have it, "excluded": must not have it, "any": don't care.
Requirement = Literal["required", "excluded", "any"]


class WillhabenSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    region: str = "kaernten"
    property_type: str = "eigentumswohnung"
    # A search copied from the browser's address bar, with willhaben's own
    # filters and sort order. When set, region and property_type are ignored.
    search_url: str | None = None
    max_pages: int = 3

    @field_validator("search_url")
    @classmethod
    def _is_willhaben_search(cls, url: str | None) -> str | None:
        prefix = "https://www.willhaben.at/iad/immobilien/"
        if url is not None and not url.startswith(prefix):
            raise ValueError(f"must be a willhaben real-estate search starting with {prefix}")
        return url


class FilterProfile(BaseModel):
    # extra="forbid": a typo such as "price_mx" is an error, not silently ignored.
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)

    name: str = "default"

    price_min: int | None = None
    price_max: int | None = None
    # Count a garage space that costs extra ("Tiefgaragenplatz um € 18.000")
    # towards price_min/price_max.
    price_includes_garage: bool = False
    monthly_costs_max: float | None = None
    area_min: float | None = None
    area_max: float | None = None
    rooms_min: float | None = None
    rooms_max: float | None = None
    floor_min: int | None = None
    floor_max: int | None = None
    year_built_min: int | None = None
    year_built_max: int | None = None
    building_types: list[BuildingType] | None = None
    parking: Requirement = "any"  # any parking, including outdoor spaces and carports
    garage: Requirement = "any"  # a covered space: garage or Tiefgarage
    elevator: Requirement = "any"
    balcony: Requirement = "any"  # balcony, loggia or terrace
    cellar: Requirement = "any"
    commission_free: Requirement = "any"
    postal_code_prefixes: list[str] | None = None
    exclude_keywords: list[str] = []

    # What to do when an ad doesn't state something you filter on. True keeps
    # it (you may miss nothing, but see more noise); False drops it.
    include_unknown: bool = True

    willhaben: WillhabenSettings = WillhabenSettings()

    def matches(self, listing: Listing, *, include_unknown: bool | None = None) -> bool:
        return not self.rejections(listing, include_unknown=include_unknown)

    def rejections(self, listing: Listing, *, include_unknown: bool | None = None) -> list[str]:
        """Why the listing fails this profile; an empty list means it matches."""
        keep_unknown = self.include_unknown if include_unknown is None else include_unknown
        reasons: list[str] = []

        def check_range(label: str, value: float | None, low: float | None, high: float | None):
            if low is None and high is None:
                return
            if value is None:
                if not keep_unknown:
                    reasons.append(f"{label} unknown")
            elif low is not None and value < low:
                reasons.append(f"{label} {value:g} < {low:g}")
            elif high is not None and value > high:
                reasons.append(f"{label} {value:g} > {high:g}")

        def check_requirement(label: str, value: bool | None, requirement: Requirement):
            if requirement == "any":
                return
            if value is None:
                if not keep_unknown:
                    reasons.append(f"{label} unknown")
            elif requirement == "required" and not value:
                reasons.append(f"no {label}")
            elif requirement == "excluded" and value:
                reasons.append(f"has {label}")

        price = listing.total_price_eur if self.price_includes_garage else listing.price_eur
        check_range("price", price, self.price_min, self.price_max)
        check_range("monthly costs", listing.monthly_costs_eur, None, self.monthly_costs_max)
        check_range("area", listing.area_m2, self.area_min, self.area_max)
        check_range("rooms", listing.rooms, self.rooms_min, self.rooms_max)
        check_range("floor", listing.floor, self.floor_min, self.floor_max)
        check_range("year built", listing.year_built, self.year_built_min, self.year_built_max)
        check_requirement("parking", listing.has_parking, self.parking)
        check_requirement("garage", listing.has_garage, self.garage)
        check_requirement("elevator", listing.has_elevator, self.elevator)
        check_requirement("balcony", listing.has_balcony, self.balcony)
        check_requirement("cellar", listing.has_cellar, self.cellar)
        check_requirement("commission-free", listing.commission_free, self.commission_free)

        if self.building_types:
            if listing.building_type is None:
                if not keep_unknown:
                    reasons.append("building type unknown")
            elif listing.building_type not in self.building_types:
                reasons.append(f"building type {listing.building_type}")

        if self.postal_code_prefixes:
            if listing.postal_code is None:
                if not keep_unknown:
                    reasons.append("postal code unknown")
            elif not listing.postal_code.startswith(tuple(self.postal_code_prefixes)):
                reasons.append(f"postal code {listing.postal_code}")

        text = listing.search_text.lower()
        reasons += [f"mentions '{kw}'" for kw in self.exclude_keywords if kw.lower() in text]
        return reasons


def load_profile(path: Path) -> FilterProfile:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return FilterProfile.model_validate(data)
