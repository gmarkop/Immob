from datetime import UTC, datetime, timedelta

import pytest

from immob.models import BuildingType, Listing
from immob.storage import Change, Storage

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def store():
    with Storage(":memory:") as storage:
        yield storage


def listing(source_id: str = "1", **fields) -> Listing:
    defaults = {"url": f"https://example.com/{source_id}", "title": "Wohnung", "price_eur": 200000}
    return Listing(source="willhaben", source_id=source_id, **(defaults | fields))


def test_insert_and_read_back(store):
    original = listing(
        floor=0, has_parking=False, building_type=BuildingType.ALTBAU, raw={"id": "1"}
    )
    assert store.upsert(original, now=T0) is Change.NEW

    stored = store.get("willhaben", "1")
    assert stored.floor == 0  # 0 stays 0, not None
    assert stored.has_parking is False  # False stays False, not None
    assert stored.building_type is BuildingType.ALTBAU
    assert stored.first_seen == T0


def test_price_change_is_recorded(store):
    store.upsert(listing(price_eur=200000), now=T0)
    assert store.upsert(listing(price_eur=200000), now=T0 + timedelta(days=1)) is Change.SEEN
    assert (
        store.upsert(listing(price_eur=189000), now=T0 + timedelta(days=2)) is Change.PRICE_CHANGED
    )

    history = store.price_history("willhaben", "1")
    assert [price for _, price in history] == [200000, 189000]
    assert store.get("willhaben", "1").last_seen == T0 + timedelta(days=2)


def test_search_results_do_not_erase_detail_fields(store):
    store.upsert(listing(floor=3, year_built=1990, details_fetched=True), now=T0)
    store.upsert(listing(floor=None, year_built=None), now=T0 + timedelta(hours=6))

    stored = store.get("willhaben", "1")
    assert stored.floor == 3
    assert stored.year_built == 1990
    assert stored.details_fetched is True


def test_pending_notifications_new_and_price_drops(store):
    store.upsert(listing("1"), now=T0)
    store.upsert(listing("2"), now=T0)
    assert {item.source_id for item in store.pending_notifications()} == {"1", "2"}

    store.mark_all_notified()
    assert store.pending_notifications() == []

    store.upsert(listing("1", price_eur=210000))  # price up: no notification
    store.upsert(listing("2", price_eur=180000))  # price down: notify
    pending = store.pending_notifications()
    assert [item.source_id for item in pending] == ["2"]
    assert pending[0].notified_price == 200000

    store.mark_notified(pending[0])
    assert store.pending_notifications() == []
