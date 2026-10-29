import httpx
import pytest

from immob.http import RobotsDisallowed
from immob.models import BuildingType
from immob.sources.base import ParseError
from immob.sources.willhaben import (
    WillhabenSource,
    extract_next_data,
    find_adverts,
    find_key,
    parse_advert,
)
from tests.conftest import advert, detail_page, search_page

KLAGENFURT_AD = advert(
    111,
    HEADING="Helle 3-Zimmer-Wohnung mit Balkon",
    PRICE="189000",
    PRICE_FOR_DISPLAY="€ 189.000",
    **{"ESTATE_SIZE/LIVING_AREA": "68.5"},
    NUMBER_OF_ROOMS="3",
    POSTCODE="9020",
    LOCATION="Klagenfurt am Wörthersee",
    SEO_URL="immobilien/d/eigentumswohnung/kaernten/klagenfurt/helle-wohnung-111/",
    ISPRIVATE="0",
)


def test_parse_advert_maps_attributes():
    listing = parse_advert(KLAGENFURT_AD)

    assert listing.source == "willhaben"
    assert listing.source_id == "111"
    assert listing.title == "Helle 3-Zimmer-Wohnung mit Balkon"
    assert listing.url == (
        "https://www.willhaben.at/iad/immobilien/d/eigentumswohnung/kaernten/klagenfurt/helle-wohnung-111/"
    )
    assert listing.price_eur == 189000
    assert listing.area_m2 == 68.5
    assert listing.rooms == 3
    assert listing.postal_code == "9020"
    assert listing.is_private is False
    assert listing.raw == KLAGENFURT_AD


def test_parse_advert_uses_text_for_missing_fields():
    ad = advert(
        5,
        HEADING="Dachgeschosswohnung",
        BODY_DYN="Baujahr 2012<br/>Tiefgaragenplatz inklusive. Provisionsfrei!",
        ESTATE_PREFERENCE=["Lift", "Balkon"],
    )
    listing = parse_advert(ad)
    assert listing.floor_label == "DG"
    assert listing.year_built == 2012
    assert listing.building_type == BuildingType.NEUBAU
    assert listing.has_parking is True
    assert listing.has_elevator is True  # found in another attribute's values
    assert listing.commission_free is True
    assert listing.description == "Baujahr 2012 Tiefgaragenplatz inklusive. Provisionsfrei!"


def test_parse_advert_falls_back_to_display_price():
    listing = parse_advert(advert(6, PRICE_FOR_DISPLAY="€ 215.500"))
    assert listing.price_eur == 215500


def test_extract_next_data_without_script_raises():
    with pytest.raises(ParseError):
        extract_next_data("<html><body>Access denied</body></html>")


def test_find_key_searches_nested_data():
    assert find_key({"a": [{"b": {"target": 1}}]}, "target") == 1
    assert find_key({"a": 1}, "target") is None


def test_search_paginates_until_all_rows_seen(make_client):
    pages = {
        "1": search_page([advert(1), advert(2)], rows_found=3),
        "2": search_page([advert(3)], rows_found=3),
    }
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        requested.append(page)
        return httpx.Response(200, text=pages[page])

    source = WillhabenSource(make_client(handler), rows=2)
    listings = list(source.search(max_pages=5))

    assert [listing.source_id for listing in listings] == ["1", "2", "3"]
    assert requested == ["1", "2"]


def test_search_respects_max_pages(make_client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=search_page([advert(1), advert(2)], rows_found=100))

    source = WillhabenSource(make_client(handler), rows=2)
    assert len(list(source.search(max_pages=2))) == 4


def test_search_skips_broken_advert(make_client):
    broken = {"attributes": {"attribute": []}}  # no "id"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=search_page([broken, advert(2)]))

    source = WillhabenSource(make_client(handler))
    assert [listing.source_id for listing in source.search(max_pages=1)] == ["2"]


def test_search_refuses_when_robots_txt_disallows(make_client):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not fetch a disallowed page")

    client = make_client(handler, robots_txt="User-agent: *\nDisallow: /iad/\n")
    with pytest.raises(RobotsDisallowed):
        list(WillhabenSource(client).search(max_pages=1))


