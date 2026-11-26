"""Keeping bookings honest during the day: late ETAs, no-shows and dock problems (D9)."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from dockpilot import trace
from dockpilot.bookings import convert_hold
from dockpilot.clock import to_iso
from dockpilot.db import write_transaction
from dockpilot.escalations import create_escalation, deliver, escalate_if_no_options
from dockpilot.feasibility import _facility_rules, check_feasibility
from dockpilot.holds import place_hold
from dockpilot.settings import SETTINGS

LIVE = ("PENDING_CONFIRMATION", "CONFIRMED", "IN_PROGRESS")
"""Appointment statuses that still occupy a slot."""


@dataclass(frozen=True)
class Review:
    """What happened to one booking: still fits?, why not, new options, escalation, and any new booking."""

    appointment_id: str
    ok: bool
    reasons: list
    options: list
    escalation: object
    new_appointment_id: str = None


def _log(conn, clock, event_type, appointment_id, shipment_id, actor, payload=None):
    """Write one booking line to the events diary (in its own transaction)."""
    with write_transaction(conn):
        conn.execute(
            "INSERT INTO events (event_ts, event_type, entity_type, entity_id, shipment_id, actor, payload) "
            "VALUES (?, ?, 'APPOINTMENT', ?, ?, ?, ?)",
            (to_iso(clock.now()), event_type, appointment_id, shipment_id, actor, json.dumps(payload or {})),
        )


def _booked_slots(conn, appointment_id):
    """The appointment's slot plus slots held for it by CONVERTED holds (two-slot bookings), in time order."""
    rows = conn.execute(
        """SELECT sl.slot_id FROM appointment_slots sl WHERE sl.slot_id IN (
               SELECT slot_id FROM appointments WHERE appointment_id = ?
               UNION SELECT slot_id FROM slot_holds WHERE appointment_id = ? AND hold_status = 'CONVERTED')
           ORDER BY sl.slot_start_ts""", (appointment_id, appointment_id)).fetchall()
    return [r["slot_id"] for r in rows]


def _options_or_escalate(conn, shipment_id, clock, settings, out):
    """New options for the truck, or a delivered NO_FEASIBLE_SLOT escalation if there are none."""
    search, esc = escalate_if_no_options(conn, shipment_id, clock, settings, out=out)
    return search.options, esc


def recheck_booking(conn, shipment_id, clock, settings=SETTINGS, out=None):
    """A new ETA arrived: does the current booking still fit? If not, flag it and offer options or escalate."""
    appt = conn.execute(
        f"SELECT appointment_id FROM appointments WHERE shipment_id = ? AND is_current = 1 "
        f"AND appointment_status IN ({','.join('?' * len(LIVE))})", (shipment_id, *LIVE)).fetchone()
    if appt is None:
        raise LookupError(f"{shipment_id} has no live booking")
    appt_id = appt["appointment_id"]
    check = check_feasibility(conn, shipment_id, _booked_slots(conn, appt_id), clock, settings)
    if check.feasible:
        return Review(appt_id, True, [], [], None)
    _log(conn, clock, "BOOKING_FLAGGED", appt_id, shipment_id, "AGENT", {"reasons": [r.code for r in check.reasons]})
    options, esc = _options_or_escalate(conn, shipment_id, clock, settings, out)
    return Review(appt_id, False, check.reasons, options, esc)


def mark_no_shows(conn, clock, settings=SETTINGS, out=None):
    """Mark CONFIRMED bookings with no check-in after the grace time as NO_SHOW; trucks still on the road get options."""
    now = clock.now()
    reviews = []
    for a in conn.execute(
        """SELECT a.appointment_id, a.shipment_id, sl.slot_start_ts, sl.facility_id, f.checkin_grace_min,
                  s.current_status
           FROM appointments a JOIN appointment_slots sl USING (slot_id)
           JOIN facilities f ON f.facility_id = sl.facility_id JOIN shipments s USING (shipment_id)
           WHERE a.appointment_status = 'CONFIRMED'""").fetchall():
        grace = int(_facility_rules(conn, a["facility_id"], now.date()).get("NO_SHOW_GRACE_MIN", a["checkin_grace_min"]))
        if datetime.fromisoformat(a["slot_start_ts"]) + timedelta(minutes=grace) > now:
            continue
        if conn.execute("SELECT 1 FROM facility_checkins WHERE shipment_id = ? AND gate_in_ts <= ?",
                        (a["shipment_id"], to_iso(now))).fetchone():
            continue
        with write_transaction(conn):
            conn.execute("UPDATE appointments SET appointment_status = 'NO_SHOW', is_current = 0, updated_at = ? "
                         "WHERE appointment_id = ?", (to_iso(now), a["appointment_id"]))
        _log(conn, clock, "BOOKING_NO_SHOW", a["appointment_id"], a["shipment_id"], "TIMER", {"grace_min": grace})
        options, esc = [], None
        if a["current_status"] == "IN_TRANSIT":
            options, esc = _options_or_escalate(conn, a["shipment_id"], clock, settings, out)
        reviews.append(Review(a["appointment_id"], False, [], options, esc))
    return reviews


