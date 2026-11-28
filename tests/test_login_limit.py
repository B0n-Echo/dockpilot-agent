"""Tests for the login limit (dp-26): 3 codes per 15 minutes per driver, then a 15-minute lockout."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from dockpilot import cognito, db
from dockpilot.auth import request_code, verify_code
from dockpilot.clock import FakeClock
from dockpilot.reader import read_message
from dockpilot.webapp import App, make_server

KAILASH, VIKRAM, ASHOK = "9000010013", "9000010007", "9000010008"


@pytest.fixture
def conn(tmp_path):
    """A fresh working copy in a temporary folder."""
    c = db.connect(db.init_work_db(tmp_path / "work.db"))
    yield c
    c.close()


def _events(conn, kind):
    """How many diary lines of this kind there are."""
    return conn.execute("SELECT COUNT(*) FROM events WHERE event_type = ?", (kind,)).fetchone()[0]


def test_contract_fourth_code_locks_for_15_minutes(conn):
    """Three codes are sent; the 4th within 15 minutes starts a 15-minute lockout; after it, codes flow again."""
    clock = FakeClock()
    for _ in range(3):
        assert request_code(conn, KAILASH, clock).ok
        clock.advance(minutes=1)
    fourth = request_code(conn, KAILASH, clock)
    assert not fourth.ok and fourth.reason == "Too many codes asked for. Please try again in 15 minutes."
    assert fourth.code is None and _events(conn, "LOGIN_LOCKED") == 1
    clock.advance(minutes=15)
    assert request_code(conn, KAILASH, clock).ok


def test_pressing_again_while_locked_does_not_extend_it(conn):
    """A refusal during the lockout counts down to the same end time and starts no new lockout."""
    clock = FakeClock()
    for _ in range(4):
        request_code(conn, KAILASH, clock)
    clock.advance(minutes=5)
    assert request_code(conn, KAILASH, clock).reason == "Too many codes asked for. Please try again in 10 minutes."
    clock.advance(minutes=9, seconds=30)
    assert request_code(conn, KAILASH, clock).reason == "Too many codes asked for. Please try again in 1 minute."
    clock.advance(seconds=30)
    assert request_code(conn, KAILASH, clock).ok and _events(conn, "LOGIN_LOCKED") == 1


def test_codes_spread_out_never_lock(conn):
    """One code every 6 minutes is never more than 3 in any 15 minutes."""
    clock = FakeClock()
    for _ in range(6):
        assert request_code(conn, KAILASH, clock).ok
        clock.advance(minutes=6)


def test_other_drivers_are_not_affected(conn):
    """Kailash's lockout doesn't touch Vikram."""
    clock = FakeClock()
    for _ in range(4):
        request_code(conn, KAILASH, clock)
    assert request_code(conn, VIKRAM, clock).ok


def test_refused_numbers_are_never_counted_or_locked(conn):
    """No trip (Ashok) or an unknown number: refused every time, never locked."""
    clock = FakeClock()
    for phone in (ASHOK, "+91-9999999999"):
        for _ in range(5):
            assert not request_code(conn, phone, clock).ok
    assert _events(conn, "LOGIN_LOCKED") == 0


def test_code_in_hand_still_works_during_lockout(conn):
    """The lockout stops new codes, not the driver who already has one."""
    clock = FakeClock()
    for _ in range(3):
        code = request_code(conn, KAILASH, clock).code
    assert not request_code(conn, KAILASH, clock).ok
    assert verify_code(conn, KAILASH, code, clock).ok


def test_cognito_is_never_called_while_locked(conn, monkeypatch):
    """In Cognito mode a locked driver gets no email."""
    calls = []
    monkeypatch.setattr(cognito, "enabled", lambda: True)
    monkeypatch.setattr(cognito, "start_sign_in", lambda user: calls.append(user) or ("sess", "y***@g***"))
    clock = FakeClock()
    for _ in range(5):
        request_code(conn, KAILASH, clock)
    assert calls == ["DRV013"] * 3


def test_web_page_gets_the_lockout_message(tmp_path):
    """The 4th "Send code" gets 403 with the lockout message the page shows."""
    app = App(db.init_work_db(tmp_path / "w.db"), FakeClock(), reader=read_message)
    httpd = make_server(app, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/api/login/start"
    send = lambda: urllib.request.urlopen(urllib.request.Request(  # noqa: E731
        url, data=json.dumps({"phone": KAILASH}).encode(), method="POST", headers={"Content-Type": "application/json"}))
    try:
        for _ in range(3):
            assert send().status == 200
        with pytest.raises(urllib.error.HTTPError) as err:
            send()
        assert err.value.code == 403
        assert json.loads(err.value.read()) == {"error": "Too many codes asked for. Please try again in 15 minutes."}
    finally:
        httpd.shutdown()
        httpd.server_close()
