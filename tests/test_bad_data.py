"""Tests for bad data (dp-21): a booking that can't be true is reported honestly and handed to a coordinator once;
clean records are unchanged; the client's data is never edited."""

from datetime import datetime

import pytest

from dockpilot import db
from dockpilot.auth import request_code, verify_code
from dockpilot.clock import IST, FakeClock
from dockpilot.conversation import handle_message
from dockpilot.reader import read_message


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


def _say(conn, clock, phone, *texts):
    """Log a driver in and send messages; returns the last Reply."""
    sid = verify_code(conn, phone, request_code(conn, phone, clock).code, clock).session.session_id
    for t in texts:
        reply = handle_message(conn, sid, t, clock, reader=read_message)
    return reply


def _handoffs(conn):
    """(shipment, summary) of every CONTRADICTORY_DATA escalation."""
    return [tuple(r) for r in conn.execute(
        "SELECT shipment_id, summary FROM escalations WHERE reason_code = 'CONTRADICTORY_DATA'")]


def test_contract_impossible_booking_is_reported_with_reason_and_handed_off(conn, clock):
    """Pradeep (SHP1014): APT1014A puts 21 t on D1 (20 t) for 75 min in 60 → reason, how to fix, one hand-off."""
    reply = _say(conn, clock, "9000010014", "what is my status?")
    assert reply.text.startswith("Your booking: D1 at 11:00, waiting for the warehouse to confirm, but it can't work as "
                                 "booked. Load is 21,000 kg; dock D1 takes at most 20,000 kg.")
    assert "Unloading takes 75 min; the slot gives 60 min." in reply.text
    assert reply.text.endswith("Send me your arrival time and I'll find you a slot that fits.")
    assert [s for s, _ in _handoffs(conn)] == ["SHP1014"]


def test_confirmed_impossible_booking_too(conn, clock):
    """SHP1005's confirmed D3 09:00 is overweight and too short: reported, not repeated as 'confirmed.'"""
    reply = _say(conn, clock, "9000010005", "what is my status?")
    assert "confirmed, but it can't work as booked" in reply.text and "dock D3 takes at most" in reply.text


def test_asking_twice_gives_one_handoff(conn, clock):
    """The coordinator gets one hand-off per truck, however often the driver asks."""
    _say(conn, clock, "9000010014", "what is my status?", "what is my status?", "is my slot still active?")
    assert len(_handoffs(conn)) == 1


def test_sound_booking_reply_is_unchanged(conn, clock):
    """Kailash's APT1013A is late but not impossible: the status reply is exactly as before, no hand-off."""
    reply = _say(conn, clock, "9000010013", "what is my status?")
    assert reply.text == ("Your booking: D2 at 11:00, waiting for the warehouse to confirm. "
                          "The arrival time we have for you is about 11:00 (from you).")
    assert _handoffs(conn) == []


def test_another_truck_on_the_same_slot_is_caught(conn, clock):
    """Planted DA-09 kind: a second live booking on Kailash's slot (the copy's safety index removed, as in dp-11)."""
    conn.execute("DROP INDEX ux_active_appointment_per_slot")
    conn.execute("INSERT INTO appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, "
                 "is_current, booked_at, updated_at) VALUES ('APT-PLANT', 'SHP1017', 'SLOT-JAI-018', 'CONFIRMED', "
                 "'PLANNER', 0, '2026-08-04T08:00:00+05:30', '2026-08-04T08:00:00+05:30')")
    reply = _say(conn, clock, "9000010013", "what is my status?")
    assert "Another truck (SHP1017, booking APT-PLANT) is booked on the same slot." in reply.text
    assert [s for s, _ in _handoffs(conn)] == ["SHP1013"]


def test_two_current_bookings_for_one_truck_are_caught(conn, clock):
    """Planted: a second 'current' live booking for SHP1013 (its per-truck index removed in the copy)."""
    conn.execute("DROP INDEX ux_current_active_appointment_per_shipment")
    conn.execute("INSERT INTO appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, "
                 "is_current, booked_at, updated_at) VALUES ('APT-TWIN', 'SHP1013', 'SLOT-JAI-034', 'CONFIRMED', "
                 "'PLANNER', 1, '2026-08-04T08:00:00+05:30', '2026-08-04T08:00:00+05:30')")
    reply = _say(conn, clock, "9000010013", "what is my status?")
    assert "The truck has two current bookings at once." in reply.text


def test_driver_on_two_moving_trucks_is_still_asked_and_handed_off(conn, clock):
    """DRV007 (DA-11) drives SHP1007 and SHP1016 at once: asked which trip as before, plus one hand-off."""
    reply = _say(conn, clock, "9000010007", "Heavy trailer ETA 12:00. Original heavy-bay slot will be missed.")
    assert reply.state == "SHIPMENT" and "Which one is this about?" in reply.text
    (shipment, summary), = _handoffs(conn)
    assert summary == "DRV007 is on two moving trucks at once: SHP1007 RJ19GT4108, SHP1016 MH04HV4107."


def test_client_data_is_never_changed(conn, clock):
    """Reporting a bad booking changes no booking row."""
    before = [tuple(r) for r in conn.execute("SELECT * FROM appointments ORDER BY appointment_id")]
    _say(conn, clock, "9000010014", "what is my status?")
    _say(conn, clock, "9000010005", "what is my status?")
    assert [tuple(r) for r in conn.execute("SELECT * FROM appointments ORDER BY appointment_id")] == before
