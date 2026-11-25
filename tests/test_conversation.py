"""Tests for the conversation agent (dp-14): offline, rule reader, real data."""

import io
import json
from datetime import datetime

import pytest

from dockpilot import db
from dockpilot.auth import end_session, request_code, verify_code
from dockpilot.bookings import check_warehouse_timeouts, confirm_booking, reject_booking
from dockpilot.clock import IST, FakeClock
from dockpilot.conversation import (CLOSE_TEXT, check_silent_drivers, handle_message, notify_booking_result,
                                    notify_warehouse_timeout)
from dockpilot.llm_reader import read_message_ai
from dockpilot.reader import read_message


@pytest.fixture
def conn(tmp_path):
    """A fresh working copy in a temporary folder."""
    c = db.connect(db.init_work_db(tmp_path / "work.db"))
    yield c
    c.close()


def _clock(hh, mm):
    """A fake clock on the snapshot day at hh:mm."""
    return FakeClock(datetime(2026, 8, 4, hh, mm, tzinfo=IST))


def _login(conn, clock, phone):
    """Log a driver in; return the session id."""
    return verify_code(conn, phone, request_code(conn, phone, clock).code, clock).session.session_id


def _say(conn, sid, text, clock, **kw):
    """One driver message with the rule reader."""
    return handle_message(conn, sid, text, clock, reader=read_message, **kw)


def _count(conn, sql, *args):
    """First column of the first row."""
    return conn.execute(sql, args).fetchone()[0]


def _active_holds(conn, shipment):
    """Number of ACTIVE hold rows for a shipment."""
    return _count(conn, "SELECT COUNT(*) FROM slot_holds WHERE shipment_id = ? AND hold_status = 'ACTIVE'", shipment)


def _thr003_to_options(conn):
    """THR003 up to the options: returns (session id, clock at 09:20, reply)."""
    clock = _clock(9, 19)
    sid = _login(conn, clock, "9000010013")
    _say(conn, sid, "I am late by one hour.", clock)
    clock.advance(minutes=1)
    return sid, clock, _say(conn, sid, "around 11:15", clock)


def test_contract_thr003_asks_first_and_holds_nothing_until_answered(conn):
    """Vague delay: one clarifying question, 0 holds; a time then gives options and 1 hold."""
    clock = _clock(9, 19)
    sid = _login(conn, clock, "9000010013")
    reply = _say(conn, sid, "I am late by one hour.", clock)
    assert reply.state == "ETA" and reply.text == (
        "Thanks. Your plan was 10:00. Will you reach around 11:00? Reply with your arrival time, like 11:15.")
    assert _active_holds(conn, "SHP1013") == 0
    clock.advance(minutes=1)
    reply = _say(conn, sid, "around 11:15", clock)
    assert reply.text == ("You can make D2 12:00–13:00 (held for you until 09:30). "
                          "Reply 1 to book it, 2 for D4 12:00, 3 for D1 13:00.")
    assert _active_holds(conn, "SHP1013") == 1
    eta = conn.execute("SELECT declared_eta_ts, confidence_code FROM eta_updates WHERE eta_update_id LIKE 'ETA_DP-%'").fetchone()
    assert tuple(eta) == ("2026-08-04T11:15:00+05:30", "MEDIUM")


def test_contract_thr010_asks_which_shipment_then_carries_on(conn):
    """DRV004 with two trips: asks "004 or 020?" with no hold; "020" picks SHP1020 and asks for the time."""
    clock = _clock(9, 31)
    sid = _login(conn, clock, "9000010004")
    reply = _say(conn, sid, "I will be late by 45 minutes.", clock)
    assert reply.state == "SHIPMENT"
    assert "ORD-260804-004 (now at the warehouse) or ORD-260804-020 (due 18:00)" in reply.text
    assert _count(conn, "SELECT COUNT(*) FROM slot_holds") == 0
    reply = _say(conn, sid, "020", clock)
    assert reply.text.startswith("Thanks. Your plan was 18:00. Will you reach around 18:45?")
    assert _count(conn, "SELECT shipment_id FROM driver_sessions WHERE session_id = ?", sid) == "SHP1020"
    reply = _say(conn, sid, "not sure", clock)
    assert reply.state == "CHOICE" and reply.text.startswith("You can make D1 20:00–21:00")
    assert _count(conn, "SELECT confidence_code FROM eta_updates WHERE eta_update_id LIKE 'ETA_DP-%'") == "LOW"


