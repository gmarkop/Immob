"""Extract structured facts from free-text German listing descriptions.

Portals only offer structured fields for some facts. Floor, parking, the
agent's commission or the construction year are often only mentioned in the
prose ("Tiefgaragenplatz im Kaufpreis inkludiert", "3. OG mit Lift"), so we
look for them with regular expressions.

Every extractor returns None when the text says nothing: "unknown" is a
different fact from "no".
"""

from __future__ import annotations

import re
from datetime import date

from immob.models import BuildingType, Listing

# --- Numbers -----------------------------------------------------------------

# "189.000" or "1.250.000": '.' used as a thousands separator, Austrian style.
_GROUPED_THOUSANDS = re.compile(r"^\d{1,3}(?:\.\d{3})+$")
_HUMAN_NUMBER = re.compile(r"\d[\d. ]*(?:,\d+)?")


def parse_number(value: str | None) -> float | None:
    """Parse a number from a portal field.

    Handles both machine values ("189000", "65.5") and Austrian human-formatted
    ones ("€ 189.000", "65,5 m²"), which use '.' for thousands and ',' for decimals.
    """
    if value is None:
        return None
    value = value.strip().replace("\u00a0", " ")
    if not _GROUPED_THOUSANDS.match(value):
        try:
            return float(value)
        except ValueError:
            pass
    match = _HUMAN_NUMBER.search(value)
    if not match:
        return None
    digits = match.group().strip().replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return float(digits)
    except ValueError:
        return None


# --- Floor -------------------------------------------------------------------

_FLOOR_NUMBERED = re.compile(
    r"\b(\d{1,2})\s*\.\s*(?:OG|Obergescho(?:ss|ß)|Stock|Stockwerk|Etage|Gescho(?:ss|ß))\b",
    re.IGNORECASE,
)
# "EG"/"DG" must be upper case, otherwise ordinary words could match.
_FLOOR_ATTIC = re.compile(r"\bDachgescho(?:ss|ß)\w*|(?-i:\bDG\b)", re.IGNORECASE)
_FLOOR_GROUND = re.compile(
    r"\b(?:Erdgescho(?:ss|ß)|Hochparterre|Parterre|Gartengescho(?:ss|ß))\w*|(?-i:\bEG\b)",
    re.IGNORECASE,
)


def extract_floor(text: str) -> tuple[int | None, str | None]:
    """Return (floor number, label). The attic ("DG") has a label but no number.

    The most specific pattern wins: "Wohnung im 3. OG, Fahrradraum im EG" is on
    the 3rd floor.
    """
    if match := _FLOOR_NUMBERED.search(text):
        floor = int(match.group(1))
        return floor, f"{floor}. OG"
    if _FLOOR_ATTIC.search(text):
        return None, "DG"
    if match := _FLOOR_GROUND.search(text):
        label = "Hochparterre" if "hochparterre" in match.group().lower() else "EG"
        return 0, label
    return None, None


# --- Year built and building type --------------------------------------------

_YEAR_BUILT = re.compile(
    r"\b(?:Baujahr|Bj|errichtet|erbaut|Fertigstellung)\b\.?\s*"
    r"(?:(?:im\s+)?Jahre?\s*)?:?\s*(?:ca\.?\s*)?(\d{4})\b",
    re.IGNORECASE,
)


def extract_year_built(text: str) -> int | None:
    for match in _YEAR_BUILT.finditer(text):
        year = int(match.group(1))
        if 1800 <= year <= date.today().year + 5:
            return year
    return None


_ALTBAU = re.compile(r"\bAltbau", re.IGNORECASE)
_NEUBAU = re.compile(r"\bNeubau", re.IGNORECASE)
# "Erstbezug nach Sanierung" (first occupancy after renovation) is common for
# old buildings, so only a plain "Erstbezug" means new construction.
_FIRST_OCCUPANCY = re.compile(r"\bErstbezug\b(?!\s+nach)", re.IGNORECASE)


