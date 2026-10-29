"""willhaben.at, Austria's largest classifieds site.

willhaben is a Next.js app: every page embeds its data as JSON in a
`<script id="__NEXT_DATA__">` tag. Parsing that JSON is far more reliable than
scraping the visible HTML, which changes with every redesign.

Adverts carry their data as a list of attributes:

    {"id": "123", "attributes": {"attribute": [
        {"name": "PRICE", "values": ["189000"]},
        {"name": "NUMBER_OF_ROOMS", "values": ["3"]}, ...]}}

The search-page attribute names in ATTRIBUTE_NAMES were checked against a real
willhaben page (October 2026); detail-page-only names are still unverified.
Check them with `immob inspect` on a saved page (reload it with F5 before
saving: willhaben navigates client-side, which leaves the embedded JSON of the
first page you opened). Whatever the structured attributes miss is filled in
from the description text (see extract.py).
"""

from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Iterator
from typing import Any

from selectolax.lexbor import LexborHTMLParser

from immob.extract import (
    extract_building_type,
    extract_floor,
    fill_from_text,
    parse_number,
)
from immob.http import PoliteClient
from immob.models import Listing
from immob.sources.base import ParseError, Source

log = logging.getLogger(__name__)

BASE_URL = "https://www.willhaben.at"

# Candidate attribute names for each field, tried in order.
ATTRIBUTE_NAMES: dict[str, tuple[str, ...]] = {
    "title": ("HEADING",),
    "description": ("BODY_DYN", "DESCRIPTION"),
    "price": ("PRICE",),
    "price_display": ("PRICE_FOR_DISPLAY",),
    "area": ("ESTATE_SIZE/LIVING_AREA", "ESTATE_SIZE/USEABLE_AREA", "ESTATE_SIZE"),
    "rooms": ("NUMBER_OF_ROOMS", "NO_OF_ROOMS"),  # not ROOMS: that's a code like "2X2"
    "floor": ("FLOOR",),
    "year_built": ("CONSTRUCTION_YEAR", "YEAR_OF_CONSTRUCTION", "BUILDING_YEAR"),
    "building_type": ("BUILDING_TYPE", "BUILDING_CONDITION"),
    "postal_code": ("POSTCODE",),
    "location": ("LOCATION", "DISTRICT"),
    "seo_url": ("SEO_URL",),
    "is_private": ("ISPRIVATE",),
}
# Attributes whose values are not prose and would confuse the text extraction.
# (ESTATE_PREFERENCE holds numeric feature codes such as "15, 23, 250", whose
# meaning is unknown so far; numbers can't match the German keywords anyway.)
_NOT_TEXT = {
    "SEO_URL",
    "COORDINATES",
    "MMO",
    "ALL_IMAGE_URLS",
    "AD_SEARCHRESULT_LOGO",
    "PUBLISHED",
    "PUBLISHED_String",
}


class WillhabenSource(Source):
    name = "willhaben"

    def __init__(
        self,
        client: PoliteClient,
        *,
        region: str = "kaernten",
        property_type: str = "eigentumswohnung",
        rows: int = 90,
    ) -> None:
        self._client = client
        self.search_url = f"{BASE_URL}/iad/immobilien/{property_type}/{region}"
        self._rows = rows

    def search(self, max_pages: int) -> Iterator[Listing]:
        seen = 0
        for page in range(1, max_pages + 1):
            page_html = self._client.get_text(self.search_url, {"rows": self._rows, "page": page})
            result = find_key(extract_next_data(page_html), "searchResult")
            if not isinstance(result, dict):
                raise ParseError("no searchResult in __NEXT_DATA__")

            adverts = (result.get("advertSummaryList") or {}).get("advertSummary") or []
            for advert in adverts:
                try:
                    yield parse_advert(advert)
                except (KeyError, ValueError, ParseError) as exc:
                    # One odd advert shouldn't stop the whole crawl.
                    log.warning("skipping unparseable advert %s: %s", advert.get("id"), exc)

            # Count what actually arrived rather than trusting our `rows` request:
            # the site may serve fewer per page than asked for.
            seen += len(adverts)
            if not adverts or seen >= int(result.get("rowsFound") or 0):
                break

    def fetch_details(self, listing: Listing) -> Listing:
        page_html = self._client.get_text(listing.url)
        details = find_key(extract_next_data(page_html), "advertDetails")
        if not isinstance(details, dict):
            raise ParseError(f"no advertDetails in __NEXT_DATA__ of {listing.url}")
        detailed = parse_advert(details, fallback_url=listing.url)

        # Detail data wins where it has a value; the summary fills the rest.
        known = {k: v for k, v in detailed.model_dump().items() if v not in (None, "")}
        known["details_fetched"] = True
        merged = listing.model_copy(update=known)
        merged.raw = detailed.raw
        return merged