def test_contract_no_session_no_reply_and_nothing_written(conn):
    """An ended session gives ok=False and writes no chat rows."""
    clock = _clock(9, 19)
    sid = _login(conn, clock, "9000010013")
    end_session(conn, sid, clock)
    before = _count(conn, "SELECT COUNT(*) FROM chat_messages")
    reply = _say(conn, sid, "I am late by one hour.", clock)
    assert not reply.ok and _count(conn, "SELECT COUNT(*) FROM chat_messages") == before


def test_yes_books_and_a_resend_gives_the_same_reply(conn):
    """"1" makes a PENDING booking; the same message_ref again gives the same reply and still one booking."""
    sid, clock, _ = _thr003_to_options(conn)
    first = _say(conn, sid, "1", clock, message_ref="wa-1")
    again = _say(conn, sid, "1", clock, message_ref="wa-1")
    assert first.state == "WAREHOUSE" and first.text.startswith("Great, I'm confirming D2 12:00 with the warehouse now.")
    assert again.text == first.text
    assert _count(conn, "SELECT COUNT(*) FROM appointments WHERE shipment_id = 'SHP1013' "
                        "AND appointment_status = 'PENDING_CONFIRMATION'") == 2  # client's APT1013A stays (R1) + ours
    assert _count(conn, "SELECT COUNT(*) FROM appointments WHERE appointment_id LIKE 'APT-%'") == 1


def test_picking_option_two_holds_that_one(conn):
    """"2" moves the hold to D4 12:00; still one active hold."""
    sid, clock, _ = _thr003_to_options(conn)
    reply = _say(conn, sid, "2", clock)
    assert reply.text.startswith("You can make D4 12:00–13:00") and "Reply 2 to book it" in reply.text
    assert _active_holds(conn, "SHP1013") == 1


def test_no_lets_the_hold_run_out(conn):
    """"no" books nothing and says when the hold ends."""
    sid, clock, _ = _thr003_to_options(conn)
    reply = _say(conn, sid, "no", clock)
    assert reply.text.startswith("OK, I won't book it. The hold ends at 09:30.")
    assert _count(conn, "SELECT COUNT(*) FROM appointments WHERE appointment_id LIKE 'APT-%'") == 0


def test_expired_hold_then_yes_gives_reason_and_fresh_options(conn):
    """A "yes" after the hold ran out explains why and offers new options."""
    sid, clock, _ = _thr003_to_options(conn)
    clock.advance(minutes=11)
    reply = _say(conn, sid, "yes", clock)
    assert reply.text.startswith("The hold ran out at 09:30. Here are fresh options. You can make")
    assert reply.state == "CHOICE"


def test_no_feasible_slot_names_the_reason_and_escalates(conn):
    """With every slot closed, the driver hears why and an escalation exists."""
    conn.execute("UPDATE appointment_slots SET slot_status = 'BLOCKED'")
    clock = _clock(9, 20)
    sid = _login(conn, clock, "9000010013")
    reply = _say(conn, sid, "I will reach by 11:15", clock)
    assert "No slot fits an arrival at 11:15: slot blocked" in reply.text and "coordinator" in reply.text
    assert reply.escalation_id
    assert _count(conn, "SELECT thread_status FROM chat_threads WHERE thread_id = ?", reply.thread_id) == "ESCALATED"


def test_warehouse_confirm_and_reject_appear_in_the_chat(conn):
    """Confirm posts "Confirmed: D2 at 12:00."; a rejection posts the reason and fresh options."""
    sid, clock, _ = _thr003_to_options(conn)
    appt = _say(conn, sid, "1", clock).appointment_id
    confirm_booking(conn, appt, clock, ref="WH-77")
    reply = notify_booking_result(conn, appt, clock)
    assert reply.text == "Confirmed: D2 at 12:00, ref WH-77."
    last = conn.execute("SELECT message_text FROM chat_messages WHERE thread_id = ? ORDER BY rowid DESC LIMIT 1",
                        (reply.thread_id,)).fetchone()[0]
    assert last == reply.text

    clock = _clock(9, 31)
    sid = _login(conn, clock, "9000010014")
    _say(conn, sid, "I will reach by 11:25", clock)
    appt = _say(conn, sid, "1", clock).appointment_id
    reject_booking(conn, appt, clock, "Dock crew short")
    reply = notify_booking_result(conn, appt, clock)
    assert reply.text.startswith("The warehouse couldn't take D4 at 12:00: Dock crew short. You can make")


