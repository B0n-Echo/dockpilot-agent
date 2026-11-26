"""Who gets the slot when trucks compete (D7): first come first served, a 30-second ranking window in a rush,
and a coordinator alert when a CRITICAL load waits behind a lower priority. Plain code, no AI."""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from dockpilot import trace
from dockpilot.clock import to_iso
from dockpilot.escalations import create_escalation, deliver
from dockpilot.feasibility import LIVE_APPOINTMENT, check_feasibility, latest_eta
from dockpilot.holds import get_hold, place_hold
from dockpilot.options import find_options
from dockpilot.settings import SETTINGS

RANK = {"CRITICAL": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3}
"""Priority order (R3): lower number goes first."""


@dataclass(frozen=True)
class RequestResult:
    """Answer to "may this truck have these slots?": HELD, WAITING or REFUSED, plus the hold and any CRITICAL alert."""

    status: str
    hold: object = None
    reasons: tuple = ()
    request_id: str | None = None
    alert: object = None


@dataclass(frozen=True)
class Decision:
    """How one waiting request ended when the window closed: WON, NEAR_TIE or LOST, with the reason for a loser."""

    request_id: str
    shipment_id: str
    thread_id: str | None
    status: str
    hold: object = None
    reason: str | None = None
    escalation_id: str | None = None


def _ship(conn, shipment_id):
    """The shipment row."""
    return conn.execute("SELECT * FROM shipments WHERE shipment_id = ?", (shipment_id,)).fetchone()


def _label(conn, slot_ids):
    """Slots in words, e.g. "D2 12:00–14:00"."""
    first = conn.execute("SELECT d.dock_code, sl.slot_start_ts FROM appointment_slots sl JOIN docks d "
                         "ON d.dock_id = sl.dock_id WHERE sl.slot_id = ?", (slot_ids[0],)).fetchone()
    end = conn.execute("SELECT slot_end_ts FROM appointment_slots WHERE slot_id = ?", (slot_ids[-1],)).fetchone()[0]
    return f"{first['dock_code']} {first['slot_start_ts'][11:16]}–{end[11:16]}"


