"""Tests for the integrity proof (dp-11, D13, M1)."""

import pytest

from dockpilot import db
from dockpilot.clock import FakeClock
from dockpilot.integrity import RACE_TIME, check_double_bookings, run_race


@pytest.fixture
def conn(tmp_path):
    """A fresh working copy in a temporary folder."""
    c = db.connect(db.init_work_db(tmp_path / "work.db"))
    yield c
    c.close()


def _book(conn, appt, shipment, slot):
    """Plant a live booking straight into the table."""
    conn.execute("INSERT INTO appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, "
                 "is_current, booked_at, updated_at) VALUES (?, ?, ?, 'PENDING_CONFIRMATION', 'DRIVER_CHAT', 0, "
                 "'2026-08-04T09:40:00+05:30', '2026-08-04T09:40:00+05:30')", (appt, shipment, slot))


def test_fresh_copy_has_no_double_bookings(conn):
    """The client's live data at the snapshot: M1 = 0 (DA-09's pair was cancelled at 09:40)."""
    assert check_double_bookings(conn, FakeClock(RACE_TIME)) == []


def test_hold_for_one_truck_and_booking_for_another_is_caught(conn):
    """The database's indexes allow this mix across tables; the check must not."""
    _book(conn, "APT-plant1", "SHP1012", "SLOT-JAI-019")
    conn.execute("INSERT INTO slot_holds (hold_id, group_id, slot_id, shipment_id, driver_id, hold_status, created_at, "
                 "expires_at, request_key) VALUES ('H1', 'G1', 'SLOT-JAI-019', 'SHP1013', 'DRV013', 'ACTIVE', "
                 "'2026-08-04T09:44:00+05:30', '2026-08-04T09:54:00+05:30', 'plant:1')")
    problems = check_double_bookings(conn, FakeClock(RACE_TIME))
    assert problems == ["D2: SLOT-JAI-019 SHP1012 (booking APT-plant1) overlaps SLOT-JAI-019 SHP1013 (hold G1)"]


def test_overlap_through_a_two_slot_booking_is_caught(conn):
    """A two-slot booking's second slot (converted hold) clashing with another truck's booking is caught."""
    _book(conn, "APT-long", "SHP1014", "SLOT-JAI-019")
    conn.execute("INSERT INTO slot_holds (hold_id, group_id, slot_id, shipment_id, driver_id, hold_status, created_at, "
                 "expires_at, appointment_id, request_key) VALUES ('H2', 'G2', 'SLOT-JAI-020', 'SHP1014', 'DRV014', "
                 "'CONVERTED', '2026-08-04T09:40:00+05:30', '2026-08-04T09:50:00+05:30', 'APT-long', 'plant:2')")
    _book(conn, "APT-next", "SHP1013", "SLOT-JAI-020")
    assert any("SLOT-JAI-020 SHP1014" in p and "SHP1013" in p for p in check_double_bookings(conn, FakeClock(RACE_TIME)))


def test_contract_hundred_rounds_never_double_book():
    """100 rounds x 8 racers (6 trucks + 2 duplicates, each retrying): 0 problems, one winner every round,
    the winner's duplicate gets the same hold, every winner's retry is answered from memory."""
    r = run_race(rounds=100, workers=8)
    assert r["rounds_with_problems"] == 0 and r["one_winner_rounds"] == 100
    assert r["duplicate_same_hold"] == r["duplicate_checks"]
    assert r["retries_replayed"] + r["loser_retries_refused"] == r["retries"] == 800


def test_contract_smoke_alarm_naive_version_is_caught():
    """Without the lock and the safety indexes, the check finds at least one double booking."""
    r = run_race(rounds=20, workers=8, naive=True)
    assert r["rounds_with_problems"] >= 1 and "overlaps" in r["example"]
