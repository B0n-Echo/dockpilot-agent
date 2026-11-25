"""The integrity proof (dp-11, D13, M1): many programs race for one slot; a check query proves nobody double-booked.

    uv run python -m dockpilot.integrity --rounds 100            # the proof
    uv run python -m dockpilot.integrity --rounds 20 --naive     # smoke alarm: a careless version must get caught
"""

import argparse
import tempfile
import time
import uuid
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path

from dockpilot import db
from dockpilot.bookings import convert_hold
from dockpilot.clock import IST, FakeClock, to_iso
from dockpilot.feasibility import LIVE_APPOINTMENT
from dockpilot.holds import place_hold

RACE_TIME = datetime(2026, 8, 4, 9, 45, tzinfo=IST)
"""Every racer's clock: the snapshot time."""

RACE_SLOT = ["SLOT-JAI-019"]
"""D2 12:00: the slot everyone wants."""

RACERS = [("SHP1006", RACE_SLOT), ("SHP1007", RACE_SLOT), ("SHP1012", RACE_SLOT), ("SHP1013", RACE_SLOT),
          ("SHP1009", ["SLOT-JAI-019", "SLOT-JAI-020"]), ("SHP1014", ["SLOT-JAI-019", "SLOT-JAI-020"]),
          ("SHP1012", RACE_SLOT), ("SHP1013", RACE_SLOT)]
"""Six real trucks that can all use D2 12:00 (two need 12:00–14:00), plus two duplicates sent with the same key."""

LIVE = ",".join(f"'{s}'" for s in LIVE_APPOINTMENT)


def check_double_bookings(conn, clock):
    """M1 check: every pair of different trucks whose bookings or running holds overlap on one dock. [] means M1 = 0."""
    rows = conn.execute(
        f"""WITH occ AS (
                SELECT shipment_id, slot_id, 'booking ' || appointment_id AS what FROM appointments
                WHERE appointment_status IN ({LIVE})
                UNION SELECT h.shipment_id, h.slot_id, 'booking ' || a.appointment_id FROM slot_holds h
                JOIN appointments a ON a.appointment_id = h.appointment_id
                WHERE h.hold_status = 'CONVERTED' AND a.appointment_status IN ({LIVE})
                UNION SELECT shipment_id, slot_id, 'hold ' || group_id FROM slot_holds
                WHERE hold_status = 'ACTIVE' AND expires_at > ?),
            w AS (SELECT o.*, d.dock_code, sl.slot_start_ts AS s, sl.slot_end_ts AS e FROM occ o
                  JOIN appointment_slots sl ON sl.slot_id = o.slot_id JOIN docks d ON d.dock_id = sl.dock_id)
            SELECT x.dock_code, x.slot_id, x.shipment_id, x.what, y.slot_id, y.shipment_id, y.what FROM w x JOIN w y
            ON x.dock_code = y.dock_code AND x.shipment_id < y.shipment_id AND x.s < y.e AND y.s < x.e""",
        (to_iso(clock.now()),)).fetchall()
    return [f"{r[0]}: {r[1]} {r[2]} ({r[3]}) overlaps {r[4]} {r[5]} ({r[6]})" for r in rows]


def _naive(conn, shipment_id, slot_ids):
    """The careless version (test only): read "is it free?", pause, write a booking. No lock."""
    busy = conn.execute(f"SELECT 1 FROM appointments WHERE slot_id = ? AND appointment_status IN ({LIVE})",
                        (slot_ids[0],)).fetchone()
    time.sleep(0.005)
    if busy:
        return {"shipment": shipment_id, "placed": False}
    now = to_iso(RACE_TIME)
    conn.execute("INSERT INTO appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, "
                 "is_current, booked_at, updated_at) VALUES (?, ?, ?, 'PENDING_CONFIRMATION', 'DRIVER_CHAT', 0, ?, ?)",
                 (f"APT-{uuid.uuid4().hex[:10]}", shipment_id, slot_ids[0], now, now))
    return {"shipment": shipment_id, "placed": True}


