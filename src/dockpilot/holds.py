"""Placing and expiring slot holds (decisions D5, D6).

A hold reserves one slot, or two back-to-back slots, for a driver while they decide:
10 minutes normally, 5 in a spike. Every hold is placed inside one write transaction:
tidy expired holds, check the slot is still free, write. Only one writer runs at a
time, so two drivers can never both win the same slot.
"""

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from dockpilot.clock import to_iso
from dockpilot.db import write_transaction
from dockpilot.feasibility import Reason, check_feasibility
from dockpilot.settings import SETTINGS


@dataclass(frozen=True)
class Hold:
    """One hold group as the rest of the code sees it.

    Attributes:
        group_id: shared by the one or two slot_holds rows of this hold.
        slot_ids: the held slots, in time order.
        shipment_id: who holds it.
        status: ACTIVE, CONVERTED or EXPIRED.
        created_at: when it was placed.
        expires_at: when it runs out.
    """

    group_id: str
    slot_ids: list
    shipment_id: str
    status: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class HoldResult:
    """The answer to "please hold this".

    Attributes:
        placed: True when the shipment now has (or already had) this hold.
        hold: the Hold, or None when refused.
        replayed: True when the request key was seen before and nothing new was written.
        reasons: why it was refused (dp-04 Reason objects); empty when placed.
    """

    placed: bool
    hold: object
    replayed: bool
    reasons: list


class _Refused(Exception):
    """Internal: raised inside the transaction to roll it back and refuse with a reason."""

    def __init__(self, reason):
        """Keep the Reason to return to the caller."""
        super().__init__(reason.message)
        self.reason = reason


