# How DockPilot works: one message, end to end

A walkthrough built from **real traces** (web demo, 26 Nov 2026, Claude Haiku via OpenRouter). Use it to learn the system, and as the backup for "do you understand your system inside and out?".

**See it yourself:**

```bash
uv run python -m dockpilot.webapp --reset --trace
```

Chat as Kailash, then, in a second terminal:

```bash
uv run python -m dockpilot.trace --last
```

**Three settings decide what's traced:**

| Setting | Effect |
|---|---|
| `DOCKPILOT_TRACE=1` (or `--trace`) | Record every step locally (`python -m dockpilot.trace --last`) |
| `LANGSMITH_TRACING=true` | Record, **and** send each turn to LangSmith (project `LANGSMITH_PROJECT`, default `dockpilot`) |
| `LANGSMITH_API_KEY` | Needed to send. Kept but unused while `LANGSMITH_TRACING=false` |

Tests always run with tracing off and LangSmith unreachable, whatever `.env` says (`tests/conftest.py`). **Drivers never see traces** (the in-chat panel was removed in dp-23): show them in LangSmith.

---

## 1. The one-minute version

- A driver's message goes through **one function**, `conversation.handle_message`.
- **The AI is called in exactly one place:** `llm_reader.read_message_ai`, which turns the free text into fields (intent, arrival time, how sure). It is reached from **two** call sites: every driver message, and the re-read of a message that was parked while asking "which trip?". It never decides anything.
- **Everything after reading is plain code:**
  - which trip
  - ask or act
  - which slots fit (15 rules)
  - who gets a contested slot (D7)
  - hold it (one writer at a time)
  - book it, escalate it
- **A timer** (every 5 s in the web demo) expires holds, handles drift (a dock breaking, a no-show, a new arrival time recorded elsewhere), settles rush-hour rivals, chases the warehouse after 30 min, and nudges or closes silent chats.

## 2. One message, step by step (real traces)

### Message 1: "I am late by one hour." (09:45)

```
driver message · I am late by one hour.                                   1690 ms
   reply: [ETA] Thanks. Your plan was 10:00. Will you reach around 11:00? Reply with your arrival time, like 11:15.
├─ login check  1 ms  ok · DRV013 · 1 trip(s) · chosen SHP1013
├─ read the message  1679 ms  → ai: REPORT_DELAY, no clock time (LOW), ask first
│  └─ AI model call (anthropic/claude-haiku-5.5) ★AI  1678 ms
│        in:  [system] (fixed instruction, 14 lines) [user] Sent at 09:45: I am late by one hour.
│        out: {"intent": "REPORT_DELAY", "eta": null, "eta_confidence": "LOW", "delay_minutes": 60, …}
└─ act on the message  1 ms  ETA
   └─ ask for the arrival time (R7)  1 ms  asked, nothing held
```