def test_silent_on_options_nudges_then_closes_without_escalation(conn):
    """Options at 09:20, no reply: 09:25 one nudge, 09:30 chat CLOSED and hold expired, no escalation."""
    sid, clock, reply = _thr003_to_options(conn)
    clock.advance(minutes=4)
    assert check_silent_drivers(conn, clock) == []
    clock.advance(minutes=1)
    assert [d["action"] for d in check_silent_drivers(conn, clock)] == ["NUDGED"]
    assert [d["action"] for d in check_silent_drivers(conn, clock)] == []  # nudged once only
    nudge = conn.execute("SELECT message_text FROM chat_messages WHERE parsed_intent = 'NUDGE'").fetchone()[0]
    assert nudge == "Are you still there? D2 12:00 is held for you until 09:30. Reply 1 to book it."
    clock.advance(minutes=5)
    done = check_silent_drivers(conn, clock)
    assert done == [{"thread_id": reply.thread_id, "action": "CLOSED", "escalation_id": None}]
    assert _count(conn, "SELECT thread_status FROM chat_threads WHERE thread_id = ?", reply.thread_id) == "CLOSED"
    assert _active_holds(conn, "SHP1013") == 0
    assert _count(conn, "SELECT COUNT(*) FROM escalations") == 0
    assert _count(conn, "SELECT message_text FROM chat_messages WHERE parsed_intent = 'CLOSED'") == CLOSE_TEXT


def test_silent_on_time_question_closes_with_eta_unresolved(conn):
    """Asked at 09:19, no reply: nudge at 09:24, close at 09:29 with one ETA_UNRESOLVED escalation."""
    clock = _clock(9, 19)
    sid = _login(conn, clock, "9000010013")
    _say(conn, sid, "I am late by one hour.", clock)
    clock.advance(minutes=5)
    check_silent_drivers(conn, clock)
    assert conn.execute("SELECT message_text FROM chat_messages WHERE parsed_intent = 'NUDGE'").fetchone()[0] == (
        "Still there? Please reply with your arrival time, like 11:15.")
    clock.advance(minutes=5)
    done = check_silent_drivers(conn, clock)
    assert done[0]["action"] == "CLOSED" and done[0]["escalation_id"]
    assert _count(conn, "SELECT COUNT(*) FROM escalations WHERE reason_code = 'ETA_UNRESOLVED'") == 1


def test_reply_after_nudge_keeps_chat_open_and_message_after_close_opens_new_thread(conn):
    """Answering after the nudge restarts the flow; after a close, the next message starts a new thread."""
    clock = _clock(9, 19)
    sid = _login(conn, clock, "9000010013")
    first = _say(conn, sid, "I am late by one hour.", clock)
    clock.advance(minutes=5)
    check_silent_drivers(conn, clock)
    reply = _say(conn, sid, "around 11:15", clock)
    assert reply.state == "CHOICE"
    clock.advance(minutes=4)
    assert check_silent_drivers(conn, clock) == []  # timer restarted by the reply
    clock.advance(minutes=1)
    check_silent_drivers(conn, clock)  # nudge
    clock.advance(minutes=5)
    check_silent_drivers(conn, clock)  # close
    again = _say(conn, sid, "what is my status?", clock)
    assert again.thread_id != first.thread_id and again.text.startswith("Your booking:")


def test_safety_words_escalate_now(conn):
    """An accident goes straight to a coordinator."""
    clock = _clock(9, 20)
    sid = _login(conn, clock, "9000010013")
    reply = _say(conn, sid, "There was an accident on the highway", clock)
    assert reply.text.startswith("I've alerted the coordinator now")
    assert _count(conn, "SELECT reason_code FROM escalations WHERE escalation_id = ?", reply.escalation_id) == "SAFETY"


def test_ai_reading_of_a_clock_time_is_not_forced_to_ask_first():
    """dp-13 open point: "half past eleven" read by the AI stands; "late by one hour" still asks first."""
    at = datetime(2026, 8, 4, 9, 30, tzinfo=IST)
    answer = lambda eta, conf: lambda _m: json.dumps(
        {"intent": "REPORT_DELAY", "eta": eta, "eta_confidence": conf, "delay_reason": "TRAFFIC"})
    clear = read_message_ai("Stuck in traffic, should make it by half past eleven", at, call=answer("11:30", "HIGH"))
    assert clear.eta.hour == 11 and not clear.needs_clarification
    vague = read_message_ai("I am late by one hour.", at, call=answer("10:30", "MEDIUM"))
    assert vague.needs_clarification


