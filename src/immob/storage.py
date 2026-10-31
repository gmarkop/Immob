"""SQLite storage: one table of listings, one table of their price history.

Plain `sqlite3` from the standard library keeps the SQL visible; there is no
ORM in between.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from immob.models import Listing, StoredListing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    source          TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    price_eur       INTEGER,
    area_m2         REAL,
    rooms           REAL,
    floor           INTEGER,
    floor_label     TEXT,
    year_built      INTEGER,
    building_type   TEXT,
    has_parking     INTEGER,
    has_garage      INTEGER,
    garage_price_eur INTEGER,
    has_elevator    INTEGER,
    has_balcony     INTEGER,
    has_cellar      INTEGER,
    heating         TEXT,
    heating_costs_eur REAL,
    monthly_costs_eur REAL,
    commission_free INTEGER,
    commission_text TEXT,
    hwb             REAL,
    energy_class    TEXT,
    postal_code     TEXT,
    location        TEXT,
    description     TEXT,
    is_private      INTEGER,
    details_fetched INTEGER NOT NULL DEFAULT 0,
    raw_json        TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    notified_price  INTEGER,
    notified_at     TEXT,
    gone_at         TEXT,
    duplicate_of    TEXT,
    PRIMARY KEY (source, source_id)
);

CREATE TABLE IF NOT EXISTS price_history (
    source    TEXT NOT NULL,
    source_id TEXT NOT NULL,
    price_eur INTEGER,
    seen_at   TEXT NOT NULL,
    FOREIGN KEY (source, source_id) REFERENCES listings (source, source_id)
);
"""

# Columns added after the first release. CREATE TABLE IF NOT EXISTS leaves an
# existing table alone, so databases created earlier get these via ALTER TABLE.
_ADDED_COLUMNS = {
    "has_garage": "INTEGER",
    "garage_price_eur": "INTEGER",
    "has_balcony": "INTEGER",
    "has_cellar": "INTEGER",
    "heating": "TEXT",
    "heating_costs_eur": "REAL",
    "monthly_costs_eur": "REAL",
    "commission_text": "TEXT",
    "hwb": "REAL",
    "energy_class": "TEXT",
    "gone_at": "TEXT",
    "duplicate_of": "TEXT",
}

# Listing fields stored as plain columns. Column names come from our own model,
# never from user input, so building SQL from them with f-strings is safe; all
# *values* go through "?" placeholders.
_DATA_COLUMNS = [
    name
    for name in Listing.model_fields
    if name not in ("source", "source_id", "details_fetched", "raw")
]


class Change(StrEnum):
    NEW = "new"
    PRICE_CHANGED = "price_changed"
    SEEN = "seen"


def _now() -> datetime:
    return datetime.now(UTC)


