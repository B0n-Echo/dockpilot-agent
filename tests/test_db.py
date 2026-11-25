"""Tests for the working-copy reset and DockPilot's new tables."""

import hashlib
import sqlite3

import pytest

from dockpilot import db, paths

NEW_TABLES = {"slot_holds", "escalations", "events"}


@pytest.fixture
def work_db(tmp_path):
    """Build a fresh working copy in a temporary folder and yield an open connection to it."""
    target = db.init_work_db(tmp_path / "work.db")
    conn = db.connect(target)
    yield conn
    conn.close()


def _tables(conn):
    """Return the set of table names in a database, excluding SQLite's own internal tables."""
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    return {r[0] for r in rows}


def _add_hold(conn, hold_id, status="ACTIVE", slot_id="SLOT-JAI-019"):
    """Insert one hold row for SHP1012 / DRV012 on the given slot."""
    conn.execute(
        """INSERT INTO slot_holds (hold_id, group_id, slot_id, shipment_id, driver_id, hold_status,
                                   created_at, expires_at, request_key)
           VALUES (?, ?, ?, 'SHP1012', 'DRV012', ?, '2026-08-04T09:45:00+05:30',
                   '2026-08-04T09:55:00+05:30', ?)""",
        (hold_id, hold_id, slot_id, status, f"key-{hold_id}"),
    )


def test_original_tables_untouched(work_db):
    """Every one of the 18 original tables keeps the same row count as the source."""
    source = sqlite3.connect(f"file:{paths.SOURCE_DB}?mode=ro", uri=True)
    try:
        originals = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert len(originals) == 18
        for table in originals:
            expected = source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert work_db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == expected, table
    finally:
        source.close()


def test_new_tables_exist(work_db):
    """The DockPilot tables (3 from dp-02, 2 login tables from dp-18, conversation_state from dp-14, slot_requests from dp-10) are added, for 25 tables in total."""
    tables = _tables(work_db)
    assert NEW_TABLES <= tables
    assert {"login_codes", "driver_sessions", "conversation_state", "slot_requests"} <= tables
    assert len(tables) == 25


def test_source_unchanged_after_init(work_db):
    """Building a working copy leaves the original database byte-for-byte identical."""
    sums = dict(reversed(line.split()) for line in (paths.DATA_SOURCE / "SHA256SUMS").read_text().splitlines())
    actual = hashlib.sha256(paths.SOURCE_DB.read_bytes()).hexdigest()
    assert actual == sums[paths.SOURCE_DB.name]


def test_second_active_hold_on_same_slot_refused(work_db):
    """Only one ACTIVE hold per slot; CONVERTED and EXPIRED holds on that slot are allowed."""
    _add_hold(work_db, "H1")
    with pytest.raises(sqlite3.IntegrityError):
        _add_hold(work_db, "H2")
    _add_hold(work_db, "H3", status="CONVERTED")
    _add_hold(work_db, "H4", status="EXPIRED")


def test_hold_on_unknown_slot_refused(work_db):
    """Foreign keys are enforced: a hold can't point at a slot that doesn't exist."""
    with pytest.raises(sqlite3.IntegrityError):
        _add_hold(work_db, "H1", slot_id="SLOT-DOES-NOT-EXIST")


def test_hold_with_unknown_status_refused(work_db):
    """The allowed-values check rejects a made-up hold status."""
    with pytest.raises(sqlite3.IntegrityError):
        _add_hold(work_db, "H1", status="MAYBE")


def test_init_again_resets(tmp_path):
    """Running init a second time wipes anything written to the previous working copy."""
    target = db.init_work_db(tmp_path / "work.db")
    conn = db.connect(target)
    conn.execute(
        "INSERT INTO events (event_ts, event_type, entity_type, entity_id, actor) "
        "VALUES ('2026-08-04T09:45:00+05:30', 'TEST', 'HOLD', 'H1', 'SYSTEM')"
    )
    conn.commit()
    conn.close()

    db.init_work_db(target)
    conn = db.connect(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    finally:
        conn.close()