def test_users_live_test_own_booking_still_works_no_crash(conn):
    """dp-19, the live test of 2026-11-25: "10:50" fits Kailash's existing D2 11:00 booking, so the agent says so;
    "1" afterwards gets "still checking", nothing is booked twice, and the warehouse's answer reaches the chat."""
    clock = FakeClock(datetime(2026, 8, 4, 9, 45, 42, tzinfo=IST))
    sid = _login(conn, clock, "9000010013")
    assert _say(conn, sid, "Hi, I am getting late by 30 mins", clock).state == "ETA"
    reply = _say(conn, sid, "10:50", clock)
    assert reply.text == ("Good news: your booking D2 11:00–12:00 still works for 10:50. It's waiting for the "
                          "warehouse to confirm; I'll tell you as soon as they do.")
    for _ in range(2):
        assert _say(conn, sid, "1", clock).text == ("I'm still checking D2 11:00 with the warehouse. "
                                                    "I'll get back to you as soon as they answer.")
    assert _count(conn, "SELECT COUNT(*) FROM appointments WHERE appointment_id LIKE 'APT-%'") == 0
    confirm_booking(conn, "APT1013A", clock)
    assert notify_booking_result(conn, "APT1013A", clock).text == "Confirmed: D2 at 11:00."


def test_own_booked_slot_is_never_listed_as_a_new_option(conn):
    """Arriving 09:55, D1/D2 10:00 come first; Kailash's own D2 11:00 is not offered as a new choice."""
    clock = _clock(9, 45)
    sid = _login(conn, clock, "9000010013")
    reply = _say(conn, sid, "I will reach by 9:55", clock)
    assert reply.state == "CHOICE" and all("SLOT-JAI-018" not in o["slots"] for o in reply.options)


def test_waiting_for_warehouse_then_timeout_tells_driver_once(conn):
    """After "1": "hello?" gets "still checking"; 30 min later one coordinator hand-off and one driver message."""
    sid, clock, _ = _thr003_to_options(conn)
    _say(conn, sid, "1", clock)
    assert _say(conn, sid, "hello?", clock).text == ("I'm still checking D2 12:00 with the warehouse. "
                                                    "I'll get back to you as soon as they answer.")
    clock.advance(minutes=30)
    told = notify_warehouse_timeout(conn, check_warehouse_timeouts(conn, clock, out=io.StringIO()), clock)
    assert [r.text for r in told] == [
        "The warehouse hasn't answered about D2 12:00 yet, so I've passed your request to a coordinator with all "
        "the details. Someone will be in touch soon. Keep heading to the warehouse; your request is still in place."]
    clock.advance(seconds=5)
    assert notify_warehouse_timeout(conn, check_warehouse_timeouts(conn, clock, out=io.StringIO()), clock) == []
    assert _say(conn, sid, "any news?", clock).text.startswith(
        "I'm still checking D2 12:00 with the warehouse; a coordinator is on it.")


def test_b1_latest_message_is_answered_after_which_trip(conn):
    """dp-16 B1: DRV004 asks about the yard, then reports a delay; after "020" the delay is handled."""
    clock = _clock(9, 31)
    sid = _login(conn, clock, "9000010004")
    _say(conn, sid, "Has a new dock been assigned?", clock)
    _say(conn, sid, "I will be late by 45 minutes.", clock)
    assert _say(conn, sid, "020", clock).text.startswith("Thanks. Your plan was 18:00. Will you reach around 18:45?")


def test_b2_same_report_resent_gets_the_same_reply_and_nothing_new(conn):
    """dp-16 B2 (THR001 / THR009): the same text 62 s later with a new id is a resend: same reply, one hold."""
    clock = _clock(9, 34)
    sid = _login(conn, clock, "9000010006")
    text = "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?"
    first = handle_message(conn, sid, text, clock, reader=read_message, message_ref="wa-9001")
    clock.advance(seconds=62)
    again = handle_message(conn, sid, text, clock, reader=read_message, message_ref="wa-9009")
    assert again.text == first.text
    assert _count(conn, "SELECT COUNT(*) FROM slot_holds WHERE shipment_id = 'SHP1006'") == 1
    assert _count(conn, "SELECT is_duplicate FROM chat_messages WHERE external_message_id = 'wa-9009'") == 1


def test_b3_in_the_yard_picks_the_trip_at_the_warehouse(conn):
    """dp-16 B3 (THR008): "I'm in the yard" means SHP1004 (waiting at the warehouse), no "which trip?"."""
    clock = _clock(9, 27)
    sid = _login(conn, clock, "9000010004")
    reply = _say(conn, sid, "I reached late and am in the yard. Has a new dock been assigned?", clock)
    assert reply.text.startswith("Your booking: D2 at 09:00")
