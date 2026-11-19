# SetuHaul Logistics: Decision Register

> Hand-in #2. The 13 decisions from page 8 of the brief. Each gets **our answer, our reason and our evidence**.
> Inputs: [01_problem_definition_sheet.md](01_problem_definition_sheet.md) and [02_data_audit.md](02_data_audit.md).

---

## Status

| # | Decision | Status |
|---|---|---|
| 1 | What must you collect before you show useful options? | 🟡 Partly agreed |
| 2 | How does a conversation link to the right driver, shipment and appointment? | 🟡 Partly agreed |
| 3 | How do you set the revised ETA and show how sure it is? | 🟡 Partly agreed |
| 4 | What makes a slot feasible for this shipment? | 🟡 Partly agreed |
| 5 | What does "available" mean while another driver is deciding? | ✅ Agreed |
| 6 | When does an option become a hold, request, reservation or confirmed booking? | ✅ Agreed |
| 7 | How do you order simultaneous requests when capacity runs short? | ✅ Agreed |
| 8 | (Optional) When does the facility schedule recalculate, which work is fixed, and what is the objective? | 🟡 Partly agreed (optional) |
| 9 | How do you handle stale options, cancellations, duplicate messages and retries? | 🟡 Partly agreed |
| 10 | What happens when no feasible slot exists? | 🟡 Partly agreed |
| 11 | What do you say when the preferred slot is not granted? | 🟡 Partly agreed |
| 12 | Which decisions need human approval or takeover? | 🟡 Mostly agreed |
| 13 | How will you prove the system did not double-book capacity? | 🟡 Partly agreed |

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

**Still open:**

- Full field list, split into "from the database" and "from the driver".
- Three example chats showing the fields asked for.

**Status:** 🟡 Partly agreed

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

**Still open:**

- The matching rule, step by step (phone → driver → active shipments → appointment).
- A message-to-rows trace for the unclear case (THR010).

**Status:** 🟡 Partly agreed

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

**Still open:**

- How free text becomes an ETA (e.g. MSG003 "tyre repaired" is a repair time, not an arrival time).
- `v_latest_eta` treats a shipment with no update as HIGH confidence: do we accept that?
- Buffer size (Q6) and max clarifying turns (Q7).
- The rule applied to two messages.

**Status:** 🟡 Partly agreed

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

**Still open:**

- How much slack counts as "fits" (ties to Q6).
- The written check run on three shipments.

**Status:** 🟡 Partly agreed

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

**Still open:**

- How the expiry nudge is sent (built with the conversation layer).

**Status:** ✅ Agreed

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

**Still open:**

- Warehouse timeout value (Q2), spike trigger (Q3 / U4), whether confirmation is always required (Q1).

**Status:** ✅ Agreed

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
| 09:19 | SHP1013 | NORMAL · 11:00 **LOW** · 14 t · 60 min | No hold yet: agent asks first (R7). Once clarified, gets the next slot with slack, likely **13:00 D3** | Zero slack on a vague ETA is not a promise we can keep |
| 09:29 | SHP1012 | NORMAL · 11:10 · 8 t · 45 min · must leave by 13:30 | **12:00 D2** (held, then requested) | First feasible request for that slot |
| 09:34 | SHP1006 | HIGH · 11:20 · 7.8 t · 45 min | **12:00 D4**; old 10:00 booking (APT1006) replaced, since it can't be reached | First feasible request for that slot |
| 09:44 | SHP1014 | CRITICAL · 11:25 · 21 t · 75 min | **13:00–14:15 on D2** (two consecutive slots on a 25 t dock) **+ coordinator alert** | 12:00 slots already held by lower priority; R1 bump not allowed automatically |

- A coordinator who chooses to act can move SHP1014 to **D4 12:00–14:00** and SHP1006 to 13:00, telling the SHP1006 driver why (R5).
- The real system booked SHP1014 into **D1 at 11:00**: too early for its ETA and too weak for its weight (DA-04). Under our rules that can't happen.

**Reason:**

- Requests in this case are **25 minutes apart**. No collection window can wait that long in a chat, so priority can't act purely by waiting. First-wins keeps answers instant; the coordinator alert keeps CRITICAL loads protected without the agent overturning another driver's slot on its own.
- In a real spike, requests do arrive seconds apart. That is where the window lets priority win automatically.

**Evidence:**

- The replay above (data: `v_slot_availability`, `v_latest_eta`, `driver_exceptions.reported_at`, `shipments`).
- Guide §6 "Simultaneous slot competition": SHP1006, SHP1012, SHP1013, SHP1014.

**Still open:**

- Window length (default 30 s) and the spike trigger (U4).
- What SHP1012's "leave before 1:30" means (MSG004 asked; it decides whether 13:00 would also have worked).
- Confirm with the client that coordinators may reassign PENDING bookings (extends R1).

**Status:** ✅ Agreed

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

**Status:** 🟡 Partly agreed (optional)

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

**Still open:**

- One replayed case for each of the four.

**Status:** 🟡 Partly agreed

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

**Still open:**

- The escalation message and a filled hand-off record for THR005.

**Status:** 🟡 Partly agreed

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

**Still open:**

- Two refusal messages in plain words.

**Status:** 🟡 Partly agreed

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

**Still open:**

- Final list in "trigger → who" form; whether 21:00 itself needs approval (Q8).

**Status:** 🟡 Mostly agreed

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

**Still open:**

- Run it once the prototype exists, and record the result.

**Status:** 🟡 Partly agreed

---
