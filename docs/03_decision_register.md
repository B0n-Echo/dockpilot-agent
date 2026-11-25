# SetuHaul Logistics: Decision Register

> Hand-in #2. The 13 decisions from page 8 of the brief. Each gets **our answer, our reason and our evidence**.
> Inputs: [01_problem_definition_sheet.md](01_problem_definition_sheet.md) and [02_data_audit.md](02_data_audit.md).

---

## Status

*Updated 2026-11-26 after the build. "✅ Built and tested" means the code and the replay answer it; anything left is a client answer (Q1–Q13), listed under that decision.*

| # | Decision | Status |
|---|---|---|
| 1 | What must you collect before you show useful options? | ✅ Built and tested |
| 2 | How does a conversation link to the right driver, shipment and appointment? | ✅ Built and tested |
| 3 | How do you set the revised ETA and show how sure it is? | ✅ Built and tested |
| 4 | What makes a slot feasible for this shipment? | ✅ Built and tested |
| 5 | What does "available" mean while another driver is deciding? | ✅ Built and tested |
| 6 | When does an option become a hold, request, reservation or confirmed booking? | ✅ Built and tested |
| 7 | How do you order simultaneous requests when capacity runs short? | ✅ Built and tested |
| 8 | (Optional) When does the facility schedule recalculate, which work is fixed, and what is the objective? | 🟡 Partly agreed (optional, not built) |
| 9 | How do you handle stale options, cancellations, duplicate messages and retries? | ✅ Built and tested |
| 10 | What happens when no feasible slot exists? | ✅ Built and tested |
| 11 | What do you say when the preferred slot is not granted? | ✅ Built and tested |
| 12 | Which decisions need human approval or takeover? | ✅ Built and tested |
| 13 | How will you prove the system did not double-book capacity? | ✅ Built and tested |

---

## UNDERSTAND

### D1. What must you collect before you show useful options?

