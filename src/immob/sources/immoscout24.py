"""immobilienscout24.at, Austria's second big property portal.

Unlike willhaben it is not a Next.js site, and the two page types deliver
their data differently (checked against saved pages, October 2026):

- Search pages embed their state as `window.__INITIAL_STATE__ = {...}`: a
  JavaScript object, almost JSON (it contains `undefined`). The listings are at
  `reduxAsyncConnect.pageData.results.hits`.
- Listing ("expose") pages describe the flat in schema.org JSON-LD (price,
  area, rooms, address). Floor, year, heating, HWB, costs and commission are
  only in the visible text, as a label on one line and its value on the next:
  "Etage" / "2", "Heizwärmebedarf (HWB)" / "203. kWh/(m²a)".

The site writes decimals with a dot ("2.28", "3.00 %"), so numbers that may
have decimals are parsed with `_decimal`, not the Austrian-style parse_number.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser

from immob.extract import (
    fill_from_text,
    parse_commission_fee,
    parse_floor_value,
    parse_number,
)
from immob.http import PoliteClient
from immob.models import Listing
from immob.sources.base import ParseError, Source, merge_details

log = logging.getLogger(__name__)

BASE_URL = "https://www.immobilienscout24.at"

# Labels on listing pages; the value is on the next line of the visible text.
_LABELS = {
    "floor": "Etage",
    "year_built": "Baujahr",
    "heating": "Heizung",
    "hwb": "Heizwärmebedarf (HWB)",
    "operating_costs": "Betriebskosten",
    "operating_costs_vat": "USt. Betriebskosten",
    "heating_costs": "Heizkosten",
}
_COMMISSION_LINE = re.compile(r"^Provision[^:\n]*:\s*(.+)$", re.MULTILINE)
_ADDRESS = re.compile(r"^(\d{4})\s+(.+)$")  # "9413 St. Gertraud"
_DECIMAL = re.compile(r"\d+(?:[.,]\d+)?")
# `undefined` as a value: after ":", "[" or "," and before ",", "]" or "}".
_UNDEFINED = re.compile(r"([:\[,]\s*)undefined(?=\s*[,\]}])")


class ImmoScout24Source(Source):
    name = "immoscout24"

    def __init__(self, client: PoliteClient, *, search_url: str) -> None:
        self._client = client
        self.search_url = search_url

    def search(self, max_pages: int) -> Iterator[Listing]:
        url: str | None = self.search_url
        fetched: set[str] = set()
        seen = 0
        for _ in range(max_pages):
            if url is None or url in fetched:
                break
            fetched.add(url)
            results = find_results(initial_state(self._client.get_text(url)))
            hits = results.get("hits") or []
            for hit in hits:
                try:
                    yield parse_hit(hit)
                except (KeyError, ValueError, TypeError) as exc:
                    log.warning("skipping unparseable hit %s: %s", hit.get("exposeId"), exc)

            seen += len(hits)
            self.total_found = int(results.get("totalHits") or 0)
            if not hits or seen >= self.total_found:
                break
            url = next_page_url(results.get("pagination"), fetched)

    def fetch_details(self, listing: Listing) -> Listing:
        detailed, detail_text = parse_expose(self._client.get_text(listing.url), listing.url)
        summary = parse_hit(listing.raw) if listing.raw else listing
        return merge_details(summary, listing.search_text, detailed, detail_text)


def initial_state(page_html: str) -> dict[str, Any]:
    """The object a search page assigns to `window.__INITIAL_STATE__`."""
    marker = "window.__INITIAL_STATE__="
    start = page_html.find(marker)
    if start == -1:
        raise ParseError(
            "no window.__INITIAL_STATE__ in page: the site layout changed or the request "
            "was blocked"
        )
    # JavaScript, not JSON: `undefined` must become null before json can parse it.
    text = _UNDEFINED.sub(r"\1null", page_html[start + len(marker) :])
    try:
        # raw_decode stops where the object ends; more JavaScript follows it.
        return json.JSONDecoder().raw_decode(text)[0]
    except json.JSONDecodeError as exc:
        raise ParseError(f"window.__INITIAL_STATE__ isn't parseable: {exc}") from None


def find_results(state: Any) -> dict[str, Any]:
    """The search result object: the first dict with both "hits" and "totalHits"."""
    if isinstance(state, dict):
        if "hits" in state and "totalHits" in state:
            return state
        children = state.values()
    elif isinstance(state, list):
        children = state
    else:
        children = []
    for child in children:
        try:
            return find_results(child)
        except ParseError:
            continue
    raise ParseError("no search results (hits/totalHits) in window.__INITIAL_STATE__")


def next_page_url(pagination: Any, fetched: set[str]) -> str | None:
    """The next result page's URL, or None.

    Only seen on single-page searches so far, so this accepts both a "next" link
    and a list of all page links ("all"), absolute or relative.
    """
    if not isinstance(pagination, dict):
        return None
    candidates = [pagination.get("next")] + list(pagination.get("all") or [])
    for link in candidates:
        if isinstance(link, dict):
            link = link.get("url") or link.get("href")
        if isinstance(link, str) and link:
            url = urljoin(BASE_URL, link)
            if url not in fetched:
                return url
    return None


def parse_hit(hit: dict[str, Any]) -> Listing:
    """One search result."""
    expose_id = str(hit["exposeId"])
    links = hit.get("links") or {}
    url = links.get("absoluteURL") or urljoin(BASE_URL, links.get("targetURL") or "")
    postal_code, location = _split_address(hit.get("addressString"))
    badges = " ".join(
        str((badge.get("details") or {}).get("value") or "")
        for badge in hit.get("typedBadges") or []
    )
    listing = Listing(
        source=ImmoScout24Source.name,
        source_id=expose_id,
        url=url or f"{BASE_URL}/expose/{expose_id}",
        title=hit.get("headline") or "",
        price_eur=_int(hit.get("primaryPrice")),
        area_m2=_float(hit.get("primaryArea")),
        rooms=_float(hit.get("numberOfRooms")),
        postal_code=postal_code,
        location=location,
        is_private=hit.get("isPrivate"),
        # Badges are codes such as PARKING_GENERIC or FURNISHED_PART.
        has_garage=True if "GARAGE" in badges else None,
        has_parking=True if "PARKING" in badges or "GARAGE" in badges else None,
        has_balcony=True if re.search(r"BALCONY|TERRACE|LOGGIA", badges) else None,
        has_cellar=True if re.search(r"CELLAR|BASEMENT", badges) else None,
        has_elevator=True if re.search(r"ELEVATOR|LIFT", badges) else None,
        raw=hit,
    )
    return fill_from_text(listing)


def parse_expose(page_html: str, url: str) -> tuple[Listing, str]:
    """A listing page: the listing from structured data, plus its visible text."""
    tree = LexborHTMLParser(page_html)
    graph = _json_ld_graph(tree)
    product = _node(graph, "Product")
    offer = (product or {}).get("offers") or {}
    estate = _node(graph, "RealEstateListing") or {}
    if not product and not estate:
        raise ParseError(f"no schema.org listing data (JSON-LD) in {url}")
    address = estate.get("address") or {}

    for node in tree.css("script, style"):
        node.decompose()  # keep only what a visitor sees
    root = tree.css_first("main") or tree.body
    lines = [line.strip() for line in root.text(separator="\n").splitlines() if line.strip()]
    text = "\n".join(lines)
    labels = _label_values(lines)

    floor, floor_label = parse_floor_value(labels.get("floor"))
    year = labels.get("year_built", "")
    operating = parse_number(labels.get("operating_costs"))
    operating_vat = parse_number(labels.get("operating_costs_vat"))
    commission = _COMMISSION_LINE.search(text)
    commission_text = commission.group(1).strip() if commission else None
    expose_id = url.rstrip("/").rsplit("/", 1)[-1]

    listing = Listing(
        source=ImmoScout24Source.name,
        source_id=expose_id,
        url=url,
        title=estate.get("name") or (product or {}).get("name") or "",
        price_eur=_int(offer.get("price")),
        area_m2=_float((estate.get("floorSize") or {}).get("value")),
        rooms=_float(estate.get("numberOfRooms")),
        postal_code=address.get("postalCode"),
        location=address.get("addressLocality"),
        description=_clean_html(estate.get("description") or (product or {}).get("description")),
        floor=floor,
        floor_label=floor_label,
        year_built=int(year) if year.isdigit() else None,
        heating=labels.get("heating"),
        hwb=_decimal(labels.get("hwb")),
        # Operating costs plus their VAT, the closest match to willhaben's gross figure.
        monthly_costs_eur=(operating + (operating_vat or 0)) if operating else None,
        heating_costs_eur=parse_number(labels.get("heating_costs")),
        commission_text=commission_text,
        commission_free=parse_commission_fee(commission_text) if commission_text else None,
        # Garage, lift, balcony, cellar: left to the text extraction in
        # merge_details, after the search page's badges.
        raw={"json_ld": graph, "labels": labels},
    )
    return listing, text


def _json_ld_graph(tree: LexborHTMLParser) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for script in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text())
        except json.JSONDecodeError:
            continue
        items = data.get("@graph", [data]) if isinstance(data, dict) else data
        nodes += [item for item in items if isinstance(item, dict)]
    return nodes


def _node(graph: list[dict[str, Any]], type_name: str) -> dict[str, Any] | None:
    return next((node for node in graph if node.get("@type") == type_name), None)


def _label_values(lines: list[str]) -> dict[str, str]:
    """Values of the known labels: the line after each label line."""
    wanted = {label: field for field, label in _LABELS.items()}
    values: dict[str, str] = {}
    for line, following in zip(lines, lines[1:], strict=False):
        field = wanted.get(line)
        if field and field not in values and following not in ("-", ","):
            values[field] = following
    return values


def _split_address(address: str | None) -> tuple[str | None, str | None]:
    if not address:
        return None, None
    if match := _ADDRESS.match(address.strip()):
        return match.group(1), match.group(2)
    return None, address.strip()


def _decimal(value: str | None) -> float | None:
    """First number in the value, "." or "," as decimal point: "203. kWh" -> 203.0."""
    if value is None or not (match := _DECIMAL.search(value)):
        return None
    return float(match.group().replace(",", "."))


def _clean_html(text: str | None) -> str | None:
    if not text:
        return None
    plain = LexborHTMLParser(text).text(separator=" ")
    return re.sub(r"\s+", " ", plain).strip() or None


def _int(value: Any) -> int | None:
    number = _float(value)
    return round(number) if number is not None else None


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return parse_number(str(value))
