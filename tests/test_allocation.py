"""Tests for the allocation policy (dp-10, D7): offline, rule reader, real data."""

from dataclasses import replace
from datetime import datetime

import pytest

from dockpilot import db
from dockpilot.allocation import is_spike, request_slot, settle_requests
from dockpilot.auth import request_code, verify_code
from dockpilot.bookings import confirm_booking, convert_hold
from dockpilot.clock import IST, FakeClock
from dockpilot.conversation import handle_message, notify_allocation
from dockpilot.reader import read_message
from dockpilot.settings import SETTINGS

RUSH = replace(SETTINGS, spike_request_count=1)
"""Settings where every request counts as a rush, to test the collection window."""


@pytest.fixture
def conn(tmp_path):
    """A fresh working copy in a temporary folder."""
    c = db.connect(db.init_work_db(tmp_path / "work.db"))
    yield c
    c.close()


def _at(hh, mm, ss=0):
    """A fake clock on the snapshot day."""
    return FakeClock(datetime(2026, 8, 4, hh, mm, ss, tzinfo=IST))


class Drivers:
    """Logged-in drivers chatting with the rule reader."""

    def __init__(self, conn, settings=SETTINGS):
        """Remember the database and settings."""
        self.conn, self.settings, self.sids = conn, settings, {}

    def say(self, phone, text, clock):
        """Log in if needed, send one message, return the Reply."""
        if phone not in self.sids:
            code = request_code(self.conn, phone, clock).code
            self.sids[phone] = verify_code(self.conn, phone, code, clock).session.session_id
        return handle_message(self.conn, self.sids[phone], text, clock, self.settings, reader=read_message)


def _escalations(conn, reason):
    """Shipment ids with an escalation of this reason."""
    return [r[0] for r in conn.execute("SELECT shipment_id FROM escalations WHERE reason_code = ?", (reason,))]


def test_contract_four_truck_replay_matches_d7(conn):
    """The client's four competing trucks, own messages and times: D7 outcomes plus the CRITICAL alert."""
    d = Drivers(conn)
    assert d.say("9000010013", "I am late by one hour.", _at(9, 19)).state == "ETA"  # asks first, no hold
    assert d.say("9000010012", "Tyre repaired. I can reach 11:10. I need to leave the warehouse before 1:30 "
                               "for another pickup.", _at(9, 29)).text.startswith("You can make D2 12:00–13:00")
    d.say("9000010012", "1", _at(9, 29))
    assert d.say("9000010006", "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?",
                 _at(9, 34)).text.startswith("You can make D4 12:00–13:00")
    d.say("9000010006", "1", _at(9, 34))
    reply = d.say("9000010014", "Origin released me late. ETA 11:25. This load is urgent. Can I get the first "
                                "possible dock?", _at(9, 44))
    assert reply.text.startswith("You can make D2 13:00–15:00")
    assert reply.text.endswith("I've also asked a coordinator whether an earlier slot can be freed for you.")
    assert _escalations(conn, "CRITICAL_WAITING") == ["SHP1014"]
    summary = conn.execute("SELECT summary FROM escalations WHERE reason_code = 'CRITICAL_WAITING'").fetchone()[0]
    assert "D2 12:00–14:00 (SHP1012 NORMAL, pending)" in summary and "D4 12:00–14:00 (SHP1006 HIGH, pending)" in summary
    d.say("9000010014", "1", _at(9, 44))
    assert d.say("9000010013", "not sure", _at(9, 45)).text.startswith("You can make D1 13:00–14:00")
    statuses = [r[0] for r in conn.execute("SELECT status FROM slot_requests ORDER BY requested_at")]
    assert set(statuses) == {"HELD"}  # requests minutes apart: no spike, first come first served


def test_normal_day_first_request_is_held_at_once(conn):
    """No rush: request_slot holds straight away and records HELD."""
    clock = _at(9, 45)
    assert not is_spike(conn, "FAC-JAI-01", clock)
    res = request_slot(conn, "SHP1012", ["SLOT-JAI-019"], "k1", clock)
    assert res.status == "HELD" and res.hold.slot_ids == ["SLOT-JAI-019"]
    assert request_slot(conn, "SHP1012", ["SLOT-JAI-019"], "k1", clock).request_id == res.request_id  # same key


