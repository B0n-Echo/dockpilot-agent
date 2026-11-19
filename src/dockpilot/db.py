"""Database access: safe connections and the working-copy reset.

The client's original database (paths.SOURCE_DB) is only ever read. All writes go to a
working copy built by init_work_db(), which also adds DockPilot's own tables.
"""

import shutil
import sqlite3
from pathlib import Path

from dockpilot import paths

MIGRATIONS = Path(__file__).parent / "migrations"
"""Folder of .sql files applied, in name order, to every new working copy."""


def connect(path):
    """Open a SQLite database the way DockPilot expects.

    Foreign-key checks are switched on (SQLite leaves them off by default), and rows
    come back addressable by column name, e.g. row["slot_id"].

    Args:
        path: file path of the database to open.

    Returns:
        An open sqlite3.Connection.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Busy timeout / write-transaction mode for concurrent claims: dp-06, dp-11.
    return conn


def init_work_db(target=paths.WORK_DB):
    """Rebuild the working database from the original: the "reset button".

    Deletes any existing working copy (and its SQLite side files), copies the original
    database byte-for-byte to `target`, then applies every migration in MIGRATIONS to the copy.

    Args:
        target: where to build the working copy. Defaults to paths.WORK_DB; tests pass a
            temporary path so they never touch your working database.

    Returns:
        The Path of the freshly built working database.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{target}{suffix}").unlink(missing_ok=True)

    shutil.copyfile(paths.SOURCE_DB, target)

    conn = connect(target)
    try:
        for sql_file in sorted(MIGRATIONS.glob("*.sql")):
            conn.executescript(sql_file.read_text())
        conn.commit()
    finally:
        conn.close()
    return target


def main():
    """Terminal entry point: `uv run python -m dockpilot.db` rebuilds the working copy."""
    print(f"Working database ready: {init_work_db()}")


if __name__ == "__main__":
    main()
