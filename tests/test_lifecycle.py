"""Tests for bookings during the day: late ETA, reschedule, no-show, dock event (D9)."""

import io
from datetime import datetime

import pytest

from dockpilot import db
from dockpilot.bookings import confirm_booking, convert_hold
from dockpilot.clock import IST, FakeClock
from dockpilot.holds import place_hold
from dockpilot.lifecycle import handle_dock_event, mark_no_shows, recheck_booking


def at(hh, mm):
    """A time on 4 Aug 2026, India time."""
    return datetime(2026, 8, 4, hh, mm, tzinfo=IST)


@pytest.fixture
def conn(tmp_path):
    """A fresh working copy in a temporary folder."""
    c = db.connect(db.init_work_db(tmp_path / "work.db"))
    yield c
    c.close()


def _appt(conn, appointment_id):
    """One appointments row."""
    return conn.execute("SELECT * FROM appointments WHERE appointment_id = ?", (appointment_id,)).fetchone()


def _events(conn, event_type):
    """All events of one type."""
    return conn.execute("SELECT * FROM events WHERE event_type = ?", (event_type,)).fetchall()


def test_contract_late_eta_flags_booking_and_offers_options(conn):
    """SHP1006 (booked D4 10:00, ETA now 11:20) is flagged and offered options; APT1006 stays booked."""
    r = recheck_booking(conn, "SHP1006", FakeClock())
    assert not r.ok and r.appointment_id == "APT1006"
    assert "ARRIVES_AFTER_START" in {x.code for x in r.reasons}
    assert r.options and r.escalation is None
    assert len(_events(conn, "BOOKING_FLAGGED")) == 1
    assert _appt(conn, "APT1006")["appointment_status"] == "CONFIRMED"


def test_contract_reschedule_sets_replaced_appointment(conn):
    """Rescheduling SHP1006 onto its first option: new booking replaces APT1006, which is cancelled."""
    clock = FakeClock()
    first = recheck_booking(conn, "SHP1006", clock).options[0]
    hold = place_hold(conn, "SHP1006", first.slot_ids, "resched-1006", clock).hold
    new = convert_hold(conn, hold.group_id, clock).appointment_id
    confirm_booking(conn, new, clock)
    assert _appt(conn, new)["replaced_appointment_id"] == "APT1006"
    assert _appt(conn, "APT1006")["appointment_status"] == "CANCELLED"


def test_contract_no_show_after_grace(conn):
    """At 10:31, APT1007 (10:00, no check-in) is NO_SHOW; APT1004 (checked in 09:25) is not."""
    reviews = mark_no_shows(conn, FakeClock(at(10, 31)), out=io.StringIO())
    assert "APT1007" in {r.appointment_id for r in reviews}
    assert _appt(conn, "APT1007")["appointment_status"] == "NO_SHOW"
    assert _appt(conn, "APT1004")["appointment_status"] == "CONFIRMED"
    shp1007 = next(r for r in reviews if r.appointment_id == "APT1007")
    assert shp1007.options or shp1007.escalation  # still on the road: offered options or escalated


def test_contract_dock_event_rehomes_or_escalates_every_affected_booking(conn):
    """DEVT001 (D3 broken) at 09:45 hits APT1005; it gets a new PENDING booking or an escalation."""
    reviews = handle_dock_event(conn, "DEVT001", FakeClock(), out=io.StringIO())
    assert [r.appointment_id for r in reviews] == ["APT1005"]
    r = reviews[0]
    assert r.new_appointment_id or r.escalation
    if r.new_appointment_id:
        new = _appt(conn, r.new_appointment_id)
        assert (new["appointment_status"], new["replaced_appointment_id"]) == ("PENDING_CONFIRMATION", "APT1005")
    assert len(_events(conn, "DOCK_EVENT_HANDLED")) == 1


def test_booking_that_still_fits_is_left_alone(conn):
    """SHP1010 (reefer, ETA 10:55, booked D5 11:00) still fits: nothing written."""
    r = recheck_booking(conn, "SHP1010", FakeClock())
    assert r.ok and r.options == []
    assert _events(conn, "BOOKING_FLAGGED") == []


def test_late_eta_with_no_options_escalates(conn):
    """SHP1015 at 16:25 no longer fits its 17:00 booking and nothing else fits: escalated."""
    r = recheck_booking(conn, "SHP1015", FakeClock(at(16, 25)), out=io.StringIO())
    assert not r.ok and r.options == []
    assert r.escalation.reason_code == "NO_FEASIBLE_SLOT"
