# DockPilot: submission

**Name:** Nikhilesh Pandey

**Repo link:** https://github.com/B0n-Echo/dockpilot-agent

**Live demo link:** https://dockpilot-agent.onrender.com
- driver chat: `/`
- warehouse page: `/warehouse`, password protected; the password is sent separately
- free tier: the first visit can take about a minute to wake up

**Stack used:**
- Python 3.12, standard library only for the app (`sqlite3`, `http.server`)
- SQLite
- uv
- LangSmith 0.16.2 for traces
- Claude Haiku via OpenRouter, for reading messages only; rules are the fallback
- pytest 9.2.0
- Render for hosting

All versions are in the [README](https://github.com/B0n-Echo/dockpilot-agent#stack-and-library-versions) and pinned in `uv.lock`.

---

## 1. Problem Definition Sheet and data audit

**Links:**
- [Problem Definition Sheet](https://github.com/B0n-Echo/dockpilot-agent/blob/main/docs/01_problem_definition_sheet.md)
- [Data audit](https://github.com/B0n-Echo/dockpilot-agent/blob/main/docs/02_data_audit.md): 14 findings, each with the query that proves it and its result

**Top data issue you found:**
- **DA-02:** a pending booking (APT1013A) locks slot SLOT-JAI-018, but the email asking the warehouse to confirm (OM002) was never sent: it's still QUEUED.
- Nothing times out, so the slot can stay invisible to every other driver all day while the warehouse doesn't even know it was asked.
- Our fix: a 30-minute confirmation timeout that alerts a coordinator and tells the driver once.

## 2. Decision register

**Link:** [Decision register](https://github.com/B0n-Echo/dockpilot-agent/blob/main/docs/03_decision_register.md): all 13 decisions, each with our answer, reason and evidence from the build.

**How you order competing requests:**
- Only trucks the slot really fits may compete. Confirmed bookings are never moved automatically.
- **Normal day:** the first valid request wins, at once. One write lock plus unique database indexes mean two requests can never both win.
- **Rush** (5+ requests in 10 minutes): requests for one slot are collected for 30 seconds, then ranked by **priority → harm already done → who asked first**.
- A clear winner is decided automatically. **A near-tie goes to a coordinator.**
- The loser always hears why and gets the next options.
- No truck is bumped twice in a day.
- A critical load that's squeezed out raises an alert naming the booking a coordinator could move.

## 3. Prototype: how to run it (one command)

```bash
uv run python -m dockpilot.webapp --reset
```

Then open http://127.0.0.1:8001 (driver chat) and http://127.0.0.1:8001/warehouse.
- This needs only [uv](https://docs.astral.sh/uv/); it installs Python 3.12 and the locked libraries itself.
- Tests: `uv run pytest` runs 197 checks, offline.

## 4. Scenario run video

**Link:** [S05 escalation video](https://drive.google.com/file/d/1Ta9ckrGLKWTaeWE5UNY30sDjw_75iKhc/view?usp=sharing). Also: [happy-path video](https://drive.google.com/file/d/1Icg0N7FLIxU844S-cVqxk0F4RxRdZ0_Z/view?usp=sharing), where Kailash is late, picks a slot, and the warehouse confirms. The scripted replay of the escalation case: [S05 transcript](https://github.com/B0n-Echo/dockpilot-agent/blob/main/docs/scenarios/S05.md). All 14 cases are in the [replay report](https://github.com/B0n-Echo/dockpilot-agent/blob/main/docs/scenarios/report.md); replay with `uv run python -m dockpilot.scenarios`.

**Which case escalates and why:**
- **S05, a reefer truck (SHP1015).** The driver says "Evening traffic. ETA 6:30. Can the reefer unload tonight?"
- The only refrigerated dock is unavailable after 18:30. **28 options were checked and none fit**: wrong dock type, needs refrigeration, the truck arrives too late, dock problem.
- DockPilot doesn't invent an answer. It tells the driver a coordinator will call, and hands over one record (`NO_FEASIBLE_SLOT`) with:
  - the driver, shipment and current booking
  - the latest arrival time and its source
  - every option checked, with why each one failed

## 5. Integrity test

**Links:**
- [The race](https://github.com/B0n-Echo/dockpilot-agent/blob/main/src/dockpilot/integrity.py): `uv run python -m dockpilot.integrity`
- [Its tests](https://github.com/B0n-Echo/dockpilot-agent/blob/main/tests/test_integrity.py)

**Result** (run on 28 Nov 2026): 8 separate programs race for the same slot, 100 times.

| Requests sent | Slots won | Double bookings |
|---|---|---|
| **800** (100 rounds × 8 racers), plus 800 retries of the same requests | **100**: exactly one winner every round | **0** |

- Every one of the 50 duplicate requests from a winner got the same hold back.
- 6 different trucks won rounds, so the start really is a race.
- **Control run:** the same race without the lock and the indexes (`--naive`) double-booked in **20 of 20** rounds, and the same check caught every one.

## 6. Client readout

**Link:** [One-page client readout](https://github.com/B0n-Echo/dockpilot-agent/blob/main/docs/09_client_readout.md): policy, limits and risks.

**Optional scheduling tool:** not attempted as a separate tool. The warehouse page lists pending bookings and coordinator hand-offs, and the timer re-homes bookings when a dock breaks or a truck doesn't show.

**What does not work yet:**
- **Login codes are shown on screen** (demo). Real email codes through Amazon Cognito are built and tested, but parked: our AWS account blocks Cognito. A login limit (3 codes per 15 minutes) stops guessing.
- **The chat is a web page, not WhatsApp or SMS.** Drivers who aren't chatting aren't told when their booking changes.
- **Coordinators can't yet close a hand-off,** or move another truck's booking, from the tool.
- **A driver's own limits** ("not before 12", "leave by 1:30") are read but not yet used to choose slots.
- **No rate limit on chat messages.**
- **Timings await the client's confirmation:** the hold time, the 30-minute warehouse timeout, the 30-minute buffer and the rush trigger are stated defaults in one settings file.
