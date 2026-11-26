"""Tests for trace mode (dp-20): every step recorded in order, the AI call in full, masking, and no LangSmith
without a key. Offline: the rule reader or a fake AI."""

import json
import threading
import urllib.request
from datetime import datetime

import pytest

from dockpilot import db, trace
from dockpilot.auth import request_code, verify_code
from dockpilot.clock import IST, FakeClock
from dockpilot.conversation import handle_message
from dockpilot.llm_reader import read_message_ai
from dockpilot.reader import read_message
from dockpilot.webapp import App, make_server


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """A fresh working copy, tracing on, and LangSmith switched off (no key)."""
    monkeypatch.setattr(trace, "_langsmith_settings", lambda: (None, "dockpilot", None))
    trace.set_enabled(True)
    c = db.connect(db.init_work_db(tmp_path / "work.db"))
    yield c
    c.close()
    trace.set_enabled(False)


def _login(conn, clock, phone="9000010013"):
    """Log a driver in; return the session id."""
    return verify_code(conn, phone, request_code(conn, phone, clock).code, clock).session.session_id


def _steps(conn):
    """Step names of the last saved turn, in the order they started."""
    turn = conn.execute("SELECT turn_id FROM trace_turns ORDER BY started_at DESC, rowid DESC LIMIT 1").fetchone()[0]
    return [r[0] for r in conn.execute("SELECT name FROM trace_spans WHERE turn_id = ? ORDER BY started_at, rowid",
                                       (turn,))], turn


def _in_order(names, expected):
    """True if `expected` appears in `names` in this order (other steps may sit between)."""
    it = iter(names)
    return all(any(n == e for n in it) for e in expected)


def test_contract_thr003_steps_recorded_in_order(conn):
    """"I am late by one hour." then "around 11:15": login, read, decide, ask; then answer, search, D7, hold."""
    clock = FakeClock(datetime(2026, 8, 4, 9, 19, tzinfo=IST))
    sid = _login(conn, clock)
    handle_message(conn, sid, "I am late by one hour.", clock, reader=read_message)
    names, turn = _steps(conn)
    assert _in_order(names, ["driver message", "login check", "read the message", "act on the message",
                             "ask for the arrival time (R7)"])
    assert "rules: REPORT_DELAY, no clock time (LOW), ask first" in trace.show(conn, turn)
    clock.advance(minutes=1)
    handle_message(conn, sid, "around 11:15", clock, reader=read_message)
    names, turn = _steps(conn)
    assert _in_order(names, ["login check", "read the message", "answer to the time question", "offer options",
                             "slot search", "who gets it (D7)", "hold the slot"])
    tree = trace.show(conn, turn)
    assert "fit · best D2 12:00" in tree and "normal day: first come, first served → held" in tree


def test_ai_call_recorded_in_full_and_fallback_reason_noted(conn):
    """The AI step carries the full input and raw answer; a timeout is noted as the reason for the rules."""
    clock = FakeClock(datetime(2026, 8, 4, 9, 30, tzinfo=IST))
    sid = _login(conn, clock)
    answer = json.dumps({"intent": "REPORT_DELAY", "eta": "11:30", "eta_confidence": "HIGH", "delay_reason": "TRAFFIC"})
    ai = lambda t, at: read_message_ai(t, at, call=lambda m: answer)
    handle_message(conn, sid, "Stuck in traffic, should make it by half past eleven", clock, reader=ai)
    row = conn.execute("SELECT summary_in, summary_out FROM trace_spans WHERE kind = 'ai'").fetchone()
    assert "[system] You extract fields" in row[0] and "[user] Sent at 09:30: Stuck in traffic" in row[0]
    assert row[1] == answer
    def timeout(messages):
        """A stand-in for a network call that times out."""
        raise TimeoutError
    slow = lambda t, at: read_message_ai(t, at, call=timeout)
    clock.advance(minutes=1)
    handle_message(conn, sid, "Stuck in traffic, reaching around 12", clock, reader=slow)
    _, turn = _steps(conn)
    assert "rules used: AI timed out" in trace.show(conn, turn)


def test_tracing_off_writes_nothing_and_replies_are_the_same(conn, tmp_path):
    """With tracing off: no trace rows, and the same replies as with tracing on."""
    clock = FakeClock(datetime(2026, 8, 4, 9, 19, tzinfo=IST))
    on = handle_message(conn, _login(conn, clock), "I am late by one hour.", clock, reader=read_message).text
    trace.set_enabled(False)
    other = db.connect(db.init_work_db(tmp_path / "off.db"))
    off = handle_message(other, _login(other, clock), "I am late by one hour.", clock, reader=read_message).text
    assert on == off
    assert other.execute("SELECT COUNT(*) FROM trace_spans").fetchone()[0] == 0


