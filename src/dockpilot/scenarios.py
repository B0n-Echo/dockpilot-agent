"""The scenario runner (dp-16): the client's seeded cases played through DockPilot on one timeline, with a transcript
per case and the M1–M9 report.

    uv run python -m dockpilot.scenarios          # rule reader, writes docs/scenarios/
    uv run python -m dockpilot.scenarios --ai     # same, reading messages with the AI
"""

import argparse
import heapq
import io
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from dockpilot import db, paths
from dockpilot.allocation import settle_requests
from dockpilot.auth import request_code, verify_code
from dockpilot.bookings import PENDING, check_warehouse_timeouts, confirm_booking
from dockpilot.clock import IST, FakeClock, to_iso
from dockpilot.conversation import _thread_for
from dockpilot.conversation import (check_silent_drivers, handle_message, notify_allocation, notify_booking_result,
                                    notify_warehouse_timeout)
from dockpilot.feasibility import check_feasibility
from dockpilot.holds import expire_holds
from dockpilot.integrity import check_double_bookings
from dockpilot.lifecycle import _booked_slots, handle_dock_event, mark_no_shows
from dockpilot.llm_reader import read_message_ai
from dockpilot.reader import read_message


def T(h, m, s=0):
    """A time on the snapshot day."""
    return datetime(2026, 8, 4, h, m, s, tzinfo=IST)


@dataclass(frozen=True)
class Case:
    """One seeded case: title, guide reference, the shipment it is about, the driver's phone, and its timed steps.
    A step is (time, text) or (time, text, message id) for the driver, or (time, "@dock_event DEVT001") /
    (time, "@no_show_check") for the day's events."""

    title: str
    seed: str
    shipment: str
    phone: str | None
    steps: tuple


CASES = {
    "S01": Case("Late ETA reported before arrival", "SHP1006 / THR001", "SHP1006", "9000010006", (
        (T(9, 34), "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?", "wa-9001"), (T(9, 35, 30), "1"))),
    "S02": Case("Tyre repaired, must leave by 13:30", "SHP1012 / THR002", "SHP1012", "9000010012", (
        (T(9, 29), "Tyre repaired. I can reach 11:10. I need to leave the warehouse before 1:30 for another pickup.",
         "wa-9002"), (T(9, 29, 30), "1"))),
    "S03": Case("Uncertain ETA (R7) and a silent driver", "SHP1013 / THR003", "SHP1013", "9000010013", (
        (T(9, 19), "I am late by one hour.", "wa-9003"), (T(9, 45), "I am late by one hour."),
        (T(9, 45, 30), "not sure"), (T(9, 46), "1"))),
    "S04": Case("Critical load competing for standard docks", "SHP1014 / THR004", "SHP1014", "9000010014", (
        (T(9, 44), "Origin released me late. ETA 11:25. This load is urgent. Can I get the first possible dock?",
         "wa-9004"), (T(9, 44, 30), "1"))),
    "S05": Case("Reefer dock unavailable: no feasible slot", "SHP1015 / THR005", "SHP1015", "9000010015", (
        (T(16, 24), "Evening traffic. ETA 6:30. Can the reefer unload tonight?", "wa-9005"),)),
    "S06": Case("Heavy vehicle, driver with two trucks", "SHP1016 / THR006", "SHP1016", "9000010007", (
        (T(9, 39), "Heavy trailer ETA 12:00. Original heavy-bay slot will be missed. What is next?", "wa-9006"),
        (T(9, 39, 30), "MH04HV4107"), (T(9, 40), "1"))),
    "S07": Case("Early arrival does not jump the queue", "SHP1003 / THR007", "SHP1003", "9000010003", (
        (T(8, 22), "I am already at the gate. Can I get a dock now instead of waiting for 9?", "wa-9007"),)),
    "S08": Case("Late arrival already in the yard", "SHP1004 / THR008", "SHP1004", "9000010004", (
        (T(9, 27), "I reached late and am in the yard. Has a new dock been assigned?", "wa-9008"),)),
    "S09": Case("Duplicate driver message (messaging retry)", "THR001 / THR009", "SHP1006", "9000010006", (
        (T(9, 35, 2), "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?", "wa-9009"),)),
    "S10": Case("Ambiguous shipment: two trips", "DRV004 / THR010", "SHP1020", "9000010004", (
        (T(9, 31), "I will be late by 45 minutes.", "wa-9010"), (T(9, 31, 30), "020"),
        (T(9, 32), "not sure"), (T(9, 32, 30), "1"))),
    "S11": Case("Ask-only conversation", "SHP1017 / THR011", "SHP1017", "9000010001", (
        (T(10, 2), "What slots are possible after 1 PM if I reach around 12:45?", "wa-9011"), (T(10, 3), "no"))),
    "S12": Case("Message about a cancelled shipment", "SHP1019 / THR012", "SHP1019", "9000010008", (
        (T(10, 12), "Is my 4 PM slot still active?", "wa-9012"),)),
    "S13": Case("Dock breakdown hits a confirmed booking", "SHP1005 / DEVT001", "SHP1005", None, (
        (T(9, 16), "@dock_event DEVT001"),)),
    "S14": Case("No-show check after the grace time", "RULE: NO_SHOW_GRACE_MIN", None, None, (
        (T(10, 35), "@no_show_check"),)),
}
"""The seeded cases from the client's database guide §6, with the client's own words and times."""

