"""Can this truck use this slot? The feasibility check (decision D4).

Plain code decides, never the AI. The check reads the database, changes nothing, and
returns every reason a slot fails, each with a short code and a plain-English message.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from dockpilot.clock import to_iso
from dockpilot.settings import SETTINGS

LIVE_APPOINTMENT = ("PENDING_CONFIRMATION", "CONFIRMED", "IN_PROGRESS")
"""Appointment statuses that still occupy a slot."""

BLOCKING_DOCK_EVENTS = ("MAINTENANCE", "BREAKDOWN", "CAPACITY_REDUCTION", "MANUAL_BLOCK")
"""Dock event types that make an overlapping slot unusable. Only REOPENED does not block."""


@dataclass(frozen=True)
class Reason:
    """One failed check.

    Attributes:
        code: short fixed code that code can test against, e.g. "OVER_WEIGHT_LIMIT".
        message: plain-English explanation with the real numbers, for people to read.
    """

    code: str
    message: str


@dataclass(frozen=True)
class Feasibility:
    """The answer of check_feasibility.

    Attributes:
        feasible: True when every check passed.
        reasons: every failed check, in the order found; empty when feasible.
        slot_ids: the slots that were checked, in time order.
    """

    feasible: bool
    reasons: list
    slot_ids: list


def _ts(text):
    """Turn a timestamp text from the database into a real datetime."""
    return datetime.fromisoformat(text)


def _hhmm(dt):
    """Format a datetime as HH:MM for messages."""
    return dt.strftime("%H:%M")


def latest_eta(conn, shipment_id, clock):
    """Return a shipment's best-known arrival time as of the clock's "now".

    Works like the client's v_latest_eta view, but ignores ETA updates logged after now,
    so a replay never "knows the future". Without any update it falls back to the
    shipment's own ETA fields, as the view does: latest_eta_ts (source "SHIPMENT_RECORD")
    or else original_eta_ts (source "ORIGINAL_PLAN").

    Args:
        conn: open database connection.
        shipment_id: e.g. "SHP1014".
        clock: a SystemClock or FakeClock.

    Returns:
        A tuple (eta datetime, source text, confidence text).

    Raises:
        LookupError: if the shipment does not exist.
    """
    row = conn.execute(
        """SELECT declared_eta_ts, source_type, confidence_code FROM eta_updates
           WHERE shipment_id = ? AND created_at <= ?
           ORDER BY created_at DESC, eta_update_id DESC LIMIT 1""",
        (shipment_id, to_iso(clock.now())),
    ).fetchone()
    if row:
        return _ts(row["declared_eta_ts"]), row["source_type"], row["confidence_code"]
    ship = conn.execute(
        "SELECT latest_eta_ts, original_eta_ts FROM shipments WHERE shipment_id = ?", (shipment_id,)
    ).fetchone()
    if ship is None:
        raise LookupError(f"Unknown shipment {shipment_id}")
    if ship["latest_eta_ts"]:
        return _ts(ship["latest_eta_ts"]), "SHIPMENT_RECORD", "HIGH"
    return _ts(ship["original_eta_ts"]), "ORIGINAL_PLAN", "HIGH"


def _facility_rules(conn, facility_id, today):
    """Return a facility's switched-on, in-effect rules as {rule_type: rule_value}.

    Args:
        conn: open database connection.
        facility_id: e.g. "FAC-JAI-01".
        today: the date the rules must be in effect on.
    """
    day = today.isoformat()
    rows = conn.execute(
        """SELECT rule_type, rule_value FROM facility_rules
           WHERE facility_id = ? AND active_flag = 1 AND effective_from <= ?
             AND (effective_to IS NULL OR effective_to = '' OR effective_to >= ?)""",
        (facility_id, day, day),
    )
    return {r["rule_type"]: r["rule_value"] for r in rows}


def check_feasibility(conn, shipment_id, slot_ids, clock, settings=SETTINGS):
    """Check whether a shipment can use one slot, or several back-to-back slots.

    Runs every check (facility, dock type, refrigeration, heavy dock, weight, arrival,
    unload length, slot open, dock active, dock events, opening hours, last start,
    taken, held) and collects all failures. The shipment's own bookings and holds are
    ignored by the taken/held checks. Nothing is written.

    Args:
        conn: open database connection.
        shipment_id: e.g. "SHP1014".
        slot_ids: list of one or more slot ids, e.g. ["SLOT-JAI-004"].
        clock: gives "now" (for arrival, hold expiry and which ETA updates count).
        settings: tunable values; eta_buffer_minutes is added to a LOW-confidence ETA.

    Returns:
        A Feasibility with feasible, reasons and slot_ids.

    Raises:
        LookupError: if the shipment or any slot does not exist.
        ValueError: if slot_ids is empty.
    """
    if not slot_ids:
        raise ValueError("slot_ids must name at least one slot")
    now = clock.now()
    now_text = to_iso(now)

    ship = conn.execute("SELECT * FROM shipments WHERE shipment_id = ?", (shipment_id,)).fetchone()
    if ship is None:
        raise LookupError(f"Unknown shipment {shipment_id}")
    slots = []
    for slot_id in slot_ids:
        row = conn.execute(
            """SELECT sl.*, d.dock_code, d.dock_type, d.supports_refrigerated,
                      d.max_vehicle_weight_kg, d.dock_status
               FROM appointment_slots sl JOIN docks d ON d.dock_id = sl.dock_id
               WHERE sl.slot_id = ?""",
            (slot_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"Unknown slot {slot_id}")
        slots.append(row)
    slots.sort(key=lambda s: _ts(s["slot_start_ts"]))
    fac = conn.execute(
        "SELECT * FROM facilities WHERE facility_id = ?", (ship["destination_facility_id"],)
    ).fetchone()
    rules = _facility_rules(conn, fac["facility_id"], now.date())
    start, end = _ts(slots[0]["slot_start_ts"]), _ts(slots[-1]["slot_end_ts"])

    found = {}  # code -> list of messages; one Reason per code at the end

    def fail(code, message):
        """Record one failed check, merging repeats of the same code."""
        msgs = found.setdefault(code, [])
        if message not in msgs:
            msgs.append(message)

    # 1. Back-to-back on one dock (only for two or more slots).
    for prev, nxt in zip(slots, slots[1:]):
        if nxt["dock_id"] != prev["dock_id"] or nxt["slot_start_ts"] != prev["slot_end_ts"]:
            fail("SLOTS_NOT_BACK_TO_BACK",
                 f"{prev['slot_id']} and {nxt['slot_id']} are not back-to-back on one dock.")

    heavy_kg = rules.get("HEAVY_DOCK_REQUIRED_KG")
    for sl in slots:
        dock = f"dock {sl['dock_code']}"
        # 2-6. Facility, dock type, refrigeration, heavy dock, weight.
        if sl["facility_id"] != ship["destination_facility_id"]:
            fail("WRONG_FACILITY",
                 f"{sl['slot_id']} is at {sl['facility_id']}; the truck goes to {ship['destination_facility_id']}.")
        if ship["required_dock_type"] != "ANY" and sl["dock_type"] != ship["required_dock_type"]:
            fail("WRONG_DOCK_TYPE",
                 f"Load needs a {ship['required_dock_type']} dock; {dock} is {sl['dock_type']}.")
        if ship["temperature_control_required"] and not sl["supports_refrigerated"]:
            fail("NEEDS_REEFER", f"Load needs refrigeration; {dock} has none.")
        if heavy_kg and ship["load_weight_kg"] > int(heavy_kg) and sl["dock_type"] != "HEAVY":
            fail("NEEDS_HEAVY_DOCK",
                 f"Loads above {int(heavy_kg):,} kg must use the heavy dock; this one is {ship['load_weight_kg']:,} kg.")
        if ship["load_weight_kg"] > sl["max_vehicle_weight_kg"]:
            fail("OVER_WEIGHT_LIMIT",
                 f"Load is {ship['load_weight_kg']:,} kg; {dock} takes at most {sl['max_vehicle_weight_kg']:,} kg.")
        # 9-11. Slot open, dock active, no dock problem.
        if sl["slot_status"] != "OPEN":
            fail("SLOT_NOT_OPEN", f"{sl['slot_id']} is {sl['slot_status']}.")
        if sl["dock_status"] != "ACTIVE":
            fail("DOCK_NOT_ACTIVE", f"{dock} is {sl['dock_status']}.")
        placeholders = ",".join("?" * len(BLOCKING_DOCK_EVENTS))
        for ev in conn.execute(
            f"""SELECT dock_event_id, event_type, reason, event_start_ts, event_end_ts
                FROM dock_status_events WHERE dock_id = ? AND event_type IN ({placeholders})""",
            (sl["dock_id"], *BLOCKING_DOCK_EVENTS),
        ):
            ev_start = _ts(ev["event_start_ts"])
            ev_end = _ts(ev["event_end_ts"]) if ev["event_end_ts"] else None
            if ev_start < _ts(sl["slot_end_ts"]) and (ev_end is None or ev_end > _ts(sl["slot_start_ts"])):
                fail("DOCK_EVENT",
                     f"{dock} has a {ev['event_type'].lower().replace('_', ' ')} ({ev['dock_event_id']}: {ev['reason']}) during {sl['slot_id']}.")
        # 14. Taken by another shipment: live booking, or converted hold whose booking is live.
        placeholders = ",".join("?" * len(LIVE_APPOINTMENT))
        taken = conn.execute(
            f"""SELECT appointment_id, shipment_id FROM appointments
                WHERE slot_id = ? AND shipment_id <> ? AND appointment_status IN ({placeholders})
                UNION
                SELECT a.appointment_id, h.shipment_id FROM slot_holds h
                JOIN appointments a ON a.appointment_id = h.appointment_id
                WHERE h.slot_id = ? AND h.shipment_id <> ? AND h.hold_status = 'CONVERTED'
                  AND a.appointment_status IN ({placeholders})""",
            (sl["slot_id"], shipment_id, *LIVE_APPOINTMENT, sl["slot_id"], shipment_id, *LIVE_APPOINTMENT),
        ).fetchone()
        if taken:
            fail("SLOT_TAKEN",
                 f"{sl['slot_id']} is already booked for {taken['shipment_id']} ({taken['appointment_id']}).")
        # 15. Held by another shipment, and the hold has not expired yet.
        held = conn.execute(
            """SELECT hold_id, shipment_id, expires_at FROM slot_holds
               WHERE slot_id = ? AND shipment_id <> ? AND hold_status = 'ACTIVE'""",
            (sl["slot_id"], shipment_id),
        ).fetchall()
        for h in held:
            if _ts(h["expires_at"]) > now:
                fail("SLOT_HELD",
                     f"{sl['slot_id']} is held for {h['shipment_id']} until {_hhmm(_ts(h['expires_at']))}.")

    # 7. Arrival: the later of now and the ETA (plus the buffer when the ETA is LOW confidence).
    eta, _source, confidence = latest_eta(conn, shipment_id, clock)
    arrival = eta
    buffer_note = ""
    if confidence == "LOW":
        arrival = eta + timedelta(minutes=settings.eta_buffer_minutes)
        buffer_note = f" (ETA {_hhmm(eta)} is LOW confidence, plus {settings.eta_buffer_minutes} min buffer)"
    arrival = max(now, arrival)
    if arrival > start:
        fail("ARRIVES_AFTER_START",
             f"Truck can be there at {_hhmm(arrival)}{buffer_note}; the slot starts {_hhmm(start)}.")

    # 8. Unload fits in the slot(s).
    slot_minutes = int((end - start).total_seconds() // 60)
    unload_end = start + timedelta(minutes=ship["expected_unload_min"])
    if unload_end > end:
        fail("UNLOAD_TOO_LONG",
             f"Unloading takes {ship['expected_unload_min']} min; the slot gives {slot_minutes} min.")

    # 12. Opening hours.
    opens, closes = time.fromisoformat(fac["open_time"]), time.fromisoformat(fac["close_time"])
    if start.time() < opens or unload_end.time() > closes or unload_end.date() > start.date():
        fail("OUTSIDE_HOURS",
             f"Unloading {_hhmm(start)}-{_hhmm(unload_end)} is outside opening hours {fac['open_time']}-{fac['close_time']}.")

    # 13. Last start time (a start at exactly the rule's time is allowed: audit Q8).
    last_start = rules.get("LAST_NEW_START_TIME")
    if last_start and start.time() > time.fromisoformat(last_start):
        fail("AFTER_LAST_START",
             f"Slot starts {_hhmm(start)}; new unloading after {last_start} needs manual approval.")

    reasons = [Reason(code, " ".join(msgs)) for code, msgs in found.items()]
    return Feasibility(feasible=not reasons, reasons=reasons, slot_ids=[s["slot_id"] for s in slots])
