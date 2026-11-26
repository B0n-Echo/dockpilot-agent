"""Which slots can this late truck have? The option search (decision D5).

It builds sensible candidates (one slot, or the fewest back-to-back slots on one dock
that cover the unload) and asks feasibility.check_feasibility about each, so there is
exactly one place that decides what "feasible" means. Read only: nothing is held here.
"""

from dataclasses import dataclass
from datetime import datetime

from dockpilot import trace
from dockpilot.clock import to_iso
from dockpilot.feasibility import check_feasibility
from dockpilot.settings import SETTINGS


@dataclass(frozen=True)
class Option:
    """One thing we could offer the driver.

    Attributes:
        slot_ids: one slot, or back-to-back slots on one dock, in time order.
        dock_id: e.g. "DOCK-JAI-D2".
        dock_code: e.g. "D2", for messages.
        start: when the first slot starts.
        end: when the last slot ends.
    """

    slot_ids: list
    dock_id: str
    dock_code: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class OptionSearch:
    """The result of find_options.

    Attributes:
        options: the best feasible Options, best first, at most settings.max_options.
        checked: every candidate looked at, as a Feasibility (pass or fail, with reasons).
    """

    options: list
    checked: list


def _minutes(run):
    """Return how many minutes a run of back-to-back slot rows covers, first start to last end."""
    start = datetime.fromisoformat(run[0]["slot_start_ts"])
    end = datetime.fromisoformat(run[-1]["slot_end_ts"])
    return int((end - start).total_seconds() // 60)


@trace.traced("slot search", show_in=lambda conn, shipment_id, *a, **k: shipment_id,
              show_out=lambda r: f"{len(r.checked)} candidates checked · {len(r.options)} fit"
              + (f" · best {r.options[0].dock_code} {r.options[0].start:%H:%M}" if r.options else ""))
def find_options(conn, shipment_id, clock, settings=SETTINGS):
    """Find the best slots a shipment can use from now on.

    Looks at slots at the shipment's destination facility that start at or after now.
    For each slot it takes the fewest back-to-back slots on the same dock that cover the
    unload time (or the slot alone if no such run exists), checks the candidate with
    check_feasibility, and ranks the ones that pass: earliest start, then least wasted
    dock time, then dock code.

    Args:
        conn: open database connection.
        shipment_id: e.g. "SHP1014".
        clock: gives "now".
        settings: tunable values; max_options limits how many options come back.

    Returns:
        An OptionSearch with the ranked options and every checked candidate.

    Raises:
        LookupError: if the shipment does not exist.
    """
    ship = conn.execute(
        "SELECT destination_facility_id, expected_unload_min FROM shipments WHERE shipment_id = ?",
        (shipment_id,),
    ).fetchone()
    if ship is None:
        raise LookupError(f"Unknown shipment {shipment_id}")
    unload = ship["expected_unload_min"]

    rows = conn.execute(
        """SELECT sl.slot_id, sl.dock_id, d.dock_code, sl.slot_start_ts, sl.slot_end_ts
           FROM appointment_slots sl JOIN docks d ON d.dock_id = sl.dock_id
           WHERE sl.facility_id = ? AND sl.slot_start_ts >= ?
           ORDER BY sl.dock_id, sl.slot_start_ts""",
        (ship["destination_facility_id"], to_iso(clock.now())),
    ).fetchall()
    by_dock = {}
    for row in rows:
        by_dock.setdefault(row["dock_id"], []).append(row)

    candidates = []
    for dock_slots in by_dock.values():
        for i, first in enumerate(dock_slots):
            run = [first]
            j = i + 1
            while (_minutes(run) < unload and j < len(dock_slots)
                   and dock_slots[j]["slot_start_ts"] == run[-1]["slot_end_ts"]):
                run.append(dock_slots[j])
                j += 1
            if _minutes(run) < unload:
                run = [first]  # can't be covered: the check will say UNLOAD_TOO_LONG
            candidates.append(run)

    checked, passed = [], []
    for run in candidates:
        result = check_feasibility(conn, shipment_id, [s["slot_id"] for s in run], clock, settings)
        checked.append(result)
        if result.feasible:
            passed.append(
                Option(
                    slot_ids=result.slot_ids,
                    dock_id=run[0]["dock_id"],
                    dock_code=run[0]["dock_code"],
                    start=datetime.fromisoformat(run[0]["slot_start_ts"]),
                    end=datetime.fromisoformat(run[-1]["slot_end_ts"]),
                )
            )

    passed.sort(key=lambda o: (o.start, (o.end - o.start).total_seconds() // 60 - unload, o.dock_code))
    return OptionSearch(options=passed[: settings.max_options], checked=checked)