def _new_id(prefix):
    """Return a fresh unique id such as "HOLD-3f9a1c2b7d4e"."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _log(conn, clock, event_type, hold_id, shipment_id, actor, payload):
    """Write one line to the events diary for a hold row."""
    conn.execute(
        """INSERT INTO events (event_ts, event_type, entity_type, entity_id, shipment_id, actor, payload)
           VALUES (?, ?, 'HOLD', ?, ?, ?, ?)""",
        (to_iso(clock.now()), event_type, hold_id, shipment_id, actor, json.dumps(payload)),
    )


def get_hold(conn, group_id):
    """Read a hold group back from the database.

    Args:
        conn: open database connection.
        group_id: the hold's group id.

    Returns:
        A Hold with its slots in time order.

    Raises:
        LookupError: if no hold has this group id.
    """
    rows = conn.execute(
        """SELECT h.* FROM slot_holds h JOIN appointment_slots sl ON sl.slot_id = h.slot_id
           WHERE h.group_id = ? ORDER BY sl.slot_start_ts""",
        (group_id,),
    ).fetchall()
    if not rows:
        raise LookupError(f"Unknown hold group {group_id}")
    first = rows[0]
    return Hold(
        group_id=group_id,
        slot_ids=[r["slot_id"] for r in rows],
        shipment_id=first["shipment_id"],
        status=first["hold_status"],
        created_at=datetime.fromisoformat(first["created_at"]),
        expires_at=datetime.fromisoformat(first["expires_at"]),
    )


def _expire(conn, clock):
    """Mark every ACTIVE hold past its expiry time as EXPIRED; must run inside a transaction.

    Returns:
        How many hold rows were expired.
    """
    rows = conn.execute(
        "SELECT hold_id, shipment_id, slot_id FROM slot_holds WHERE hold_status = 'ACTIVE' AND expires_at <= ?",
        (to_iso(clock.now()),),
    ).fetchall()
    for r in rows:
        conn.execute("UPDATE slot_holds SET hold_status = 'EXPIRED' WHERE hold_id = ?", (r["hold_id"],))
        _log(conn, clock, "HOLD_EXPIRED", r["hold_id"], r["shipment_id"], "TIMER", {"slot_id": r["slot_id"]})
    return len(rows)


def expire_holds(conn, clock):
    """Expire every hold whose time is up, and write a HOLD_EXPIRED event for each.

    Opens its own write transaction, unless the connection is already inside one.

    Args:
        conn: open database connection.
        clock: gives "now".

    Returns:
        How many hold rows were expired.
    """
    if conn.in_transaction:
        return _expire(conn, clock)
    with write_transaction(conn):
        return _expire(conn, clock)


def place_hold(conn, shipment_id, slot_ids, request_key, clock, settings=SETTINGS, spike=False, thread_id=None):
    """Hold one slot, or back-to-back slots, for a shipment, safely.

    All in one write transaction: return the earlier hold if this request key was seen
    before; expire overdue holds; re-check feasibility (dp-04); release this shipment's
    other active holds; write the new hold rows and their events. If the database's
    one-active-hold-per-slot index still refuses the insert, everything is rolled back
    and the request is refused with SLOT_HELD.

    Args:
        conn: a connection from db.connect().
        shipment_id: e.g. "SHP1014".
        slot_ids: list of one or more slot ids, e.g. ["SLOT-JAI-019", "SLOT-JAI-020"].
        request_key: idempotency key of the request; the same key returns the same hold.
        clock: gives "now".
        settings: tunable values (hold_minutes, spike_hold_minutes, eta_buffer_minutes).
        spike: True to use the shorter spike-mode hold (spike detection is dp-10).
        thread_id: optional chat thread the request came from.

    Returns:
        A HoldResult.

    Raises:
        ValueError: if the request key was used before for a different shipment or slots.
        LookupError: if the shipment or a slot does not exist.
    """
    try:
        with write_transaction(conn):
            existing = conn.execute(
                "SELECT group_id FROM slot_holds WHERE request_key = ?", (f"{request_key}:1",)
            ).fetchone()
            if existing:
                hold = get_hold(conn, existing["group_id"])
                asked = check_feasibility(conn, shipment_id, slot_ids, clock, settings).slot_ids
                if hold.shipment_id != shipment_id or hold.slot_ids != asked:
                    raise ValueError(f"Request key {request_key!r} was already used for a different request")
                return HoldResult(placed=True, hold=hold, replayed=True, reasons=[])

            _expire(conn, clock)
            check = check_feasibility(conn, shipment_id, slot_ids, clock, settings)
            if not check.feasible:
                return HoldResult(placed=False, hold=None, replayed=False, reasons=check.reasons)

            for old in conn.execute(
                "SELECT hold_id, slot_id FROM slot_holds WHERE shipment_id = ? AND hold_status = 'ACTIVE'",
                (shipment_id,),
            ).fetchall():
                conn.execute("UPDATE slot_holds SET hold_status = 'EXPIRED' WHERE hold_id = ?", (old["hold_id"],))
                _log(conn, clock, "HOLD_RELEASED", old["hold_id"], shipment_id, "AGENT",
                     {"slot_id": old["slot_id"], "why": "shipment placed a new hold"})

            driver_id = conn.execute(
                "SELECT driver_id FROM shipments WHERE shipment_id = ?", (shipment_id,)
            ).fetchone()["driver_id"]
            now = clock.now()
            minutes = settings.spike_hold_minutes if spike else settings.hold_minutes
            expires = now + timedelta(minutes=minutes)
            group_id = _new_id("HGRP")
            try:
                for n, slot_id in enumerate(check.slot_ids, start=1):
                    hold_id = _new_id("HOLD")
                    conn.execute(
                        """INSERT INTO slot_holds (hold_id, group_id, slot_id, shipment_id, driver_id, thread_id,
                                                   hold_status, created_at, expires_at, request_key)
                           VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?)""",
                        (hold_id, group_id, slot_id, shipment_id, driver_id, thread_id,
                         to_iso(now), to_iso(expires), f"{request_key}:{n}"),
                    )
                    _log(conn, clock, "HOLD_PLACED", hold_id, shipment_id, "AGENT",
                         {"slot_id": slot_id, "group_id": group_id, "expires_at": to_iso(expires), "spike": spike})
            except sqlite3.IntegrityError as exc:
                if "slot_holds.slot_id" not in str(exc):
                    raise
                raise _Refused(Reason("SLOT_HELD", "Another driver took this slot a moment ago.")) from exc
            return HoldResult(placed=True, hold=get_hold(conn, group_id), replayed=False, reasons=[])
    except _Refused as refused:
        return HoldResult(placed=False, hold=None, replayed=False, reasons=[refused.reason])
