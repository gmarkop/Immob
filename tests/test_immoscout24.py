import json

import httpx
import pytest

from immob.sources.base import ParseError
from immob.sources.immoscout24 import (
    ImmoScout24Source,
    find_results,
    initial_state,
    next_page_url,
    parse_expose,
    parse_hit,
)

SEARCH_URL = "https://www.immobilienscout24.at/regional/kaernten/wohnung-kaufen"

# A real search hit (saved page, October 2026); images and contact trimmed.
REAL_HIT = {
    "displayType": "PREMIUM",
    "exposeId": "6ac7397a64f40d33d27244ec",
    "links": {
        "targetURL": "/expose/6ac7397a64f40d33d27244ec",
        "absoluteURL": "https://www.immobilienscout24.at/expose/6ac7397a64f40d33d27244ec",
    },
    "mainKeyFacts": [{"value": "30 m²", "label": None}, {"value": "2,5", "label": "Zi."}],
    "addressString": "9413 St. Gertraud",
    "typedBadges": [
        {"type": "REGULAR", "details": {"value": "PARKING_GENERIC"}},
        {"type": "REGULAR", "details": {"value": "FURNISHED_PART"}},
    ],
    "isPrivate": False,
    "primaryPrice": 52000,
    "primaryArea": 30,
    "numberOfRooms": 2.5,
    "headline": "9413 St.Gertraud/Frantschach Kleine liebe Singlewohnung",
    "meta": {"transferType": "BUY", "useType": "RESIDENTIAL", "estateType": "APARTMENT"},
    "dateCreated": "2026-10-08T06:34:35.084Z",
    "realtorContact": {"company": "Beispiel Makler GmbH", "name": "Max Mustermann"},
}

# The JSON-LD of a real listing page (trimmed; agent contact replaced).
REAL_JSON_LD = {
    "@context": "https://schema.org",
    "@graph": [
        {"@type": "WebPage", "name": "NEUER PREIS! Helle Dachgeschoßwohnung"},
        {
            "@type": "Product",
            "name": "NEUER PREIS! Helle Dachgeschoßwohnung im Zentrum von St.Andrä!",
            "offers": {"@type": "Offer", "priceCurrency": "EUR", "price": 117000},
        },
        {
            "@type": "RealEstateListing",
            "name": "NEUER PREIS! Helle Dachgeschoßwohnung im Zentrum von St.Andrä!",
            "description": " \r<br /><p>Verkauft wird diese helle Dachgeschoßwohnung!</p>",
            "floorSize": {"@type": "QuantitativeValue", "value": 65.55, "unitCode": "MTK"},
            "numberOfRooms": 2,
            "address": {
                "@type": "PostalAddress",
                "addressLocality": "St. Andrä",
                "addressRegion": "Kärnten",
                "postalCode": "9433",
            },
            "provider": {"@type": "RealEstateAgent", "name": "Beispiel Makler"},
        },
    ],
}

# Visible lines of the same page, as the probe printed them.
REAL_EXPOSE_LINES = [
    "Etage", "2", "Baujahr", "-",
    "Provision zzgl. 20% USt.: 3.00 % zuzüglich USt.",
    "Provision: 3.00 % zuzüglich USt.",
    "Laufende Kosten", "Betriebskosten", "250 €", "USt. Betriebskosten", "37,2 €",
    "Unterkellert", ",", "2. Etage", ",", "Insgesamt 2 Etagen", "Wohnfläche 65,55 m²",
    "Kellerfläche 3 m²", "Heizung", "Zentralheizung",
    "Heizwärmebedarf (HWB)", "203. kWh/(m²a)",
    "Gesamtenergieeffizienzfaktor (fGEE)", "2.28",
    "Die Wohnung befindet sich im zweiten Stock und hat eine südwestliche Ausrichtung.",
]  # fmt: skip


def search_page(hits, total=None, pagination=None) -> str:
    state = {
        "adverts": {"obj_ityp": ["2"]},
        "searchUI": {"totalHits": total, "aggregations": "UNDEFINED"},
        "reduxAsyncConnect": {
            "pageData": {
                "results": {
                    "totalHits": len(hits) if total is None else total,
                    "pagination": pagination,
                    "hits": hits,
                }
            }
        },
    }
    # The real page is JavaScript: it contains `undefined`, and more code follows.
    js = json.dumps(state).replace('"UNDEFINED"', "undefined")
    return (
        "<html><head><title>ImmoScout24</title></head><body>"
        f"<script>window.__INITIAL_STATE__={js};window.__LOADABLE__=true</script>"
        "</body></html>"
    )


def expose_page(json_ld=REAL_JSON_LD, lines=REAL_EXPOSE_LINES) -> str:
    body = "".join(f"<div>{line}</div>" for line in lines)
    return (
        "<html><head>"
        f'<script type="application/ld+json">{json.dumps(json_ld)}</script>'
        '<script>window.__INITIAL_STATE__={"config":{"appUrl":"https://www.immobilienscout24.at"}}'
        "</script></head>"
        f"<body><header>Menü</header><main>{body}</main></body></html>"
    )


def hit(expose_id: str, **fields) -> dict:
    return REAL_HIT | {"exposeId": expose_id, "links": {}} | fields