def _racer(db_path, shipment_id, slot_ids, key, start_at, naive):
    """One racer in its own process: wait for the start, hold, say yes, then resend the same request."""
    conn, clock = db.connect(db_path), FakeClock(RACE_TIME)
    try:
        time.sleep(max(0.0, start_at - time.time()))
        if naive:
            return _naive(conn, shipment_id, slot_ids)
        first = place_hold(conn, shipment_id, slot_ids, key, clock)
        booked = convert_hold(conn, first.hold.group_id, clock) if first.placed else None
        retry = place_hold(conn, shipment_id, slot_ids, key, clock)
        return {"shipment": shipment_id, "placed": first.placed, "replayed": first.replayed,
                "group": first.hold and first.hold.group_id, "appointment": booked and booked.appointment_id,
                "retry_replayed": retry.replayed, "retry_placed": retry.placed}
    finally:
        conn.close()


def race_round(pool, db_path, round_no, naive=False):
    """Free the slot, let every racer go at the same moment, then run the check."""
    conn = db.connect(db_path)
    try:
        conn.execute("DELETE FROM slot_holds")
        conn.execute("DELETE FROM appointments WHERE appointment_id LIKE 'APT-%'")
        start_at = time.time() + 0.05
        results = pool.starmap(_racer, [(str(db_path), s, slots, f"r{round_no}-{s}", start_at, naive)
                                        for s, slots in RACERS], chunksize=1)
        return {"results": results, "problems": check_double_bookings(conn, FakeClock(RACE_TIME))}
    finally:
        conn.close()


def run_race(rounds=100, workers=8, naive=False):
    """Run the race on a throwaway copy and count everything. naive=True removes the lock and the copy's
    one-active-per-slot indexes, so double bookings become possible and the check must find them."""
    with tempfile.TemporaryDirectory() as tmp:
        path = db.init_work_db(Path(tmp) / "race.db")
        if naive:
            conn = db.connect(path)
            conn.execute("DROP INDEX ux_active_appointment_per_slot")
            conn.execute("DROP INDEX ux_active_hold_per_slot")
            conn.close()
        report = {"rounds": rounds, "workers": workers, "naive": naive, "rounds_with_problems": 0, "example": None,
                  "one_winner_rounds": 0, "duplicate_same_hold": 0, "duplicate_checks": 0,
                  "retries_replayed": 0, "loser_retries_refused": 0, "retries": 0, "winners": {}}
        with Pool(workers) as pool:
            for n in range(rounds):
                out = race_round(pool, path, n, naive)
                if out["problems"]:
                    report["rounds_with_problems"] += 1
                    report["example"] = report["example"] or out["problems"][0]
                won = [r for r in out["results"] if r["placed"]]
                report["one_winner_rounds"] += len({r["shipment"] for r in won}) == 1
                for s in {r["shipment"] for r in won}:
                    report["winners"][s] = report["winners"].get(s, 0) + 1
                if naive:
                    continue
                for s in {r["shipment"] for r in won}:
                    groups = {r["group"] for r in won if r["shipment"] == s}
                    if sum(r["shipment"] == s for r in out["results"]) > 1:
                        report["duplicate_checks"] += 1
                        report["duplicate_same_hold"] += len(groups) == 1
                report["retries"] += len(out["results"])
                report["retries_replayed"] += sum(r["placed"] and r["retry_replayed"] for r in out["results"])
                report["loser_retries_refused"] += sum(not r["placed"] and not r["retry_placed"] for r in out["results"])
    return report


def main(argv=None):
    """Print the race report."""
    p = argparse.ArgumentParser(description="DockPilot integrity proof (M1).")
    p.add_argument("--rounds", type=int, default=100)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--naive", action="store_true")
    args = p.parse_args(argv)
    started = time.time()
    r = run_race(args.rounds, args.workers, args.naive)
    print(f"{'NAIVE (no lock, no safety indexes)' if r['naive'] else 'DockPilot'}: {r['rounds']} rounds x "
          f"{r['workers']} racers for D2 12:00, {time.time() - started:.1f} s")
    print(f"  rounds with a double booking (M1):   {r['rounds_with_problems']}")
    print(f"  rounds with exactly one winner:      {r['one_winner_rounds']}")
    if not r["naive"]:
        print(f"  winner's duplicate got same hold:    {r['duplicate_same_hold']} of {r['duplicate_checks']}")
        print(f"  retries: winners answered from memory {r['retries_replayed']}, losers refused again "
              f"{r['loser_retries_refused']}, of {r['retries']}")
    print(f"  rounds won, by truck:                {dict(sorted(r['winners'].items()))}")
    if r["example"]:
        print(f"  example: {r['example']}")


if __name__ == "__main__":
    main()
