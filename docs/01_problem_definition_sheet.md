# SetuHaul Logistics: Problem Definition Sheet

> Hand-in #1 (part A). Part B, the data audit, lives in `02_data_audit.md`.

**Author:** _…_  **Last updated:** 19 Nov 2026  **Snapshot:** 4 Aug 2026, Asia/Kolkata

---

## 1. Problem statement

**Spoken version (30 sec):**
> When trucks run late, the drivers message us asking for a new time to unload at the warehouse. On a bad evening, like heavy rain, dozens of drivers ask at once, but there are only a few free unloading bays. Right now, two drivers can both be told "yes" for the same bay at the same time. We're building a service that chats with each driver, only offers times that will really work for their truck, never gives the same time to two people, and brings in a person when there's no good option.

**One line:** Many late trucks, few unloading bays. Every "yes" we give a driver must be a "yes" we can keep.

**The real problem:** Talking to the driver is the easy part. The hard part is that many drivers want the same few slots at the same moment, and we must never promise one slot twice.

**Root-cause hypothesis** (to be proven or broken by the data audit):
Double bookings and impossible promises happen because **showing a free slot is treated as reserving it** (a requested booking blocks the slot, but nothing holds it while a driver is still choosing between options), **the data behind each answer isn't reliable** (ETAs are guesses, records can disagree), and **the database only blocks duplicate rows, not impossible bookings** (overlapping times, wrong dock type, blocked docks and arrival time go unchecked).

| Part | Status after audit |
|---|---|
| H1: No hold between showing an option and requesting it | ✅ Supported (audit §5: no hold state exists) |
| H2: Unreliable data (ETAs, conflicting records) | ✅ Supported: ETAs consistent, but bookings and statuses go stale (DA-05, DA-06, DA-10) |
| H3: Database guards duplicates, not feasibility | ✅ Supported (DA-04, DA-07) |

---

## 1b. Stakeholders and what each needs

| Party | What it knows | What it needs during an exception | What it fears |
|---|---|---|---|
| **Driver** | Where they are, what broke, traffic, real delay, their next commitment | Clear options, clear instructions, a confirmation they can trust | Not getting a slot, missing the delivery, unpaid waiting, missing the next pickup, being bumped after a promise |
| **Dispatcher / carrier** | Which driver and vehicle, driver availability | Revised plan; whether it's escalated and who owns it | Delay cascading into next loads; detention cost |
| **SetuHaul operations (coordinators)** | Shipment plan, customer commitments, open exceptions | One consistent view across many simultaneous requests | Being swamped in a spike; conflicting promises to clean up; customer penalties |
| **Warehouse planner** | Dock capacity, blockages, labour, equipment, local rules | Feasible arrivals without overbooking | Trucks bunching up, or docks and crew sitting idle |
| **Consignee / customer** | Why the load matters (inventory / production depends on it) | A reliable revised arrival time | Line stoppage or stock-out; repeated broken promises |

**Takeaway:** every party holds a different piece of the truth, and nobody holds all of it. The service's job is to join those pieces and give each party what it needs.

---

## 1c. Design scale vs test scale

| Dimension | Real SetuHaul (design for) | Snapshot (test on) |
|---|---|---|
| Loads per day | 280 to 360 (design for the peak, 360) | 21 shipments |
| Facilities | 6 | 2 |
| Dock doors | 32 | 9 |
| Appointments per day | 190 to 240 | 20 appointment rows (includes history: cancelled, replaced, no-show) · 106 slots |
| Driver exception messages: normal day | 15 to 25 per day | 20 chat messages (all senders; driver-only count to confirm in the audit) · 10 recorded exceptions |
| Driver exception messages: **spike** | **20 to 35 within 30 minutes** | Not modelled as a timed spike; to check in the audit |
| Coordinators per shift | 5 | Not in the snapshot (`facility_contacts` are warehouse/gate contacts, not coordinators) |
| Live GPS feeds | 0 | 0 (ETAs come from plan, driver, warehouse or ops override) |

**What gets harder at real scale:**
- **Same-moment collisions:** at 20 to 35 messages in 30 minutes, two drivers grabbing the same slot becomes routine, so the database has to prevent double booking every time.
- **Coordinators get swamped:** 35 cases across 5 coordinators is about 4 minutes per case, so the system must resolve most cases itself.
- **Special docks stay scarce:** more facilities still means only one or two reefer or heavy docks each, so "no feasible slot" cases grow.
- **The world changes mid-decision:** ETAs, breakdowns and cancellations keep arriving, so every option must be re-checked at the moment of confirmation.