def harm(conn, shipment_id, clock):
    """Harm already done (R3): minutes later than the original plan, never below 0."""
    eta = latest_eta(conn, shipment_id, clock)[0]
    plan = datetime.fromisoformat(_ship(conn, shipment_id)["original_eta_ts"])
    return max(0, int((eta - plan).total_seconds() // 60))


def is_spike(conn, facility_id, clock, settings=SETTINGS):
    """True when the facility had spike_request_count or more slot requests in the last spike_window_minutes."""
    now = clock.now()
    n = conn.execute("SELECT COUNT(*) FROM slot_requests WHERE facility_id = ? AND requested_at > ? AND requested_at <= ?",
                     (facility_id, to_iso(now - timedelta(minutes=settings.spike_window_minutes)), to_iso(now))).fetchone()[0]
    return n >= settings.spike_request_count


def _decide(conn, request_id, status, clock, hold=None, reason=None, escalation_id=None):
    """Write how a request ended."""
    conn.execute("UPDATE slot_requests SET status = ?, decided_at = ?, hold_group = ?, reason = ?, escalation_id = ? "
                 "WHERE request_id = ?", (status, to_iso(clock.now()), hold and hold.group_id, reason, escalation_id,
                                          request_id))


@trace.traced("who gets it (D7)", "decision",
              show_in=lambda conn, shipment_id, slot_ids, *a, **k: f"{shipment_id} {','.join(slot_ids)}",
              show_out=lambda r: {"HELD": "normal day: first come, first served → held",
                                  "WAITING": "rush (spike): wait 30 s for rivals"}.get(r.status, r.status)
              + (" + CRITICAL alert" if r.alert else ""))
def request_slot(conn, shipment_id, slot_ids, request_key, clock, settings=SETTINGS, thread_id=None):
    """The agent's one door to a slot: hold now on a normal day (first wins), or wait for rivals in a spike.
    A CRITICAL load that gets a hold is checked by critical_alert. The same request_key gives the same answer."""
    old = conn.execute("SELECT * FROM slot_requests WHERE request_key = ?", (request_key,)).fetchone()
    if old:
        hold = get_hold(conn, old["hold_group"]) if old["hold_group"] else None
        return RequestResult(old["status"], hold, (), old["request_id"], old["escalation_id"])
    ship, rid = _ship(conn, shipment_id), f"REQ-{uuid.uuid4().hex[:12]}"
    conn.execute("INSERT INTO slot_requests (request_id, shipment_id, facility_id, slot_ids, thread_id, request_key, "
                 "requested_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'WAITING')",
                 (rid, shipment_id, ship["destination_facility_id"], json.dumps(slot_ids), thread_id, request_key,
                  to_iso(clock.now())))
    if is_spike(conn, ship["destination_facility_id"], clock, settings):
        return RequestResult("WAITING", request_id=rid)
    res = place_hold(conn, shipment_id, slot_ids, request_key, clock, settings, thread_id=thread_id)
    if not res.placed:
        _decide(conn, rid, "REFUSED", clock, reason=res.reasons[0].message if res.reasons else None)
        return RequestResult("REFUSED", None, tuple(res.reasons), rid)
    alert = critical_alert(conn, shipment_id, res.hold, clock, settings) if ship["priority_code"] == "CRITICAL" else None
    _decide(conn, rid, "HELD", clock, res.hold, escalation_id=alert and alert.escalation_id)
    return RequestResult("HELD", res.hold, (), rid, alert and alert.escalation_id)


@trace.traced("settle rush-hour rivals", "timer",
              show_out=lambda ds: ", ".join(f"{d.shipment_id} {d.status}" for d in ds))
def settle_requests(conn, clock, settings=SETTINGS):
    """Close every collection window that is 30 s old: R0 feasible only, R3 rank (priority, harm, first asked),
    R4 near-tie -> first asked keeps it plus a NEAR_TIE escalation. Returns one Decision per waiting request."""
    cutoff = to_iso(clock.now() - timedelta(seconds=settings.collection_window_seconds))
    groups = {}
    for r in conn.execute("SELECT * FROM slot_requests WHERE status = 'WAITING' ORDER BY requested_at, rowid"):
        groups.setdefault(json.loads(r["slot_ids"])[0], []).append(r)
    decisions = []
    for group in groups.values():
        if group[0]["requested_at"] > cutoff:
            continue
        ranked, info = [], {}
        for r in group:
            check = check_feasibility(conn, r["shipment_id"], json.loads(r["slot_ids"]), clock, settings)
            if not check.feasible:  # R0
                _decide(conn, r["request_id"], "LOST", clock, reason=check.reasons[0].message)
                decisions.append(Decision(r["request_id"], r["shipment_id"], r["thread_id"], "LOST",
                                          reason=check.reasons[0].message))
                continue
            info[r["request_id"]] = (RANK[_ship(conn, r["shipment_id"])["priority_code"]], harm(conn, r["shipment_id"], clock))
            ranked.append(r)
        ranked.sort(key=lambda r: (info[r["request_id"]][0], -info[r["request_id"]][1], r["requested_at"]))  # R3
        if len(ranked) > 1 and _near(info, ranked[0], ranked[1], settings) and ranked[1]["requested_at"] < ranked[0]["requested_at"]:
            ranked[0], ranked[1] = ranked[1], ranked[0]  # R4: in a near-tie the first to ask goes first
        winner = None
        for r in ranked:
            res = None if winner else place_hold(conn, r["shipment_id"], json.loads(r["slot_ids"]), r["request_key"],
                                                 clock, settings, spike=True, thread_id=r["thread_id"])
            if res and res.placed:
                winner = (r, res.hold)
                continue
            label = _label(conn, json.loads(r["slot_ids"]))
            if winner is None:
                why = res.reasons[0].message if res and res.reasons else f"{label} was just taken."
            else:
                (wp, wh), (lp, lh) = info[winner[0]["request_id"]], info[r["request_id"]]
                why = (f"{label} went to a higher-priority load." if wp < lp else
                       f"{label} went to a truck that is already later than you." if wh > lh + settings.near_tie_minutes
                       else f"{label} went to a truck that asked a moment before you.")
            _decide(conn, r["request_id"], "LOST", clock, reason=why)
            decisions.append(Decision(r["request_id"], r["shipment_id"], r["thread_id"], "LOST", reason=why))
        if winner:
            decisions.append(_win(conn, winner, ranked, info, clock, settings))
    return decisions


def _near(info, a, b, settings):
    """Near-tie (R4): same priority and harm within near_tie_minutes."""
    (pa, ha), (pb, hb) = info[a["request_id"]], info[b["request_id"]]
    return pa == pb and abs(ha - hb) <= settings.near_tie_minutes


def _win(conn, winner, ranked, info, clock, settings):
    """Record the winner: NEAR_TIE (with escalation) if the runner-up is the same priority within near_tie_minutes."""
    r, hold = winner
    rest = [x for x in ranked if x["request_id"] != r["request_id"]]
    (wp, wh), esc = info[r["request_id"]], None
    if rest and _near(info, r, rest[0], settings):
        other = rest[0]
        esc = create_escalation(
            conn, "NEAR_TIE", r["shipment_id"], clock, thread_id=r["thread_id"],
            summary=f"Near-tie for {_label(conn, hold.slot_ids)}: {r['shipment_id']} ({wh} min late, asked "
                    f"{r['requested_at'][11:19]}) got it over {other['shipment_id']} ({info[other['request_id']][1]} min "
                    f"late, asked {other['requested_at'][11:19]}). Same priority; swap if you see fit.")
        if esc.delivery_status != "DELIVERED":
            esc = deliver(conn, esc, clock)
    status = "NEAR_TIE" if esc else "WON"
    alert = critical_alert(conn, r["shipment_id"], hold, clock, settings) if wp == RANK["CRITICAL"] else None
    _decide(conn, r["request_id"], status, clock, hold, escalation_id=(esc or alert) and (esc or alert).escalation_id)
    return Decision(r["request_id"], r["shipment_id"], r["thread_id"], status, hold,
                    escalation_id=esc and esc.escalation_id)


def _blockers(conn, slot_ids, shipment_id, clock):
    """Who occupies these slots (other than this shipment): (shipment, priority, HELD or booking status)."""
    q = ",".join("?" * len(slot_ids))
    live = ",".join("?" * len(LIVE_APPOINTMENT))
    return conn.execute(
        f"""SELECT h.shipment_id, s.priority_code, 'HELD' FROM slot_holds h JOIN shipments s ON s.shipment_id = h.shipment_id
            WHERE h.slot_id IN ({q}) AND h.shipment_id <> ? AND h.hold_status = 'ACTIVE' AND h.expires_at > ?
            UNION SELECT a.shipment_id, s.priority_code, a.appointment_status FROM appointments a
            JOIN shipments s ON s.shipment_id = a.shipment_id
            WHERE a.slot_id IN ({q}) AND a.shipment_id <> ? AND a.appointment_status IN ({live})
            UNION SELECT h.shipment_id, s.priority_code, a.appointment_status FROM slot_holds h
            JOIN appointments a ON a.appointment_id = h.appointment_id JOIN shipments s ON s.shipment_id = h.shipment_id
            WHERE h.slot_id IN ({q}) AND h.shipment_id <> ? AND h.hold_status = 'CONVERTED' AND a.appointment_status IN ({live})""",
        (*slot_ids, shipment_id, to_iso(clock.now()), *slot_ids, shipment_id, *LIVE_APPOINTMENT,
         *slot_ids, shipment_id, *LIVE_APPOINTMENT)).fetchall()


def critical_alert(conn, shipment_id, hold, clock, settings=SETTINGS):
    """A CRITICAL load holds `hold`, but an earlier slot it could use is held or PENDING for a lower priority:
    open one CRITICAL_WAITING escalation naming them. Confirmed bookings are locked (R1). Moves nothing."""
    start = conn.execute("SELECT slot_start_ts FROM appointment_slots WHERE slot_id = ?", (hold.slot_ids[0],)).fetchone()[0]
    found = []
    for f in find_options(conn, shipment_id, clock, settings).checked:
        codes = {r.code for r in f.reasons}
        if not codes or not codes <= {"SLOT_HELD", "SLOT_TAKEN"}:
            continue
        f_start = conn.execute("SELECT slot_start_ts FROM appointment_slots WHERE slot_id = ?", (f.slot_ids[0],)).fetchone()[0]
        who = _blockers(conn, f.slot_ids, shipment_id, clock)
        if f_start < start and who and all(RANK[p] > RANK["CRITICAL"] and k in ("HELD", "PENDING_CONFIRMATION")
                                           for _, p, k in who):
            found.append((f, who))
    if not found:
        return None
    named = "; ".join(f"{_label(conn, f.slot_ids)} ({', '.join(f'{s} {p}, {k.lower().replace('_confirmation', '')}' for s, p, k in who)})"
                      for f, who in found)
    esc = create_escalation(
        conn, "CRITICAL_WAITING", shipment_id, clock, options_checked=[f for f, _ in found],
        summary=f"CRITICAL {shipment_id} holds {_label(conn, hold.slot_ids)}. Earlier slots it could use are taken by "
                f"lower priority, not yet confirmed: {named}. Nothing moved; a coordinator may reassign (R1, R6).")
    return esc if esc.delivery_status == "DELIVERED" else deliver(conn, esc, clock)