WAREHOUSE_SILENT = {"SHP1016"}
"""Requests the replay's warehouse never answers, to show the timeout path (decision 4)."""

CONFIRM_AFTER = timedelta(minutes=5)
"""How long the replay's warehouse takes to confirm a request."""



class Replay:
    """One run: the working copy, the merged timeline, and everything that happened, per case."""

    def __init__(self, reader, path):
        """Fresh working copy at `path`; empty logs."""
        self.conn, self.reader = db.connect(db.init_work_db(path)), reader
        self.log = {c: [] for c in CASES}
        self.sessions, self.thread_case, self.scheduled = {}, {}, set()
        self.m1, self.m2, self.out = [], [], io.StringIO()

    def note(self, case, at, who, text):
        """Add a line to a case's transcript."""
        self.log[case].append((at, who, text))

    def agent_lines(self, replies, at):
        """Log replies posted by the timer or the warehouse into the case that owns their chat."""
        for r in replies:
            case = self.thread_case.get(r.thread_id)
            if case:
                self.note(case, at, "DockPilot", r.text)

    def tick(self, clock):
        """The web app's timer at this moment: holds, rivals, warehouse timeouts (driver told), silent drivers."""
        expire_holds(self.conn, clock)
        self.agent_lines(notify_allocation(self.conn, settle_requests(self.conn, clock), clock), clock.now())
        escs = check_warehouse_timeouts(self.conn, clock, out=self.out)
        for esc in escs:
            case = next((c for c, k in CASES.items() if k.shipment == esc.shipment_id), None)
            if case and esc.created_at == to_iso(clock.now()):
                self.note(case, clock.now(), "Coordinator", f"Hand-off {esc.reason_code}: {esc.summary}")
        self.agent_lines(notify_warehouse_timeout(self.conn, escs, clock), clock.now())
        for done in check_silent_drivers(self.conn, clock, out=self.out):
            text = self.conn.execute("SELECT message_text FROM chat_messages WHERE thread_id = ? ORDER BY rowid DESC "
                                     "LIMIT 1", (done["thread_id"],)).fetchone()[0]
            case = self.thread_case.get(done["thread_id"])
            if case:
                self.note(case, clock.now(), "DockPilot", text)
                if done["escalation_id"]:
                    self.note(case, clock.now(), "Coordinator", f"Hand-off {done['escalation_id']} (ETA_UNRESOLVED)")

    def driver(self, case, clock, text, ref):
        """One driver message: log in first if needed (a refusal is an outcome), then the agent."""
        phone = CASES[case].phone
        self.note(case, clock.now(), "Driver", text)
        if phone not in self.sessions:
            code = request_code(self.conn, phone, clock)
            self.sessions[phone] = code.code and verify_code(self.conn, phone, code.code, clock).session.session_id
            if not code.ok:
                self.note(case, clock.now(), "DockPilot", f"(login refused) {code.reason}")
                return
        if not self.sessions[phone]:
            return
        reply = handle_message(self.conn, self.sessions[phone], text, clock, reader=self.reader, message_ref=ref)
        self.thread_case[reply.thread_id] = case
        self.note(case, clock.now(), "DockPilot", reply.text)

    def event(self, case, clock, text):
        """A day event: a dock breakdown (dp-09) or the no-show check (dp-09)."""
        if text.startswith("@dock_event"):
            ev = text.split()[1]
            for r in handle_dock_event(self.conn, ev, clock, out=self.out):
                self.note(case, clock.now(), "Event", f"{ev} hits {r.appointment_id}: "
                          + (f"new booking requested {r.new_appointment_id}" if r.new_appointment_id else
                             f"escalated {r.escalation.reason_code}" if r.escalation else "no change"))
        else:
            reviews = mark_no_shows(self.conn, clock, out=self.out)
            for r in reviews:
                self.note(case, clock.now(), "Event", f"{r.appointment_id} marked NO_SHOW (30 min grace, no check-in)"
                          + (f"; escalated {r.escalation.reason_code}" if r.escalation else
                             f"; {len(r.options)} new options found" if r.options else ""))
            if not reviews:
                self.note(case, clock.now(), "Event", "No confirmed booking past its grace time without a check-in.")

    def confirm(self, clock, appointment_id):
        """The replay's warehouse confirms a request; the slot is re-checked first (M2)."""
        a = self.conn.execute("SELECT * FROM appointments WHERE appointment_id = ?", (appointment_id,)).fetchone()
        if a["appointment_status"] != PENDING:
            return
        check = check_feasibility(self.conn, a["shipment_id"], _booked_slots(self.conn, appointment_id), clock)
        if not check.feasible:
            self.m2.append(f"{appointment_id}: {check.reasons[0].message}")
        confirm_booking(self.conn, appointment_id, clock, ref=f"WH-{appointment_id[-4:].upper()}")
        case = self.thread_case.get(_thread_for(self.conn, appointment_id)) or next(
            (c for c, k in CASES.items() if k.shipment == a["shipment_id"]), None)
        if case:
            self.note(case, clock.now(), "Warehouse", f"Confirmed {appointment_id} (re-check: "
                      f"{'still feasible' if check.feasible else 'NOT feasible'})")
            told = notify_booking_result(self.conn, appointment_id, clock)
            if told.ok:
                self.note(case, clock.now(), "DockPilot", told.text)

    def run(self):
        """Play the merged timeline; a timer tick every minute between steps; the M1 check after every step."""
        steps = sorted((s[0], case, s[1], s[2] if len(s) > 2 else None) for case, c in CASES.items() for s in c.steps)
        queue = [(at, i, case, text, ref) for i, (at, case, text, ref) in enumerate(steps)]
        heapq.heapify(queue)
        n, next_tick = len(queue), min(q[0] for q in queue).replace(second=0)
        while queue:
            at, _, case, text, ref = heapq.heappop(queue)
            while next_tick < at:
                self.tick(FakeClock(next_tick))
                next_tick += timedelta(minutes=1)
            clock = FakeClock(at)
            if case == "@confirm":
                self.confirm(clock, text)
            elif text.startswith("@"):
                self.event(case, clock, text)
            else:
                self.driver(case, clock, text, ref)
            self.m1 += check_double_bookings(self.conn, clock)
            for p in self.conn.execute("SELECT appointment_id, shipment_id, booked_at FROM appointments WHERE "
                                       "appointment_id LIKE 'APT-%' AND appointment_status = ?", (PENDING,)):
                if p[0] not in self.scheduled and p[1] not in WAREHOUSE_SILENT:
                    self.scheduled.add(p[0])
                    n += 1
                    heapq.heappush(queue, (datetime.fromisoformat(p[2]) + CONFIRM_AFTER, n, "@confirm", p[0], None))
            if not queue:
                end = at + timedelta(minutes=31)
                while next_tick <= end:
                    self.tick(FakeClock(next_tick))
                    next_tick += timedelta(minutes=1)


