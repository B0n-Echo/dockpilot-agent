# DockPilot

An agent that reschedules late trucks into warehouse dock slots, backed by a capacity layer that never double-books.

A driver who is running late messages DockPilot. DockPilot works out which unloading slots still really fit the truck, holds one safely for 10 minutes, asks the warehouse to confirm, and tells the driver the answer. Anything it can't solve goes to a coordinator with the whole story in one record.

**The AI only reads.** A small language model turns a driver's free text ("stuck in traffic, should make it by half past eleven") into fields: intent, arrival time, how sure. Every decision (does the slot fit, who gets it, book it, escalate it) is plain code you can test. If the AI is unavailable, simple rules read the message instead.

Everything here runs on a dummy logistics dataset: one day (4 Aug 2026, 09:45 India time), two warehouses, 21 shipments.

---

## How it works

```
driver message
   │
   ▼
login: phone + one-time code ──── no trip in progress? refused ───────────────── auth.py
   │
   ▼
reader: text → intent, arrival time, confidence (AI, or rules as fallback) ───── llm_reader.py, reader.py
   │
   ▼
conversation: which trip? vague time? → one clear question, no hold yet ──────── conversation.py
   │
   ▼
slot check: 15 rules (dock type, weight, arrival, unload time, breakdowns…) ──── feasibility.py
   │
   ▼
options: best 3 feasible slots ── none? → coordinator hand-off ───────────────── options.py, escalations.py
   │
   ▼
who gets it: first come first served; in a rush, rank by priority ────────────── allocation.py
   │
   ▼
hold (10 min, one writer at a time) → "1" → booking request ──────────────────── holds.py, bookings.py
   │
   ▼
warehouse confirms / rejects / says nothing for 30 min (→ coordinator) ───────── bookings.py, webapp.py
```

During the day, the timer (`lifecycle.check_drift`) re-checks bookings when a dock breaks down, a truck doesn't show up or a new arrival time is recorded, and tells the driver if they're chatting.

---

## Try it