---

## 2. Problem Definition Sheet

### 2.1 Decision maker(s)

- **Head of operations:** accepts or rejects the solution and signs off on the policy (allocation rules, hold timers, what gets escalated).
- **SetuHaul coordinators:** decide live, case by case, whenever the system escalates (no feasible slot, near-ties, bumping a confirmed booking).
- **Warehouse planner:** controls the docks; decides which docks and slots are usable and whether an arrival can be accepted.

### 2.2 Criteria / measures for success

- **Head of operations:** delays still happen, but each is resolved fast, correctly and without broken promises.
- **SetuHaul coordinators:** most cases resolve automatically; only hard ones reach a human, each with a complete record.
- **Warehouse planner:** every arrival offered is one their docks can take, with no overbooking and few wasted slots.
- Full measures, methods and targets in **§2a**.

### 2.3 Key forces acting on decision makers

- **Head of ops:** blamed if the tool fails; missed customer commitments; detention costs and penalties.
- **Coordinators:** a tool that adds work, takes control away, or makes them clean up its mistakes.
- **Warehouse planner:** overbooked docks, or idle docks and crew.
- **Main conflicts:** priority vs fairness, automation vs control, full docks vs held slots, speed vs accuracy.
- Full detail in **§2b**.

### 2.4 Time frame for resolution

- **Per driver: minutes.** A usable outcome (booking, clear refusal with options, or escalation) must come well before the truck reaches the gate. Holds last 10 min (5 min in a spike).
- **Per spike: 30 minutes.** 20–35 cases land at once; at ≈4 min per coordinator case, anything not automated queues up.
- **Per facility: same day.** Re-plans must happen before the affected slots start; a slot that has passed can't be recovered.
- **Cost of waiting:** missed slots, detention, broken customer commitments (brief p1).

### 2.5 Boundaries / constraints

- **The agent never decides alone:** whether two drivers share capacity, whether a vehicle fits a dock, how scarce capacity is ranked, whether a booking committed. Code and the database decide these.
- **A person approves or takes over:** driver safety; penalties, compensation and customer commitments; no feasible slot; contradictory data; regulated loads; emergencies. By our policy also: bumping any confirmed booking (R1) and near-ties (R4).
- **Out of scope:** network-wide optimisation, carrier selection and rates, autonomous safety decisions, customs / hazmat / legal, approving commercial penalties.
- **Data constraints:** no live GPS (ETAs are estimates); one snapshot, not a live feed; the database only enforces one active booking per slot and one current active booking per shipment, so everything else is our code's job.

### 2.6 Accuracy necessary

- **Must be exact (zero tolerance):** slot capacity (no double booking, no overlapping windows), dock compatibility, blocked / closed docks, the booking status the driver is told, compliance with the allocation policy, the shipment a chat is linked to.
- **Good enough, if stated honestly:** ETAs (always shown with source and confidence), reading the driver's free text (ask when unsure), utilisation and waiting estimates.
- **Rule:** if anything in the "exact" list is uncertain, the system doesn't promise; it asks or escalates.

---

## 2a. Measures for success: definitions, method, targets

**Target policy:** firm targets only where anything else is a failure of correctness. Where SetuHaul has no baseline, we measure but do not invent a target; the head of operations sets it once a baseline exists. The snapshot (21 shipments, one frozen moment) cannot supply a performance baseline.

### Hard targets (non-negotiable)

| # | Measure (brief p9) | Exact definition | How we measure it | Target | Owner who cares |
|---|---|---|---|---|---|
| M1 | Conflicting or duplicate allocations | Any slot with more than one active booking (pending, confirmed or in progress) at the same time, or any dock with two active bookings whose time windows overlap | Integrity test: N drivers race for one slot, repeated many times, incl. duplicate and retried messages; plus a check query run after every test and scenario | **0** | Head of ops, warehouse planner |
| M2 | Options later found infeasible | A booking **confirmed** for a slot that, at the moment of confirmation, was taken, blocked, closed, on an incompatible dock, or outside the truck's arrival window | Re-check every option at confirmation; count confirmations that fail the re-check | **0 confirmed.** Options that disappear before confirmation are allowed, but each must be reported to the driver | Warehouse planner |
| M3 | No-slot escalations handled correctly | A case with no feasible slot reaches a coordinator with a complete hand-off record (driver, shipment, current appointment, latest ETA + source, options checked and why each failed, driver constraints) | Scenario run: count no-slot cases; check each hand-off record against the required fields | **100%** escalated, **100%** complete | Coordinators |
| M4 | Priority-policy violations | Any allocation decision that contradicts allocation policy v1 (e.g. a confirmed booking bumped without a human, a truck bumped twice in one day) | Log every allocation decision with the rule that justified it; check each against the policy | **0** | Head of ops |