def test_phone_numbers_are_masked_and_keys_never_recorded(conn):
    """A phone number in a message is stored as 90000•••13; nothing that looks like a key is stored."""
    clock = FakeClock(datetime(2026, 8, 4, 9, 19, tzinfo=IST))
    handle_message(conn, _login(conn, clock), "Call me on 9000010013, late by one hour", clock, reader=read_message)
    stored = " ".join(str(v) for t in ("trace_turns", "trace_spans") for r in conn.execute(f"SELECT * FROM {t}") for v in r)
    assert "9000010013" not in stored and "90000•••13" in stored
    assert "sk-" not in stored and "OPENROUTER_API_KEY" not in stored


def test_langsmith_only_with_a_key(conn, monkeypatch):
    """No key: nothing is sent. With a key: the finished turn, AI step included, goes to LangSmith."""
    sent = []
    monkeypatch.setattr(trace, "_post", lambda t, key, project, endpoint: sent.append((t, key, project)))
    monkeypatch.setattr(trace.threading, "Thread", lambda target, args, daemon: type(
        "T", (), {"start": lambda self: target(*args)})())
    clock = FakeClock(datetime(2026, 8, 4, 9, 19, tzinfo=IST))
    sid = _login(conn, clock)
    handle_message(conn, sid, "I am late by one hour.", clock, reader=read_message)
    assert sent == []
    monkeypatch.setattr(trace, "_langsmith_settings", lambda: ("test-key", "dockpilot", None))
    clock.advance(minutes=1)
    handle_message(conn, sid, "around 11:15", clock, reader=read_message)
    assert len(sent) == 1 and sent[0][2] == "dockpilot"
    assert {s.name for s in sent[0][0].spans} >= {"driver message", "slot search", "hold the slot"}


def test_web_panel_shows_only_the_drivers_own_turns(conn, tmp_path):
    """/api/trace needs a session and returns only the logged-in driver's turns."""
    app = App(tmp_path / "work.db", FakeClock(datetime(2026, 8, 4, 9, 19, tzinfo=IST)), reader=read_message)
    httpd = make_server(app, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    def browser(phone):
        """Log in through the API; return a cookie-keeping opener."""
        import http.cookiejar
        o = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        post = lambda path, body: json.loads(o.open(urllib.request.Request(
            base + path, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"})).read())
        code = post("/api/login/start", {"phone": phone})["demo_sms"]
        post("/api/login/verify", {"phone": phone, "code": code})
        return o, post
    try:
        kailash, say = browser("9000010013")
        say("/api/chat", {"text": "I am late by one hour."})
        mine = json.loads(kailash.open(base + "/api/trace").read())
        assert mine["enabled"] and mine["turns"][0]["label"] == "I am late by one hour."
        sandeep, _ = browser("9000010012")
        assert json.loads(sandeep.open(base + "/api/trace").read())["turns"] == []
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_quiet_timer_tick_is_not_saved(conn, tmp_path):
    """A timer tick where nothing happened leaves no trace (the timer runs every 5 seconds)."""
    App(tmp_path / "work.db", FakeClock(datetime(2026, 8, 4, 9, 19, tzinfo=IST))).tick()
    assert conn.execute("SELECT COUNT(*) FROM trace_turns WHERE name = 'timer tick'").fetchone()[0] == 0


def test_langsmith_needs_the_tracing_flag_as_well_as_the_key(monkeypatch):
    """LANGSMITH_TRACING decides: a key alone sends nothing; true + key sends; the EU endpoint is passed on."""
    monkeypatch.undo()  # the real settings function, not conftest's offline stand-in
    monkeypatch.setattr(trace, "_env", lambda: {"LANGSMITH_API_KEY": "k"})
    assert trace._langsmith_settings()[0] is None
    monkeypatch.setattr(trace, "_env", lambda: {"LANGSMITH_API_KEY": "k", "LANGSMITH_TRACING": "false"})
    assert trace._langsmith_settings()[0] is None
    monkeypatch.setattr(trace, "_env", lambda: {"LANGSMITH_API_KEY": "k", "LANGSMITH_TRACING": "True",
                                                "LANGSMITH_ENDPOINT": "https://eu.api.smith.langchain.com"})
    assert trace._langsmith_settings() == ("k", "dockpilot", "https://eu.api.smith.langchain.com")


def test_langsmith_tracing_flag_also_turns_recording_on(monkeypatch):
    """LANGSMITH_TRACING=true records locally too, so there is something to send; neither flag means off."""
    trace.set_enabled(None)
    monkeypatch.setattr(trace, "_env", lambda: {"LANGSMITH_TRACING": "true"})  # recording only; sending is stubbed
    assert trace.enabled()
    monkeypatch.setattr(trace, "_env", lambda: {})
    assert not trace.enabled()
    trace.set_enabled(False)