def handle_dock_event(conn, dock_event_id, clock, settings=SETTINGS, out=None):
    """Re-home every live booking hit by a dock event (request the best free slot), or escalate it."""
    ev = conn.execute("SELECT * FROM dock_status_events WHERE dock_event_id = ?", (dock_event_id,)).fetchone()
    if ev is None:
        raise LookupError(f"Unknown dock event {dock_event_id}")
    now = to_iso(clock.now())
    hit = conn.execute(
        f"""SELECT DISTINCT a.appointment_id, a.shipment_id, a.appointment_status
            FROM appointments a
            JOIN appointment_slots sl ON sl.slot_id = a.slot_id
               OR sl.slot_id IN (SELECT slot_id FROM slot_holds h
                                 WHERE h.appointment_id = a.appointment_id AND h.hold_status = 'CONVERTED')
            WHERE sl.dock_id = ? AND a.appointment_status IN ({','.join('?' * len(LIVE))})
              AND sl.slot_end_ts > ? AND sl.slot_end_ts > ? AND (? IS NULL OR sl.slot_start_ts < ?)""",
        (ev["dock_id"], *LIVE, now, ev["event_start_ts"], ev["event_end_ts"], ev["event_end_ts"])).fetchall()
    reviews = []
    for a in hit:
        new_id, options, esc = None, [], None
        if a["appointment_status"] == "IN_PROGRESS":
            esc = create_escalation(conn, "APPROVAL_REQUIRED", a["shipment_id"], clock,
                                    appointment_id=a["appointment_id"],
                                    summary=f"{ev['event_type']} on the dock during unloading of {a['appointment_id']}.")
            esc = deliver(conn, esc, clock, out)
        else:
            options, esc = _options_or_escalate(conn, a["shipment_id"], clock, settings, out)
            if options:
                held = place_hold(conn, a["shipment_id"], options[0].slot_ids,
                                  f"rehome-{a['appointment_id']}-{dock_event_id}", clock, settings)
                if held.placed:
                    new_id = convert_hold(conn, held.hold.group_id, clock, settings).appointment_id
        _log(conn, clock, "DOCK_EVENT_HANDLED", a["appointment_id"], a["shipment_id"], "AGENT",
             {"dock_event_id": dock_event_id, "new_appointment_id": new_id,
              "escalation_id": esc.escalation_id if esc else None})
        reviews.append(Review(a["appointment_id"], False, [], options, esc, new_id))
    return reviews


@dataclass(frozen=True)
class Drift:
    """dp-22: one thing that changed during the day (DOCK_EVENT, NO_SHOW or NEW_ETA) and what dp-09 did about it."""

    kind: str
    shipment_id: str
    appointment_id: str
    review: Review
    detail: str


def _shipment_of(conn, appointment_id):
    """The shipment a booking belongs to."""
    return conn.execute("SELECT shipment_id FROM appointments WHERE appointment_id = ?", (appointment_id,)).fetchone()[0]


@trace.traced("drift check (dock events, no-shows, new arrival times)", "timer",
              show_out=lambda ds: ", ".join(f"{d.kind} {d.shipment_id}" for d in ds))
def check_drift(conn, clock, since, settings=SETTINGS, out=None):
    """dp-22: run the dp-09 checks for whatever happened since the last timer tick: dock events now on record (each
    once), no-shows, and new arrival times recorded outside our chat (our own ETA_DP- rows are skipped)."""
    now, items = to_iso(clock.now()), []
    handled = {json.loads(r[0] or "{}").get("dock_event_id")
               for r in conn.execute("SELECT payload FROM events WHERE event_type = 'DOCK_EVENT_HANDLED'")}
    for ev in conn.execute(
            """SELECT e.dock_event_id, e.event_type, e.reason, d.dock_code FROM dock_status_events e
               JOIN docks d ON d.dock_id = e.dock_id WHERE e.created_at <= ? ORDER BY e.created_at""", (now,)).fetchall():
        if ev[0] in handled:
            continue
        detail = f"Dock {ev[3]} has a {ev[1].lower().replace('_', ' ')} ({ev[2][:1].lower() + ev[2][1:]})"
        items += [Drift("DOCK_EVENT", _shipment_of(conn, r.appointment_id), r.appointment_id, r, detail)
                  for r in handle_dock_event(conn, ev[0], clock, settings, out)]
    items += [Drift("NO_SHOW", _shipment_of(conn, r.appointment_id), r.appointment_id, r, "no check-in in time")
              for r in mark_no_shows(conn, clock, settings, out)]
    for shipment, eta in conn.execute(
            "SELECT shipment_id, declared_eta_ts FROM eta_updates WHERE created_at > ? AND created_at <= ? "
            "AND eta_update_id NOT LIKE 'ETA_DP-%' ORDER BY created_at", (to_iso(since), now)).fetchall():
        try:
            r = recheck_booking(conn, shipment, clock, settings, out)
        except LookupError:  # no live booking to re-check
            continue
        if not r.ok:
            items.append(Drift("NEW_ETA", shipment, r.appointment_id, r, eta[11:16]))
    return items