### Measured, target set later (no baseline yet)

| # | Measure (brief p9) | Exact definition | How we measure it | Target | Owner who cares |
|---|---|---|---|---|---|
| M5 | Time from first message to usable outcome | Time and turns from the driver's first message to a confirmed booking, a clear refusal with options, or an escalation | Timestamps on every message and outcome in the scenario run | Reported per scenario; target set by head of ops | Head of ops |
| M6 | Share resolved without manual takeover | Cases that end in a confirmed booking or accepted outcome with no coordinator involvement ÷ all cases | Count over the scenario set | Reported; target set by head of ops. **Never raised by skipping a required escalation** | Coordinators, head of ops |
| M7 | Driver clarification turns | Questions the system asked per resolved case, excluding questions about facts the backend already holds (those count as errors) | Count from transcripts | Reported; any question about a backend-held fact = defect | Head of ops |
| M8 | Average driver waiting after rescheduling | Gate-in to dock-in time for rescheduled trucks | From check-in timestamps in the scenario run | Reported; compared with the original schedule | Warehouse planner |
| M9 | Warehouse slot utilisation | Slots used ÷ slots open in the window, per facility | Query on the facility schedule before and after the scenario run | Snapshot reference: **14%** (14 of 99 usable slots occupied, audit §4); target set with the planner | Warehouse planner |

### Guardrails between measures

- **M6 vs M3:** automation must never rise by skipping an escalation the boundaries require.
- **M7 vs M2:** fewer questions must never mean confirming on missing facts.
- **M9 vs holds:** short holds (10 min normal, 5 min spike) cost some utilisation; that cost is accepted to protect M1 and M2.

---

## 2b. Key forces: fears, concerns, conflicting agendas

**Source note:** the brief contains no direct statement of anyone's fears. Everything below is **inferred**, except where marked as confirmed, from the brief (p1 cost of delay, p2 stakeholder needs, p9 boundaries) and our own analysis. It must be validated by talking to these people.

### Fears and concerns

**Head of operations**
- Being **blamed** if the tool they approved double-books or makes a wrong promise
- **Missing customer commitments**, especially during a spike
- **Detention costs, penalties and compensation** when trucks wait or loads arrive late
- Paying for a tool that coordinators **don't adopt**
- *Clue:* p1 "one delay breaks several promises", ATRI detention figures; p9 penalties and compensation need human approval

**SetuHaul coordinators**
- The tool **adds work** instead of removing it (checking every bot answer)
- **Losing control**: the bot makes decisions they'd have made differently
- **Cleaning up the bot's mistakes** and taking the blame for them
- Getting escalations with **half the story**, forcing them to call the driver again
- Being **swamped** in a spike: 35 cases ÷ 5 coordinators ≈ 4 min per case
- *Clue:* p2 ops need "one consistent view across many simultaneous requests"; p1 spike figures

**Warehouse planner**
- **Overbooking**: more trucks than docks, labour and equipment can handle
- **Idle docks and crew**: slots held or promised to trucks that don't come (no-shows)
- Their **local rules and blocked docks being ignored** by a system that only sees empty rows
- **Losing say** over their own docks to an outside tool
- *Clue:* p2 planner needs "feasible arrivals without overbooking"; facility rules and dock events in the data

### Conflicting agendas

| # | Pulls one way | Pulls the other way | How our design responds |
|---|---|---|---|
| C1 | **Driver vs driver:** each wants the same scarce slot | Only one can have it | Database allows one active booking per slot; the loser hears why and gets alternatives (policy R5) |
| C2 | **Priority:** high-value loads first (customer commitments) | **Fairness:** the truck waiting longest, or already bumped | Policy R3 ranks priority → harm already done → first asked; R6 never bumps the same truck twice |
| C3 | **Head of ops wants automation** (fewer manual cases, lower cost) | **Coordinators want control** (no surprise decisions) | Clear-cut cases are automatic; near-ties, bumps and no-slot cases go to a human (R1, R4); guardrail M6 vs M3 |
| C4 | **Planner wants full docks** (utilisation) | **Holds keep a slot empty while a driver decides** | Short holds: 10 min normal, 5 min spike; guardrail M9 vs holds |
| C5 | **Speed:** answer the driver fast, ask little | **Accuracy:** never confirm on missing facts | Ask only for facts the backend lacks; re-check at confirmation; guardrail M7 vs M2 |
| C6 | **SetuHaul wants slots for its trucks** | **The warehouse serves other carriers too** *(confirmed by the client team; see F6)* | Treat warehouse confirmation as external; never promise beyond what the warehouse has accepted (A1, U1) |
| C7 | **Keep promises already made** (trust) | **Re-plan for a better overall schedule** (scheduling tool) | Confirmed bookings are fixed unless a human approves a change (R1); objective includes "fewer changes to a schedule already told to a driver" |

