# Chunk List: DockPilot

- **Feature:** DockPilot, a conversational exception agent for late trucks, backed by a dock-capacity layer that never double-books.
- **Source spec:** `docs/01_problem_definition_sheet.md`, `docs/02_data_audit.md`, `docs/03_decision_register.md`, and the challenge brief's hand-ins #3–#5.
- **Verified:** 2026-11-20
- **Decisions:** [00-decisions.md](00-decisions.md)

---

## Blast radius

### Repo: `dockpilot-agent` (prefix `dp`)

Scanned 2026-11-19 (`ls`, `git ls-remote`). No `/codebase-map` or `/context-map` tool is available in this session; the repo is new, so a direct listing covers it.

| Surface | Confirmed file(s) | Change relevance |
|---|---|---|
| Repo root | `README.md`, `LICENSE`, `.gitignore` (Python template) | README rewritten (dp-17); `.gitignore` extended for `.env`, `data/work/` (dp-01) |
| Remote | `origin` → `github.com/B0n-Echo/dockpilot-agent`, branch `main`, 1 commit | Target for all pushes |
| Data (outside repo, to move in) | `../SetuHaul_Data_Package/` (`.db`, schema SQL, data dictionary, guide, core ER `.png`) | Copied as-is into `data/source/` (dp-01) |
| Docs (outside repo, to move in) | `../docs/01_problem_definition_sheet.md` (+ stale `.docx`), `02_data_audit.md`, `03_decision_register.md` | Moved into `docs/` (dp-01) |
| Tooling | Python 3.9.6 (system), `uv` 0.13.0, `sqlite3` 3.54.0; `gh` not signed in | Python 3.12 via `uv` (dp-01) |

**Doc-vs-scan contradiction:** the database guide lists `setuhaul_er_diagram_core.svg` and `setuhaul_er_diagram_full.png/.svg`; only `setuhaul_er_diagram_core.png` exists. Does not block any chunk.

---

## Chunk list: `dockpilot-agent`

### Foundation

- **dp-01 Repo scaffold**
  - Contract: given a fresh clone, then `uv run pytest` passes a smoke test; the data package sits unchanged in `data/source/`; docs sit in `docs/`; `.env` and `data/work/` are ignored by git; `.env.example` exists.
  - Depends on: none.
- **dp-02 Working database + migration**
  - Contract: given the source database, then init produces a working copy in `data/work/` with the 18 original tables untouched plus `slot_holds` (with a partial unique index on active holds), `escalations` and `events`; the source file's checksum is unchanged; running init again resets it.
  - Depends on: dp-01.
- **dp-03 Clock + settings**
  - Contract: given a fake clock at 2026-08-04 09:45 +05:30, when moved forward 11 minutes, then it reads 09:56; hold (10 / 5 min), spike (5 requests / 10 min), window (30 s), warehouse timeout and buffer values are read from one settings module.
  - Depends on: dp-01.

### Capacity layer

- **dp-04 Feasibility check (D4)**
  - Contract: given SHP1014 and SLOT-JAI-004, then the check fails with reasons (arrives after start, over weight limit, unload longer than slot); given SHP1012 and SLOT-JAI-019, then it passes.
  - Depends on: dp-02, dp-03.
- **dp-05 Find options (D5)**
  - Contract: given SHP1014 at 09:44, then the options start at 12:00 on D2 or D4 and cover two consecutive slots; slots with an active hold or booking are never offered.
  - Depends on: dp-04.
- **dp-06 Atomic hold (D5, D6)**
  - Contract: given an active hold on a slot, then a second hold on it is refused; given a repeated request key, then the same hold is returned; given the fake clock moved 11 minutes, then the hold is EXPIRED and the slot is offered again.
  - Depends on: dp-05.
- **dp-07 Escalations (D10, D12)**
  - Contract: given SHP1015 with no feasible slot, then an escalation row is written with every M3 field (driver, shipment, current appointment, latest ETA + source, options checked and why each failed, driver constraints) and printed to the console.
  - Depends on: dp-05.
- **dp-08 Hold → PENDING → CONFIRMED / REJECTED (D6)**
  - Contract: given an active hold, when converted, then a PENDING appointment exists linked to the hold, which is CONVERTED; when confirmed, then CONFIRMED; when rejected, then REJECTED and the slot is free; when the warehouse timeout passes, then an escalation is raised; given an expired hold, then conversion is refused.
  - Depends on: dp-06, dp-07.
- **dp-09 Lifecycle (D9)**
  - Contract: given a new ETA later than the booked slot start, then the booking is flagged and new options are offered; given a reschedule, then the new appointment sets `replaced_appointment_id`; given the grace period passed without check-in, then NO_SHOW; given a dock event, then every affected booking is re-homed or escalated.
  - Depends on: dp-08.
- **dp-10 Allocation policy (D7)**
  - Contract: given the four-truck replay (SHP1006, SHP1012, SHP1013, SHP1014), then outcomes match the D7 table; given a near-tie inside a spike window, then an escalation is raised; given a CRITICAL load whose best slot is held by a lower priority, then a coordinator alert is raised.
  - Depends on: dp-09.
- **dp-11 Integrity proof (D13)**
  - Contract: given 100 rounds of N processes racing for one slot (with duplicates and retries), then the check query finds 0 double bookings in every round; given the naive no-lock version, then the test detects at least one double booking.
  - Depends on: dp-08.

### Conversation

- **dp-12 Rule-based message reader**
  - Contract: given MSG003, MSG005 and MSG007, then the reader returns the expected intent, ETA and confidence (MSG005 → LOW).
  - Depends on: dp-01.
- **dp-13 LLM reader (OpenRouter)**
  - Contract: given a key in `.env`, then a message is turned into the same fields as dp-12; given no key or a failed call, then the rule-based reader is used without crashing.
  - Depends on: dp-12.
- **dp-14 Conversation agent (D1–D3, D11, R7)**
  - Contract: given the THR003 script, then the agent asks the clarifying question and places no hold until it is answered; given the THR010 script, then it asks which shipment; every refusal states the reason and next options.
  - Depends on: dp-10, dp-13.
- **dp-15 CLI chat simulator**
  - Contract: given the simulator running, when you type as a driver, then the agent replies; `confirm` and `reject` commands act as the warehouse and the driver is notified.
  - Depends on: dp-14.

### Proof and hand-in

- **dp-16 Scenario runner**
  - Contract: given the seeded cases, then each replay writes a transcript and the run reports M1–M9, with M1, M2 and M4 at 0 and M3 at 100% / 100%.
  - Depends on: dp-11, dp-15.
- **dp-17 README + docs**
  - Contract: given the finished build, then the README explains DockPilot and how to run it, and the decision register's written examples are filled from the scenario output.
  - Depends on: dp-16.

---

## Execution order

1. dp-01
2. dp-02
3. dp-03
4. dp-04
5. dp-05
6. dp-06
7. dp-07
8. dp-08
9. dp-09
10. dp-10
11. dp-11
12. dp-12
13. dp-13
14. dp-14
15. dp-15
16. dp-16
17. dp-17
