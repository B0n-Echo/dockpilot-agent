"""The local web demo (dp-15): driver chat page, warehouse view, and a timer. Built-in http.server only.

    uv run python -m dockpilot.webapp            # then open http://127.0.0.1:8001 and /warehouse
    uv run python -m dockpilot.webapp --reset    # fresh working database first
"""

import argparse
import base64
import hmac
import io
import json
import os
import secrets
import threading
import time
import traceback
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dockpilot import db, paths, trace
from dockpilot.allocation import settle_requests
from dockpilot.auth import end_session, get_session, request_code, verify_code
from dockpilot.bookings import PENDING, check_warehouse_timeouts, confirm_booking, reject_booking
from dockpilot.clock import DemoClock, to_iso
from dockpilot.conversation import (check_silent_drivers, handle_message, notify_allocation, notify_booking_result,
                                    notify_drift, notify_warehouse_timeout)
from dockpilot.escalations import format_escalation, get_escalation
from dockpilot.holds import expire_holds
from dockpilot.lifecycle import check_drift
from dockpilot.llm_reader import read_message_ai
from dockpilot.settings import SETTINGS

STATIC = Path(__file__).parent / "static"
"""Folder with chat.html and warehouse.html."""

MAX_BODY = 10 * 1024
"""Largest request body accepted, in bytes."""

COOKIE = "dp_session"
"""Name of the session cookie."""


class App:
    """What the server needs: the working database, clock, settings, reader, and one lock so writes run one at a time."""

    def __init__(self, db_path=paths.WORK_DB, clock=None, settings=SETTINGS, reader=read_message_ai):
        """Set up; the clock defaults to a DemoClock starting at the snapshot time."""
        self.db_path, self.clock, self.settings, self.reader = db_path, clock or DemoClock(), settings, reader
        self.lock, self.log = threading.Lock(), io.StringIO()
        self.since = self.clock.now()  # dp-22: drift is checked for what happened after this

    def tick(self):
        """Timer work: expire holds, settle rush-hour slot rivals, chase the warehouse, nudge or close silent chats."""
        conn = db.connect(self.db_path)
        try:
            with trace.turn(conn, "timer tick", quiet=True):
                self._tick(conn)
        finally:
            conn.close()

    def _tick(self, conn):
        """The timer's jobs, in order."""
        expire_holds(conn, self.clock)
        now = self.clock.now()
        notify_drift(conn, check_drift(conn, self.clock, self.since, self.settings, out=self.log), self.clock, self.settings)
        self.since = now
        notify_allocation(conn, settle_requests(conn, self.clock, self.settings), self.clock, self.settings)
        notify_warehouse_timeout(conn, check_warehouse_timeouts(conn, self.clock, self.settings, out=self.log),
                                 self.clock)
        check_silent_drivers(conn, self.clock, self.settings, out=self.log)


def _trips(session):
    """Trips as plain dicts for the page."""
    return [{"shipment_id": t.shipment_id, "order_reference": t.order_reference, "status": t.status}
            for t in session.trips]


def login_start(app, conn, body, sid):
    """Step 1: the code to show on screen (demo SMS), or the refusal."""
    r = request_code(conn, str(body.get("phone", "")), app.clock, app.settings)
    return (200, {"demo_sms": r.code}) if r.ok else (403, {"error": r.reason})


def login_verify(app, conn, body, sid):
    """Step 2: check the code; set the session cookie and greet the driver."""
    r = verify_code(conn, str(body.get("phone", "")), str(body.get("code", "")), app.clock, app.settings)
    if not r.ok:
        return 403, {"error": r.reason}
    s, first = r.session, r.session.driver_name.split()[0]
    t = next((t for t in s.trips if t.shipment_id == s.shipment_id), None)
    greeting = f"Hi {first}. Trip {t.order_reference} ({t.shipment_id})." if t else \
        f"Hi {first}. You have {len(s.trips)} trips today."
    cookie = f"{COOKIE}={s.session_id}; HttpOnly; SameSite=Strict; Path=/; Max-Age={app.settings.session_hours * 3600}"
    return 200, {"driver": s.driver_name, "greeting": greeting, "trips": _trips(s)}, {"Set-Cookie": cookie}


def chat_post(app, conn, body, sid):
    """One driver message through the agent (dp-14)."""
    text = str(body.get("text", "")).strip()[:1000]
    if not text:
        return 400, {"error": "Please type a message."}
    reply = handle_message(conn, sid, text, app.clock, app.settings, app.reader, message_ref=body.get("ref"))
    return (200, {"reply": reply.text, "state": reply.state}) if reply.ok else (401, {"error": reply.text})


