"""Tests for placing and expiring holds (D5, D6), against a fresh working copy at 09:45."""

from datetime import timedelta

import pytest

from dockpilot import db
from dockpilot.clock import FakeClock
from dockpilot.holds import expire_holds, get_hold, place_hold
from dockpilot.options import find_options

D2_PAIR = ["SLOT-JAI-019", "SLOT-JAI-020"]
"""Dock D2, 12:00-14:00: SHP1014's first option."""

D4_PAIR = ["SLOT-JAI-047", "SLOT-JAI-048"]
"""Dock D4, 12:00-14:00: SHP1014's second option."""


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


def _count(conn, sql, *args):
    """Return the single number a COUNT query gives."""
    return conn.execute(sql, args).fetchone()[0]


def test_contract_second_hold_on_a_held_slot_is_refused(conn, clock):
    """Contract: with SHP1014 holding SLOT-JAI-019, SHP1012's hold on it is refused."""
    assert place_hold(conn, "SHP1014", D2_PAIR, "req-1014", clock).placed
    second = place_hold(conn, "SHP1012", ["SLOT-JAI-019"], "req-1012", clock)
    assert not second.placed
    assert "SLOT_HELD" in {r.code for r in second.reasons}
    assert _count(conn, "SELECT COUNT(*) FROM slot_holds WHERE slot_id = 'SLOT-JAI-019' AND hold_status = 'ACTIVE'") == 1


def test_contract_repeated_request_key_returns_the_same_hold(conn, clock):
    """Contract: the same request key gives back the same hold and writes nothing new."""
    first = place_hold(conn, "SHP1014", D2_PAIR, "req-1014", clock)
    again = place_hold(conn, "SHP1014", D2_PAIR, "req-1014", clock)
    assert again.placed and again.replayed
    assert again.hold.group_id == first.hold.group_id
    assert _count(conn, "SELECT COUNT(*) FROM slot_holds") == 2
    assert _count(conn, "SELECT COUNT(*) FROM events WHERE event_type = 'HOLD_PLACED'") == 2  # one per slot row


def test_contract_hold_expires_after_11_minutes_and_slot_is_offered_again(conn, clock):
    """Contract: 11 minutes later the hold is EXPIRED and SLOT-JAI-019 is offered again."""
    hold = place_hold(conn, "SHP1014", D2_PAIR, "req-1014", clock).hold
    assert "SLOT-JAI-019" not in {s for o in find_options(conn, "SHP1012", clock).options for s in o.slot_ids}
    clock.advance(minutes=11)
    assert expire_holds(conn, clock) == 2
    assert get_hold(conn, hold.group_id).status == "EXPIRED"
    assert _count(conn, "SELECT COUNT(*) FROM events WHERE event_type = 'HOLD_EXPIRED' AND actor = 'TIMER'") == 2
    assert "SLOT-JAI-019" in {s for o in find_options(conn, "SHP1012", clock).options for s in o.slot_ids}


def test_normal_hold_lasts_10_minutes_spike_hold_5(conn, clock):
    """A normal hold expires 10 minutes after creation; a spike-mode hold after 5."""
    normal = place_hold(conn, "SHP1014", D2_PAIR, "req-a", clock).hold
    assert normal.expires_at - normal.created_at == timedelta(minutes=10)
    spike = place_hold(conn, "SHP1012", ["SLOT-JAI-047"], "req-b", clock, spike=True).hold
    assert spike.expires_at - spike.created_at == timedelta(minutes=5)


def test_infeasible_slot_is_refused_and_nothing_written(conn, clock):
    """SHP1014 on SLOT-JAI-004 is refused with dp-04's three reasons; no rows are written."""
    result = place_hold(conn, "SHP1014", ["SLOT-JAI-004"], "req-bad", clock)
    assert not result.placed
    assert {r.code for r in result.reasons} == {"ARRIVES_AFTER_START", "OVER_WEIGHT_LIMIT", "UNLOAD_TOO_LONG"}
    assert _count(conn, "SELECT COUNT(*) FROM slot_holds") == 0
    assert _count(conn, "SELECT COUNT(*) FROM events") == 0


def test_new_hold_releases_the_trucks_earlier_hold(conn, clock):
    """SHP1014 switching from D2 to D4 releases the D2 hold."""
    d2 = place_hold(conn, "SHP1014", D2_PAIR, "req-d2", clock).hold
    d4 = place_hold(conn, "SHP1014", D4_PAIR, "req-d4", clock).hold
    assert get_hold(conn, d2.group_id).status == "EXPIRED"
    assert get_hold(conn, d4.group_id).status == "ACTIVE"
    assert _count(conn, "SELECT COUNT(*) FROM events WHERE event_type = 'HOLD_RELEASED'") == 2


def test_request_key_reused_for_different_slots_raises(conn, clock):
    """The same key with different slots is a caller bug, not a new request."""
    place_hold(conn, "SHP1014", D2_PAIR, "req-1014", clock)
    with pytest.raises(ValueError):
        place_hold(conn, "SHP1014", D4_PAIR, "req-1014", clock)


def test_overdue_hold_is_tidied_inside_place_hold(conn, clock):
    """After expiry time, a new driver can hold the slot at once; the old hold becomes EXPIRED."""
    old = place_hold(conn, "SHP1014", D2_PAIR, "req-1014", clock).hold
    clock.advance(minutes=11)
    assert place_hold(conn, "SHP1012", ["SLOT-JAI-019"], "req-1012", clock).placed
    assert get_hold(conn, old.group_id).status == "EXPIRED"
