"""Tests for drift on the timer (dp-22): dock events, no-shows and new arrival times handled once, drivers with an
open chat told. Real data, fake clock, rule reader."""

from datetime import datetime

import pytest

from dockpilot import db
from dockpilot.auth import request_code, verify_code
from dockpilot.clock import IST, FakeClock
from dockpilot.conversation import handle_message
from dockpilot.reader import read_message
from dockpilot.webapp import App


@pytest.fixture
def app(tmp_path):
    """A web app at 09:45 on a fresh working copy, plus a connection to inspect it."""
    a = App(db.init_work_db(tmp_path / "work.db"), FakeClock(), reader=read_message)
    a.conn = db.connect(a.db_path)
    yield a
    a.conn.close()


def _at(app, hh, mm):
    """Move the app's clock to hh:mm (forward only)."""
    app.clock.advance(seconds=(datetime(2026, 8, 4, hh, mm, tzinfo=IST) - app.clock.now()).total_seconds())


def _chat(app, phone, text):
    """Log a driver in and send one message, so they have a chat open."""
    sid = verify_code(app.conn, phone, request_code(app.conn, phone, app.clock).code, app.clock).session.session_id
    return handle_message(app.conn, sid, text, app.clock, reader=read_message)


def _last(app, thread):
    """The last message in a chat."""
    return app.conn.execute("SELECT message_text FROM chat_messages WHERE thread_id = ? ORDER BY rowid DESC LIMIT 1",
                            (thread,)).fetchone()[0]


def _requests(app, shipment):
    """DockPilot's booking requests for a truck."""
    return [tuple(r) for r in app.conn.execute(
        "SELECT appointment_id, appointment_status, replaced_appointment_id FROM appointments "
        "WHERE shipment_id = ? AND appointment_id LIKE 'APT-%'", (shipment,))]


def test_contract_dock_breakdown_rehomes_once(app):
    """First tick: DEVT001 (D3 broken 09:15–13:00) re-homes SHP1005 with a PENDING request; a second tick adds nothing."""
    app.tick()
    (req,) = _requests(app, "SHP1005")
    assert req[1] == "PENDING_CONFIRMATION" and req[2] == "APT1005"  # old booking stays current until confirmed (R1)
    app.tick()
    assert len(_requests(app, "SHP1005")) == 1


def test_new_arrival_time_from_outside_the_chat_reaches_an_open_chat(app):
    """SHP1017's driver has a chat open; at 09:55 an ETA of 12:45 is recorded elsewhere → told once, best slot held."""
    thread = _chat(app, "9000010001", "hello").thread_id
    app.tick()
    _at(app, 9, 56)
    app.tick()
    text = _last(app, thread)
    assert text.startswith("Your new arrival time (12:45) doesn't fit your D1 12:00 booking. You can make")
    app.tick()
    assert _last(app, thread) == text  # handled once
    assert app.conn.execute("SELECT COUNT(*) FROM slot_holds WHERE shipment_id = 'SHP1017' AND hold_status = 'ACTIVE'"
                            ).fetchone()[0] == 1


def test_no_show_tells_the_driver_and_offers_options(app):
    """APT1007 (D1 10:00, SHP1007 on the road) passes its grace at 10:30 → NO_SHOW; DRV007's chat gets options."""
    thread = _chat(app, "9000010007", "what is the status of ORD-260804-007?").thread_id
    app.tick()
    _at(app, 10, 31)
    app.tick()
    assert app.conn.execute("SELECT appointment_status FROM appointments WHERE appointment_id = 'APT1007'"
                            ).fetchone()[0] == "NO_SHOW"
    assert _last(app, thread).startswith("You missed D1 10:00 (no check-in within the grace time). You can make")


def test_nothing_fits_goes_to_a_coordinator(app):
    """16:25: SHP1015's new 18:30 arrival misses D5 17:00, and the reefer dock is in maintenance → hand-off + message."""
    thread = _chat(app, "9000010015", "hello").thread_id
    app.tick()
    _at(app, 16, 26)
    app.tick()
    assert _last(app, thread).startswith("Your new arrival time (18:30) doesn't fit your D5 17:00 booking. "
                                         "No slot fits an arrival at 18:30")
    assert app.conn.execute("SELECT COUNT(*) FROM escalations WHERE shipment_id = 'SHP1015' AND "
                            "reason_code = 'NO_FEASIBLE_SLOT'").fetchone()[0] == 1


def test_no_chat_open_no_message_but_the_work_is_done(app):
    """Nobody from SHP1005 is chatting: no message is posted, but the re-home request still exists."""
    app.tick()
    assert _requests(app, "SHP1005")
    assert app.conn.execute("SELECT COUNT(*) FROM chat_messages WHERE chat_message_id LIKE 'DPM-%'").fetchone()[0] == 0


def test_arrival_times_from_our_own_chat_are_not_acted_on_twice(app):
    """An ETA the agent saved from the chat (ETA_DP-…) doesn't trigger the timer again."""
    thread = _chat(app, "9000010013", "I will reach by 11:15").thread_id
    before = _last(app, thread)
    app.clock.advance(seconds=30)
    app.tick()
    assert _last(app, thread) == before


def test_a_driver_mid_conversation_is_not_interrupted(app):
    """Kailash is choosing a slot when the client records his "late by one hour" as ETA 11:00 (ETA008, 09:20):
    the timer doesn't post new options over his conversation."""
    clock = FakeClock(datetime(2026, 8, 4, 9, 19, tzinfo=IST))
    app.clock, app.since = clock, clock.now()
    thread = _chat(app, "9000010013", "I will reach by 11:15").thread_id
    before = _last(app, thread)
    _at(app, 9, 21)
    app.tick()
    assert _last(app, thread) == before
