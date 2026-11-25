"""Tests for the web demo (dp-15): a real server on a free port, the rule reader and a fake clock."""

import http.cookiejar
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime

import pytest

from dockpilot import db, webapp
from dockpilot.clock import IST, SNAPSHOT_TIME, DemoClock, FakeClock
from dockpilot.reader import read_message
from dockpilot.webapp import App, make_server


@pytest.fixture
def server(tmp_path):
    """Start the demo server at 09:19 on a fresh working copy; yield (base url, app)."""
    app = App(db.init_work_db(tmp_path / "work.db"), FakeClock(datetime(2026, 8, 4, 9, 19, tzinfo=IST)),
              reader=read_message)
    httpd = make_server(app, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", app
    httpd.shutdown()
    httpd.server_close()


class Browser:
    """A tiny client that keeps cookies, like one browser window."""

    def __init__(self, base):
        """Remember the server address and start an empty cookie jar."""
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None, raw=None):
        """Send one request; returns (status, json)."""
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def login(self, phone):
        """Request a code and enter it; returns the verify answer."""
        code = self.call("POST", "/api/login/start", {"phone": phone})[1]["demo_sms"]
        return self.call("POST", "/api/login/verify", {"phone": phone, "code": code})

    def say(self, text):
        """Send a chat message; returns the agent's reply text."""
        return self.call("POST", "/api/chat", {"text": text})[1]["reply"]

    def messages(self):
        """Texts of the chat so far."""
        return [m["text"] for m in self.call("GET", "/api/chat")[1]["messages"]]


def _to_pending(base):
    """DRV013 logs in, gives a time, books option 1; returns (driver browser, warehouse answer)."""
    driver = Browser(base)
    driver.login("9000010013")
    driver.say("I am late by one hour.")
    driver.say("around 11:15")
    driver.say("1")
    return driver, Browser(base).call("GET", "/api/warehouse")[1]


def test_pages_are_served(server):
    """/ is the chat page and /warehouse the warehouse page."""
    base, _ = server
    for path, title in (("/", "Driver chat"), ("/warehouse", "Warehouse")):
        with urllib.request.urlopen(base + path) as resp:
            assert resp.status == 200 and title in resp.read().decode()


def test_chat_without_login_is_401(server):
    """No session cookie: the agent does nothing."""
    base, _ = server
    assert Browser(base).call("POST", "/api/chat", {"text": "hello"})[0] == 401
    assert Browser(base).call("GET", "/api/chat")[0] == 401


def test_contract_login_chat_confirm_driver_sees_result(server):
    """DRV013 logs in, chats, the warehouse confirms, and the driver's chat shows "Confirmed"."""
    base, _ = server
    driver = Browser(base)
    status, hello = driver.login("+91 90000 10013")
    assert status == 200 and hello["greeting"] == "Hi Kailash. Trip ORD-260804-013 (SHP1013)."
    assert driver.say("I am late by one hour.").startswith("Thanks. Your plan was 10:00. Will you reach around 11:00?")
    assert driver.say("around 11:15").startswith("You can make D2 12:00–13:00")
    assert driver.say("1").startswith("Great, I'm confirming D2 12:00 with the warehouse now.")
    warehouse = Browser(base)
    mine = [p for p in warehouse.call("GET", "/api/warehouse")[1]["pending"] if p["shipment_id"] == "SHP1013"
            and p["slot"].startswith("D2 12:00")]
    assert len(mine) == 1 and mine[0]["driver"] == "Kailash Gurjar"
    assert warehouse.call("POST", "/api/warehouse/confirm", {"appointment_id": mine[0]["appointment_id"]})[1] == \
        {"told_driver": True}
    assert driver.messages()[-1].startswith("Confirmed: D2 at 12:00, ref WH-")


def test_reject_gives_the_reason_and_new_options(server):
    """A rejection reaches the driver with the reason and fresh options."""
    base, _ = server
    driver, wh = _to_pending(base)
    appt = next(p["appointment_id"] for p in wh["pending"] if p["slot"].startswith("D2 12:00"))
    Browser(base).call("POST", "/api/warehouse/reject", {"appointment_id": appt, "reason": "Dock crew short"})
    assert driver.messages()[-1].startswith("The warehouse couldn't take D2 at 12:00: Dock crew short. You can make")


def test_clock_button_nudges_then_closes_a_silent_chat(server):
    """Options, then +5 min: the nudge appears; +5 more: the chat is closed."""
    base, _ = server
    driver = Browser(base)
    driver.login("9000010013")
    driver.say("I am late by one hour.")
    driver.say("around 11:15")
    wh = Browser(base)
    assert wh.call("POST", "/api/clock/advance", {"minutes": 5})[1]["clock"].startswith("2026-08-04T09:24")
    assert driver.messages()[-1].startswith("Are you still there? D2 12:00 is held for you")
    wh.call("POST", "/api/clock/advance", {"minutes": 5})
    assert driver.messages()[-1].startswith("I haven't heard back, so I've closed this chat.")


def test_refused_driver_bad_input_and_big_body(server):
    """DRV008 is refused with the reason; bad JSON is 400; a body over 10 KB is 413."""
    base, _ = server
    b = Browser(base)
    status, data = b.call("POST", "/api/login/start", {"phone": "9000010008"})
    assert status == 403 and data["error"].startswith("No trip in progress or booked slot found")
    assert b.call("POST", "/api/login/start", raw=b"not json")[0] == 400
    assert b.call("POST", "/api/chat", raw=b"x" * (11 * 1024))[0] == 413
    assert b.call("POST", "/api/clock/advance", {"minutes": 500})[0] == 400


def test_logout_stops_the_chat(server):
    """After logout the same browser gets 401."""
    base, _ = server
    b = Browser(base)
    b.login("9000010013")
    assert b.call("GET", "/api/chat")[0] == 200
    b.call("POST", "/api/logout", {})
    assert b.call("GET", "/api/chat")[0] == 401


def test_demo_clock_starts_at_snapshot_and_jumps():
    """DemoClock starts at 09:45, runs on, and +10 min jumps ahead; backwards is refused."""
    clock = DemoClock()
    assert SNAPSHOT_TIME <= clock.now() < SNAPSHOT_TIME.replace(second=5)
    assert clock.advance(minutes=10) >= SNAPSHOT_TIME.replace(minute=55)
    with pytest.raises(ValueError):
        clock.advance(minutes=-1)


def test_server_error_gives_the_driver_a_plain_message(server, monkeypatch):
    """A crash inside the agent answers 500 with a sentence for the driver, never silence or a stack trace."""
    base, _ = server
    driver = Browser(base)
    driver.login("9000010013")
    monkeypatch.setattr(webapp, "handle_message", lambda *a, **k: 1 / 0)
    status, data = driver.call("POST", "/api/chat", {"text": "hello"})
    assert status == 500 and data["error"] == "Sorry, something went wrong on my side. Please send that again in a moment."


def test_warehouse_timeout_reaches_the_driver_through_the_timer(server):
    """No warehouse answer for 30 minutes: the "+ minutes" button runs the timer and the driver is told once."""
    base, _ = server
    driver, _ = _to_pending(base)
    wh = Browser(base)
    wh.call("POST", "/api/clock/advance", {"minutes": 30})
    wh.call("POST", "/api/clock/advance", {"minutes": 1})
    told = [m for m in driver.messages() if m.startswith("The warehouse hasn't answered about D2 12:00 yet")]
    assert len(told) == 1