---

## 3. Facts, estimates, assumptions, unknowns

**Labels:** **Fact** = stated for certain, or provable with a query. **Estimate** = a number that's a guess. **Assumption** = something *we* choose to believe so we can proceed. **Unknown** = something we can't know from what we have. Note the source for each (brief page, guide section, or query).

### 3.1 Facts

| # | Fact | Source |
|---|---|---|
| F1 | The Jaipur DC has only one reefer dock (D5) | Brief p6 diagram; `docks` table |
| F2 | SetuHaul coordinates 280 to 360 loads per day | Brief p1 operating profile (client-reported) |
| F3 | SetuHaul has no live GPS feeds; ETAs come from the plan, the driver, the warehouse or an ops override | Brief p1, p3; guide §1 |
| F4 | The database blocks two active bookings on the same slot | Guide §7; index `ux_active_appointment_per_slot` |
| F5 | A disruption spike brings 20 to 35 driver messages within 30 minutes | Brief p1 operating profile (client-reported) |
| F6 | The warehouse also serves other carriers, not only SetuHaul | Client team discussion |
| F7 | The database blocks a shipment from having two current active bookings at once | Guide §7; index `ux_current_active_appointment_per_shipment` |

### 3.2 Estimates

| # | Estimate | Why it's uncertain | Source |
|---|---|---|---|
| E1 | A driver's stated delay ("I'm 90 minutes late") | Driver's own guess; may mean repair time, not arrival delay; no GPS to check | Driver chat, brief p3 |
| E2 | The end time of a blocked window (e.g. the D3 breakdown) | It is a guess at repair time; the breakdown can run longer | `dock_status_events`, brief p6 |

### 3.3 Assumptions

| # | Assumption | Why we assume it | Risk if wrong | Who should confirm |
|---|---|---|---|---|
| A1 | A booking isn't final until the warehouse confirms it | Safest choice: we never promise more than the warehouse has accepted (see U1) | If SetuHaul can confirm alone, drivers wait longer than needed | Warehouse planner, head of ops |
| A2 | A 10-minute hold (5 in a spike) is long enough for a driver to decide | Choosing between a few options in chat takes minutes | Too short: drivers lose slots mid-reply. Too long: scarce slots sit idle | Head of ops, after testing |
| A3 | A high-priority load outranks a truck's waiting time | Customer commitments carry the biggest cost (policy R3) | Unfair outcomes; drivers lose trust | Head of ops |
| A4 | ~~A truck's unloading fits inside its slot~~ **Disproved by the audit (DA-08):** 4 live bookings overrun their 60-min slot | Slots are fixed per dock type; unload times vary 45–90 min | Overruns push into the next booking | Warehouse planner (Q10) |
| A5 | The 5 coordinators are available to take escalations during a spike | Brief gives 5 per shift | Escalations queue up unanswered | Head of ops |
| A6 | Drivers reply within the hold time | Drivers who are actively chatting usually respond quickly | Slots released while drivers are still deciding | Validate in a pilot |
| A7 | The snapshot shows the types of cases we must handle | Brief: "test on this snapshot"; guide §6 seeds the cases | We miss case types that exist in real life | Head of ops |

### 3.4 Unknowns

| # | Unknown | Why it matters | How we'd find out |
|---|---|---|---|
| U1 | Whether the warehouse must confirm a booking before it is final | Decides what we can promise a driver (covered by A1 for now) | Ask the warehouse planner / client team |
| U2 | How SetuHaul performs today (automation share, waiting times) | No baseline, so non-critical targets can't be set (§2a) | Measure current operations before go-live |
| U3 | How often each case type happens on a real day | The snapshot is deliberately packed with edge cases | Historical data from SetuHaul |
| U4 | What switches the system into spike mode (shorter holds) | Decides when the 5-minute hold applies | Agree a trigger with the head of ops (e.g. a coordinator switch, or a request-rate threshold) |