def test_initial_state_handles_javascript():
    state = initial_state(search_page([REAL_HIT]))
    assert state["searchUI"]["aggregations"] is None  # was `undefined`
    with pytest.raises(ParseError):
        initial_state("<html>Access denied</html>")


def test_find_results():
    assert find_results(initial_state(search_page([REAL_HIT])))["totalHits"] == 1
    with pytest.raises(ParseError):
        find_results({"searchUI": {"totalHits": 3}})  # no hits there


def test_parse_real_hit():
    listing = parse_hit(REAL_HIT)

    assert listing.source == "immoscout24"
    assert listing.source_id == "6ac7397a64f40d33d27244ec"
    assert listing.url == "https://www.immobilienscout24.at/expose/6ac7397a64f40d33d27244ec"
    assert listing.title == "9413 St.Gertraud/Frantschach Kleine liebe Singlewohnung"
    assert (listing.price_eur, listing.area_m2, listing.rooms) == (52000, 30, 2.5)
    assert (listing.postal_code, listing.location) == ("9413", "St. Gertraud")
    assert listing.is_private is False
    assert listing.has_parking is True  # PARKING_GENERIC badge
    assert listing.has_garage is None  # parking, but not necessarily covered


def test_garage_badge():
    listing = parse_hit(hit("x", typedBadges=[{"details": {"value": "GARAGE"}}]))
    assert (listing.has_garage, listing.has_parking) == (True, True)


def test_parse_real_expose():
    url = "https://www.immobilienscout24.at/expose/6acb9e6c635249b92341bc32"
    listing, text = parse_expose(expose_page(), url)

    assert listing.source_id == "6acb9e6c635249b92341bc32"
    assert listing.title == "NEUER PREIS! Helle Dachgeschoßwohnung im Zentrum von St.Andrä!"
    assert (listing.price_eur, listing.area_m2, listing.rooms) == (117000, 65.55, 2)
    assert (listing.postal_code, listing.location) == ("9433", "St. Andrä")
    assert listing.floor == 2
    assert listing.year_built is None  # "-"
    assert listing.heating == "Zentralheizung"
    assert listing.hwb == 203  # "203. kWh/(m²a)": a dot as decimal point
    assert listing.monthly_costs_eur == pytest.approx(287.2)  # 250 + 37.2 VAT
    assert listing.commission_text == "3.00 % zuzüglich USt."
    assert listing.commission_free is False
    assert listing.description == "Verkauft wird diese helle Dachgeschoßwohnung!"
    assert "Menü" not in text  # only the page's main content


def test_expose_without_json_ld_raises():
    with pytest.raises(ParseError):
        parse_expose("<html><main>nothing</main></html>", "https://x/expose/1")


def test_fetch_details_merges(make_client):
    url = "https://www.immobilienscout24.at/expose/6acb9e6c635249b92341bc32"
    summary = parse_hit(hit("6acb9e6c635249b92341bc32", links={"absoluteURL": url}))

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == url
        return httpx.Response(200, text=expose_page())

    source = ImmoScout24Source(make_client(handler), search_url=SEARCH_URL)
    merged = source.fetch_details(summary)

    assert merged.details_fetched is True
    assert merged.price_eur == 117000  # the detail page wins
    assert merged.has_parking is True  # kept from the search page's badge
    assert merged.has_cellar is True  # "Kellerfläche" in the text
    assert merged.energy_class == "F"  # derived from HWB 203
    assert merged.raw["labels"]["floor"] == "2"


def test_search_follows_pagination(make_client):
    pages = {
        "1": search_page(
            [hit("a"), hit("b")],
            total=3,
            pagination={
                "all": [
                    "/regional/kaernten/wohnung-kaufen",
                    "/regional/kaernten/wohnung-kaufen?page=2",
                ]
            },
        ),
        "2": search_page([hit("c")], total=3),
    }
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        requested.append(page)
        return httpx.Response(200, text=pages[page])

    source = ImmoScout24Source(make_client(handler), search_url=SEARCH_URL)
    listings = list(source.search(max_pages=5))

    assert [listing.source_id for listing in listings] == ["a", "b", "c"]
    assert requested == ["1", "2"]
    assert source.total_found == 3


def test_next_page_url_formats():
    fetched = {SEARCH_URL}
    assert (
        next_page_url({"next": "/x?page=2"}, fetched) == "https://www.immobilienscout24.at/x?page=2"
    )
    assert next_page_url({"all": [{"url": "/x?page=2"}]}, fetched).endswith("page=2")
    assert next_page_url({"all": ["/regional/kaernten/wohnung-kaufen"]}, fetched) is None
    assert next_page_url(None, fetched) is None


def test_initial_state_real_fragment():
    # Verbatim from a real search page: `undefined` without spaces, more code after.
    page = (
        '<script>window.__INITIAL_STATE__={"searchUI":{"showOverlay":false,"totalHits":10,'
        '"aggregations":undefined},"shortlist":{"items":[],"error":undefined}};'
        "window.x=1</script>"
    )
    state = initial_state(page)
    assert state["shortlist"] == {"items": [], "error": None}
