"""Smoke tests: the package loads and the client's data is present and untouched."""

import hashlib
import sqlite3

from dockpilot import paths


def _sha256(path):
    """Return the SHA-256 hex fingerprint of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _open_source_readonly():
    """Open the original database in read-only mode, so a test can never alter it."""
    return sqlite3.connect(f"file:{paths.SOURCE_DB}?mode=ro", uri=True)


def test_package_imports():
    """The dockpilot package imports and points at an existing source database."""
    import dockpilot

    assert dockpilot.paths.SOURCE_DB.exists()


def test_source_files_unchanged():
    """Every file in data/source/ still matches the fingerprint saved in SHA256SUMS."""
    lines = (paths.DATA_SOURCE / "SHA256SUMS").read_text().splitlines()
    assert len(lines) == 5
    for line in lines:
        expected, filename = line.split()
        assert _sha256(paths.DATA_SOURCE / filename) == expected, filename


def test_source_db_readable_and_complete():
    """The original database opens read-only and holds 18 tables and 21 shipments."""
    conn = _open_source_readonly()
    try:
        tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
        shipments = conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0]
    finally:
        conn.close()
    assert tables == 18
    assert shipments == 21
    # Working copy + migration (slot_holds, escalations, events) → dp-02
