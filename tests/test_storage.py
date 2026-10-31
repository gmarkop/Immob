import sqlite3
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


def test_every_listing_field_has_a_column(store):
    columns = {row["name"] for row in store._conn.execute("PRAGMA table_info(listings)")}
    assert set(Listing.model_fields) - {"raw"} <= columns


def test_old_database_gets_new_columns(tmp_path):
    """A database created before has_garage & co. existed is upgraded on open."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE listings (
            source TEXT NOT NULL, source_id TEXT NOT NULL, url TEXT NOT NULL,
            title TEXT NOT NULL, price_eur INTEGER, area_m2 REAL, rooms REAL,
            floor INTEGER, floor_label TEXT, year_built INTEGER, building_type TEXT,
            has_parking INTEGER, has_elevator INTEGER, commission_free INTEGER,
            postal_code TEXT, location TEXT, description TEXT, is_private INTEGER,
            details_fetched INTEGER NOT NULL DEFAULT 0, raw_json TEXT,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            notified_price INTEGER, notified_at TEXT,
            PRIMARY KEY (source, source_id)
        );
        INSERT INTO listings (source, source_id, url, title, price_eur, first_seen, last_seen)
        VALUES ('willhaben', '1', 'https://example.com/1', 'Alt', 150000,
                '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00');
        """
    )
    old.close()

    with Storage(path) as storage:
        assert storage.get("willhaben", "1").has_garage is None  # old row still readable
        storage.upsert(listing("1", has_garage=True, garage_price_eur=18000))
        assert storage.get("willhaben", "1").garage_price_eur == 18000


def test_mark_gone_and_reappear(store):
    store.upsert(listing("1"), now=T0)
    store.upsert(listing("2"), now=T0)
    store.upsert(Listing(source="other", source_id="9", url="u", title="t"), now=T0)

    assert store.mark_gone("willhaben", seen_ids={"1"}, now=T0 + timedelta(days=3)) == 1
    gone = store.get("willhaben", "2")
    assert gone.gone_at == T0 + timedelta(days=3)
    assert gone.days_online == 3
    assert store.get("other", "9").gone_at is None  # other sources untouched
    assert [item.source_id for item in store.gone_since(T0)] == ["2"]

    store.upsert(listing("2"), now=T0 + timedelta(days=4))  # listed again
    assert store.get("willhaben", "2").gone_at is None


def test_duplicate_links(store):
    original = listing("1", area_m2=54)
    copy = Listing(source="immoscout24", source_id="a", url="u", title="t", area_m2=54)
    store.upsert(original)
    store.upsert(copy)
    assert [c.source_id for c in store.duplicate_candidates(copy, 0.03)] == ["1"]

    store.set_duplicate_of(copy, original)

    assert store.get("immoscout24", "a").duplicate_of == "willhaben:1"
    assert [c.source_id for c in store.duplicates_of(original)] == ["a"]
    assert store.get_ref("willhaben:1").source_id == "1"


def test_duplicate_groups_and_unlink(store):
    original = listing("1", area_m2=54)
    copy = Listing(source="immoscout24", source_id="a", url="u", title="t", area_m2=54)
    store.upsert(original)
    store.upsert(copy)
    store.upsert(listing("2"))  # a flat on one site only
    store.set_duplicate_of(copy, original)

    [(group_original, copies)] = store.duplicate_groups()
    assert group_original.ref == "willhaben:1"
    assert [c.ref for c in copies] == ["immoscout24:a"]

    store.unlink_duplicate(copy)
    assert store.duplicate_groups() == []
    assert store.get("immoscout24", "a").duplicate_of is None
