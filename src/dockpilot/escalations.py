"""Hand a case to a person with the whole story in one record (D10, D12, M3)."""

import json
import sys
import uuid
from dataclasses import dataclass

from dockpilot.clock import to_iso
from dockpilot.db import write_transaction
from dockpilot.feasibility import latest_eta
from dockpilot.options import find_options
from dockpilot.settings import SETTINGS

LIVE = ("PENDING_CONFIRMATION", "CONFIRMED", "IN_PROGRESS")
"""Appointment statuses that count as the shipment's current booking."""


@dataclass(frozen=True)
class Escalation:
    """One hand-off record: a row of the escalations table, with options_checked as a list."""

    escalation_id: str
    created_at: str
    reason_code: str
    driver_id: str
    shipment_id: str
    thread_id: str
    appointment_id: str
    latest_eta_ts: str
    eta_source: str
    eta_confidence: str
    options_checked: list
    driver_constraints: str
    summary: str
    status: str
    delivery_status: str


def get_escalation(conn, escalation_id):
    """Read one escalation back as an Escalation; LookupError if it doesn't exist."""
    row = conn.execute("SELECT * FROM escalations WHERE escalation_id = ?", (escalation_id,)).fetchone()
    if row is None:
        raise LookupError(f"Unknown escalation {escalation_id}")
    fields = {k: row[k] for k in Escalation.__dataclass_fields__}
    fields["options_checked"] = json.loads(row["options_checked"] or "[]")
    return Escalation(**fields)


def _log(conn, clock, event_type, esc_id, shipment_id, actor):
    """Write one escalation line to the events diary."""
    conn.execute(
        "INSERT INTO events (event_ts, event_type, entity_type, entity_id, shipment_id, actor) "
        "VALUES (?, ?, 'ESCALATION', ?, ?, ?)",
        (to_iso(clock.now()), event_type, esc_id, shipment_id, actor),
    )


def _constraints(conn, shipment_id, now):
    """Driver constraints from the newest driver_exceptions report made by now, or "None recorded"."""
    row = conn.execute(
        "SELECT earliest_acceptable_ts, latest_acceptable_ts, description FROM driver_exceptions "
        "WHERE shipment_id = ? AND reported_at <= ? ORDER BY reported_at DESC LIMIT 1",
        (shipment_id, now),
    ).fetchone()
    if row is None:
        return "None recorded"
    hhmm = lambda ts: ts[11:16] if ts else "not given"
    return (f"Earliest {hhmm(row['earliest_acceptable_ts'])}; latest {hhmm(row['latest_acceptable_ts'])}. "
            f"Driver reported: {row['description']}")