def _outcome(rp, case):
    """How a case ended, from its transcript and the database."""
    lines, c = rp.log[case], CASES[case]
    said = " ".join(t for _, w, t in lines if w == "DockPilot")
    esc = c.shipment and rp.conn.execute("SELECT COUNT(*) FROM escalations WHERE shipment_id = ?", (c.shipment,)).fetchone()[0]
    if "(login refused)" in said:
        return "REFUSED"
    if any(w == "Warehouse" for _, w, _ in lines) and ("Confirmed:" in said or not c.phone):
        return "CONFIRMED"
    if any(w == "Driver" for _, w, _ in lines) and rp.conn.execute(
            "SELECT 1 FROM chat_messages WHERE is_duplicate = 1 AND chat_message_id LIKE 'DPM-%' AND message_text IN "
            f"({','.join('?' * len(lines))})", [t for *_, t in lines]).fetchone():
        return "RESEND_RECOGNISED"
    if "No slot fits" in said or (esc and not any(w == "Warehouse" for _, w, _ in lines)):
        return "ESCALATED"
    if "Good news: your booking" in said:
        return "BOOKING_STANDS"
    if "I won't book it" in said:
        return "DECLINED"
    if any(w == "Event" for _, w, _ in lines):
        return "HANDLED"
    if "Your booking:" in said:
        return "ANSWERED"
    if "I'm confirming" in said:
        return "REQUESTED"
    return "OPEN"


