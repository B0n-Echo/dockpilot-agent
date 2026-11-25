"""Driver login and trip check (dp-18): only a real driver with a trip in progress or a live booking may chat."""

import hashlib
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from dockpilot.clock import to_iso
from dockpilot.db import write_transaction
from dockpilot.settings import SETTINGS

IN_PROGRESS = ("IN_TRANSIT", "AT_GATE", "WAITING", "IN_DOCK")
"""Shipment states that mean the truck is on the road or at the warehouse."""

LIVE = ("PENDING_CONFIRMATION", "CONFIRMED", "IN_PROGRESS")
"""Appointment states that count as a live booking."""

NO_TRIP = "No trip in progress or booked slot found for you. Please call your dispatcher."
"""Refusal shown to a driver with nothing active."""


@dataclass(frozen=True)
class Trip:
    """One active trip of a driver, as the chat shows it."""

    shipment_id: str
    order_reference: str
    vehicle_reg: str
    planned_eta: str
    status: str


@dataclass(frozen=True)
class Session:
    """A logged-in driver; shipment_id is None until their trip is known."""

    session_id: str
    driver_id: str
    driver_name: str
    shipment_id: str | None
    thread_id: str | None
    trips: list


@dataclass(frozen=True)
class LoginResult:
    """The answer to a login step: ok, or a plain reason; plus the code or the session."""

    ok: bool
    reason: str | None = None
    code: str | None = None
    session: Session | None = None


def _log(conn, clock, event_type, entity_id, shipment_id=None, payload=None):
    """Write one line to the events diary (never the code itself)."""
    conn.execute(
        """INSERT INTO events (event_ts, event_type, entity_type, entity_id, shipment_id, actor, payload)
           VALUES (?, ?, 'LOGIN', ?, ?, 'DRIVER', ?)""",
        (to_iso(clock.now()), event_type, entity_id, shipment_id, json.dumps(payload or {})),
    )


def _hash(code_id, code):
    """Hash a code with its row id, so equal codes never share a hash."""
    return hashlib.sha256(f"{code_id}:{code}".encode()).hexdigest()


def _driver(conn, phone):
    """Find a driver by the last 10 digits of a phone number, in any format."""
    digits = re.sub(r"\D", "", phone or "")[-10:]
    if len(digits) != 10:
        return None
    for row in conn.execute("SELECT * FROM drivers"):
        if re.sub(r"\D", "", row["phone"])[-10:] == digits:
            return row
    return None


def active_trips(conn, driver_id, clock):
    """The driver's trips on the road / at the warehouse, or with a live booking; never cancelled or completed."""
    rows = conn.execute(
        f"""SELECT s.shipment_id, s.order_reference, v.registration_number, s.original_eta_ts, s.current_status
            FROM shipments s JOIN vehicles v ON v.vehicle_id = s.vehicle_id
            WHERE s.driver_id = ? AND s.current_status NOT IN ('COMPLETED', 'CANCELLED')
              AND (s.current_status IN {IN_PROGRESS}
                   OR EXISTS (SELECT 1 FROM appointments a WHERE a.shipment_id = s.shipment_id
                              AND a.appointment_status IN {LIVE}))
            ORDER BY s.original_eta_ts""",
        (driver_id,),
    ).fetchall()
    return [Trip(*r) for r in rows]


def request_code(conn, phone, clock, settings=SETTINGS):
    """Login step 1: check the driver and their trips, then make a 6-digit code (shown on screen in the demo)."""
    driver = _driver(conn, phone)
    reason = ("This number isn't registered as a driver." if driver is None
              else "Your driver account isn't active. Please call your dispatcher." if driver["driver_status"] != "ACTIVE"
              else NO_TRIP if not active_trips(conn, driver["driver_id"], clock) else None)
    if reason:
        with write_transaction(conn):
            _log(conn, clock, "LOGIN_REFUSED", driver["driver_id"] if driver else "unknown", payload={"reason": reason})
        return LoginResult(False, reason)
    code, code_id, now = f"{secrets.randbelow(10**6):06d}", f"CODE-{uuid.uuid4().hex[:12]}", clock.now()
    with write_transaction(conn):
        conn.execute("UPDATE login_codes SET cancelled_at = ? WHERE driver_id = ? AND used_at IS NULL AND cancelled_at IS NULL",
                     (to_iso(now), driver["driver_id"]))
        conn.execute("INSERT INTO login_codes (code_id, driver_id, code_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                     (code_id, driver["driver_id"], _hash(code_id, code), to_iso(now),
                      to_iso(now + timedelta(minutes=settings.login_code_minutes))))
        _log(conn, clock, "LOGIN_CODE_SENT", driver["driver_id"], payload={"code_id": code_id})
    return LoginResult(True, code=code)