def create_escalation(conn, reason_code, shipment_id, clock, options_checked=(), summary=None,
                      thread_id=None, driver_constraints=None):
    """Open an escalation with every M3 field filled from the database; return it.

    options_checked is a list of dp-04 Feasibility results. If the shipment already has an
    OPEN escalation with the same reason, that one is returned instead of a duplicate.
    """
    now = to_iso(clock.now())
    with write_transaction(conn):
        existing = conn.execute(
            "SELECT escalation_id FROM escalations WHERE shipment_id = ? AND reason_code = ? AND status = 'OPEN'",
            (shipment_id, reason_code),
        ).fetchone()
        if existing:
            return get_escalation(conn, existing["escalation_id"])

        ship = conn.execute("SELECT * FROM shipments WHERE shipment_id = ?", (shipment_id,)).fetchone()
        if ship is None:
            raise LookupError(f"Unknown shipment {shipment_id}")
        eta, source, confidence = latest_eta(conn, shipment_id, clock)
        appt = conn.execute(
            f"SELECT appointment_id FROM appointments WHERE shipment_id = ? AND is_current = 1 "
            f"AND appointment_status IN ({','.join('?' * len(LIVE))})",
            (shipment_id, *LIVE),
        ).fetchone()
        if thread_id is None:
            t = conn.execute(
                "SELECT thread_id FROM chat_threads WHERE shipment_id = ? AND opened_at <= ? "
                "ORDER BY opened_at DESC LIMIT 1", (shipment_id, now),
            ).fetchone()
            thread_id = t["thread_id"] if t else None
        checked = [{"slot_ids": f.slot_ids,
                    "failed_because": [{"code": r.code, "message": r.message} for r in f.reasons]}
                   for f in options_checked]
        if summary is None:
            need = "needs reefer" if ship["temperature_control_required"] else f"needs {ship['required_dock_type']} dock"
            summary = (f"No feasible slot for {shipment_id} ({ship['priority_code']}, {need}) arriving "
                       f"{eta:%H:%M}. {len(checked)} options checked, none fit.")

        esc_id = f"ESC-{uuid.uuid4().hex[:12]}"
        conn.execute(
            """INSERT INTO escalations (escalation_id, created_at, reason_code, driver_id, shipment_id, thread_id,
                   appointment_id, latest_eta_ts, eta_source, eta_confidence, options_checked,
                   driver_constraints, summary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (esc_id, now, reason_code, ship["driver_id"], shipment_id, thread_id,
             appt["appointment_id"] if appt else None, to_iso(eta), source, confidence, json.dumps(checked),
             driver_constraints or _constraints(conn, shipment_id, now), summary),
        )
        _log(conn, clock, "ESCALATION_RAISED", esc_id, shipment_id, "AGENT")
    return get_escalation(conn, esc_id)


def format_escalation(conn, esc):
    """Return the hand-off as readable text for a coordinator, with options grouped by dock."""
    lines = [
        f"ESCALATION {esc.escalation_id}  [{esc.reason_code}]",
        f"  Shipment:      {esc.shipment_id}   Driver: {esc.driver_id}   Chat: {esc.thread_id or '-'}",
        f"  Booked now:    {esc.appointment_id or 'nothing'}",
        f"  Latest ETA:    {esc.latest_eta_ts[11:16]} ({esc.eta_source}, {esc.eta_confidence} confidence)",
        f"  Driver says:   {esc.driver_constraints}",
        f"  Summary:       {esc.summary}",
        "  Options checked:",
    ]
    groups = {}  # dock -> [first start, last end, option count, {reason code: count}]
    for opt in esc.options_checked:
        first = conn.execute(
            "SELECT d.dock_code, sl.slot_start_ts FROM appointment_slots sl JOIN docks d USING (dock_id) "
            "WHERE sl.slot_id = ?", (opt["slot_ids"][0],)).fetchone()
        last = conn.execute("SELECT slot_end_ts FROM appointment_slots WHERE slot_id = ?",
                            (opt["slot_ids"][-1],)).fetchone()
        g = groups.setdefault(first["dock_code"], [first["slot_start_ts"], last["slot_end_ts"], 0, {}])
        g[1], g[2] = max(g[1], last["slot_end_ts"]), g[2] + 1
        for r in opt["failed_because"]:
            g[3][r["code"]] = g[3].get(r["code"], 0) + 1
    for dock, (start, end, n, codes) in sorted(groups.items()):
        why = ", ".join(f"{SHORT.get(c, c)} ({k})" for c, k in codes.items())
        lines.append(f"    {dock:3} {start[11:16]}-{end[11:16]}  {n} options: {why}")
    lines.append("  (Full reason for every option is stored in the record.)")
    return "\n".join(lines)


SHORT = {
    "ARRIVES_AFTER_START": "truck arrives too late", "OVER_WEIGHT_LIMIT": "too heavy for dock",
    "UNLOAD_TOO_LONG": "unload longer than slot", "WRONG_DOCK_TYPE": "wrong dock type",
    "NEEDS_REEFER": "needs refrigeration", "NEEDS_HEAVY_DOCK": "needs heavy dock",
    "SLOT_NOT_OPEN": "slot blocked", "DOCK_NOT_ACTIVE": "dock out of service", "DOCK_EVENT": "dock problem",
    "SLOT_TAKEN": "already booked", "SLOT_HELD": "held by another driver", "OUTSIDE_HOURS": "outside opening hours",
    "AFTER_LAST_START": "after last start time", "WRONG_FACILITY": "wrong facility",
    "SLOTS_NOT_BACK_TO_BACK": "slots not back-to-back",
}
"""Short plain labels for reason codes, used in the printed hand-off."""


def deliver(conn, esc, clock, out=None):
    """Print the hand-off (the console is the delivery channel for now); mark DELIVERED, or FAILED."""
    try:
        print(format_escalation(conn, esc), file=out or sys.stdout)
        status, event = "DELIVERED", "ESCALATION_DELIVERED"
    except Exception:
        status, event = "FAILED", "ESCALATION_DELIVERY_FAILED"
    with write_transaction(conn):
        conn.execute("UPDATE escalations SET delivery_status = ? WHERE escalation_id = ?", (status, esc.escalation_id))
        _log(conn, clock, event, esc.escalation_id, esc.shipment_id, "SYSTEM")
    return get_escalation(conn, esc.escalation_id)


def escalate_if_no_options(conn, shipment_id, clock, settings=SETTINGS, thread_id=None, out=None):
    """Find options; if there are none, open and deliver a NO_FEASIBLE_SLOT escalation.

    Returns (OptionSearch, Escalation or None).
    """
    search = find_options(conn, shipment_id, clock, settings)
    if search.options:
        return search, None
    esc = create_escalation(conn, "NO_FEASIBLE_SLOT", shipment_id, clock,
                            options_checked=search.checked, thread_id=thread_id)
    if esc.delivery_status != "DELIVERED":
        esc = deliver(conn, esc, clock, out)
    return search, esc
