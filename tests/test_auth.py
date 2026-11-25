"""Tests for driver login and the trip check (dp-18), on the real drivers at 09:45."""

import pytest

from dockpilot import db
from dockpilot.auth import NO_TRIP, active_trips, choose_shipment, end_session, get_session, request_code, verify_code
from dockpilot.clock import FakeClock


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


def _login(conn, clock, phone):
    """Request a code and enter it correctly; return the LoginResult."""
    return verify_code(conn, phone, request_code(conn, phone, clock).code, clock)


def test_contract_one_trip_driver_gets_session_for_that_shipment(conn, clock):
    """DRV013 (SHP1013 on the road): code, then correct code, gives a session tied to SHP1013."""
    sent = request_code(conn, "+91-9000010013", clock)
    assert sent.ok and len(sent.code) == 6 and sent.code.isdigit()
    result = verify_code(conn, "9000010013", sent.code, clock)
    assert result.ok and result.session.shipment_id == "SHP1013"
    assert result.session.driver_name == "Kailash Gurjar"
    events = [r[0] for r in conn.execute("SELECT event_type FROM events WHERE entity_type = 'LOGIN' ORDER BY event_id")]
    assert events == ["LOGIN_CODE_SENT", "LOGIN_OK"]


def test_phone_formats_are_accepted(conn, clock):
    """Spaces, dashes and the +91 prefix all find the same driver."""
    for phone in ("9000010013", "+91 90000 10013", "+91-9000010013"):
        assert request_code(conn, phone, clock).ok


def test_wrong_code_counts_tries_and_three_burn_the_code(conn, clock):
    """Wrong codes say how many tries are left; after 3 even the right code is refused."""
    code = request_code(conn, "9000010013", clock).code
    wrong = "111111" if code != "111111" else "222222"
    assert verify_code(conn, "9000010013", wrong, clock).reason == "Wrong code. 2 tries left."
    assert verify_code(conn, "9000010013", wrong, clock).reason == "Wrong code. 1 try left."
    assert not verify_code(conn, "9000010013", wrong, clock).ok
    assert not verify_code(conn, "9000010013", code, clock).ok


def test_code_expires_after_five_minutes(conn, clock):
    """A code entered after 5 minutes is refused."""
    code = request_code(conn, "9000010013", clock).code
    clock.advance(minutes=5)
    assert "expired" in verify_code(conn, "9000010013", code, clock).reason


def test_used_code_cannot_be_reused_and_new_code_cancels_old(conn, clock):
    """A used code is gone; asking again cancels the earlier unused code."""
    code = request_code(conn, "9000010013", clock).code
    assert verify_code(conn, "9000010013", code, clock).ok
    assert not verify_code(conn, "9000010013", code, clock).ok
    first = request_code(conn, "9000010013", clock).code
    second = request_code(conn, "9000010013", clock).code
    if first != second:
        assert not verify_code(conn, "9000010013", first, clock).ok
    assert verify_code(conn, "9000010013", second, clock).ok


def test_driver_with_cancelled_trips_is_refused_without_a_code(conn, clock):
    """DRV008 (both trips cancelled) gets the reason and no code row is made."""
    result = request_code(conn, "9000010008", clock)
    assert not result.ok and result.reason == NO_TRIP and result.code is None
    assert conn.execute("SELECT COUNT(*) FROM login_codes").fetchone()[0] == 0


def test_unknown_phone_is_refused(conn, clock):
    """A number that isn't a driver is refused."""
    assert request_code(conn, "+91-9999999999", clock).reason == "This number isn't registered as a driver."


def test_inactive_driver_is_refused(conn, clock):
    """A driver who isn't ACTIVE is refused even with a trip."""
    conn.execute("UPDATE drivers SET driver_status = 'SUSPENDED' WHERE driver_id = 'DRV013'")
    assert "isn't active" in request_code(conn, "9000010013", clock).reason


def test_two_trip_drivers_get_session_without_shipment(conn, clock):
    """DRV004 (waiting + booked for 18:00) and DRV007 (two trucks on the road) must pick first."""
    for phone, trips in (("9000010004", {"SHP1004", "SHP1020"}), ("9000010007", {"SHP1007", "SHP1016"})):
        session = _login(conn, clock, phone).session
        assert session.shipment_id is None
        assert {t.shipment_id for t in session.trips} == trips


def test_choose_shipment_accepts_own_trip_only(conn, clock):
    """DRV004 can pick SHP1020, not someone else's SHP1013."""
    sid = _login(conn, clock, "9000010004").session.session_id
    assert choose_shipment(conn, sid, "SHP1013", clock) is None
    assert choose_shipment(conn, sid, "SHP1020", clock).shipment_id == "SHP1020"


def test_assigned_without_booking_does_not_count(conn, clock):
    """DRV003: SHP1018 is assigned with only a no-show booking, so the session ties to SHP1003."""
    assert [t.shipment_id for t in active_trips(conn, "DRV003", clock)] == ["SHP1003"]
    assert _login(conn, clock, "9000010003").session.shipment_id == "SHP1003"


def test_session_expires_ends_and_stops_when_trip_cancelled(conn, clock):
    """get_session gives None after 12 hours, after logout, or once the trip is cancelled."""
    sid = _login(conn, clock, "9000010013").session.session_id
    assert get_session(conn, sid, clock)
    conn.execute("UPDATE shipments SET current_status = 'CANCELLED' WHERE shipment_id = 'SHP1013'")
    conn.execute("UPDATE appointments SET appointment_status = 'CANCELLED' WHERE shipment_id = 'SHP1013'")
    assert get_session(conn, sid, clock) is None

    sid = _login(conn, clock, "9000010009").session.session_id
    end_session(conn, sid, clock)
    assert get_session(conn, sid, clock) is None

    sid = _login(conn, clock, "9000010010").session.session_id
    clock.advance(minutes=12 * 60)
    assert get_session(conn, sid, clock) is None
    assert get_session(conn, "made-up", clock) is None


def test_code_is_never_stored_in_plain_text(conn, clock):
    """Neither the code table nor the events diary contains the code."""
    code = request_code(conn, "9000010013", clock).code
    verify_code(conn, "9000010013", code, clock)
    stored = [str(v) for r in conn.execute("SELECT * FROM login_codes") for v in r]
    stored += [str(v) for r in conn.execute("SELECT * FROM events") for v in r]
    assert not any(code in v for v in stored)