def measures(rp):
    """M1–M9 from the database and the transcripts."""
    conn, now = rp.conn, FakeClock(T(17, 0))
    no_slot = [(case, t) for case, lines in rp.log.items() for at, w, t in lines if w == "DockPilot" and "No slot fits" in t]
    escs = conn.execute("SELECT * FROM escalations WHERE reason_code = 'NO_FEASIBLE_SLOT'").fetchall()
    def complete(e):
        """All M3 fields of one hand-off are filled."""
        opts = json.loads(e["options_checked"] or "[]")
        has_booking = conn.execute("SELECT 1 FROM appointments WHERE shipment_id = ? AND appointment_status IN "
                                   "('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')", (e["shipment_id"],)).fetchone()
        return all([e["driver_id"], e["shipment_id"], e["appointment_id"] or not has_booking, e["latest_eta_ts"],
                    e["eta_source"], opts, all(o["failed_because"] for o in opts), e["driver_constraints"]])
    m4 = [f"{a['appointment_id']} ({a['shipment_id']}) cancelled without its own confirmed replacement"
          for a in conn.execute("SELECT * FROM appointments WHERE appointment_status = 'CANCELLED'")
          if a["appointment_id"] not in rp.cancelled_before and not conn.execute("SELECT 1 FROM appointments WHERE replaced_appointment_id = ? AND shipment_id = ? AND "
                              "appointment_status = 'CONFIRMED'", (a["appointment_id"], a["shipment_id"])).fetchone()]
    per_case = {}
    for case, lines in rp.log.items():
        driver = [l for l in lines if l[1] == "Driver"]
        qs = [t for _, w, t in lines if w == "DockPilot" and ("Which one is this about?" in t or "Reply with your arrival time" in t)]
        defects = [t for (_, w, t), nxt in zip(lines, lines[1:]) if w == "Driver" and nxt[1] == "DockPilot" and (
            ("Which one is this about?" in nxt[2] and re.search(r"SHP\d{4}|ORD-\d{6}-\d{3}", t))
            or ("Reply with your arrival time" in nxt[2] and re.search(r"\b\d{1,2}[:.]\d{2}\b", t)))]
        outcome = _outcome(rp, case)
        end = next((at for at, w, t in lines if (w == "DockPilot" and re.match(r"Confirmed:|No slot fits|Good news|OK, I won't|"
                    r"Your booking:|\(login refused\)|The warehouse hasn't answered", t)) or w in ("Event", "Warehouse")), None)
        start = lines[0][0] if lines else None
        per_case[case] = {"outcome": outcome, "questions": len(qs), "defects": len(defects),
                          "minutes": end and start and round((end - start).total_seconds() / 60, 1),
                          "turns": len([l for l in driver if not end or l[0] <= end]),
                          "coordinator": bool(CASES[case].shipment and conn.execute(
                              "SELECT 1 FROM escalations WHERE shipment_id = ?", (CASES[case].shipment,)).fetchone())}
    waits = []
    for a in conn.execute("SELECT a.shipment_id, sl.slot_start_ts, a.booked_at FROM appointments a JOIN appointment_slots sl "
                          "ON sl.slot_id = a.slot_id WHERE a.appointment_id LIKE 'APT-%' AND a.appointment_status = 'CONFIRMED'"):
        eta = conn.execute("SELECT declared_eta_ts FROM eta_updates WHERE shipment_id = ? AND created_at <= ? ORDER BY "
                           "created_at DESC, eta_update_id DESC LIMIT 1", (a[0], a[2])).fetchone()
        if eta:
            waits.append((a[0], int((datetime.fromisoformat(a[1]) - datetime.fromisoformat(eta[0])).total_seconds() // 60)))
    use = lambda: conn.execute(
        """SELECT COUNT(DISTINCT sl.slot_id) FROM appointment_slots sl WHERE sl.facility_id = 'FAC-JAI-01' AND (
               EXISTS (SELECT 1 FROM appointments a WHERE a.slot_id = sl.slot_id AND a.appointment_status IN
                       ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS','COMPLETED'))
               OR EXISTS (SELECT 1 FROM slot_holds h JOIN appointments a ON a.appointment_id = h.appointment_id
                          WHERE h.slot_id = sl.slot_id AND h.hold_status = 'CONVERTED' AND a.appointment_status IN
                          ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS','COMPLETED')))""").fetchone()[0]
    usable = conn.execute("SELECT COUNT(*) FROM appointment_slots sl JOIN docks d ON d.dock_id = sl.dock_id WHERE "
                          "sl.facility_id = 'FAC-JAI-01' AND sl.slot_status = 'OPEN' AND d.dock_status = 'ACTIVE'").fetchone()[0]
    resolved = [c for c, v in per_case.items() if v["outcome"] in ("CONFIRMED", "BOOKING_STANDS", "ANSWERED", "DECLINED",
                                                                     "RESEND_RECOGNISED", "HANDLED")
                and not v["coordinator"]]
    return {
        "M1": rp.m1, "M2": rp.m2,
        "M3": {"no_slot_replies": len(no_slot), "escalated": sum(1 for _ in escs), "complete": sum(complete(e) for e in escs)},
        "M4": m4, "cases": per_case,
        "M6": (len(resolved), len(per_case)), "M8": waits,
        "M9": {"used_after": use(), "usable": usable, "baseline": rp.m9_before},
    }


def _pct(a, b):
    """a of b as a whole percentage."""
    return f"{round(100 * a / b)}%" if b else "n/a"


def report(m, reader_name):
    """The M1–M9 report as Markdown."""
    m3, m9 = m["M3"], m["M9"]
    lines = [
        "# DockPilot scenario run: M1–M9", "",
        f"Snapshot 4 Aug 2026, Jaipur DC. {len(m['cases'])} seeded cases on one timeline (08:22 → 16:55). Reader: {reader_name}.",
        "", "## Hard targets", "", "| # | Measure | Target | Result |", "|---|---|---|---|",
        f"| M1 | Double bookings (check after every step) | 0 | **{len(m['M1'])}** |",
        f"| M2 | Confirmed bookings infeasible at confirmation | 0 | **{len(m['M2'])}** |",
        f"| M3 | No-slot cases escalated / hand-off complete | 100% / 100% | **{_pct(m3['escalated'], m3['no_slot_replies'])} / "
        f"{_pct(m3['complete'], m3['escalated'])}** ({m3['escalated']} of {m3['no_slot_replies']}) |",
        f"| M4 | Priority-policy violations | 0 | **{len(m['M4'])}** |",
        "", "## Measured (targets set later by the head of operations)", "",
        "| Case | Title | Outcome | M5 first message → outcome | M7 questions (defects) | Coordinator involved |",
        "|---|---|---|---|---|---|"]
    for case, v in m["cases"].items():
        mins = f"{v['minutes']} min, {v['turns']} turns" if v["minutes"] is not None else "—"
        lines.append(f"| [{case}]({case}.md) | {CASES[case].title} | {v['outcome']} | {mins} | {v['questions']} ({v['defects']}) | "
                     f"{'yes' if v['coordinator'] else 'no'} |")
    w = m["M8"]
    lines += ["", f"- **M6** resolved with no coordinator: **{m['M6'][0]} of {m['M6'][1]}** cases "
                  f"({_pct(*m['M6'])}). Required escalations are never skipped to raise this.",
              f"- **M7** clarifying questions: **{sum(v['questions'] for v in m['cases'].values())}** in all, "
              f"**{sum(v['defects'] for v in m['cases'].values())}** about a fact we already held.",
              f"- **M8** planned wait (slot start − declared arrival; *planned, not measured*: a replay has no real arrivals): "
              f"average **{round(sum(x for _, x in w) / len(w)) if w else 0} min** over {len(w)} rebooked trucks "
              f"({', '.join(f'{s} {x} min' for s, x in w)}).",
              f"- **M9** Jaipur DC slots in use (booked or done, of open slots on active docks): **{_pct(m9['baseline'], m9['usable'])}"
              f" → {_pct(m9['used_after'], m9['usable'])}** ({m9['baseline']} → {m9['used_after']} of {m9['usable']}). "
              "The audit's 14% counted all usable slots across both facilities.",
              "", "## Details", ""]
    lines += [f"- M1: {p}" for p in m["M1"]] + [f"- M2: {p}" for p in m["M2"]] + [f"- M4: {p}" for p in m["M4"]]
    if not (m["M1"] or m["M2"] or m["M4"]):
        lines.append("- No problems found for M1, M2 or M4.")
    return "\n".join(lines) + "\n"


def transcript(rp, case, outcome):
    """One case's transcript as Markdown."""
    c = CASES[case]
    early = c.steps[0][0] < T(9, 45)
    lines = [f"# {case} · {c.title}", "", f"Seed reference: `{c.seed}`" + (f" · shipment `{c.shipment}`" if c.shipment else ""),
             "", "| Time | Who | Message / event |", "|---|---|---|"]
    lines += [f"| {at:%H:%M:%S} | {who} | {text.replace('|', '/')} |" for at, who, text in sorted(rp.log[case], key=lambda x: x[0])]
    lines += ["", f"**Outcome:** {outcome}"]
    if early:
        lines.append("\n*Note: this case starts before the 09:45 snapshot. Bookings are as of 09:45; "
                     "ETA updates from later than the step's time are ignored.*")
    return "\n".join(lines) + "\n"


def run_scenarios(reader=read_message, out_dir=paths.REPO_ROOT / "docs" / "scenarios"):
    """Play every case, write one transcript per case and report.md into out_dir; return the measures."""
    with tempfile.TemporaryDirectory() as tmp:
        rp = Replay(reader, Path(tmp) / "replay.db")
        rp.cancelled_before = {r[0] for r in rp.conn.execute(
            "SELECT appointment_id FROM appointments WHERE appointment_status = 'CANCELLED'")}
        rp.m9_before = rp.conn.execute(
            """SELECT COUNT(DISTINCT slot_id) FROM appointments a WHERE appointment_status IN
               ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS','COMPLETED') AND slot_id IN
               (SELECT slot_id FROM appointment_slots WHERE facility_id = 'FAC-JAI-01')""").fetchone()[0]
        rp.run()
        m = measures(rp)
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for case, v in m["cases"].items():
            (out / f"{case}.md").write_text(transcript(rp, case, v["outcome"]), encoding="utf-8")
        name = "AI (OpenRouter)" if reader is read_message_ai else "rules"
        (out / "report.md").write_text(report(m, name), encoding="utf-8")
        rp.conn.close()
    return m


def main(argv=None):
    """Run the replay and print the report."""
    p = argparse.ArgumentParser(description="DockPilot scenario replay (M1–M9).")
    p.add_argument("--ai", action="store_true", help="read messages with the AI instead of the rules")
    p.add_argument("--out", default=str(paths.REPO_ROOT / "docs" / "scenarios"))
    args = p.parse_args(argv)
    run_scenarios(read_message_ai if args.ai else read_message, args.out)
    print((Path(args.out) / "report.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
