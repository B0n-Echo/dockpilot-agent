"""Tests for hold -> PENDING -> CONFIRMED / REJECTED, timeouts and expired holds (D6)."""

import io

import pytest

from dockpilot import db
from dockpilot.bookings import check_warehouse_timeouts, confirm_booking, convert_hold, reject_booking
from dockpilot.clock import FakeClock
from dockpilot.holds import get_hold, place_hold
from dockpilot.options import find_options


@pytest.fixture
def conn(tmp_path):
    """A fresh working copy in a temporary folder."""
    c = db.connect(db.init_work_db(tmp_path / "work.db"))
    yield c
    c.close()


@pytest.fixture
def clock():
    """A fake clock at 09:45."""
    return FakeClock()


def _appt(conn, appointment_id):
    """One appointments row."""
    return conn.execute("SELECT * FROM appointments WHERE appointment_id = ?", (appointment_id,)).fetchone()


def _hold_1012(conn, clock):
    """SHP1012 holds the 12:00 slot on D2; returns the hold group id."""
    return place_hold(conn, "SHP1012", ["SLOT-JAI-019"], "req-1012", clock).hold.group_id


def test_contract_convert_makes_pending_booking_linked_to_hold(conn, clock):
    """Converting gives a PENDING appointment; the hold is CONVERTED and points at it."""
    group = _hold_1012(conn, clock)
    r = convert_hold(conn, group, clock)
    assert r.ok and r.status == "PENDING_CONFIRMATION"
    assert _appt(conn, r.appointment_id)["slot_id"] == "SLOT-JAI-019"
    row = conn.execute("SELECT hold_status, appointment_id FROM slot_holds WHERE group_id = ?", (group,)).fetchone()
    assert (row["hold_status"], row["appointment_id"]) == ("CONVERTED", r.appointment_id)


def test_contract_confirm(conn, clock):
    """The warehouse confirms: CONFIRMED and current."""
    r = convert_hold(conn, _hold_1012(conn, clock), clock)
    confirm_booking(conn, r.appointment_id, clock, ref="WH-1")
    a = _appt(conn, r.appointment_id)
    assert (a["appointment_status"], a["is_current"], a["warehouse_confirmation_ref"]) == ("CONFIRMED", 1, "WH-1")


def test_contract_reject_frees_the_slot(conn, clock):
    """The warehouse rejects: REJECTED, and the slot is offered to others again."""
    r = convert_hold(conn, _hold_1012(conn, clock), clock)
    offered = lambda: {s for o in find_options(conn, "SHP1013", clock).options for s in o.slot_ids}
    assert "SLOT-JAI-019" not in offered()
    reject_booking(conn, r.appointment_id, clock, "Dock crew short")
    assert _appt(conn, r.appointment_id)["appointment_status"] == "REJECTED"
    assert "SLOT-JAI-019" in offered()


def test_contract_warehouse_timeout_raises_escalation(conn, clock):
    """No answer for 30 minutes: an escalation is raised and the booking stays PENDING."""
    r = convert_hold(conn, _hold_1012(conn, clock), clock)
    clock.advance(minutes=31)
    escs = check_warehouse_timeouts(conn, clock, out=io.StringIO())
    mine = [e for e in escs if e.shipment_id == "SHP1012"]
    assert len(mine) == 1 and mine[0].reason_code == "WAREHOUSE_TIMEOUT"
    assert mine[0].appointment_id == r.appointment_id
    assert _appt(conn, r.appointment_id)["appointment_status"] == "PENDING_CONFIRMATION"
    assert "SHP1013" in {e.shipment_id for e in escs}  # the client's APT1013A, waiting since 09:32 (DA-02)


def test_contract_expired_hold_cannot_be_converted(conn, clock):
    """After the hold runs out, converting is refused and nothing is booked."""
    group = _hold_1012(conn, clock)
    clock.advance(minutes=11)
    r = convert_hold(conn, group, clock)
    assert not r.ok and r.reasons[0].code == "HOLD_EXPIRED"
    assert conn.execute("SELECT COUNT(*) FROM appointments WHERE shipment_id = 'SHP1012' "
                        "AND booking_source = 'DRIVER_CHAT'").fetchone()[0] == 0


def test_old_booking_is_kept_until_the_new_one_is_confirmed(conn, clock):
    """SHP1014 keeps APT1014A while the new booking waits; on confirm, APT1014A is cancelled."""
    group = place_hold(conn, "SHP1014", ["SLOT-JAI-019", "SLOT-JAI-020"], "req-1014", clock).hold.group_id
    r = convert_hold(conn, group, clock)
    old = _appt(conn, "APT1014A")
    assert (old["appointment_status"], old["is_current"]) == ("PENDING_CONFIRMATION", 1)
    confirm_booking(conn, r.appointment_id, clock)
    old, new = _appt(conn, "APT1014A"), _appt(conn, r.appointment_id)
    assert (old["appointment_status"], old["is_current"]) == ("CANCELLED", 0)
    assert (new["appointment_status"], new["is_current"], new["replaced_appointment_id"]) == ("CONFIRMED", 1, "APT1014A")
    assert "SLOT-JAI-020" not in {s for o in find_options(conn, "SHP1012", clock).options for s in o.slot_ids}


def test_saying_yes_twice_books_once(conn, clock):
    """A repeated 'yes' returns the same booking."""
    group = _hold_1012(conn, clock)
    first, again = convert_hold(conn, group, clock), convert_hold(conn, group, clock)
    assert again.replayed and again.appointment_id == first.appointment_id
    assert get_hold(conn, group).status == "CONVERTED"


def test_booking_the_slot_the_truck_already_has_is_refused_not_a_crash(conn, clock):
    """dp-19: SHP1013 (now arriving 10:50) holds its own pending slot D2 11:00 (APT1013A); yes is a plain refusal."""
    conn.execute("INSERT INTO eta_updates (eta_update_id, shipment_id, source_type, declared_eta_ts, confidence_code, "
                 "created_at) VALUES ('ETA_DP-t', 'SHP1013', 'DRIVER_DECLARED', '2026-08-04T10:50:00+05:30', 'HIGH', "
                 "'2026-08-04T09:45:00+05:30')")
    group = place_hold(conn, "SHP1013", ["SLOT-JAI-018"], "own-slot", clock).hold.group_id
    r = convert_hold(conn, group, clock)
    assert not r.ok and r.reasons[0].code == "SLOT_TAKEN"
