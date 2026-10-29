import pytest

from immob.extract import (
    extract_building_type,
    extract_commission_free,
    extract_elevator,
    extract_floor,
    extract_parking,
    extract_year_built,
    fill_from_text,
    parse_number,
)
from immob.models import BuildingType, Listing


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("189000", 189000),
        ("65.5", 65.5),
        ("€ 189.000", 189000),
        ("189.000", 189000),  # thousands separator, not 189.0
        ("1.250.000", 1250000),
        ("65,48 m²", 65.48),
        ("€ 1.250,50", 1250.5),
        ("auf Anfrage", None),
        (None, None),
    ],
)
def test_parse_number(text, expected):
    assert parse_number(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Schöne Wohnung im 3. OG", (3, "3. OG")),
        ("liegt im 2.Stock", (2, "2. OG")),
        ("1. Obergeschoss mit Balkon", (1, "1. OG")),
        ("Gartenwohnung im Erdgeschoss", (0, "EG")),
        ("Erdgeschosswohnung", (0, "EG")),
        ("Wohnung EG", (0, "EG")),
        ("Hochparterre", (0, "Hochparterre")),
        ("Dachgeschosswohnung mit Blick", (None, "DG")),
        ("Wohnung im 3. OG, Fahrradraum im EG", (3, "3. OG")),  # most specific wins
        ("Die Wohnung liegt zentral", (None, None)),
        ("Wir legen Wert auf ein gepflegtes Haus", (None, None)),  # lowercase "eg" in words
    ],
)
def test_extract_floor(text, expected):
    assert extract_floor(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Baujahr: 1998", 1998),
        ("Baujahr ca. 1975", 1975),
        ("Bj. 2005", 2005),
        ("erbaut im Jahr 1910", 1910),
        ("Das Haus wurde 1998 renoviert", None),
        ("Baujahr 3020", None),  # implausible
    ],
)
def test_extract_year_built(text, expected):
    assert extract_year_built(text) == expected


@pytest.mark.parametrize(
    ("text", "year", "expected"),
    [
        ("Altbauwohnung mit Flügeltüren", None, BuildingType.ALTBAU),
        ("Neubauprojekt in Klagenfurt", None, BuildingType.NEUBAU),
        ("Erstbezug!", None, BuildingType.NEUBAU),
        ("Erstbezug nach Sanierung", None, None),
        ("Gepflegte Wohnung", 1930, BuildingType.ALTBAU),
        ("Gepflegte Wohnung", 1985, BuildingType.NEUBAU),
        ("Gepflegte Wohnung", None, None),
    ],
)
def test_extract_building_type(text, year, expected):
    assert extract_building_type(text, year) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("inkl. Tiefgaragenplatz", True),
        ("Garage vorhanden", True),
        ("PKW-Stellplatz um € 15.000 erwerbbar", True),
        ("Carport", True),
        ("Kein Parkplatz, aber gute Öffis", False),
        ("ohne eigenen Stellplatz", False),
        ("Fahrradabstellplatz im Keller", None),
        ("Kellerabteil", None),
    ],
)
def test_extract_parking(text, expected):
    assert extract_parking(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Haus mit Lift", True),
        ("Personenaufzug vorhanden", True),
        ("kein Lift", False),
        ("5 Minuten zum Skilift", None),
        ("Treppenhaus", None),
    ],
)
def test_extract_elevator(text, expected):
    assert extract_elevator(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Provisionsfrei für den Käufer!", True),
        ("keine Käuferprovision", True),
        ("Provision: 0 %", True),
        ("Käuferprovision: 3 % zzgl. 20 % USt.", False),
        ("3,0 % Provision", False),
        ("Vermittlungsprovision lt. Honorarverordnung", False),
        ("Schöne Wohnung", None),
    ],
)
def test_extract_commission_free(text, expected):
    assert extract_commission_free(text) == expected


def _listing(**fields) -> Listing:
    return Listing(source="test", source_id="1", url="https://example.com/1", title="T", **fields)


def test_fill_from_text_only_fills_gaps():
    listing = _listing(floor=2, has_parking=False, description="1. OG, Tiefgarage, Baujahr 1990")
    filled = fill_from_text(listing)
    assert filled.floor == 2  # structured value kept
    assert filled.has_parking is False  # structured value kept
    assert filled.year_built == 1990
    assert filled.building_type == BuildingType.NEUBAU


def test_fill_from_text_private_seller_is_commission_free():
    assert fill_from_text(_listing(is_private=True)).commission_free is True
    assert fill_from_text(_listing(is_private=False)).commission_free is None
