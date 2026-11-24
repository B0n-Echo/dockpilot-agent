"""Tests for the option search (D5), run against a fresh working copy at 09:44."""

import dataclasses
from datetime import datetime

import pytest

from dockpilot import db
from dockpilot.clock import IST, FakeClock
from dockpilot.options import find_options
from dockpilot.settings import SETTINGS

AT_0944 = datetime(2026, 8, 4, 9, 44, tzinfo=IST)
"""The minute SHP1014's driver messaged (DA-04)."""


@pytest.fixture
def conn(tmp_path):
    """Build a fresh working copy in a temporary folder and yield an open connection to it."""
    c = db.connect(db.init_work_db(tmp_path / "work.db"))
    yield c
    c.close()


@pytest.fixture
def clock():
    """A fake clock at 2026-08-04 09:44 +05:30."""
    return FakeClock(AT_0944)


def _slots_offered(search):
    """Return every slot id that appears in any option."""
    return {s for o in search.options for s in o.slot_ids}


def test_contract_shp1014_first_option_is_12_00_on_d2_or_d4(conn, clock):
    """Contract: SHP1014's first option starts 12:00 on D2 or D4; every option is two back-to-back slots."""
    search = find_options(conn, "SHP1014", clock)
    first = search.options[0]
    assert first.start == datetime(2026, 8, 4, 12, 0, tzinfo=IST)
    assert first.dock_code in {"D2", "D4"}
    for o in search.options:
        assert len(o.slot_ids) == 2
        assert o.dock_code in {"D2", "D4"}


def test_contract_held_slot_is_never_offered(conn, clock):
    """Contract: another shipment's active hold on SLOT-JAI-047 keeps it out of every option."""
    conn.execute(
        """INSERT INTO slot_holds (hold_id, group_id, slot_id, shipment_id, driver_id, hold_status,
                                   created_at, expires_at, request_key)
           VALUES ('H1', 'H1', 'SLOT-JAI-047', 'SHP1012', 'DRV012', 'ACTIVE',
                   '2026-08-04T09:40:00+05:30', '2026-08-04T09:50:00+05:30', 'key-H1')"""
    )
    search = find_options(conn, "SHP1014", clock)
    assert "SLOT-JAI-047" not in _slots_offered(search)
    assert search.options[0].slot_ids == ["SLOT-JAI-019", "SLOT-JAI-020"]


def test_contract_booked_slots_are_never_offered(conn, clock):
    """Contract: the 11:00 slots booked for SHP1013 and SHP1009 are never offered to anyone else."""
    for shipment in ("SHP1012", "SHP1014", "SHP1017"):
        offered = _slots_offered(find_options(conn, shipment, clock))
        assert not {"SLOT-JAI-018", "SLOT-JAI-046"} & offered


def test_short_unload_uses_one_slot(conn, clock):
    """SHP1012 (45 min, ETA 11:10) gets single-slot options, the first at 12:00."""
    search = find_options(conn, "SHP1012", clock)
    assert all(len(o.slot_ids) == 1 for o in search.options)
    assert search.options[0].start == datetime(2026, 8, 4, 12, 0, tzinfo=IST)


def test_options_are_limited_by_settings(conn, clock):
    """At most max_options come back; a copy with max_options=1 gives exactly one."""
    assert len(find_options(conn, "SHP1014", clock).options) == SETTINGS.max_options
    one = dataclasses.replace(SETTINGS, max_options=1)
    assert len(find_options(conn, "SHP1014", clock, one).options) == 1


def test_checked_holds_passes_and_failures(conn, clock):
    """Every option is in `checked` as feasible; failures carry reasons (D1 is too weak for SHP1014)."""
    search = find_options(conn, "SHP1014", clock)
    feasible = {tuple(c.slot_ids) for c in search.checked if c.feasible}
    assert all(tuple(o.slot_ids) in feasible for o in search.options)
    d1_fails = [c for c in search.checked
                if c.slot_ids[0].startswith("SLOT-JAI-00") and not c.feasible]
    assert d1_fails
    assert all("OVER_WEIGHT_LIMIT" in {r.code for r in c.reasons} for c in d1_fails)


def test_no_options_still_reports_what_was_checked(conn, clock):
    """SHP1015 (reefer, ETA 18:30) has no usable slot: D5 is under maintenance from 18:00."""
    search = find_options(conn, "SHP1015", clock)
    assert search.options == []
    assert search.checked
    assert all(not c.feasible and c.reasons for c in search.checked)