- **Why it matters (brief):** Missing inputs produce wrong options.
- **Evidence to bring (brief):** Three example chats with the fields you ask for.
- **Where to look:** DA-03 (booked before the question was answered) · MSG002/MSG004/MSG006 (agent's existing questions) · R7

**Our answer:**

- The agent must know **which shipment** before showing options: from the driver, or from the driver's active loads. If it can't tell, it asks; if still unclear, it escalates.
- If the ETA is vague or LOW confidence, the agent asks **one clarifying question before** showing or booking anything (R7).

**Reason:**

- DA-03: a slot was booked at 09:32 while the agent's own question ("does one hour mean 11:00?") was still unanswered. A guess became a promise.
- `chat_threads.shipment_id` is optional, so a chat can arrive with no load attached.

**Evidence:**

- THR003 trace: MSG005 → MSG006 (question) → APT1013A booked anyway (DA-03).
- THR010 / MSG017: driver with two loads, no shipment linked.

**Built and tested** (2026-11-26; transcripts in [scenarios/](scenarios/report.md)):

| From the database (never asked) | From the driver (asked only if missing) |
|---|---|
| Driver (by phone), active trips, vehicle, load weight, dock type, reefer need, unload time, priority, current booking, original ETA, facility rules, dock events | Which trip (only if 2+ trips and the message doesn't say: shipment, order number or its last digits, vehicle, or "I'm in the yard") · arrival time (only if vague: one question, R7) · yes/no or 1/2/3 for an option |

Three example chats:
- [S03](scenarios/S03.md): "I am late by one hour." → *"Your plan was 10:00. Will you reach around 11:00? Reply with your arrival time, like 11:15."* No hold until answered.
- [S02](scenarios/S02.md): "Tyre repaired. I can reach 11:10. I need to leave the warehouse before 1:30…" → no question at all; options straight away.
- [S10](scenarios/S10.md): two trips, no reference → *"You have 2 trips today: ORD-260804-004 (now at the warehouse) or ORD-260804-020 (due 18:00). Which one is this about?"*

**Status:** ✅ Built and tested

---

### D2. How does a conversation link to the right driver, shipment and appointment?

- **Why it matters (brief):** One driver can hold two shipments.
- **Evidence to bring (brief):** A message-to-rows trace for an unclear case.
- **Where to look:** Your THR003 trace · DA-11 (DRV007 on two trucks) · THR010 / DRV004 (no shipment)

**Our answer:**

- No booking action until the thread is tied to **one** shipment.
- If the driver has more than one active shipment, the agent confirms which one (by vehicle or reference). If it can't, it escalates.

**Reason:**

- One driver can hold two loads (DRV004), and the data even shows one driver on two trucks at once (DRV007). Acting on the wrong load moves the wrong booking.

**Evidence:**

- THR003 trace: chat_threads → shipments → appointments (`is_current = 1`) → appointment_slots → docks.
- V2: 11 of 12 threads linked correctly; THR010 is not (DA-11).

**Built and tested** (2026-11-26; transcripts in [scenarios/](scenarios/report.md)):

**The matching rule:**
1. Phone (any format) → `drivers` (must be ACTIVE).
2. Active trips: shipments on the road or at the warehouse, or with a live booking. Cancelled or completed ones never count; "assigned" alone doesn't either.
3. One trip → tied at login. Two or more → the message must say which: shipment id, order number or its last digits, vehicle, or "I'm in the yard" (the one trip at the warehouse). Otherwise the agent asks. A new topic from a two-trip driver asks again.
4. The trip's live booking (`appointments`, live status) → its slot(s) and dock.

**THR010 trace** (replay [S10](scenarios/S10.md), DRV004):

| Time | Message | Rows |
|---|---|---|
| 09:31:00 | "I will be late by 45 minutes." | `chat_messages` (REPORT_DELAY); `conversation_state` awaiting SHIPMENT, message parked |
| 09:31:30 | "020" | `driver_sessions.shipment_id` = SHP1020; `chat_threads.shipment_id` = SHP1020; parked message re-read → "Will you reach around 18:45?" |
| 09:32:00 | "not sure" | `eta_updates` ETA_DP-… 18:45 LOW (+30 min buffer) → options; `slot_holds` HGRP-… on SLOT-JAI-013 (D1 20:00) |
| 09:32:30 | "1" | `appointments` APT-… PENDING, `replaced_appointment_id` = APT1020; hold CONVERTED |
| 09:37:30 | warehouse confirms | APT-… CONFIRMED and current; APT1020 (D2 18:00) CANCELLED as replaced (R1) |

**Status:** ✅ Built and tested

---

### D3. How do you set the revised ETA and show how sure it is?

- **Why it matters (brief):** A repair time is not an arrival time.
- **Evidence to bring (brief):** Your ETA rule applied to two messages.
- **Where to look:** `eta_updates.confidence_code` · `v_latest_eta` (defaults to HIGH) · MSG003 (tyre repaired) · MSG005 · R7

**Our answer:**

- Every ETA is stored and shown with its **source and confidence**; it is never presented as exact (PDS §2.6).
- GPS is not assumed. ETA sources stay pluggable through `eta_updates.source_type` and `confidence_code`.
- **R7:** a LOW-confidence ETA triggers one clarifying question, then a booking with a buffer. Escalate only if no buffered slot exists, the load is high priority, or the driver goes silent.

**Reason:**

- SHP1013 was booked with zero slack on "late by one hour" (LOW confidence). Uncertainty can be handled by a rule; it does not need a human every time.

**Evidence:**

- ETA008 ("Driver said only: late by one hour", LOW) · DA-03 · V1: ETA records are consistent (F8).

**Built and tested** (2026-11-26; transcripts in [scenarios/](scenarios/report.md)):

- **Text → ETA:** a clock time counts as the arrival only after "reach / arrive / ETA" (or as the whole answer to our time question). "Tyre repaired" is not an arrival. "Around / about" → MEDIUM. A relative delay ("late by one hour") gives **no** time: LOW, and one question. The AI reader follows the same rule; when it reads a real clock time ("half past eleven"), its reading stands.
- **`v_latest_eta`:** we don't use its blanket HIGH. Our `latest_eta` ignores updates from after "now" and labels a shipment with no update `SHIPMENT_RECORD` (or `ORIGINAL_PLAN`), so the hand-off shows where the time came from.
- **Max clarifying turns: 1.** If the second answer is still vague, the best guess (plan + delay) is used as LOW, so the 30-min buffer applies. Silence → nudge at 5 min, close at 10 with `ETA_UNRESOLVED`.
- **The rule on two messages** (rule reader, real output):
  - MSG003 "Tyre repaired. I can reach 11:10. I need to leave the warehouse before 1:30…" → ETA **11:10 HIGH**, reason BREAKDOWN, leave-by 13:30, no question.
  - MSG005 "I am late by one hour." → **no ETA**, LOW, delay 60 min, *ask first*.

**Still open:** buffer size, 30 min for now (*waiting on client, Q6*).

**Status:** ✅ Built and tested

---

## DECIDE

### D4. What makes a slot feasible for this shipment?

- **Why it matters (brief):** Many rules apply at the same time.
- **Evidence to bring (brief):** A written check run on three shipments.
- **Where to look:** DA-04, DA-05, DA-07, DA-08, DA-14 · `facility_rules` · PDS §2a M2

**Our answer:**

- Feasibility is decided by **code, not the agent** (PDS §2.5).
- A slot is feasible only if all of these pass: right facility · right dock type and reefer · load within dock weight limit · slot starts after the truck can arrive · unload time fits the window (or consecutive slots) · slot OPEN, no dock event overlapping · within operating hours · not already taken or held.

**Reason:**

- The database accepts infeasible bookings (H3). Each check above failed at least once in the snapshot.

**Evidence:**

- DA-04 (late + overweight critical load), DA-05 (6 stale bookings), DA-06 (dock events), DA-07 (weight), DA-08 (unload > slot), DA-14 (operating hours).

**Built and tested** (2026-11-26; transcripts in [scenarios/](scenarios/report.md)):

- **Slack:** a HIGH or MEDIUM arrival must be at or before the slot start; a LOW one gets +30 min first.
- **The check on three shipments** (real output of `check_feasibility`):
  - **SHP1014 on D1 11:00** (the client's real booking, DA-04): *"Load is 21,000 kg; dock D1 takes at most 20,000 kg." · "Truck can be there at 11:25; the slot starts 11:00." · "Unloading takes 75 min; the slot gives 60 min."*
  - **SHP1013 on D2 11:00** (APT1013A): *"Truck can be there at 11:30 (ETA 11:00 is LOW confidence, plus 30 min buffer); the slot starts 11:00."*
  - **SHP1015 (reefer) after 18:30** ([S05](scenarios/S05.md)): all 28 candidates fail. D1–D4 and D6: *wrong dock type / needs refrigeration*. D5, the only reefer dock: *slot blocked, dock problem* (maintenance 18:00–22:00).

**Still open:** buffer size (*waiting on client, Q6*).

**Status:** ✅ Built and tested

---

### D5. What does "available" mean while another driver is deciding?

- **Why it matters (brief):** Two readers can see one slot.
- **Evidence to bring (brief):** A definition and the state it needs.
- **Where to look:** `v_slot_availability` (PENDING counts as occupied) · H1 · hold timer in PDS §6

**Our answer:**

- **Available** = slot is OPEN **and** has no active booking (PENDING, CONFIRMED, IN_PROGRESS) **and** has no active hold. `slot_status` alone never means available.
- Holds live in a **new `slot_holds` table** (option B). We add; we don't change the client's tables, CHECK lists or indexes.

| Column | Purpose |
|---|---|
| `hold_id` | Primary key |
| `slot_id` | → `appointment_slots`: which slot is held |
| `shipment_id`, `driver_id`, `thread_id` | Who holds it, and from which chat |
| `hold_status` | ACTIVE → CONVERTED (driver said yes) or EXPIRED (timer ran out or driver declined) |
| `created_at`, `expires_at` | 10 min after creation, 5 in a spike |
| `appointment_id` | → `appointments`: empty until the hold converts, then points to the new PENDING booking |
| `request_key` | Idempotency key, so a retried message can't create a second hold (D9) |

**How it stays safe:**

- **One active hold per slot:** a partial unique index on `slot_holds(slot_id) WHERE hold_status = 'ACTIVE'`, the same pattern the client already uses.
- **Hold vs booking on the same slot:** one index can't cover two tables, so the capacity layer does every claim inside **one write transaction**: expire overdue holds → check no active hold and no active booking → insert. Only one writer runs at a time, so two drivers can't both pass the check.
- **HELD → PENDING** happens in the same single transaction: insert the appointment, mark the hold CONVERTED, link the two. Both happen, or neither does.

**Reason:**

- **Safer for the client:** existing tables, constraints and history stay untouched; removing the feature means dropping one table.
- **Clean history:** many holds expire unused in a spike; they stay out of `appointments`, which keeps only real bookings.
- **Traceable:** linked to the slot and, once converted, to the appointment, so every booking can be traced back to the hold it came from.
- **Trade-off accepted:** the cross-table check lives in our code (one transaction) rather than in a single index. The integrity test (D13) proves it holds.

**Evidence:**

- THR003 trace: SLOT-JAI-018 is OPEN yet taken; `v_slot_availability` counts PENDING as occupied.
- H1: the schema has no hold state, so two drivers shown the same slot can both pick it.
- Schema review: partial unique indexes only guard one table each.

**Built and tested** (2026-11-26; transcripts in [scenarios/](scenarios/report.md)): the nudge is built. With no reply 5 min after the options, the driver gets *"Are you still there? D2 12:00 is held for you until 09:30. Reply 1 to book it."* After 10 min the chat closes and the hold runs out at the same moment. The integrity proof (D13) shows the one-writer-at-a-time hold never double-books.

**Status:** ✅ Built and tested

---

## COMMIT

### D6. When does an option become a hold, request, reservation or confirmed booking?

- **Why it matters (brief):** Showing is not reserving. Reserving is not confirming.
- **Evidence to bring (brief):** Your states, who moves each one and what the driver sees.
- **Where to look:** DA-01, DA-02 · `appointment_status` values · Q1, Q2 · hold timer (10 / 5 min)

**Our answer:**

| State | Means | Moved forward by | Exits if it fails | Driver sees |
|---|---|---|---|---|
| **Shown** (not stored) | Agent listed feasible options | Driver picks one → HELD | None: nothing was reserved | "11:00 at Dock 2 or 12:00 at Dock 4. Which one?" |
| **HELD** (new) | Slot reserved for this driver for 10 min (5 in a spike) | Driver says "yes, book it" → PENDING | Timer runs out or driver declines → **EXPIRED** (slot free again; nudge sent before expiry) | "Holding 11:00 at Dock 2 for 10 min. Reply YES to request it." |
| **PENDING_CONFIRMATION** | Request sent to the warehouse; slot still blocked | Warehouse confirms → CONFIRMED | Warehouse says no → **REJECTED** · driver withdraws → **CANCELLED** · timeout → coordinator chases (stays PENDING) | "Requested, **not final yet**. I'll message you when the warehouse confirms." |
| **CONFIRMED** | Warehouse said yes; truck not yet at the dock | Truck docks (check-in record) → IN_PROGRESS | Truck never arrives within the grace period (RULE002) → **NO_SHOW** · ETA no longer fits, driver or customer cancels, or a human bumps it (R1) → **CANCELLED** | "Confirmed: Dock 2, 11:00–12:00." |
| **IN_PROGRESS** | Truck at the dock, unloading | Unload ends → COMPLETED | Dock fails mid-unload → coordinator handles it (no automatic cancel) | "You're at Dock 2." |
| **COMPLETED** (end) | Unloaded | — | — | — |
| **REJECTED** (end) | Warehouse refused | Agent offers next options (R5) as a **new** appointment | — | "The warehouse couldn't take 11:00 because ___. Next options: ___." |
| **CANCELLED** (end) | Booking withdrawn | If the truck is still coming → **new** appointment linked by `replaced_appointment_id` | — | "Your 11:00 slot is cancelled because ___. Next options: ___." |
| **NO_SHOW** (end) | Truck missed the slot | Slot released; agent contacts the driver | — | "You missed 11:00. Are you still coming?" |
| **EXPIRED** (new, end) | Hold ran out | Slot free again | — | "Your hold on 11:00 has expired. Want fresh options?" |

Rules:

- **Only HELD, PENDING, CONFIRMED and IN_PROGRESS block a slot.** Every other state frees it.
- **An end state never moves again.** A retry or reschedule is always a new appointment, so history stays clean.
- **IN_PROGRESS can't become NO_SHOW or REJECTED:** the truck is already there.
- HELD and EXPIRED are stored in the new `slot_holds` table (D5).

**Reason:**

- Two waits (driver and warehouse) need two timers. A pending booking with no timeout locks a slot all day.
- The hold closes the gap where two drivers can pick the same slot (H1).
- The driver is never told "confirmed" before the warehouse confirms.

**Evidence:**

- DA-01 (thread and booking statuses disagree) · DA-02 (pending slot whose request was never sent) · APT1012A (CONFIRMED → CANCELLED when the ETA moved) · APT1018 (NO_SHOW) · CHK1002 (IN_PROGRESS = truck at the dock).

**Built and tested** (2026-11-26; transcripts in [scenarios/](scenarios/report.md)):

- Every state above is built. The driver is answered at once after "1" (*"Great, I'm confirming D2 12:00 with the warehouse now…"*) and on any message while waiting (*"I'm still checking…"*).
- After the 30-min warehouse timeout, the coordinator gets the hand-off **and** the driver is told once ([S06](scenarios/S06.md)).

**Still open** (*waiting on client*): the warehouse timeout value (Q2; default 30 min), the spike trigger (Q3 / U4; default 5 requests in 10 min), and whether confirmation is always required (Q1; we always require it).

**Status:** ✅ Built and tested

---

### D7. How do you order simultaneous requests when capacity runs short?

- **Why it matters (brief):** Fairness and service levels conflict.
- **Evidence to bring (brief):** A policy tested on competing trucks.
- **Where to look:** Policy R0–R6 (PDS §6) · guide §6 "Simultaneous slot competition" (SHP1006, 1012, 1013, 1014) · open: first-wins vs collection window

**Our answer:**

**Policy v1** (unchanged):

- R0 only feasible trucks compete.
- R1 confirmed bookings are locked; only a human can bump one, and only if the high-priority truck has no other feasible slot.
- R2 holds are respected until expiry.
- R3 rank by priority → harm already done → first asked.
- R4 a clear winner is decided automatically; a near-tie goes to a human.
- R5 the loser always hears why and gets next options.
- R6 never bump the same truck twice in a day.

**Mechanism (new):**

- **First-wins on the claim.** Every hold is taken inside one write transaction (D5), so the first valid request gets the slot instantly. Normal days rarely see two requests for one slot at the same moment, and drivers get an answer straight away.
- **Short collection window in spike mode.** When spike mode is on (same trigger as the 5-min hold, U4), requests for the same slot that arrive within **30 seconds** (configurable) are ranked by R0 → R3 before the winner is held. This is where priority acts automatically.
- **CRITICAL protection via a human, not automatically.** If a CRITICAL load's earliest feasible slot is later than a slot currently HELD or PENDING for a lower-priority truck, the system books the CRITICAL load's best available slot **and** alerts a coordinator, who may reassign (R1 extended to pending; R6 still applies). Confirmed bookings stay locked unless R1 is met.

**Replay: the four competing trucks** (requests between 09:19 and 09:44, standard docks at Jaipur DC)

Feasibility first (R0): all four ETAs are 11:00 or later, so no 11:00 slot gives any slack. Free standard slots: 12:00 on D2 and D4 (25 t docks); 13:00 on D1, D2, D3, D4.

| Order | Truck | Priority, ETA, constraints | Outcome | Why |
|---|---|---|---|---|
| 09:19 | SHP1013 | NORMAL · 11:00 **LOW** · 14 t · 60 min | No hold yet: agent asks first (R7). Once clarified, gets the next slot with slack: **13:00 D1** *(updated after the replay: D1 and D3 both fit; D1 wins the tie on dock code)* | Zero slack on a vague ETA is not a promise we can keep |
| 09:29 | SHP1012 | NORMAL · 11:10 · 8 t · 45 min · must leave by 13:30 | **12:00 D2** (held, then requested) | First feasible request for that slot |
| 09:34 | SHP1006 | HIGH · 11:20 · 7.8 t · 45 min | **12:00 D4**; old 10:00 booking (APT1006) replaced, since it can't be reached | First feasible request for that slot |
| 09:44 | SHP1014 | CRITICAL · 11:25 · 21 t · 75 min | **13:00–15:00 on D2** (two consecutive slots on a 25 t dock) **+ coordinator alert** *(updated after the build: the run is two full slots)* | 12:00 slots already held by lower priority; R1 bump not allowed automatically |

- A coordinator who chooses to act can move SHP1014 to **D4 12:00–14:00** and SHP1006 to 13:00, telling the SHP1006 driver why (R5).
- The real system booked SHP1014 into **D1 at 11:00**: too early for its ETA and too weak for its weight (DA-04). Under our rules that can't happen.

**Reason:**

- Requests in this case are **25 minutes apart**. No collection window can wait that long in a chat, so priority can't act purely by waiting. First-wins keeps answers instant; the coordinator alert keeps CRITICAL loads protected without the agent overturning another driver's slot on its own.
- In a real spike, requests do arrive seconds apart. That is where the window lets priority win automatically.

**Evidence:**

- The replay above (data: `v_slot_availability`, `v_latest_eta`, `driver_exceptions.reported_at`, `shipments`).
- Guide §6 "Simultaneous slot competition": SHP1006, SHP1012, SHP1013, SHP1014.

**Built and tested** (2026-11-26; transcripts in [scenarios/](scenarios/report.md)):

- **The four-truck table above is a test** on the real data (`tests/test_allocation.py`). The CRITICAL alert names *"D2 12:00–14:00 (SHP1012 NORMAL, pending); D4 12:00–14:00 (SHP1006 HIGH, pending)"*.
- **Rush mode is tested too:** HIGH beats NORMAL inside the 30-s window; a near-tie goes to the first asker plus a `NEAR_TIE` escalation.
- **The full-day replay differs, and that's expected** ([S01](scenarios/S01.md)–[S04](scenarios/S04.md)):
  - The dock breakdown at 09:16 re-homes SHP1005 into D2 12:00 first ([S13](scenarios/S13.md)).
  - The replay's warehouse confirms within 5 min, so by 09:44 the 12:00 bookings are **confirmed**. Confirmed bookings are locked (R1), so SHP1014 correctly gets **no** alert.
  - The replay's outcome: SHP1012 D4 12:00, SHP1006 D1 13:00, SHP1014 D4 13:00–15:00, SHP1013 D3 13:00.
- **Not built yet:** using a driver's own limits ("leave before 1:30", "after 12") to choose slots; a coordinator tool to move a booking (R1 bump, R6 check).

**Still open** (*waiting on client*): the window length (default 30 s) and spike trigger (U4); what "leave before 1:30" means (MSG004); whether coordinators may reassign PENDING bookings (extends R1).

**Status:** ✅ Built and tested

---

### D8. **Optional.** When does the facility schedule recalculate, which work is fixed, and what is the objective?

- **Why it matters (brief):** Changes can ripple through the whole day.
- **Evidence to bring (brief):** A rule, an objective and one replay.
- **Where to look:** Your C→A→B example · DA-06 (D2 overrun cascade) · CHK1003 early truck

**Our answer:**

- Principle: **evidence over estimates.** A truck at the gate goes before a truck that is only estimated, once the one already unloading finishes (C unloading → A at gate → B late).

**Reason:**

- An estimate can slip; a truck in the yard is certain.

**Evidence:**

- DA-06: the D2 overrun cascade · CHK1003 (early truck at the gate) · guide §6 "Early truck does not automatically win".

**Still open:**

- When the schedule recalculates, what is fixed, and the objective.
- Reconcile with "early truck does not automatically win" (confirmed bookings stay locked, R1).
- One replay.

*Not built (optional decision).* The nearest built piece: an early truck at the gate does not jump the queue ([S07](scenarios/S07.md)), and confirmed bookings stay locked (R1).

**Status:** 🟡 Partly agreed (optional, not built)

---

### D9. How do you handle stale options, cancellations, duplicate messages and retries?

- **Why it matters (brief):** Chat channels repeat messages.
- **Evidence to bring (brief):** One replayed case for each.
- **Where to look:** DA-05 (stale), DA-10 (cancellations), DA-12 (duplicates) · `dedupe_key` · `is_duplicate`

**Our answer:**

- **Stale options:** every new ETA re-checks the current booking; if it no longer fits, offer new options. Holds expire on their own (R2).
- **Cancellations:** if the truck is still coming, open an exception until it is rebooked. Every reschedule sets `replaced_appointment_id`.
- **Duplicates and retries:** every request carries an idempotency key; the same key returns the same result.

**Reason:**

- Stale bookings hide capacity, lost reschedules strand trucks, and a retried message must never book twice.

**Evidence:**

- DA-05 (stale), DA-10 (cancelled with no replacement), DA-12 (duplicate opened its own thread) · `dedupe_key` · `is_duplicate`.

**Built and tested** (2026-11-26; transcripts in [scenarios/](scenarios/report.md)): one case each:
- **Stale option:** a "yes" after the hold ran out → *"The hold ran out at 09:30. Here are fresh options. You can make…"* (`tests/test_conversation.py`). A booking the truck can no longer reach is replaced, and the old one cancelled only once the new one is confirmed ([S10](scenarios/S10.md): APT1020 → D1 20:00).
- **Cancellation:** a message about a cancelled shipment is refused at login with the reason ([S12](scenarios/S12.md)). A dock breakdown re-homes the booking it hits ([S13](scenarios/S13.md)).
- **Duplicate:** THR009, the same text 62 s later with a new message id, gets the same reply. It's marked `is_duplicate`, and nothing new is held ([S09](scenarios/S09.md)).
- **Retry:** the same request key returns the same hold or booking. The integrity proof resent all 800 requests: winners were answered from memory, losers refused again, no second booking (D13).

**Status:** ✅ Built and tested

---

## FAIL SAFELY

### D10. What happens when no feasible slot exists?

- **Why it matters (brief):** An invented answer is worse than an escalation.
- **Evidence to bring (brief):** The escalation message and the hand-off record.
- **Where to look:** THR005 / SHP1015 (reefer, no slot) · MSG010 · DA-13 (escalation email failed) · M3

**Our answer:**

- The agent **never invents** an answer: no feasible slot means escalate (PDS §2.5).
- The hand-off record holds: driver, shipment, current appointment, latest ETA + source, options checked and why each failed, driver constraints (M3).
- An escalation counts as done only when delivered and acknowledged; a failure retries and alerts a coordinator.

**Reason:**

- An invented "yes" breaks a promise; a silent failure leaves the truck with nobody handling it.

**Evidence:**

- THR005 / SHP1015 (reefer, no slot) · MSG010 · DA-13 (escalation email OM004 FAILED) · M3 target 100% / 100%.

**Built and tested** (2026-11-26; transcripts in [scenarios/](scenarios/report.md)): [S05](scenarios/S05.md), THR005 at 16:24:

> Driver: "Evening traffic. ETA 6:30. Can the reefer unload tonight?"
> DockPilot: "No slot fits an arrival at 18:30: wrong dock type, needs refrigeration, truck arrives too late. I've passed this to the coordinator; they'll call you."

The filled hand-off record (from the replay; ids vary per run):

```
ESCALATION ESC-…  [NO_FEASIBLE_SLOT]
  Shipment:      SHP1015   Driver: DRV015   Chat: DPT-…
  Booked now:    APT1015A
  Latest ETA:    18:30 (DRIVER_DECLARED, HIGH confidence)
  Driver says:   Earliest 18:30; latest not given. Driver reported: Only compatible reefer dock is unavailable after the new ETA.
  Summary:       No feasible slot for SHP1015 (HIGH, needs reefer) arriving 18:30. 28 options checked, none fit.
  Options checked:
    D1  17:00-22:00  5 options: wrong dock type (5), needs refrigeration (5), truck arrives too late (2), already booked (1)
    D2  17:00-22:00  5 options: wrong dock type (5), needs refrigeration (5), truck arrives too late (2)
    D3  17:00-22:00  5 options: wrong dock type (5), needs refrigeration (5), truck arrives too late (2)
    D4  17:00-22:00  5 options: wrong dock type (5), needs refrigeration (5), truck arrives too late (2)
    D5  17:00-22:00  5 options: truck arrives too late (2), slot blocked (4), dock problem (4)
    D6  17:00-21:30  3 options: wrong dock type (3), needs refrigeration (3), truck arrives too late (1)
  (Full reason for every option is stored in the record.)
```

M3 in the replay: **100% escalated, 100% complete.** Delivery is printed to the coordinator console and shown on the warehouse page. Acknowledging a hand-off isn't built yet.

**Status:** ✅ Built and tested

---

### D11. What do you say when the preferred slot is not granted?

- **Why it matters (brief):** Refusals without a reason cost trust.
- **Evidence to bring (brief):** Two refusal messages in plain words.
- **Where to look:** Policy R5 · MSG013 (early-arrival reply) · MSG020 (cancelled shipment)

**Our answer:**

- **R5:** the driver always hears **why** and gets the **next options**.

**Reason:**

- A refusal without a reason costs trust, and the driver can't plan.

**Evidence:**

- MSG013 (early-arrival reply explains the policy) · MSG020 (cancelled shipment).

**Built and tested** (2026-11-26; transcripts in [scenarios/](scenarios/report.md)):
- **No slot** ([S05](scenarios/S05.md)): *"No slot fits an arrival at 18:30: wrong dock type, needs refrigeration, truck arrives too late. I've passed this to the coordinator; they'll call you."*
- **Lost a slot to a higher priority in a rush** (`tests/test_allocation.py`): *"D2 12:00–13:00 went to a higher-priority load. It's busy right now: I'm checking who gets D4 12:00 and will tell you within a minute."*
- **Warehouse said no** (`tests/test_webapp.py`): *"The warehouse couldn't take D2 at 12:00: Dock crew short. You can make …"*, followed by fresh options with the best one held.

**Status:** ✅ Built and tested

---

### D12. Which decisions need human approval or takeover?

- **Why it matters (brief):** Safety, penalties and disputes carry risk.
- **Evidence to bring (brief):** A list with the trigger for each.
- **Where to look:** R1, R4, R7 escalations · PDS §2 boundaries · RULE005 (after 21:00) · Q5 coordinators

**Our answer:**

- A person approves or takes over when:
- Driver safety or an emergency is involved.
- Penalties, compensation or customer commitments are at stake.
- No feasible slot exists (D10).
- Data contradicts itself.
- The load is regulated.
- A confirmed booking would be bumped (R1).
- Two requests are a near-tie (R4).
- R7 runs out: no buffered slot, high-priority load, or driver silent.
- The warehouse hasn't confirmed within the timeout (D6).
- A start needs approval under facility rules (RULE005, after 21:00).

**Reason:**

- Safety, money and disputes carry risk the agent must not own (PDS §2.5).

**Evidence:**

- PDS §2.5 boundaries · DA-02, DA-13, DA-14.

**Built and tested** (2026-11-26; transcripts in [scenarios/](scenarios/report.md)): the triggers the code raises today. Each goes to the **coordinator** as one hand-off record:

| Trigger | Escalation |
|---|---|
| No feasible slot | `NO_FEASIBLE_SLOT` |
| Warehouse hasn't answered in 30 min | `WAREHOUSE_TIMEOUT` (driver told too) |
| Asked for the arrival time, driver silent 10 min | `ETA_UNRESOLVED` |
| CRITICAL load behind a lower priority's held or pending slot | `CRITICAL_WAITING` |
| Near-tie in a rush (R4) | `NEAR_TIE` |
| Safety words (accident, injured, fire…) | `SAFETY` |
| Dock problem during unloading | `APPROVAL_REQUIRED` |

A start at exactly 21:00 is allowed; only later starts need approval.

**Still open** (*waiting on client*): whether 21:00 itself needs approval (Q8); who besides the coordinator gets which trigger (Q5).

**Status:** ✅ Built and tested

---

## PROVE

### D13. How will you prove the system did not double-book capacity?

- **Why it matters (brief):** The business trusts the allocation only with evidence.
- **Evidence to bring (brief):** A concurrency test and its result.
- **Where to look:** Integrity-proof idea (N concurrent requests, duplicates, retries, smoke-alarm naive version) · DA-09 · M1

**Our answer:**

- Fire **N concurrent requests at one slot**, repeated many times, including duplicates and retries. After every run, a check query confirms at most one active booking per slot and no overlapping windows per dock (M1 = 0).
- Optional: run the same test against a test-only naive version (no lock) to show the test catches a double booking ("smoke alarm test button").

**Reason:**

- The business trusts the allocation only with evidence. History can't be the proof: it already shows one double-confirmed slot.

**Evidence:**

- DA-09 (APT1007 and APT1012A on SLOT-JAI-003) · M1.

**Built and tested** (2026-11-26, `uv run python -m dockpilot.integrity`):

```
DockPilot: 100 rounds x 8 racers for D2 12:00, 12.9 s
  rounds with a double booking (M1):   0
  rounds with exactly one winner:      100
  winner's duplicate got same hold:    48 of 48
  retries: winners answered from memory 148, losers refused again 652, of 800
  rounds won, by truck:                {'SHP1006': 13, 'SHP1007': 10, 'SHP1009': 15, 'SHP1012': 17, 'SHP1013': 31, 'SHP1014': 14}
NAIVE (no lock, no safety indexes): 20 rounds x 8 racers for D2 12:00, 2.0 s
  rounds with a double booking (M1):   20
```

- 8 separate programs, each with its own database connection: 6 real trucks plus 2 duplicates, all racing for D2 12:00 (two of them for D2 12:00–14:00), each resending once. **0 double bookings in 100 rounds.** Every truck won some rounds, so the start is a real race.
- The naive version (no lock, and the database's one-active-per-slot indexes removed on a throwaway copy) double-booked **20 of 20** rounds, and the same check caught it every time.
- The check also runs after every step of the 14-case replay: **M1 = 0** ([report](scenarios/report.md)).

**Status:** ✅ Built and tested

---
