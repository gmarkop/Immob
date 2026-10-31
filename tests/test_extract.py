import pytest

from immob.extract import (
    extract_balcony,
    extract_building_type,
    extract_cellar,
    extract_commission_free,
    extract_elevator,
    extract_energy_class,
    extract_floor,
    extract_garage,
    extract_garage_price,
    extract_heating,
    extract_hwb,
    extract_parking,
    extract_year_built,
    fill_from_text,
    parse_commission_fee,
    parse_number,
)
from immob.models import BuildingType, Listing, energy_class_for


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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Stockwerk: 1", (1, None)),
        ("Stockwerk: 1. Etage", (1, "1. OG")),
        ("Anzahl Stockwerke: 4", (None, None)),  # the building's height, not the floor
    ],
)
def test_extract_floor_labeled(text, expected):
    assert extract_floor(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "in der 1. Etage (Lift ist keiner vorhanden)",  # from a real willhaben ad
        "Lift: nein",
        "Aufzug nicht vorhanden",
        "keinen Aufzug",
    ],
)
def test_elevator_negations(text):
    assert extract_elevator(text) is False


@pytest.mark.parametrize(
    ("text", "garage", "parking"),
    [
        ("Tiefgaragenplatz", True, True),
        ("Garagenbox im Hof", True, True),
        ("TG-Platz vorhanden", True, True),
        ("Carport", None, True),  # parking, but not a covered garage
        ("PKW-Stellplatz im Freien", None, True),
        ("Fahrradgarage", None, None),  # for bikes
        ("keine Garage", False, False),
        ("Garage: nein", False, False),
        ("Tiefgaragenplatz nicht im Kaufpreis enthalten", True, True),  # exists, costs extra
    ],
)
def test_extract_garage_vs_parking(text, garage, parking):
    assert extract_garage(text) == garage
    assert extract_parking(text) == parking


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Tiefgaragenplatz um € 18.000 erwerbbar", 18000),
        ("TG-Platz: € 15.000,-", 15000),
        ("Garagenplatz kann um 22.000,- Euro gekauft werden", 22000),
        ("Tiefgaragenstellplatz optional um EUR 25000", 25000),
        ("inkl. Tiefgaragenplatz", 0),
        ("INVESTMENT: VERMIETET BIS 2027 INKL. TIEFGARAGENSTELLPLATZ", 0),
        ("Der Tiefgaragenplatz ist im Kaufpreis inkludiert.", 0),
        ("Tiefgaragenplatz nicht im Kaufpreis enthalten", None),  # extra, amount unknown
        ("zzgl. Tiefgaragenplatz", None),
        ("Tiefgarage vorhanden. Kaufpreis € 250.000", None),  # different sentence
        ("Garage € 80 monatlich", None),  # a rent, not a purchase price
    ],
)
def test_extract_garage_price(text, expected):
    assert extract_garage_price(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Südbalkon", True),
        ("Balkonanzahl:1", True),
        ("Loggia mit Blick", True),
        ("Dachterrasse", True),
        ("Gemeinschaftsterrasse im Hof", None),  # shared, not the flat's own
        ("kein Balkon", False),
    ],
)
def test_extract_balcony(text, expected):
    assert extract_balcony(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Kellerabteil", True), ("Kellerfläche: 7.58 m²", True), ("Keller: nein", False)],
)
def test_extract_cellar(text, expected):
    assert extract_cellar(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Heizung: Gas", "Gas"),
        ("Beheizt mit Fernwärme", "Fernwärme"),
        ("Luftwärmepumpe und Fußbodenheizung", "Luftwärmepumpe"),
        ("Fußbodenheizung", None),  # how heat is distributed, not where it comes from
    ],
)
def test_extract_heating(text, expected):
    assert extract_heating(text) == expected


@pytest.mark.parametrize(
    ("fee", "expected"),
    [
        ("3% des Kaufpreises zzgl. 20% USt.", False),  # real ADDITIONAL_COST/FEE value
        ("€ 4.500,- inkl. USt.", False),
        ("0 %", True),
        ("provisionsfrei", True),
        ("lt. Vereinbarung", None),
    ],
)
def test_parse_commission_fee(fee, expected):
    assert parse_commission_fee(fee) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("HWB: 72 kWh/m²a", 72),
        ("HWB-Wert 45,3", 45.3),
        ("HWB Ref,SK: 61", 61),
        ("HWB 72 fGEE 1,1", 72),
        ("HWB: C, fGEE: 1,1", None),  # a class and another value, but no HWB number
        ("Energieausweis liegt vor", None),
        ("HWB: 2", None),  # implausible
    ],
)
def test_extract_hwb(text, expected):
    assert extract_hwb(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Energieklasse: B", "B"),
        ("HWB-Klasse C", "C"),
        ("Energieeffizienzklasse A+", "A+"),
        ("Energieklasse A++", "A++"),
        ("Klasse: Business", None),
    ],
)
def test_extract_energy_class(text, expected):
    assert extract_energy_class(text) == expected


@pytest.mark.parametrize(
    ("hwb", "expected"),
    [(8, "A++"), (10, "A++"), (12, "A+"), (25, "A"), (49.9, "B"), (72, "C"), (251, "G")],
)
def test_energy_class_for(hwb, expected):
    assert energy_class_for(hwb) == expected


def test_fill_from_text_derives_energy_class_from_hwb():
    filled = fill_from_text(_listing(description="HWB 72 kWh/m²a"))
    assert (filled.hwb, filled.energy_class) == (72, "C")
    stated = fill_from_text(_listing(description="HWB 72, Energieklasse B"))
    assert stated.energy_class == "B"  # a stated class wins over the derived one
