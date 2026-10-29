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
# "Stockwerk: 1" (needs the colon: "Anzahl Stockwerke: 4" is the building's height).
_FLOOR_LABELED = re.compile(
    r"\b(?:Stockwerk|Etage|Gescho(?:ss|ß))\s*:\s*(\d{1,2})\b(?!\s*\.)", re.IGNORECASE
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
    if match := _FLOOR_LABELED.search(text):
        floor = int(match.group(1))
        return floor, None
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


def _negation(words: str) -> re.Pattern[str]:
    """Negated mentions of `words`, before or after the word.

    Before: "kein Lift", "keinen Aufzug", "ohne eigenen Stellplatz".
    After:  "Lift: nein", "Lift ist keiner vorhanden", "Keller nicht vorhanden".
    Only "nicht vorhanden" counts after the word: "Garage nicht im Kaufpreis
    enthalten" means there is a garage that costs extra.
    """
    return re.compile(
        rf"\b(?:kein(?:e[nrm]?)?|ohne)\s+(?:eigene[nrm]?\s+)?(?:{words})"
        rf"|\b(?:{words})\s*(?::\s*(?:nein|keine[nrm]?)\b"
        rf"|(?:(?:ist|sind)\s+)?(?:keine[nrm]?|nicht)\s+vorhanden)",
        re.IGNORECASE,
    )


# A covered space. Not "Fahrradgarage" (bikes): only these prefixes count.
_GARAGE_WORDS = r"(?:Tief|Sammel|Duplex|Einzel|Doppel)?garage\w*|TG[- ]?(?:Platz|Stellplatz|Box)\w*"
_GARAGE = re.compile(rf"\b(?:{_GARAGE_WORDS})", re.IGNORECASE)
_NO_GARAGE = _negation(_GARAGE_WORDS)

_PARKING_WORDS = (
    rf"{_GARAGE_WORDS}|(?:PKW[- ]?|Auto)?Stellpl[aä]tz\w*"
    r"|Autoabstellpl[aä]tz\w*|Parkpl[aä]tz\w*|Carport\w*"
)
_PARKING = re.compile(rf"\b(?:{_PARKING_WORDS})", re.IGNORECASE)
_NO_PARKING = _negation(_PARKING_WORDS)


def extract_garage(text: str) -> bool | None:
    return _yes_no(text, _GARAGE, _NO_GARAGE)


def extract_parking(text: str) -> bool | None:
    """Any parking: garage, outdoor space or carport."""
    return _yes_no(text, _PARKING, _NO_PARKING)


# "€ 18.000", "EUR 18.000,-", "18.000,- Euro"
_EUR_AMOUNT = (
    r"(?:€|EUR|Euro)\s*(\d{1,3}(?:\.\d{3})+|\d{4,6})"
    r"|(\d{1,3}(?:\.\d{3})+|\d{4,6})(?:,[\d-]+)?\s*(?:€|EUR|Euro)"
)
# "Tiefgaragenplatz um € 18.000", "TG-Platz: € 15.000,-". The gap may not hold
# a full stop or digits, so the amount belongs to the same phrase.
_GARAGE_PRICE = re.compile(
    rf"\b(?:{_GARAGE_WORDS})[^.€\d\n]{{0,60}}?(?:{_EUR_AMOUNT})", re.IGNORECASE
)
_GARAGE_EXTRA = re.compile(
    rf"\b(?:{_GARAGE_WORDS})[^.]{{0,40}}?\bnicht\s+(?:im\s+(?:Kauf)?preis\s+)?"
    r"(?:inkludiert|inbegriffen|enthalten)"
    rf"|\b(?:zzgl\.?|zuzüglich)\s+(?:\w+\s+){{0,2}}(?:{_GARAGE_WORDS})",
    re.IGNORECASE,
)
_GARAGE_INCLUDED = re.compile(
    rf"\b(?:inkl\.?|inklusive|samt)\s+(?:\w+\s+){{0,2}}(?:{_GARAGE_WORDS})"
    rf"|\b(?:{_GARAGE_WORDS})[^.]{{0,40}}?\b(?:inkludiert|inbegriffen|im\s+(?:Kauf)?preis\s+enthalten)",
    re.IGNORECASE,
)


def extract_garage_price(text: str) -> int | None:
    """Extra cost of the garage space: an amount, 0 if included, None if unknown.

    "nicht im Kaufpreis enthalten" without an amount is a cost we can't put a
    number on, so it stays None rather than becoming 0.
    """
    for match in _GARAGE_PRICE.finditer(text):
        amount = int((match.group(1) or match.group(2)).replace(".", ""))
        if 1_000 <= amount <= 100_000:  # not a monthly rent, not the flat's price
            return amount
    if _GARAGE_EXTRA.search(text):
        return None
    if _GARAGE_INCLUDED.search(text):
        return 0
    return None


_ELEVATOR_WORDS = r"(?:Personen)?(?:Lift|Aufzug|Fahrstuhl)(?:anlage|e)?"
_ELEVATOR = re.compile(rf"\b{_ELEVATOR_WORDS}\b", re.IGNORECASE)
_NO_ELEVATOR = _negation(_ELEVATOR_WORDS)


def extract_elevator(text: str) -> bool | None:
    return _yes_no(text, _ELEVATOR, _NO_ELEVATOR)


# "Südbalkon" counts, so no word boundary before balkon/loggia; a terrace must
# be the flat's own, not a "Gemeinschaftsterrasse".
_BALCONY_WORDS = r"\w*balkon\w*|\w*loggia\w*|\b(?:Dach)?terrasse\w*"
_BALCONY = re.compile(_BALCONY_WORDS, re.IGNORECASE)
_NO_BALCONY = _negation(_BALCONY_WORDS)


def extract_balcony(text: str) -> bool | None:
    """Balcony, loggia or terrace."""
    return _yes_no(text, _BALCONY, _NO_BALCONY)


_CELLAR_WORDS = r"Keller\w*"
_CELLAR = re.compile(rf"\b{_CELLAR_WORDS}", re.IGNORECASE)
_NO_CELLAR = _negation(_CELLAR_WORDS)


def extract_cellar(text: str) -> bool | None:
    return _yes_no(text, _CELLAR, _NO_CELLAR)


# --- Heating -----------------------------------------------------------------

_HEATING_LABELED = re.compile(r"\bHeizung(?:sart)?\s*:\s*([A-Za-zÄÖÜäöüß][\wäöüß/-]+)")
_HEATING_TYPES = re.compile(
    r"\b(Fernwärme|Nahwärme|(?:Luft|Erd)?[Ww]ärmepumpe|Erdwärme"
    r"|Gas(?:etagen|zentral)?heizung|Zentralheizung|Etagenheizung|Pellets\w*"
    r"|Öl(?:zentral)?heizung|Elektroheizung|Infrarotheizung|Hackschnitzel\w*)\b",
    re.IGNORECASE,
)


def extract_heating(text: str) -> str | None:
    """The heating type as written: "Heizung: Gas" -> "Gas", or a known type."""
    if match := _HEATING_LABELED.search(text):
        return match.group(1)
    if match := _HEATING_TYPES.search(text):
        return match.group(1)
    return None


# --- Commission --------------------------------------------------------------

_COMMISSION_FREE = re.compile(
    r"provisionsfrei"
    r"|(?:keine|ohne)\s+(?:Käufer|Makler|Vermittlungs)?provision"
    r"|(?<![\d,.])0\s*%\s*(?:Käufer)?provision"
    r"|provision\s*:?\s*(?:keine|0(?:[.,]0+)?\s*%)",
    re.IGNORECASE,
)
_COMMISSION = re.compile(r"\b(?:Käufer|Makler|Vermittlungs)?provision\w*", re.IGNORECASE)
_ZERO_PERCENT = re.compile(r"(?<![\d,.])0(?:[.,]0+)?\s*%")


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


def parse_commission_fee(fee: str) -> bool | None:
    """Interpret a dedicated commission field such as "3% des Kaufpreises zzgl. 20% USt.".

    The field rarely contains the word "Provision", so any non-zero number in it
    means a commission is charged.
    """
    if (free := extract_commission_free(fee)) is not None:
        return free
    if _ZERO_PERCENT.search(fee):  # "0 %" (but not the "0" in "20% USt.")
        return True
    if re.search(r"[1-9]", fee):
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

    has_garage = listing.has_garage
    if has_garage is None:
        has_garage = updates["has_garage"] = extract_garage(text)
    if has_garage and listing.garage_price_eur is None:
        updates["garage_price_eur"] = extract_garage_price(text)
    if listing.has_parking is None:
        updates["has_parking"] = True if has_garage else extract_parking(text)

    if listing.has_elevator is None:
        updates["has_elevator"] = extract_elevator(text)
    if listing.has_balcony is None:
        updates["has_balcony"] = extract_balcony(text)
    if listing.has_cellar is None:
        updates["has_cellar"] = extract_cellar(text)
    if listing.heating is None:
        updates["heating"] = extract_heating(text)
    if listing.commission_free is None:
        commission_free = extract_commission_free(text)
        # Private sellers don't charge a commission unless they say otherwise.
        if commission_free is None and listing.is_private:
            commission_free = True
        updates["commission_free"] = commission_free

    return listing.model_copy(update=updates)