def verify_code(conn, phone, code, clock, settings=SETTINGS):
    """Login step 2: check the code (time, tries), re-check trips, open a session tied to the only trip if there is one."""
    driver = _driver(conn, phone)
    row = driver and conn.execute(
        """SELECT * FROM login_codes WHERE driver_id = ? AND used_at IS NULL AND cancelled_at IS NULL
           ORDER BY created_at DESC LIMIT 1""", (driver["driver_id"],)).fetchone()
    now = clock.now()
    if not row or row["wrong_tries"] >= settings.login_max_tries:
        return LoginResult(False, "No valid code. Please ask for a new one.")
    if datetime.fromisoformat(row["expires_at"]) <= now:
        return LoginResult(False, "This code has expired. Please ask for a new one.")
    did = driver["driver_id"]
    if not secrets.compare_digest(_hash(row["code_id"], (code or "").strip()), row["code_hash"]):
        left = settings.login_max_tries - row["wrong_tries"] - 1
        with write_transaction(conn):
            conn.execute("UPDATE login_codes SET wrong_tries = wrong_tries + 1 WHERE code_id = ?", (row["code_id"],))
            _log(conn, clock, "LOGIN_REFUSED", did, payload={"reason": "wrong code", "tries_left": left})
        return LoginResult(False, f"Wrong code. {left} {'try' if left == 1 else 'tries'} left." if left else "Wrong code. Please ask for a new one.")
    trips = active_trips(conn, did, clock)
    if not trips:
        return LoginResult(False, NO_TRIP)
    sid, shipment = secrets.token_urlsafe(24), trips[0].shipment_id if len(trips) == 1 else None
    with write_transaction(conn):
        conn.execute("UPDATE login_codes SET used_at = ? WHERE code_id = ?", (to_iso(now), row["code_id"]))
        conn.execute("INSERT INTO driver_sessions (session_id, driver_id, shipment_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                     (sid, did, shipment, to_iso(now), to_iso(now + timedelta(hours=settings.session_hours))))
        _log(conn, clock, "LOGIN_OK", did, shipment, {"trips": [t.shipment_id for t in trips]})
    return LoginResult(True, session=get_session(conn, sid, clock))


def get_session(conn, session_id, clock):
    """Gatekeeper on every message: the session if valid, not expired and the driver still has a trip; else None."""
    row = conn.execute(
        """SELECT ds.*, d.driver_name FROM driver_sessions ds JOIN drivers d ON d.driver_id = ds.driver_id
           WHERE ds.session_id = ? AND ds.ended_at IS NULL""", (session_id or "",)).fetchone()
    if not row or datetime.fromisoformat(row["expires_at"]) <= clock.now():
        return None
    trips = active_trips(conn, row["driver_id"], clock)
    if not trips or (row["shipment_id"] and row["shipment_id"] not in {t.shipment_id for t in trips}):
        return None
    return Session(row["session_id"], row["driver_id"], row["driver_name"], row["shipment_id"], row["thread_id"], trips)


def choose_shipment(conn, session_id, shipment_id, clock):
    """Tie a session to one of this driver's own active trips; None if not allowed."""
    session = get_session(conn, session_id, clock)
    if not session or shipment_id not in {t.shipment_id for t in session.trips}:
        return None
    with write_transaction(conn):
        conn.execute("UPDATE driver_sessions SET shipment_id = ? WHERE session_id = ?", (shipment_id, session_id))
        _log(conn, clock, "SHIPMENT_CHOSEN", session.driver_id, shipment_id)
    return get_session(conn, session_id, clock)


def end_session(conn, session_id, clock):
    """Logout: the session stops working at once."""
    with write_transaction(conn):
        row = conn.execute("SELECT driver_id FROM driver_sessions WHERE session_id = ? AND ended_at IS NULL",
                           (session_id,)).fetchone()
        if row:
            conn.execute("UPDATE driver_sessions SET ended_at = ? WHERE session_id = ?", (to_iso(clock.now()), session_id))
            _log(conn, clock, "SESSION_ENDED", row["driver_id"])