def test_fetch_details_merges_detail_data(make_client):
    summary = parse_advert(KLAGENFURT_AD)
    details = advert(
        111,
        FLOOR="2",
        CONSTRUCTION_YEAR="1998",
        BODY_DYN="Tiefgaragenplatz um € 18.000. Käuferprovision 3 % zzgl. USt.",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == summary.url
        return httpx.Response(200, text=detail_page(details))

    merged = WillhabenSource(make_client(handler)).fetch_details(summary)

    assert merged.details_fetched is True
    assert merged.title == summary.title  # kept: the detail advert has no HEADING
    assert merged.price_eur == 189000  # kept from the summary
    assert merged.floor == 2
    assert merged.year_built == 1998
    assert merged.has_parking is True
    assert merged.commission_free is False


def test_find_adverts_ignores_key_names():
    data = {"props": {"renamed": {"items": [advert(1), {"nested": advert(2)}]}, "other": [1, 2]}}
    assert [ad["id"] for ad in find_adverts(data)] == ["1", "2"]
    assert find_adverts({"props": {"nothing": "here"}}) == []


# Attributes as willhaben really sends them on a search page (from a saved page,
# October 2026; long values trimmed).
REAL_SEARCH_AD = advert(
    858253157,
    ADID="858253157",
    BODY_DYN="Attraktive Kapitalanlage in gefragter Lage von Klagenfurt Waidmannsdorf",
    COORDINATES="46.614105,14.26903635",
    DISTRICT="Klagenfurt",
    ESTATE_PREFERENCE="15, 23, 250, 27",
    **{
        "ESTATE_PRICE/PRICE_SUGGESTION": "232000.0",
        "ESTATE_SIZE/LIVING_AREA": "54",
        "PRICE/SQUARE_METER": "4296.2964",
    },
    ESTATE_SIZE="54",
    FLOOR="1",
    FREE_AREA_TYPE_NAME="Balkon",
    HEADING="INVESTMENT IN WAIDMANNSDORF: VERMIETET BIS 2027 INKL. TIEFGARAGENSTELLPLATZ",
    ISPRIVATE="0",
    LOCATION="Klagenfurt",
    NUMBER_OF_ROOMS="2",
    POSTCODE="9020",
    PRICE="232000",
    PRICE_FOR_DISPLAY="€ 232.000",
    PROPERTY_TYPE="Wohnung",
    PUBLISHED="1793281645575",
    PUBLISHED_String="2026-10-29T14:47:25Z",
    ROOMS="2X2",
    SEO_URL="immobilien/d/eigentumswohnung/kaernten/klagenfurt/investment-in-waidmannsdorf-858253157/",
    STATE="Kärnten",
)


def test_parse_real_search_advert():
    listing = parse_advert(REAL_SEARCH_AD)

    assert listing.source_id == "858253157"
    assert listing.price_eur == 232000
    assert listing.area_m2 == 54
    assert listing.rooms == 2  # from NUMBER_OF_ROOMS, not the "2X2" code
    assert (listing.floor, listing.floor_display) == (1, "1. OG")
    assert listing.postal_code == "9020"
    assert listing.location == "Klagenfurt"
    assert listing.is_private is False
    assert listing.has_parking is True  # "TIEFGARAGENSTELLPLATZ" in the heading
    assert listing.year_built is None  # "BIS 2027" is not a construction year
    assert listing.url.startswith("https://www.willhaben.at/iad/immobilien/d/eigentumswohnung/")


def test_search_pages_smaller_than_requested(make_client):
    """We ask for 90 per page; if the site sends 30, keep paging until rowsFound."""

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        ids = range((page - 1) * 30, min(page * 30, 70))
        return httpx.Response(200, text=search_page([advert(i) for i in ids], rows_found=70))

    source = WillhabenSource(make_client(handler), rows=90)
    assert len(list(source.search(max_pages=5))) == 70


def test_parse_floor_values():
    assert parse_advert(advert(1, FLOOR="0")).floor == 0
    assert parse_advert(advert(1, FLOOR="-1")).floor == -1
    assert parse_advert(advert(1, FLOOR="Dachgeschoss")).floor_label == "DG"
