"""Tests for real sign-in with Cognito (dp-24). A fake Cognito answers; the real one is never called."""

import json
import threading
import urllib.request

import pytest

from dockpilot import cognito, db
from dockpilot.auth import request_code, verify_code
from dockpilot.clock import FakeClock
from dockpilot.reader import read_message
from dockpilot.webapp import App, make_server


class FakeCognito:
    """Stands in for Cognito's public sign-in API: one valid code per sign-in session."""

    def __init__(self):
        """No sign-ins yet; records every call."""
        self.calls, self.code = [], "482913"

    def __call__(self, target, body):
        """Answer InitiateAuth / RespondToAuthChallenge like Cognito does."""
        self.calls.append((target, body))
        if target == "InitiateAuth":
            if body["AuthParameters"]["USERNAME"] == "DRV009":  # a driver nobody created in Cognito
                raise cognito.CognitoError(cognito.WORDS["UserNotFoundException"])
            return {"ChallengeName": "EMAIL_OTP", "Session": "sess-1",
                    "ChallengeParameters": {"CODE_DELIVERY_DESTINATION": "y***@g***"}}
        answer = body["ChallengeResponses"]["EMAIL_OTP_CODE"]
        if answer == "000000":
            raise cognito.CognitoError(cognito.WORDS["ExpiredCodeException"])
        if answer != self.code:
            raise cognito.CognitoError(cognito.WORDS["CodeMismatchException"])
        return {"AuthenticationResult": {"AccessToken": "not-stored", "ExpiresIn": 3600}}


@pytest.fixture
def fake(monkeypatch):
    """Cognito mode on, with the fake answering."""
    f = FakeCognito()
    monkeypatch.setattr(cognito, "enabled", lambda: True)
    monkeypatch.setattr(cognito, "_call", f)
    return f


@pytest.fixture
def conn(tmp_path):
    """A fresh working copy in a temporary folder."""
    c = db.connect(db.init_work_db(tmp_path / "work.db"))
    yield c
    c.close()


def test_contract_code_by_email_then_session_for_the_trip(conn, fake):
    """Kailash: Cognito emails the code (no code for us), the right code opens the session tied to SHP1013."""
    clock = FakeClock()
    sent = request_code(conn, "9000010013", clock)
    assert sent.ok and sent.code is None and sent.sent_to == "y***@g***"
    assert fake.calls[0][1]["AuthParameters"] == {"USERNAME": "DRV013", "PREFERRED_CHALLENGE": "EMAIL_OTP"}
    result = verify_code(conn, "9000010013", "482913", clock)
    assert result.ok and result.session.shipment_id == "SHP1013"
    assert fake.calls[1][1]["Session"] == "sess-1"


def test_wrong_code_counts_tries_and_expired_ends_it(conn, fake):
    """Wrong codes say how many tries are left; an expired one asks for a new code."""
    clock = FakeClock()
    request_code(conn, "9000010013", clock)
    assert verify_code(conn, "9000010013", "111111", clock).reason == "Wrong code. 2 tries left."
    assert verify_code(conn, "9000010013", "000000", clock).reason == "This code has expired. Please ask for a new one."
    assert not verify_code(conn, "9000010013", "482913", clock).ok  # that code is finished


def test_refused_drivers_never_reach_cognito(conn, fake):
    """No trip (Ashok) or unknown phone: refused before any email is sent."""
    clock = FakeClock()
    assert not request_code(conn, "9000010008", clock).ok
    assert not request_code(conn, "+91-9999999999", clock).ok
    assert fake.calls == []


def test_driver_without_a_cognito_account_hears_why(conn, fake):
    """Vikram (DRV009) has a trip but no Cognito account: a plain reason, logged, no crash."""
    result = request_code(conn, "9000010009", FakeClock())
    assert not result.ok and result.reason == "This driver has no sign-in account yet. Please call your dispatcher."
    assert conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'LOGIN_REFUSED'").fetchone()[0] == 1


def test_nothing_about_the_code_is_stored(conn, fake):
    """Cognito mode stores only Cognito's session and a marker: never the code, never Cognito's tokens."""
    clock = FakeClock()
    request_code(conn, "9000010013", clock)
    verify_code(conn, "9000010013", "482913", clock)
    stored = " ".join(str(v) for t in ("login_codes", "driver_sessions", "events") for r in conn.execute(
        f"SELECT * FROM {t}") for v in r)
    assert "482913" not in stored and "not-stored" not in stored
    assert conn.execute("SELECT code_hash, cognito_session FROM login_codes").fetchone()[:] == ("cognito", "sess-1")


def test_web_page_says_where_the_code_went_never_the_code(tmp_path, fake):
    """In Cognito mode the login answer has 'sent_to' and no 'demo_sms'; /api/config tells the page the mode."""
    app = App(db.init_work_db(tmp_path / "w.db"), FakeClock(), reader=read_message)
    httpd = make_server(app, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        req = urllib.request.Request(base + "/api/login/start", data=json.dumps({"phone": "9000010013"}).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            assert json.loads(resp.read()) == {"sent_to": "y***@g***"}
        with urllib.request.urlopen(base + "/api/config") as resp:
            assert json.loads(resp.read()) == {"auth_mode": "cognito"}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cognito_errors_become_plain_words(monkeypatch):
    """A real HTTP error from Cognito is turned into our wording; a network failure too."""
    import io
    import urllib.error
    def boom(req, timeout):
        """Cognito answering 400 CodeMismatchException."""
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                     io.BytesIO(b'{"__type": "CodeMismatchException"}'))
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(cognito.CognitoError, match="Wrong code."):
        cognito._call("RespondToAuthChallenge", {})
    def offline(req, timeout):
        """No network."""
        raise urllib.error.URLError("down")
    monkeypatch.setattr(urllib.request, "urlopen", offline)
    with pytest.raises(cognito.CognitoError, match="unavailable"):
        cognito._call("InitiateAuth", {})
