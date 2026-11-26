"""From held slot to booking: driver says yes, warehouse says yes / no / nothing (D6)."""

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import timedelta

from dockpilot import trace
from dockpilot.clock import to_iso
from dockpilot.db import write_transaction
from dockpilot.escalations import create_escalation, deliver
from dockpilot.feasibility import Reason, check_feasibility
from dockpilot.holds import _expire, get_hold
from dockpilot.settings import SETTINGS

PENDING = "PENDING_CONFIRMATION"
"""Status of a booking that waits for the warehouse."""


@dataclass(frozen=True)
class BookingResult:
    """Answer to "book this hold": ok, the appointment, its status, whether it was a repeat, and refusal reasons."""

    ok: bool
    appointment_id: str
    status: str
    replayed: bool
    reasons: list


def _log(conn, clock, event_type, appointment_id, shipment_id, actor):
    """Write one booking line to the events diary."""
    conn.execute(
        "INSERT INTO events (event_ts, event_type, entity_type, entity_id, shipment_id, actor) "
        "VALUES (?, ?, 'APPOINTMENT', ?, ?, ?)",
        (to_iso(clock.now()), event_type, appointment_id, shipment_id, actor),
    )


def _pending(conn, appointment_id):
    """Return a PENDING appointment row, or raise ValueError if it isn't pending."""
    a = conn.execute("SELECT * FROM appointments WHERE appointment_id = ?", (appointment_id,)).fetchone()
    if a is None or a["appointment_status"] != PENDING:
        raise ValueError(f"{appointment_id} is not waiting for the warehouse")
    return a


@trace.traced("booking request", "db", show_out=lambda r: f"{r.status} {r.appointment_id}" + (" (repeat)" if r.replayed else "")
              if r.ok else f"refused: {r.reasons[0].message}")
def convert_hold(conn, group_id, clock, settings=SETTINGS):
    """Driver said yes: turn an active hold into a PENDING booking; refuse if expired, no longer feasible, or the
    database refuses a second active booking on the slot (dp-19: a refusal, never a crash)."""
    try:
        now = to_iso(clock.now())
        with write_transaction(conn):
            _expire(conn, clock)
            hold = get_hold(conn, group_id)
            if hold.status == "CONVERTED":
                appt = conn.execute(
                    "SELECT h.appointment_id, a.appointment_status FROM slot_holds h "
                    "JOIN appointments a USING (appointment_id) WHERE h.group_id = ? LIMIT 1", (group_id,)).fetchone()
                return BookingResult(True, appt["appointment_id"], appt["appointment_status"], True, [])
            if hold.status == "EXPIRED":
                return BookingResult(False, None, None, False,
                                     [Reason("HOLD_EXPIRED", f"The hold ran out at {hold.expires_at:%H:%M}.")])
            check = check_feasibility(conn, hold.shipment_id, hold.slot_ids, clock, settings)
            if not check.feasible:
                return BookingResult(False, None, None, False, check.reasons)

            old = conn.execute(
                f"SELECT appointment_id FROM appointments WHERE shipment_id = ? AND is_current = 1 "
                f"AND appointment_status IN ('{PENDING}', 'CONFIRMED', 'IN_PROGRESS')", (hold.shipment_id,)).fetchone()
            appt_id = f"APT-{uuid.uuid4().hex[:10]}"
            conn.execute(
                """INSERT INTO appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source,
                       is_current, booked_at, replaced_appointment_id, updated_at)
                   VALUES (?, ?, ?, ?, 'DRIVER_CHAT', 0, ?, ?, ?)""",
                (appt_id, hold.shipment_id, hold.slot_ids[0], PENDING, now, old["appointment_id"] if old else None, now),
            )
            conn.execute("UPDATE slot_holds SET hold_status = 'CONVERTED', appointment_id = ? WHERE group_id = ?",
                         (appt_id, group_id))
            _log(conn, clock, "BOOKING_REQUESTED", appt_id, hold.shipment_id, "AGENT")
        return BookingResult(True, appt_id, PENDING, False, [])
    except sqlite3.IntegrityError as exc:
        if "appointments." not in str(exc):
            raise
        return BookingResult(False, None, None, False, [Reason("SLOT_TAKEN", "This slot was just booked.")])


@trace.traced("warehouse confirms", "db", show_in=lambda conn, appointment_id, *a, **k: appointment_id)
def confirm_booking(conn, appointment_id, clock, ref=None):
    """Warehouse said yes: CONFIRMED and current; the booking it replaces is cancelled now."""
    now = to_iso(clock.now())
    with write_transaction(conn):
        a = _pending(conn, appointment_id)
        if a["replaced_appointment_id"]:
            conn.execute(
                "UPDATE appointments SET appointment_status = 'CANCELLED', is_current = 0, cancelled_at = ?, "
                "cancellation_reason = 'Replaced by rebooking', updated_at = ? WHERE appointment_id = ?",
                (now, now, a["replaced_appointment_id"]))
        conn.execute(
            "UPDATE appointments SET appointment_status = 'CONFIRMED', is_current = 1, confirmed_at = ?, "
            "warehouse_confirmation_ref = ?, updated_at = ? WHERE appointment_id = ?",
            (now, ref, now, appointment_id))
        _log(conn, clock, "BOOKING_CONFIRMED", appointment_id, a["shipment_id"], "WAREHOUSE")


@trace.traced("warehouse rejects", "db", show_in=lambda conn, appointment_id, clock, reason: f"{appointment_id}: {reason}")
def reject_booking(conn, appointment_id, clock, reason):
    """Warehouse said no: REJECTED, so its slot is free again; any older booking is untouched."""
    now = to_iso(clock.now())
    with write_transaction(conn):
        a = _pending(conn, appointment_id)
        conn.execute(
            "UPDATE appointments SET appointment_status = 'REJECTED', cancellation_reason = ?, updated_at = ? "
            "WHERE appointment_id = ?", (reason, now, appointment_id))
        _log(conn, clock, "BOOKING_REJECTED", appointment_id, a["shipment_id"], "WAREHOUSE")


def check_warehouse_timeouts(conn, clock, settings=SETTINGS, out=None):
    """Hand every booking PENDING longer than the warehouse timeout to a coordinator; return the escalations."""
    cutoff = to_iso(clock.now() - timedelta(minutes=settings.warehouse_timeout_minutes))
    late = conn.execute(
        "SELECT appointment_id, shipment_id, slot_id, booked_at FROM appointments "
        "WHERE appointment_status = ? AND booked_at <= ? ORDER BY booked_at", (PENDING, cutoff)).fetchall()
    result = []
    for a in late:
        esc = create_escalation(
            conn, "WAREHOUSE_TIMEOUT", a["shipment_id"], clock, appointment_id=a["appointment_id"],
            summary=f"Warehouse has not confirmed {a['appointment_id']} ({a['slot_id']}) "
                    f"requested at {a['booked_at'][11:16]}.")
        if esc.delivery_status != "DELIVERED":
            esc = deliver(conn, esc, clock, out)
        result.append(esc)
    return result