def extract_next_data(page_html: str) -> dict[str, Any]:
    node = LexborHTMLParser(page_html).css_first("script#__NEXT_DATA__")
    if node is None:
        raise ParseError(
            "no __NEXT_DATA__ script in page: the site layout changed or the request was blocked"
        )
    return json.loads(node.text())


def find_key(data: Any, key: str) -> Any:
    """Depth-first search for `key` in nested dicts/lists.

    Safer than a hard-coded path like data["props"]["pageProps"]["searchResult"],
    which breaks whenever the site moves things around.
    """
    if isinstance(data, dict):
        if key in data:
            return data[key]
        children = data.values()
    elif isinstance(data, list):
        children = data
    else:
        return None
    for child in children:
        if (found := find_key(child, key)) is not None:
            return found
    return None


def find_adverts(data: Any) -> list[dict[str, Any]]:
    """Every advert-shaped object in the data, wherever it sits.

    An advert is recognized by its shape ({"attributes": {"attribute": [...]}})
    rather than by the key it is stored under, so renamed keys don't hide it.
    """
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            attrs = node.get("attributes")
            if isinstance(attrs, dict) and isinstance(attrs.get("attribute"), list):
                found.append(node)
                return
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(data)
    return found


def attributes(advert: dict[str, Any]) -> dict[str, list[str]]:
    """willhaben's attribute list as a {name: values} dict."""
    items = (advert.get("attributes") or {}).get("attribute") or []
    return {item["name"]: [str(v) for v in item.get("values") or []] for item in items}


def parse_advert(advert: dict[str, Any], fallback_url: str | None = None) -> Listing:
    attrs = attributes(advert)

    def first(field: str) -> str | None:
        for name in ATTRIBUTE_NAMES[field]:
            if values := attrs.get(name):
                return values[0]
        return None

    source_id = str(advert["id"])
    if seo_url := first("seo_url"):
        url = f"{BASE_URL}/iad/{seo_url.lstrip('/')}"
    else:
        url = fallback_url or f"{BASE_URL}/iad/object?adId={source_id}"

    price = parse_number(first("price")) or parse_number(first("price_display"))
    floor, floor_label = _parse_floor(first("floor"))
    year = parse_number(first("year_built"))
    building_type_text = first("building_type")
    is_private = first("is_private")

    listing = Listing(
        source=WillhabenSource.name,
        source_id=source_id,
        url=url,
        title=first("title") or advert.get("description") or "",
        price_eur=round(price) if price else None,
        area_m2=parse_number(first("area")),
        rooms=parse_number(first("rooms")),
        floor=floor,
        floor_label=floor_label,
        year_built=int(year) if year else None,
        building_type=extract_building_type(building_type_text) if building_type_text else None,
        postal_code=first("postal_code"),
        location=first("location"),
        description=_clean_html(first("description")),
        is_private=None if is_private is None else is_private == "1",
        raw=advert,
    )

    # Feature lists (e.g. "Garage", "Lift") often live in other attributes, so
    # search those values too, not just title and description.
    extra = " ".join(
        value for name, values in attrs.items() if name not in _NOT_TEXT for value in values
    )
    return fill_from_text(listing, f"{listing.search_text}\n{extra}")


def _parse_floor(value: str | None) -> tuple[int | None, str | None]:
    if value is None:
        return None, None
    try:
        return int(value), None  # willhaben sends plain numbers, e.g. "1"
    except ValueError:
        return extract_floor(value)


def _clean_html(text: str | None) -> str | None:
    """Descriptions may contain HTML such as <br />; keep plain text only."""
    if text is None:
        return None
    return html.unescape(re.sub(r"<[^>]+>", " ", text)).strip()