def extract_building_type(text: str, year_built: int | None = None) -> BuildingType | None:
    if _ALTBAU.search(text):
        return BuildingType.ALTBAU
    if _NEUBAU.search(text):
        return BuildingType.NEUBAU
    if year_built is not None:
        return BuildingType.ALTBAU if year_built < 1945 else BuildingType.NEUBAU
    if _FIRST_OCCUPANCY.search(text):
        return BuildingType.NEUBAU
    return None


# --- Yes/no features ---------------------------------------------------------


def _yes_no(text: str, positive: re.Pattern[str], negative: re.Pattern[str]) -> bool | None:
    """Check the negative pattern first: "kein Lift" also contains "Lift"."""
    if negative.search(text):
        return False
    if positive.search(text):
        return True
    return None


_PARKING_WORDS = (
    r"(?:Tief)?garage\w*|TG[- ]?(?:Platz|Stellplatz|Box)|(?:PKW[- ]?|Auto)?Stellpl[aä]tz\w*"
    r"|Autoabstellpl[aä]tz\w*|Parkpl[aä]tz\w*|Carport\w*"
)
_PARKING = re.compile(rf"\b(?:{_PARKING_WORDS})", re.IGNORECASE)
_NO_PARKING = re.compile(
    rf"\b(?:kein(?:e|en)?|ohne)\s+(?:eigene[nrm]?\s+)?(?:{_PARKING_WORDS})", re.IGNORECASE
)


def extract_parking(text: str) -> bool | None:
    return _yes_no(text, _PARKING, _NO_PARKING)


_ELEVATOR_WORDS = r"(?:Personen)?(?:Lift|Aufzug|Fahrstuhl)(?:anlage|e)?"
_ELEVATOR = re.compile(rf"\b{_ELEVATOR_WORDS}\b", re.IGNORECASE)
_NO_ELEVATOR = re.compile(rf"\b(?:kein(?:en)?|ohne)\s+{_ELEVATOR_WORDS}\b", re.IGNORECASE)


def extract_elevator(text: str) -> bool | None:
    return _yes_no(text, _ELEVATOR, _NO_ELEVATOR)


_COMMISSION_FREE = re.compile(
    r"provisionsfrei"
    r"|(?:keine|ohne)\s+(?:Käufer|Makler|Vermittlungs)?provision"
    r"|(?<![\d,.])0\s*%\s*(?:Käufer)?provision"
    r"|provision\s*:?\s*(?:keine|0(?:[.,]0+)?\s*%)",
    re.IGNORECASE,
)
_COMMISSION = re.compile(r"\b(?:Käufer|Makler|Vermittlungs)?provision\w*", re.IGNORECASE)


def extract_commission_free(text: str) -> bool | None:
    """True for "provisionsfrei", False if a commission is mentioned at all.

    The order is reversed compared to `_yes_no`: "keine Provision" also contains
    "Provision", so the commission-free phrasing has to be checked first.
    """
    if _COMMISSION_FREE.search(text):
        return True
    if _COMMISSION.search(text):
        return False
    return None


# --- Putting it together -----------------------------------------------------


def fill_from_text(listing: Listing, text: str | None = None) -> Listing:
    """Fill the listing's unknown fields from free text; known fields are kept.

    Structured portal data is more reliable than our regexes, so text only ever
    fills gaps.
    """
    text = text if text is not None else listing.search_text
    updates: dict[str, object] = {}

    if listing.floor is None and listing.floor_label is None:
        floor, label = extract_floor(text)
        updates.update(floor=floor, floor_label=label)
    year_built = listing.year_built
    if year_built is None:
        year_built = updates["year_built"] = extract_year_built(text)
    if listing.building_type is None:
        updates["building_type"] = extract_building_type(text, year_built)
    if listing.has_parking is None:
        updates["has_parking"] = extract_parking(text)
    if listing.has_elevator is None:
        updates["has_elevator"] = extract_elevator(text)
    if listing.commission_free is None:
        commission_free = extract_commission_free(text)
        # Private sellers don't charge a commission unless they say otherwise.
        if commission_free is None and listing.is_private:
            commission_free = True
        updates["commission_free"] = commission_free

    return listing.model_copy(update=updates)
