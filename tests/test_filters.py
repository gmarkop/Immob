from pathlib import Path

import pytest
from pydantic import ValidationError

from immob.filters import FilterProfile, load_profile
from immob.models import BuildingType, Listing


def listing(**fields) -> Listing:
    return Listing(source="t", source_id="1", url="https://example.com", title="Wohnung", **fields)


def test_empty_profile_matches_everything():
    assert FilterProfile().matches(listing())


def test_ranges():
    profile = FilterProfile(price_max=200000, rooms_min=2, floor_min=1)
    assert profile.matches(listing(price_eur=190000, rooms=2, floor=1))
    assert profile.rejections(listing(price_eur=250000, rooms=1, floor=0)) == [
        "price 250000 > 200000",
        "rooms 1 < 2",
        "floor 0 < 1",
    ]


def test_unknown_values_follow_include_unknown():
    unknown_floor = listing(floor=None)
    assert FilterProfile(floor_min=1).matches(unknown_floor)
    assert not FilterProfile(floor_min=1, include_unknown=False).matches(unknown_floor)
    # The override is what the crawler uses to decide on fetching detail pages.
    strict = FilterProfile(floor_min=1, include_unknown=False)
    assert strict.matches(unknown_floor, include_unknown=True)


def test_requirements():
    profile = FilterProfile(parking="required", commission_free="required")
    assert profile.matches(listing(has_parking=True, commission_free=True))
    assert profile.rejections(listing(has_parking=False, commission_free=False)) == [
        "no parking",
        "no commission-free",
    ]
    assert not FilterProfile(elevator="excluded").matches(listing(has_elevator=True))


def test_building_type_postal_code_and_keywords():
    profile = FilterProfile(
        building_types=[BuildingType.NEUBAU],
        postal_code_prefixes=["90"],
        exclude_keywords=["Baurecht"],
    )
    assert profile.matches(listing(building_type=BuildingType.NEUBAU, postal_code="9020"))
    assert not profile.matches(listing(building_type=BuildingType.ALTBAU))
    assert not profile.matches(listing(postal_code="9500"))
    assert not profile.matches(listing(description="Wohnung auf BAURECHT bis 2080"))


def test_profile_rejects_typos():
    with pytest.raises(ValidationError):
        FilterProfile.model_validate({"price_mx": 100})


def test_postal_codes_may_be_written_as_numbers(tmp_path):
    path = tmp_path / "p.yaml"
    path.write_text("postal_code_prefixes: [9020, 95]\n")
    assert load_profile(path).postal_code_prefixes == ["9020", "95"]


def test_example_profile_loads():
    profile = load_profile(Path("profiles/kaernten.yaml"))
    assert profile.willhaben.region == "kaernten"


def test_search_url_must_be_a_willhaben_search():
    url = "https://www.willhaben.at/iad/immobilien/eigentumswohnung/kaernten?sort=1"
    assert FilterProfile.model_validate({"willhaben": {"search_url": url}}).willhaben.search_url
    with pytest.raises(ValidationError, match="willhaben real-estate search"):
        FilterProfile.model_validate({"willhaben": {"search_url": "https://example.com/"}})


def test_garage_balcony_cellar_and_monthly_costs():
    profile = FilterProfile(garage="required", balcony="required", monthly_costs_max=250)
    assert profile.matches(listing(has_garage=True, has_balcony=True, monthly_costs_eur=217.85))
    assert profile.rejections(
        listing(has_garage=False, has_balcony=False, monthly_costs_eur=300)
    ) == ["monthly costs 300 > 250", "no garage", "no balcony"]
    assert not FilterProfile(cellar="required").matches(listing(has_cellar=False))


def test_price_can_include_an_extra_cost_garage():
    flat = listing(price_eur=210000, has_garage=True, garage_price_eur=18000)
    assert FilterProfile(price_max=220000).matches(flat)
    assert not FilterProfile(price_max=220000, price_includes_garage=True).matches(flat)
    included = listing(price_eur=210000, has_garage=True, garage_price_eur=0)
    assert FilterProfile(price_max=220000, price_includes_garage=True).matches(included)
