"""Tests for the feasibility check (D4), run against a fresh working copy at 09:45."""

import dataclasses
from datetime import datetime

import pytest

from dockpilot import db
from dockpilot.clock import IST, FakeClock
from dockpilot.feasibility import check_feasibility, latest_eta
from dockpilot.settings import SETTINGS


@pytest.fixture
def conn(tmp_path):
    """Build a fresh working copy in a temporary folder and yield an open connection to it."""
    c = db.connect(db.init_work_db(tmp_path / "work.db"))
    yield c
    c.close()


@pytest.fixture
def clock():
    """A fake clock at the snapshot time, 2026-08-04 09:45 +05:30."""
    return FakeClock()


def _codes(result):
    """Return the set of reason codes in a Feasibility result."""
    return {r.code for r in result.reasons}


def test_contract_shp1014_slot004_fails(conn, clock):
    """Contract: SHP1014 in SLOT-JAI-004 fails on arrival, weight and unload length, and nothing else."""
    result = check_feasibility(conn, "SHP1014", ["SLOT-JAI-004"], clock)
    assert not result.feasible
    assert _codes(result) == {"ARRIVES_AFTER_START", "OVER_WEIGHT_LIMIT", "UNLOAD_TOO_LONG"}


def test_contract_shp1012_slot019_passes(conn, clock):
    """Contract: SHP1012 in SLOT-JAI-019 passes with no reasons."""
    result = check_feasibility(conn, "SHP1012", ["SLOT-JAI-019"], clock)
    assert result.feasible
    assert result.reasons == []


def test_two_back_to_back_slots_fit_a_long_unload(conn, clock):
    """SHP1014 (75 min, 21 t) fits D4 12:00-14:00 across two slots."""
    result = check_feasibility(conn, "SHP1014", ["SLOT-JAI-047", "SLOT-JAI-048"], clock)
    assert result.feasible, result.reasons


def test_slots_on_different_docks_are_not_back_to_back(conn, clock):
    """Two slots on different docks are refused as a pair."""
    result = check_feasibility(conn, "SHP1014", ["SLOT-JAI-019", "SLOT-JAI-048"], clock)
    assert "SLOTS_NOT_BACK_TO_BACK" in _codes(result)


def test_reefer_load_on_standard_dock(conn, clock):
    """SHP1015 needs refrigeration; a standard dock fails on reefer and dock type."""
    result = check_feasibility(conn, "SHP1015", ["SLOT-JAI-019"], clock)
    assert {"NEEDS_REEFER", "WRONG_DOCK_TYPE"} <= _codes(result)


def test_dock_event_on_an_open_slot(conn, clock):
    """SLOT-JAI-030 is OPEN, but dock D3 has been broken since 09:15 (DEVT001)."""
    result = check_feasibility(conn, "SHP1012", ["SLOT-JAI-030"], clock)
    assert "DOCK_EVENT" in _codes(result)


def test_slot_taken_by_another_shipment(conn, clock):
    """SLOT-JAI-018 holds SHP1013's live booking, so SHP1012 can't have it."""
    result = check_feasibility(conn, "SHP1012", ["SLOT-JAI-018"], clock)
    assert "SLOT_TAKEN" in _codes(result)


def test_active_hold_blocks_until_it_expires(conn, clock):
    """Another shipment's active hold blocks the slot until its expiry time passes."""
    conn.execute(
        """INSERT INTO slot_holds (hold_id, group_id, slot_id, shipment_id, driver_id, hold_status,
                                   created_at, expires_at, request_key)
           VALUES ('H1', 'H1', 'SLOT-JAI-020', 'SHP1013', 'DRV013', 'ACTIVE',
                   '2026-08-04T09:45:00+05:30', '2026-08-04T09:55:00+05:30', 'key-H1')"""
    )
    assert "SLOT_HELD" in _codes(check_feasibility(conn, "SHP1012", ["SLOT-JAI-020"], clock))
    clock.advance(minutes=11)
    assert check_feasibility(conn, "SHP1012", ["SLOT-JAI-020"], clock).feasible


def test_low_confidence_eta_gets_the_buffer(conn, clock):
    """SHP1013 (ETA 11:00, LOW) fits a 12:00 slot with 30 min buffer, but not with 90."""
    assert check_feasibility(conn, "SHP1013", ["SLOT-JAI-019"], clock).feasible
    big_buffer = dataclasses.replace(SETTINGS, eta_buffer_minutes=90)
    result = check_feasibility(conn, "SHP1013", ["SLOT-JAI-019"], clock, big_buffer)
    assert _codes(result) == {"ARRIVES_AFTER_START"}


def test_latest_eta_ignores_updates_from_the_future(conn, clock):
    """At 09:45, SHP1017's 09:55 update (ETA012) is not visible; at 09:55 it is."""
    before = latest_eta(conn, "SHP1017", clock)
    clock.advance(minutes=10)
    after = latest_eta(conn, "SHP1017", clock)
    assert after == (datetime(2026, 8, 4, 12, 45, tzinfo=IST), "DRIVER_DECLARED", "LOW")
    assert before != after


def test_unknown_ids_raise(conn, clock):
    """An unknown shipment or slot is an error, not a 'not feasible' answer."""
    with pytest.raises(LookupError):
        check_feasibility(conn, "SHP9999", ["SLOT-JAI-019"], clock)
    with pytest.raises(LookupError):
        check_feasibility(conn, "SHP1012", ["SLOT-NOPE"], clock)