### 3.5 Verified in the audit (now facts)

| # | Check | Result |
|---|---|---|
| V1 | Does `shipments.latest_eta_ts` match the newest row in `eta_updates`? | **F8:** Yes, 11 of 11 shipments match |
| V2 | Is every chat thread linked to the correct shipment? | **F9:** 11 of 12; THR010 has no shipment (driver has two loads, DA-11) |
| V3 | Do slot lengths differ by dock type? | **F10:** Yes: 60 min standard and reefer, 90 min heavy, fixed per type; unload times still vary 45–90 (DA-08) |

Full results: [02_data_audit.md](02_data_audit.md).

---

## 4. What the snapshot cannot tell us

- **SetuHaul's current performance:** no baseline for automation share, response time or waiting (U2).
- **How often situations really occur:** the snapshot is seeded with edge cases, so it shows types, not frequencies (U3).
- **When a blocked dock will really reopen:** only an estimated end time (E2).
- **How the world changes over time:** it is one frozen moment, with no stream of new ETAs, check-ins and cancellations.
- **Where trucks actually are:** no GPS, so every ETA is an estimate (F3).

---

## 5. RAPID check

### R: Requirement

Build a conversational exception service for drivers, backed by a capacity layer that **holds, confirms and releases** dock slots, with an escalation path to a human coordinator, **plus an integrity test proving that competing requests for the same slot never produce a double booking.** A facility-level scheduling tool is optional.

### A: Assumptions

- **A1:** a booking isn't final until the warehouse confirms it.
- **A2:** a 10-minute hold (5 in a spike) is long enough for a driver to decide.
- **A3:** a high-priority load outranks a truck's waiting time.
- **A7:** the snapshot shows the types of cases we must handle.
- Full list with risks and owners in **§3.3**.

### P: Priority

**The capacity layer, with escalation.** It is the part that makes every "yes" one we can keep: holds, confirmation with a feasibility re-check, and one booking per slot, proven by the integrity test. With escalation, it can work even behind a basic conversation. A polished chatbot on a weak capacity layer would make promises it can't keep, which is the exact failure we're here to fix. Conversation quality comes second.

### I: Implications

- **Coordinators' work changes:** they stop handling every late truck and handle only escalations. That only works if they trust the tool and every hand-off is complete.
- **Mistakes scale:** a human's mistake affects one driver, but a bug in an automated rule repeats across every driver in a spike. So rules must be tested before go-live and easy to switch off.
- **New risks if the design is weak:** double bookings, wrong or overconfident answers to drivers, and messages piling up unanswered.
- **Drivers and planners rely on the system's answers:** a confirmed slot becomes a commitment the warehouse must honour, so the warehouse's say over confirmations must be settled first.
- **A record of every decision now exists:** a decision log enables auditing and fair dispute handling, but someone has to own and review it.

### D: Dependency

- **Data we can link:** each chat can be tied to the right driver and shipment, and each shipment has a dock type, a priority and an ETA with its source. *The audit confirms this.*
- **A database that supports safe booking:** holds and confirmations happen as one all-or-nothing step, plus new tables we're allowed to add (holds, escalations, decision log).
- **Policy sign-off:** the head of ops approves the allocation policy, the hold timers and the escalation triggers.
- **An escalation channel:** a defined way a case reaches a coordinator, and who picks it up.
- **A rule on who confirms a booking:** SetuHaul alone, or the warehouse too (open question, decision 6).
- **Tooling:** a language model that can call tools, and a simulated driver chat channel for the prototype.

---

## 6. Agreed so far (carry into the decision register)

- **Allocation policy v1:** R0 only feasible trucks compete · R1 confirmed bookings locked; only a human can bump, and only if the high-priority truck has no other feasible slot · R2 holds respected until expiry · R3 rank by priority → harm already done → first asked · R4 clear winner = auto, near-tie = human · R5 the loser always hears why + next options · R6 never bump the same truck twice in a day. · R7 low-confidence ETA (`confidence_code = LOW`): ask one clarifying question, then book a slot with a buffer; escalate only if no buffered slot exists, the load is high priority, or the driver goes silent. *Open: buffer size, max clarifying turns (audit Q6–Q7). Found tracing THR003 / SHP1013.*
- **Hold timer:** 10 min on a normal day, 5 min during a spike, with a nudge to the driver. *Open: what triggers spike mode (U4).*
- **GPS:** not assumed. ETA sources stay pluggable via `eta_updates.source_type` + `confidence_code`. Recommend GPS in the client readout.
