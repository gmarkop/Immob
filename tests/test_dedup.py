import pytest

from immob.dedup import find_original, same_flat
from immob.models import Listing
from immob.storage import Storage


def flat(source="willhaben", source_id="1", **fields) -> Listing:
    defaults = {
        "url": f"https://example.com/{source}/{source_id}",
        "title": "Helle 2-Zimmer-Wohnung mit Tiefgarage",
        "price_eur": 199000,
        "area_m2": 54.0,
        "rooms": 2,
        "postal_code": "9500",
        "location": "Villach",
    }
    return Listing(source=source, source_id=source_id, **(defaults | fields))


def test_same_flat_on_two_sites():
    assert same_flat(flat(), flat("immoscout24", "a", area_m2=54.3, price_eur=195000))


@pytest.mark.parametrize(
    "other",
    [
        flat("willhaben", "2"),  # same site: told apart by ID
        flat("immoscout24", "a", postal_code="9020"),
        flat("immoscout24", "a", rooms=3),
        flat("immoscout24", "a", area_m2=60),
        flat("immoscout24", "a", price_eur=230000),
        flat("immoscout24", "a", area_m2=None),  # no basis for a match
    ],
)
def test_different_flats(other):
    assert not same_flat(flat(), other)


def test_location_used_when_postal_code_missing():
    assert same_flat(flat(postal_code=None), flat("immoscout24", "a", postal_code=None))
    assert not same_flat(
        flat(postal_code=None), flat("immoscout24", "a", postal_code=None, location="Spittal")
    )


def test_find_original_prefers_most_similar_title():
    with Storage(":memory:") as store:
        store.upsert(flat("willhaben", "1", title="Penthouse über den Dächern"))
        store.upsert(flat("willhaben", "2", title="Helle 2-Zimmer-Wohnung, Tiefgarage"))
        store.upsert(flat("willhaben", "3", area_m2=80))  # area too different

        original = find_original(store, flat("immoscout24", "a"))

        assert original.source_id == "2"