def chat_get(app, conn, body, sid):
    """The driver's chat so far, so warehouse answers and nudges show up."""
    s = get_session(conn, sid, app.clock)
    if s is None:
        return 401, {"error": "Please log in again."}
    rows = conn.execute("SELECT sender_type, message_text, message_ts FROM chat_messages WHERE thread_id = ? "
                        "ORDER BY rowid", (s.thread_id or "",)).fetchall()
    trip = next((t for t in s.trips if t.shipment_id == s.shipment_id), None)
    return 200, {"driver": s.driver_name, "trip": trip and f"{trip.order_reference} ({trip.shipment_id})",
                 "clock": to_iso(app.clock.now()),
                 "messages": [{"from": r[0], "text": r[1], "at": r[2][11:16]} for r in rows]}


def logout(app, conn, body, sid):
    """End the session and clear the cookie."""
    end_session(conn, sid or "", app.clock)
    return 200, {"ok": True}, {"Set-Cookie": f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"}


def warehouse_get(app, conn, body, sid):
    """Bookings waiting for the warehouse, open escalations and the demo clock."""
    now = app.clock.now()
    pending = conn.execute(
        """SELECT a.appointment_id, a.shipment_id, a.booked_at, d.dock_code, sl.slot_start_ts, sl.slot_end_ts,
                  dr.driver_name
           FROM appointments a JOIN appointment_slots sl ON sl.slot_id = a.slot_id JOIN docks d ON d.dock_id = sl.dock_id
           JOIN shipments s ON s.shipment_id = a.shipment_id JOIN drivers dr ON dr.driver_id = s.driver_id
           WHERE a.appointment_status = ? AND a.booked_at <= ? ORDER BY a.booked_at DESC""",
        (PENDING, to_iso(now))).fetchall()
    escs = conn.execute("SELECT escalation_id FROM escalations WHERE status = 'OPEN' ORDER BY created_at DESC").fetchall()
    return 200, {
        "clock": to_iso(now),
        "pending": [{"appointment_id": p["appointment_id"], "shipment_id": p["shipment_id"], "driver": p["driver_name"],
                     "slot": f"{p['dock_code']} {p['slot_start_ts'][11:16]}–{p['slot_end_ts'][11:16]}",
                     "waiting_min": int((now - datetime.fromisoformat(p["booked_at"])).total_seconds() // 60)}
                    for p in pending],
        "escalations": [{"escalation_id": e.escalation_id, "reason": e.reason_code, "shipment_id": e.shipment_id,
                         "text": format_escalation(conn, e)} for e in map(lambda r: get_escalation(conn, r[0]), escs)],
    }


def warehouse_confirm(app, conn, body, sid):
    """Warehouse says yes (dp-08), then the driver is told (dp-14)."""
    appt = str(body.get("appointment_id", ""))
    with trace.turn(conn, "warehouse confirms", appt):
        confirm_booking(conn, appt, app.clock, ref=f"WH-{secrets.randbelow(10**4):04d}")
        return 200, {"told_driver": notify_booking_result(conn, appt, app.clock, app.settings).ok}


def warehouse_reject(app, conn, body, sid):
    """Warehouse says no with a reason, then the driver gets the reason and fresh options."""
    appt, reason = str(body.get("appointment_id", "")), str(body.get("reason", "")).strip()[:200]
    with trace.turn(conn, "warehouse rejects", appt):
        reject_booking(conn, appt, app.clock, reason or "No reason given")
        return 200, {"told_driver": notify_booking_result(conn, appt, app.clock, app.settings).ok}


def healthz(app, conn, body, sid):
    """For Render's health check (dp-23): the app is up and the database answers."""
    conn.execute("SELECT 1").fetchone()
    return 200, {"ok": True}


def clock_advance(app, conn, body, sid):
    """Demo button: jump the clock forward, then run the timer at once."""
    minutes = body.get("minutes", 10)
    if not isinstance(minutes, int) or not 1 <= minutes <= 120:
        return 400, {"error": "minutes must be 1 to 120"}
    app.clock.advance(minutes=minutes)
    app.tick()
    return 200, {"clock": to_iso(app.clock.now())}


ROUTES = {
    ("POST", "/api/login/start"): login_start, ("POST", "/api/login/verify"): login_verify,
    ("POST", "/api/chat"): chat_post, ("GET", "/api/chat"): chat_get, ("POST", "/api/logout"): logout,
    ("GET", "/api/warehouse"): warehouse_get, ("POST", "/api/warehouse/confirm"): warehouse_confirm,
    ("POST", "/api/warehouse/reject"): warehouse_reject, ("POST", "/api/clock/advance"): clock_advance,
    ("GET", "/healthz"): healthz,
}
"""API routes: (method, path) -> handler(app, conn, body, session id) -> (status, json[, headers])."""

PAGES = {"/": "chat.html", "/warehouse": "warehouse.html"}
"""Page routes."""

STAFF = {"/warehouse", "/api/warehouse", "/api/warehouse/confirm", "/api/warehouse/reject", "/api/clock/advance"}
"""Staff-only paths: they need WAREHOUSE_PASSWORD when it is set (dp-23)."""


def _staff_ok(header):
    """True when no WAREHOUSE_PASSWORD is set, or the Basic-auth header carries it (any user name)."""
    password = os.environ.get("WAREHOUSE_PASSWORD")
    if not password:
        return True
    try:
        given = base64.b64decode((header or "").removeprefix("Basic ")).decode().partition(":")[2]
    except ValueError:
        return False
    return hmac.compare_digest(given, password)


class Handler(BaseHTTPRequestHandler):
    """Routes requests; errors come back as a short message, never a stack trace."""

    app = None

    def _send(self, status, body, content_type="application/json; charset=utf-8", headers=None):
        """Send one response."""
        raw = (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)).encode()
        self.send_response(status)
        for k, v in {"Content-Type": content_type, "Content-Length": str(len(raw)), "Cache-Control": "no-store",
                     "X-Content-Type-Options": "nosniff", **(headers or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self, method):
        """Run a page or API route."""
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in STAFF and not _staff_ok(self.headers.get("Authorization")):
            return self._send(401, {"error": "Warehouse password needed."},
                              headers={"WWW-Authenticate": 'Basic realm="DockPilot warehouse"'})
        if method == "GET" and path in PAGES:
            return self._send(200, (STATIC / PAGES[path]).read_text(encoding="utf-8"), "text/html; charset=utf-8")
        route = ROUTES.get((method, path))
        if route is None:
            return self._send(404, {"error": "Not found."})
        size = int(self.headers.get("Content-Length") or 0)
        if size > MAX_BODY:
            return self._send(413, {"error": "Message too large."})
        try:
            body = json.loads(self.rfile.read(size) or b"{}") if method == "POST" else {}
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._send(400, {"error": "Please send JSON."})
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        sid = cookie[COOKIE].value if COOKIE in cookie else None
        conn = db.connect(self.app.db_path)
        try:
            with self.app.lock:
                status, payload, *extra = route(self.app, conn, body, sid)
        except (ValueError, LookupError) as exc:
            status, payload, extra = 409, {"error": str(exc)}, []
        except Exception:
            traceback.print_exc()  # for whoever runs the server; the driver only sees the plain sentence
            status, payload, extra = 500, {"error": "Sorry, something went wrong on my side. "
                                                    "Please send that again in a moment."}, []
        finally:
            conn.close()
        headers = dict(extra[0]) if extra else None
        if headers and "Set-Cookie" in headers and self.headers.get("X-Forwarded-Proto") == "https":
            headers["Set-Cookie"] += "; Secure"  # behind Render's HTTPS (dp-23)
        self._send(status, payload, headers=headers)

    def do_GET(self):
        """GET pages and API."""
        self._handle("GET")

    def do_POST(self):
        """POST API."""
        self._handle("POST")

    def log_message(self, format, *args):
        """No line per request."""


def make_server(app, host="127.0.0.1", port=8001):
    """A threaded server bound to this computer only, serving `app`."""
    handler = type("AppHandler", (Handler,), {"app": app})
    return ThreadingHTTPServer((host, port), handler)


def _timer(app, seconds=5):
    """Run app.tick every `seconds`, forever (daemon thread)."""
    while True:
        time.sleep(seconds)
        try:
            with app.lock:
                app.tick()
        except Exception as exc:
            print(f"timer: {exc}")


def _has_latest_tables(path):
    """True if the working database exists and already has every migration (dp-20's table is the newest)."""
    if not Path(path).exists():
        return False
    conn = db.connect(path)
    try:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'trace_spans'").fetchone() is not None
    finally:
        conn.close()


def main(argv=None):
    """Start the demo: --host / --port (default HOST / PORT, else 127.0.0.1:8001), --reset for fresh data."""
    parser = argparse.ArgumentParser(description="DockPilot web chat demo.")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8001)))
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--trace", action="store_true", help="record every step (see 'What happened?' in the chat)")
    args = parser.parse_args(argv)
    if args.trace:
        trace.set_enabled(True)
    if args.reset or not _has_latest_tables(paths.WORK_DB):
        db.init_work_db()
    app = App()
    threading.Thread(target=_timer, args=(app,), daemon=True).start()
    server = make_server(app, host=args.host, port=args.port)
    staff = "password set" if os.environ.get("WAREHOUSE_PASSWORD") else "no password"
    print(f"Driver chat: http://{args.host}:{args.port}   Warehouse: http://{args.host}:{args.port}/warehouse "
          f"({staff})  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