class Storage:
    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring a database created by an older version up to the current schema."""
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(listings)")}
        with self._conn:
            for column, sql_type in _ADDED_COLUMNS.items():
                if column not in existing:
                    self._conn.execute(f"ALTER TABLE listings ADD COLUMN {column} {sql_type}")

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    # --- writing -------------------------------------------------------------

    def upsert(self, listing: Listing, now: datetime | None = None) -> Change:
        """Insert a new listing or update a known one, recording price changes."""
        now = now or _now()
        existing = self.get(*listing.key)
        values = listing.model_dump(mode="json")  # enums -> strings, bools stay bools
        data = [values[column] for column in _DATA_COLUMNS]
        raw_json = json.dumps(listing.raw, ensure_ascii=False) if listing.raw else None

        with self._conn:  # one transaction: commits on success, rolls back on error
            if existing is None:
                columns = [
                    "source",
                    "source_id",
                    *_DATA_COLUMNS,
                    "details_fetched",
                    "raw_json",
                    "first_seen",
                    "last_seen",
                ]
                self._conn.execute(
                    f"INSERT INTO listings ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' * len(columns))})",
                    [
                        listing.source,
                        listing.source_id,
                        *data,
                        listing.details_fetched,
                        raw_json,
                        now.isoformat(),
                        now.isoformat(),
                    ],
                )
                self._record_price(listing, now)
                return Change.NEW

            # COALESCE keeps the stored value when the new one is NULL. A search
            # page lacks detail fields, so it mustn't wipe what a detail page found.
            assignments = ", ".join(f"{c} = COALESCE(?, {c})" for c in _DATA_COLUMNS)
            self._conn.execute(
                f"UPDATE listings SET {assignments}, "
                "details_fetched = MAX(?, details_fetched), "
                # Seen again, so not gone (any more).
                "raw_json = COALESCE(?, raw_json), last_seen = ?, gone_at = NULL "
                "WHERE source = ? AND source_id = ?",
                [*data, listing.details_fetched, raw_json, now.isoformat(), *listing.key],
            )
            if listing.price_eur is not None and listing.price_eur != existing.price_eur:
                self._record_price(listing, now)
                return Change.PRICE_CHANGED
            return Change.SEEN

    def _record_price(self, listing: Listing, now: datetime) -> None:
        self._conn.execute(
            "INSERT INTO price_history (source, source_id, price_eur, seen_at) VALUES (?, ?, ?, ?)",
            [*listing.key, listing.price_eur, now.isoformat()],
        )

    def mark_notified(self, listing: Listing, now: datetime | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE listings SET notified_price = price_eur, notified_at = ? "
                "WHERE source = ? AND source_id = ?",
                [(now or _now()).isoformat(), *listing.key],
            )

    def mark_gone(self, source: str, seen_ids: set[str], now: datetime | None = None) -> int:
        """Mark the source's listings that a complete crawl didn't see as gone."""
        stored = {
            row["source_id"]
            for row in self._conn.execute(
                "SELECT source_id FROM listings WHERE source = ? AND gone_at IS NULL", [source]
            )
        }
        missing = stored - seen_ids
        with self._conn:
            self._conn.executemany(
                "UPDATE listings SET gone_at = ? WHERE source = ? AND source_id = ?",
                [((now or _now()).isoformat(), source, source_id) for source_id in missing],
            )
        return len(missing)

    def set_duplicate_of(self, listing: Listing, original: Listing) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE listings SET duplicate_of = ? WHERE source = ? AND source_id = ?",
                [original.ref, *listing.key],
            )

    def unlink_duplicate(self, listing: Listing) -> None:
        """Undo a duplicate link: the listing counts as a flat of its own again."""
        with self._conn:
            self._conn.execute(
                "UPDATE listings SET duplicate_of = NULL WHERE source = ? AND source_id = ?",
                list(listing.key),
            )

    def mark_all_notified(self, now: datetime | None = None) -> int:
        """Treat everything stored so far as already notified (first-run seeding)."""
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE listings SET notified_price = price_eur, notified_at = ? "
                "WHERE notified_at IS NULL OR price_eur < notified_price",
                [(now or _now()).isoformat()],
            )
            return cursor.rowcount

    # --- reading -------------------------------------------------------------

    def get(self, source: str, source_id: str) -> StoredListing | None:
        row = self._conn.execute(
            "SELECT * FROM listings WHERE source = ? AND source_id = ?", [source, source_id]
        ).fetchone()
        return _to_listing(row) if row else None

    def all(self) -> list[StoredListing]:
        rows = self._conn.execute("SELECT * FROM listings ORDER BY price_eur").fetchall()
        return [_to_listing(row) for row in rows]

    def get_ref(self, ref: str) -> StoredListing | None:
        source, _, source_id = ref.partition(":")
        return self.get(source, source_id)

    def duplicate_candidates(self, listing: Listing, tolerance: float) -> list[StoredListing]:
        """Active, original listings from other sources with a similar area."""
        if listing.area_m2 is None:
            return []
        low, high = listing.area_m2 * (1 - tolerance), listing.area_m2 * (1 + tolerance)
        rows = self._conn.execute(
            "SELECT * FROM listings WHERE source != ? AND gone_at IS NULL "
            "AND duplicate_of IS NULL AND area_m2 BETWEEN ? AND ?",
            [listing.source, low, high],
        ).fetchall()
        return [_to_listing(row) for row in rows]

    def duplicates_of(self, listing: Listing) -> list[StoredListing]:
        """The same flat on other sites."""
        rows = self._conn.execute(
            "SELECT * FROM listings WHERE duplicate_of = ?", [listing.ref]
        ).fetchall()
        return [_to_listing(row) for row in rows]

    def duplicate_groups(self) -> list[tuple[StoredListing, list[StoredListing]]]:
        """Each flat listed on several sites: (original listing, its copies)."""
        listings = {listing.ref: listing for listing in self.all()}
        copies: dict[str, list[StoredListing]] = {}
        for listing in listings.values():
            if listing.duplicate_of in listings:
                copies.setdefault(listing.duplicate_of, []).append(listing)
        return [(listings[ref], group) for ref, group in copies.items()]

    def gone_since(self, since: datetime) -> list[StoredListing]:
        rows = self._conn.execute(
            "SELECT * FROM listings WHERE gone_at >= ? ORDER BY gone_at DESC", [since.isoformat()]
        ).fetchall()
        return [_to_listing(row) for row in rows]

    def pending_notifications(self) -> list[StoredListing]:
        """Listings never notified about, or whose price dropped since then."""
        rows = self._conn.execute(
            "SELECT * FROM listings "
            "WHERE notified_at IS NULL OR price_eur < notified_price "
            "ORDER BY first_seen"
        ).fetchall()
        return [_to_listing(row) for row in rows]

    def price_history(self, source: str, source_id: str) -> list[tuple[datetime, int | None]]:
        rows = self._conn.execute(
            "SELECT seen_at, price_eur FROM price_history "
            "WHERE source = ? AND source_id = ? ORDER BY seen_at",
            [source, source_id],
        ).fetchall()
        return [(datetime.fromisoformat(row["seen_at"]), row["price_eur"]) for row in rows]


def _to_listing(row: sqlite3.Row) -> StoredListing:
    data = dict(row)
    data.pop("raw_json")
    # Pydantic converts the SQLite types back: 0/1 -> bool, ISO text -> datetime.
    return StoredListing.model_validate(data)