def test_spike_higher_priority_wins_and_loser_hears_why_with_new_options(conn):
    """Rush: NORMAL SHP1012 asks for D2 12:00, HIGH SHP1006 asks 10 s later; after 30 s HIGH wins."""
    d = Drivers(conn, RUSH)
    first = d.say("9000010012", "I can reach 11:10", _at(9, 45))
    assert first.text.startswith("It's busy right now: I'm checking who gets D2 12:00")
    assert d.say("9000010006", "Reaching around 11:20", _at(9, 45, 10)).text.startswith(
        "It's busy right now: I'm checking who gets D2 12:00")
    assert d.say("9000010012", "1", _at(9, 45, 20)).text == "Still deciding between the trucks that asked, one moment."
    assert settle_requests(conn, _at(9, 45, 20), RUSH) == []  # window not closed yet
    clock = _at(9, 45, 30)
    decisions = settle_requests(conn, clock, RUSH)
    assert {x.shipment_id: x.status for x in decisions} == {"SHP1006": "WON", "SHP1012": "LOST"}
    replies = {r.thread_id: r.text for r in notify_allocation(conn, decisions, clock, RUSH)}
    texts = sorted(replies.values())
    assert texts[0].startswith("D2 12:00–13:00 went to a higher-priority load. It's busy right now: "
                               "I'm checking who gets D4 12:00")
    assert texts[1].startswith("You can make D2 12:00–13:00 (held for you until 09:50)")  # 5-min spike hold
    later = _at(9, 46)
    assert [x.status for x in settle_requests(conn, later, RUSH)] == ["WON"]  # loser gets D4 next


def test_clear_harm_difference_beats_asking_first(conn):
    """Same priority: SHP1012 (85 min late) beats SHP1013 (60 min late) even though SHP1013 asked first."""
    request_slot(conn, "SHP1013", ["SLOT-JAI-019"], "a", _at(9, 45), RUSH)
    request_slot(conn, "SHP1012", ["SLOT-JAI-019"], "b", _at(9, 45, 5), RUSH)
    decisions = {x.shipment_id: x for x in settle_requests(conn, _at(9, 45, 30), RUSH)}
    assert decisions["SHP1012"].status == "WON"
    assert decisions["SHP1013"].reason == "D2 12:00–13:00 went to a truck that is already later than you."
    assert _escalations(conn, "NEAR_TIE") == []


def test_near_tie_first_asked_keeps_it_and_coordinator_is_told(conn):
    """SHP1013 says 11:20 (80 min late) vs SHP1012 (85): within 15 min, so first asked wins plus NEAR_TIE."""
    d = Drivers(conn, RUSH)
    d.say("9000010013", "I will reach by 11:20", _at(9, 45))
    d.say("9000010012", "I can reach 11:10", _at(9, 45, 5))
    clock = _at(9, 45, 30)
    decisions = {x.shipment_id: x for x in settle_requests(conn, clock, RUSH)}
    assert decisions["SHP1013"].status == "NEAR_TIE" and decisions["SHP1012"].status == "LOST"
    assert decisions["SHP1012"].reason == "D2 12:00–13:00 went to a truck that asked a moment before you."
    assert _escalations(conn, "NEAR_TIE") == ["SHP1013"]
    replies = notify_allocation(conn, list(decisions.values()), clock, RUSH)
    assert any(r.text.endswith("a coordinator may still change this.") for r in replies)


def test_confirmed_booking_is_locked_so_no_critical_alert(conn):
    """If the earlier slots are CONFIRMED for lower priority (R1), the CRITICAL load gets no alert."""
    clock = _at(9, 44)
    for shp, slot in (("SHP1012", "SLOT-JAI-019"), ("SHP1006", "SLOT-JAI-047")):
        res = request_slot(conn, shp, [slot], f"k-{shp}", clock)
        confirm_booking(conn, convert_hold(conn, res.hold.group_id, clock).appointment_id, clock)
    res = request_slot(conn, "SHP1014", ["SLOT-JAI-020", "SLOT-JAI-021"], "k-14", clock)
    assert res.status == "HELD" and res.alert is None
    assert _escalations(conn, "CRITICAL_WAITING") == []


def test_settling_twice_changes_nothing(conn):
    """A second settle finds nothing waiting: no extra hold, no extra escalation."""
    request_slot(conn, "SHP1013", ["SLOT-JAI-019"], "a", _at(9, 45), RUSH)
    request_slot(conn, "SHP1012", ["SLOT-JAI-019"], "b", _at(9, 45, 5), RUSH)
    assert len(settle_requests(conn, _at(9, 45, 30), RUSH)) == 2
    assert settle_requests(conn, _at(9, 45, 31), RUSH) == []
    assert conn.execute("SELECT COUNT(*) FROM slot_holds WHERE hold_status = 'ACTIVE'").fetchone()[0] == 1