You need [uv](https://docs.astral.sh/uv/). Python 3.12 is picked up automatically. There are no other dependencies.

**Run the tests** (181 checks, about 25 seconds, all offline):

```bash
uv run pytest
```

**Online:** DockPilot can run on Render: see [docs/07_deploy_render.md](docs/07_deploy_render.md). The warehouse side is password-protected there.

**The web demo:** a driver chat page and a warehouse page.

```bash
uv run python -m dockpilot.webapp --reset
```

Open these two pages side by side:
- driver chat: http://127.0.0.1:8001
- warehouse view: http://127.0.0.1:8001/warehouse

Then:
1. Click **Kailash** in the demo-drivers list, then **Send code**, then type the code shown in the grey box.
2. Type `I am late by one hour.`, answer `around 11:15`, then reply `1`.
3. Press **Confirm** on the warehouse page. Within 3 seconds the driver sees "Confirmed: D2 at 12:00".
4. Try **Ashok** too: both his trips are cancelled, so he is refused.

The demo clock starts at the data's 09:45 and runs in real time. **+10 min** on the warehouse page jumps ahead, to show holds running out, silent-driver nudges and the 30-minute warehouse timeout.

**The integrity proof:** 8 separate programs race for one slot, 100 times.

```bash
uv run python -m dockpilot.integrity
```

```bash
uv run python -m dockpilot.integrity --rounds 20 --naive
```

The `--naive` run is the "smoke alarm test button": no lock and no database safety net, so it must double-book, and the same check must catch it.

**The replay:** the dataset's 14 seeded cases on one timeline. It writes a transcript per case and the M1–M9 report into `docs/scenarios/`.

```bash
uv run python -m dockpilot.scenarios
```

**Optional: the AI reader.** Copy `.env.example` to `.env` and fill in `OPENROUTER_API_KEY` and `OPENROUTER_MODEL` (a small Claude model, e.g. `anthropic/claude-haiku-5.5`). `.env` is ignored by git. The demo then reads messages with the AI. Tests never use it.

```bash
uv run python -m dockpilot.llm_reader "Stuck in traffic near Shahpura, should make it by half past eleven"
```

**See every step a message goes through (trace mode):**

```bash
uv run python -m dockpilot.webapp --reset --trace
```

Every message is recorded as a tree of steps: login check, reading (★ the AI call, with exactly what was sent and what came back, or why it fell back to the rules), slot search, hold, booking, reply, with milliseconds per step. Traces are for developers and LangSmith; drivers never see them. In a second terminal:

```bash
uv run python -m dockpilot.trace --last
```

With `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` in `.env` (optionally `LANGSMITH_PROJECT`, and `LANGSMITH_ENDPOINT` for the EU region), the same trees also go to LangSmith. Set `LANGSMITH_TRACING=false` to keep the key but send nothing. The walkthrough built from real traces is [docs/04_how_it_works.md](docs/04_how_it_works.md).

**Reset the working database** (the original data in `data/source/` is never written to):

```bash
uv run python -m dockpilot.db
```

---

## Results

**Hard targets** from the replay ([docs/scenarios/report.md](docs/scenarios/report.md)):

| # | Measure | Target | Result |
|---|---|---|---|
| M1 | Double bookings (checked after every step) | 0 | **0** |
| M2 | Confirmed bookings that were infeasible at confirmation | 0 | **0** |
| M3 | No-slot cases escalated / hand-off record complete | 100% / 100% | **100% / 100%** |
| M4 | Allocation-policy violations | 0 | **0** |

**Integrity proof** (`dockpilot.integrity`):
- 100 rounds × 8 racers: **0 double bookings**, exactly one winner every round.
- Every truck won some rounds, so the start really is a race.
- A duplicate message always got the same hold back, and all 800 resends were handled safely.
- The naive version double-booked in **20 of 20** rounds, and the check caught every one.

**The 14 replayed cases** (each links to its transcript):

| Case | What it tests | Outcome |
|---|---|---|
| [S01](docs/scenarios/S01.md) | Late ETA reported before arrival | Confirmed |
| [S02](docs/scenarios/S02.md) | Tyre repaired, must leave by 13:30 | Confirmed |
| [S03](docs/scenarios/S03.md) | Vague delay, one question, then a silent driver | Closed after 10 min (coordinator told); later confirmed |
| [S04](docs/scenarios/S04.md) | Critical load competing for standard docks | Confirmed |
| [S05](docs/scenarios/S05.md) | Reefer dock unavailable: no feasible slot | Escalated with a full hand-off |
| [S06](docs/scenarios/S06.md) | Heavy truck; warehouse never answers | Coordinator after 30 min; driver told |
| [S07](docs/scenarios/S07.md) | Early arrival doesn't jump the queue | Existing booking stands |
| [S08](docs/scenarios/S08.md) | Late arrival already in the yard | Status answered, no needless question |
| [S09](docs/scenarios/S09.md) | Duplicate message from a messaging retry | Same reply, nothing booked twice |
| [S10](docs/scenarios/S10.md) | Driver with two trips, unclear which | Asks which, then confirmed |
| [S11](docs/scenarios/S11.md) | Ask-only conversation | Options shown, driver declines |
| [S12](docs/scenarios/S12.md) | Message about a cancelled shipment | Refused at login with the reason |
| [S13](docs/scenarios/S13.md) | Dock breakdown hits a confirmed booking | Re-homed and confirmed |
| [S14](docs/scenarios/S14.md) | No-show after the grace time | Marked no-show, new options found |

Also measured:
- **M6:** 10 of 14 cases (71%) were resolved with no coordinator.
- **M7:** 5 clarifying questions in all, none about a fact the system already held.
- **M8:** the planned wait at the warehouse averages 102 min. That's slot start minus declared arrival, planned rather than measured.
- **M9:** Jaipur slot use goes from 21% to 25%.

---

## Where things are

| Path | What |
|---|---|
| `src/dockpilot/` | The code: one file per building block (see "How it works") |
| `src/dockpilot/static/` | The two web pages (plain HTML + JavaScript) |
| `src/dockpilot/migrations/` | DockPilot's own tables, added to a working copy of the data |
| `tests/` | 181 checks, offline, on the real dummy data |
| `data/source/` | The dataset exactly as received, with checksums |
| `docs/` | Problem definition, data audit, decision register, and the replay output |
| `progress/` | An internal build-progress page (`uv run python progress/server.py`) |

---

## Honest limits

- The login code is shown on screen instead of sent by SMS, and the chat is a local web page instead of WhatsApp.
- Coordinators see hand-offs on the warehouse page but can't mark them handled, or move another truck's booking, yet.
- A driver's own limits ("any slot after 12", "must leave before 1:30") are read but not yet used to choose slots.
- Drivers who aren't chatting with DockPilot aren't told when their booking changes (no SMS / WhatsApp channel yet).
- Timings that the business hasn't confirmed yet run on stated defaults in `src/dockpilot/settings.py`. These are the 30-minute warehouse timeout, the 30-minute buffer for vague arrival times, and the spike trigger (5 requests in 10 minutes).

---

## Design documents

- [Problem definition](docs/01_problem_definition_sheet.md): the problem, the measures (M1–M9) and the boundaries.
- [Data audit](docs/02_data_audit.md): every data issue, with the query that proves it.
- [Decision register](docs/03_decision_register.md): the 13 design decisions, each with its answer, reason and evidence from the build.
- [How it works](docs/04_how_it_works.md): one message end to end from real traces, who calls whom, what happens when things fail, and the guardrails.
- [Bad data and edge cases](docs/05_bad_data_and_edge_cases.md): what DockPilot does when the data is wrong or something breaks, with the proof for each and the honest gaps.