| Step | File · function | What happens and why |
|---|---|---|
| login check | `auth.get_session` | The session must exist, not be expired, and the driver must **still** have a trip in progress. Otherwise nothing happens. |
| read the message | `llm_reader.read_message_ai` | The rules read the message first (`reader.read_message`), then the AI. The AI's JSON is checked field by field (`_validate`): any wrong value means the rules' reading is used instead. |
| AI model call ★ | `llm_reader._call_openrouter` | One HTTPS request (10 s limit). The input is a fixed instruction plus "Sent at 09:45: <text>". The AI is told never to turn "late by one hour" into a clock time. |
| act on the message | `conversation._fresh` | A delay with no clock time is **vague**. |
| ask for the arrival time | `conversation._ask_eta` | **R7:** one clarifying question, with a guess (plan 10:00 + 60 min). **Nothing is held**, because a guess must not become a promise (the client's DA-03). |

### Message 2: "should make it by half past eleven" (09:45)

```
driver message · should make it by half past eleven                       3467 ms
   reply: [CHOICE] You can make D2 12:00–13:00 (held for you until 09:55). Reply 1 to book it, 2 for D4 12:00, 3 for D1 13:00.
├─ login check  1 ms
├─ same message again?  0 ms  no
├─ read the message  3449 ms  → ai: UNKNOWN, ETA 11:30 HIGH
│  └─ AI model call ★AI  3448 ms  … "eta": "11:30", "eta_confidence": "HIGH" …
└─ answer to the time question  14 ms  CHOICE
   └─ offer options  13 ms
      ├─ slot search  9 ms  SHP1013 → 67 candidates checked · 3 fit · best D2 12:00
      └─ who gets it (D7)  3 ms  → normal day: first come, first served → held
         └─ hold the slot  2 ms  SHP1013 SLOT-JAI-019 → held until 09:55
```

| Step | File · function | What happens and why |
|---|---|---|
| same message again? | `conversation._resend` | A resend (the same text within 2 min, already answered) gets the earlier reply and nothing new. |
| read | AI | "Half past eleven" is something the rules can't read; the AI can, and its reading stands. |
| answer to the time question | `conversation._answer_eta` | Saves the arrival time in the client's `eta_updates` table (`DRIVER_DECLARED`). |
| slot search | `options.find_options` → `feasibility.check_feasibility` | For each slot from now on (and back-to-back runs for long unloads), **15 rules**: dock type, reefer, weight, arrival, unload length, open, dock events, hours, taken, held… The best 3 that pass, earliest first. |
| who gets it (D7) | `allocation.request_slot` | Normal day: the first valid request wins at once. In a rush (5+ requests in 10 min), rivals wait 30 s and are ranked by priority → how late they already are → who asked first. |
| hold the slot | `holds.place_hold` | Inside **one write transaction** (`BEGIN IMMEDIATE`): expire old holds, re-check the slot, write the hold. Only one writer at a time, so two drivers can never both win. |

### Message 3: "1"

```
├─ read the message  1695 ms  → ai: UNKNOWN, no clock time
│  └─ AI model call ★AI  1694 ms
└─ answer to the options  3 ms  WAREHOUSE
   └─ booking request  2 ms  PENDING_CONFIRMATION APT-…
```

`bookings.convert_hold` turns the hold into a **PENDING** booking. The driver's old booking stays current until the warehouse confirms (R1). The driver hears *"Great, I'm confirming D2 12:00 with the warehouse now…"*.

> **Found by tracing:** the AI is called even for "1", which costs 1.7 s, though the rules read it perfectly. Skipping the AI for short replies (digits, yes/no) is a clear improvement for later.

### The warehouse confirms

```
warehouse confirms · APT-…                                    4 ms
├─ warehouse confirms  2 ms
└─ tell the driver the warehouse's answer  2 ms  Confirmed: D2 at 12:00, ref WH-9271.
```

`bookings.confirm_booking` makes the request CONFIRMED and current, and cancels the old booking. `conversation.notify_booking_result` posts the answer into the driver's chat.

**Time budget:** reading with the AI takes **1.7–3.4 s** per message. **Everything else (all the decisions) takes about 15 ms.**

## 3. Who calls whom

The full map, with a picture, file:line links for four requests and the data map, is [06_code_map.md](06_code_map.md).

```
web page ──► webapp.py routes
   /api/login/*  ──► auth.request_code / verify_code
   /api/chat     ──► conversation.handle_message
                        ├─ auth.get_session
                        ├─ _resend
                        ├─ _read ──► llm_reader.read_message_ai ──► reader.read_message (rules)
                        │                                       └─► _call_openrouter  ★ THE ONLY AI CALL
                        ├─ _pick_shipment ──► _match_trip, auth.choose_shipment   (2+ trips; may re-read: ★ second call site)
                        ├─ _answer_eta / _answer_choice / _waiting / _fresh
                        │     └─ _offer ──► escalations.escalate_if_no_options ──► options.find_options ──► feasibility.check_feasibility ×N
                        │               └─► _hold_and_list ──► allocation.request_slot ──► holds.place_hold (+ critical_alert)
                        │     └─ _answer_choice ──► bookings.convert_hold
                        └─ _finish (save the reply, set the chat status)
   /api/warehouse/confirm|reject ──► bookings.confirm_booking / reject_booking ──► conversation.notify_booking_result
timer (every 5 s) ──► holds.expire_holds · allocation.settle_requests → notify_allocation
                      bookings.check_warehouse_timeouts → escalations (+ deliver) → notify_warehouse_timeout
                      conversation.check_silent_drivers (nudge 5 min, close 10 min)
timer (dp-22) ──► lifecycle.check_drift: handle_dock_event · mark_no_shows · recheck_booking (new arrival times)
                  └─► conversation.notify_drift (driver told if their chat is open and idle)
```

## 4. What if it fails?

| Failure | What the system does | Where you see it |
|---|---|---|
| **AI has no key** | Rules read the message | Trace note: *rules used: no AI key or model in .env* |
| **AI slow** | 10-second limit, then rules | *rules used: AI timed out* (tested) |
| **AI unreachable** | Rules | *rules used: AI unreachable (URLError)* |
| **AI gives a wrong answer** (bad JSON, an unknown value, a time like "25:00") | The answer is checked field by field and rejected; rules | *rules used: AI answer invalid (…)* |
| **AI "understands" a vague delay as a time** | The rules' "ask first" still wins for relative delays | Test `test_ai_reading_of_a_clock_time…` |
| **Two drivers grab one slot at the same instant** | One write transaction at a time, plus the database's one-active-per-slot indexes | Integrity proof: 100 rounds, 0 double bookings |
| **Duplicate or resent message** | Same reply, nothing new booked | Replay S09; integrity proof (800 resends) |
| **No slot fits** | Escalation with the full hand-off record; the driver is told why | Replay S05 (M3 100% / 100%) |
| **Warehouse never answers** | 30 min → coordinator hand-off; the driver is told once | Replay S06 |
| **Driver goes silent** | Nudge at 5 min, close at 10 (+ `ETA_UNRESOLVED` if we asked for the time) | Replay S03 |
| **Two trips, unclear which** | Asks which; "in the yard" picks the one at the warehouse | Replay S08, S10 |
| **Server error inside a message** | The driver sees "Sorry, something went wrong on my side…"; the full error is printed in the server terminal | `test_server_error_gives…` |
| **Tracing or LangSmith fails** | Never breaks the agent: errors are caught; sending runs in the background | `trace._post` |

## 5. Guardrails and benchmarks

| Guardrail | How it's enforced | Proof |
|---|---|---|
| The AI only reads; code decides | The AI's output is only a `Reading` (fields), validated | `llm_reader._validate`, tests |
| Never double-book (M1 = 0) | `BEGIN IMMEDIATE` + unique indexes + the M1 check query | `dockpilot.integrity`: 100 × 8 racers, 0; naive version caught 20/20 |
| Never confirm an infeasible slot (M2 = 0) | 15-rule check before every hold and booking | Replay M2 = 0 |
| Never invent; escalate (M3) | No options → hand-off with every M3 field | Replay M3 100% / 100% |
| Policy respected (M4 = 0) | Confirmed bookings are locked (R1); priority only in a rush; near-ties go to a person | Replay M4 = 0; `test_allocation.py` |
| Ask before promising (R7) | Vague arrival → one question, no hold | Trace above; replay S03 |
| Only real drivers with a trip | Login check on **every** message | `test_auth.py` |
| The driver is never left waiting | An answer to every message; a timeout message | dp-19 tests |
| No secrets or phone numbers in traces | Keys are never recorded; phone numbers are masked | `test_trace.py` |

**Benchmarks you can quote:**
- **Tests:** 160 checks, offline.
- **Integrity proof:** 0 double bookings in 100 rounds × 8 racers, and the naive version double-booked in 20 of 20 rounds.
- **Replay:** M1 0, M2 0, M3 100% / 100%, M4 0; 10 of 14 cases resolved with no coordinator.
- **Speed:** the AI read takes 1.7–3.4 s per message; all decisions together take about 15 ms.
