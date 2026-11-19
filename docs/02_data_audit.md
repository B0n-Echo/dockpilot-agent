# SetuHaul Logistics: Data Audit

> Hand-in #1 (part B). Every data issue below comes with the query that proves it.
> Database: `data/source/setuhaul_freight_operations.db` (opened read-only). Snapshot: 4 Aug 2026, Asia/Kolkata.

---

## 1. How to run the queries

From the repository root:

```bash
sqlite3 -readonly -box data/source/setuhaul_freight_operations.db
```

Then paste any query below. `.tables` lists tables, `.schema <table>` shows a table's columns, `.quit` exits.

---

## 2. Method

We check the data for five kinds of problems:

| Category | Question it asks | Example |
|---|---|---|
| **Consistency** | Do two places that store the same fact agree? | `shipments.latest_eta_ts` vs newest `eta_updates` row |
| **Completeness** | Is something missing that should be there? | A chat thread with no shipment linked |
| **Validity** | Is a record impossible or against the rules? | A booking in a blocked slot, or on the wrong dock type |
| **Timeliness** | Is a record out of date? | An ETA older than a later driver message |
| **Duplicates** | Is the same thing recorded twice? | Repeated chat messages |

Each finding gets: **what's wrong → the query → what it returned → why it matters → what our design does about it.**

---

## 3. Orientation

| Check | Result |
|---|---|
| Row counts match the database guide §3 (18 tables) | ✅ All 18 match (e.g. 21 shipments, 106 slots, 20 appointments) |
| Seeded cases (guide §6) | Every finding below traces back to a seeded case or a gap around it |

---

## 4. Checks run

| # | Check | Category | Result |
|---|---|---|---|
| V1 | `shipments.latest_eta_ts` matches the newest `eta_updates` row | Consistency | ✅ Pass: 11 of 11 shipments with updates match |
| V2 | Every chat thread is linked to the correct shipment | Completeness | ⚠️ 11 of 12 correct; THR010 has no shipment (DA-11) |
| V3 | Slot lengths differ by dock type | Validity | ⚠️ Fixed per type (60 min standard and reefer, 90 heavy), but unload times vary 45–90 (DA-08) |
| S1 | Overlapping slots on the same dock | Validity | ✅ Pass: none |
| S2 | Active bookings in blocked / closed slots, or during dock events | Validity | ❌ None in BLOCKED slots, but 3 active bookings sit inside dock events (DA-06) |
| S3 | Shipments booked on an incompatible dock | Validity | ❌ Type and reefer OK; 2 loads over the dock weight limit (DA-07) |
| S4 | Slot's facility vs dock's facility vs shipment's destination | Consistency | ✅ Pass: no mismatches |
| S5 | Shipment, driver and vehicle point to the same carrier | Consistency | ✅ Pass; every vehicle also fits its load |
| S6 | Appointment history is clean | Validity | ❌ Two confirmed bookings on one slot in history (DA-09); 3 shipments with no live booking (DA-10) |
| S7 | Actual dock at check-in vs booked dock | Consistency | ✅ Pass: both docked trucks are at their booked dock |
| S8 | Duplicate chat messages | Duplicates | ⚠️ Flagged correctly, but the duplicate opened its own thread (DA-12) |
| S9 | Same driver or vehicle in two places at once | Validity | ❌ DRV007 is driving two trucks at the same time (DA-11) |
| T1 | Active bookings that start before the truck can arrive (added) | Timeliness | ❌ 6 of 15 (DA-05) |
| H1–H3 | Root-cause hypotheses from the PDS | — | All 3 supported (see §5) |

**Utilisation reference for M9:** 14 slots occupied out of 99 usable (7 blocked) = **14%** across both facilities on the snapshot day.

---

## 5. Hypotheses (from PDS §1)

| # | Hypothesis | Verdict | Evidence |
|---|---|---|---|
| H1 | No hold between showing an option and requesting it | **Supported** | `appointment_status` has no HOLD value and no hold table exists; the agent goes straight from "I found a slot" to PENDING_CONFIRMATION (MSG008 → APT1014A) |
| H2 | Unreliable data | **Supported, with nuance** | ETAs are consistent (V1), but bookings, statuses and messages go stale: DA-01, DA-05, DA-06, DA-10, DA-13 |
| H3 | Database guards duplicates, not feasibility | **Supported** | The unique index holds, yet the database accepts a booking the truck can't reach in time on a dock too weak for it (DA-04, DA-07) |

---

## 6. Findings

Ordered by severity. 🔴 = can produce a broken promise to a driver; 🟠 = wastes capacity or misleads; 🟡 = hygiene.

### DA-01 🟠 Thread status and booking status tell different stories

- **Category:** Consistency
- **What's wrong:** THR003 says WAITING_FOR_DRIVER, but its booking APT1013A is waiting for the warehouse. One status field can't show both sides.
- **Query:**

```sql
SELECT t.thread_id, t.thread_status, a.appointment_id, a.appointment_status
FROM chat_threads t JOIN appointments a ON a.shipment_id = t.shipment_id AND a.is_current = 1
WHERE t.thread_id = 'THR003';
```

- **Result:** THR003 WAITING_FOR_DRIVER · APT1013A PENDING_CONFIRMATION.
- **Why it matters:** A coordinator reading the thread thinks the ball is with the driver; nobody chases the warehouse.
- **What our design does:** The agent and the coordinator view derive "who are we waiting on" from both records, never the thread status alone.

### DA-02 🔴 A pending booking locks a slot, and its confirmation request was never sent

- **Category:** Timeliness
- **What's wrong:** APT1013A blocks SLOT-JAI-018 as PENDING, but the email asking the warehouse to confirm (OM002) is still QUEUED. No timeout exists.
- **Query:**

```sql
SELECT a.appointment_id, a.slot_id, a.appointment_status, a.booked_at, m.operational_message_id, m.delivery_status
FROM appointments a LEFT JOIN operational_messages m ON m.appointment_id = a.appointment_id
WHERE a.appointment_status = 'PENDING_CONFIRMATION';
```

- **Result:** APT1013A: OM002 QUEUED. APT1014A: OM001 SENT. Neither confirmed.
- **Why it matters:** The slot is invisible to every other driver, and the warehouse doesn't even know it was asked. It can stay locked all day.
- **What our design does:** A confirmation timeout (Q2) plus delivery check: an undelivered request or an expired wait escalates to a coordinator and is shown to the driver as "not final".

### DA-03 🔴 The agent booked before its own clarifying question was answered

- **Category:** Validity
- **What's wrong:** At 09:19:05 the agent asked SHP1013's driver what "late by one hour" means (MSG006). With no reply, a booking was made at 09:32 on an assumed 11:00 ETA (LOW confidence), in a slot starting at exactly 11:00 with a 60-min unload in a 60-min slot.
- **Query:**

```sql
SELECT m.chat_message_id, m.sender_type, m.message_ts, m.message_text, e.confidence_code, a.booked_at, sl.slot_start_ts
FROM chat_messages m
JOIN eta_updates e ON e.shipment_id = 'SHP1013'
JOIN appointments a ON a.shipment_id = 'SHP1013' AND a.is_current = 1
JOIN appointment_slots sl ON sl.slot_id = a.slot_id
WHERE m.thread_id = 'THR003';
```

- **Result:** Question open, ETA LOW, booking made anyway with zero slack.
- **Why it matters:** This is how a "yes we can't keep" is born: a guess turned into a promise.
- **What our design does:** Policy R7: ask first; book only once clarified or with a buffer; escalate if neither works.

### DA-04 🔴 A chat booking for a critical load is infeasible on two counts

- **Category:** Validity
- **What's wrong:** At 09:44 the SHP1014 driver said ETA 11:25. At 09:52 the chat booked APT1014A in an 11:00 slot on DOCK-JAI-D1 (20 t limit) for a 21 t load, and 75 min of unloading into 60 min.
- **Query:**

```sql
SELECT a.appointment_id, s.priority_code, sl.slot_start_ts, v.effective_eta_ts, s.load_weight_kg, d.max_vehicle_weight_kg, s.expected_unload_min
FROM appointments a JOIN shipments s USING (shipment_id) JOIN appointment_slots sl USING (slot_id)
JOIN docks d ON d.dock_id = sl.dock_id JOIN v_latest_eta v ON v.shipment_id = s.shipment_id
WHERE a.appointment_id = 'APT1014A';
```

- **Result:** CRITICAL load · slot 11:00 · ETA 11:25 · 21,000 kg vs 20,000 kg limit · 75 min vs 60.
- **Why it matters:** The database accepted it without complaint. If the warehouse confirms, the most important truck of the morning is promised a slot it can't use.
- **What our design does:** Feasibility checks (arrival, weight, type, reefer, unload length) run in the capacity layer before any hold or booking (R0, M2).

### DA-05 🟠 Confirmed bookings go stale when ETAs move

- **Category:** Timeliness
- **What's wrong:** 6 of 15 active bookings start before the truck can arrive. They still hold their slots.
- **Query:**

```sql
SELECT a.appointment_id, a.appointment_status, sl.slot_start_ts, v.effective_eta_ts, v.eta_confidence
FROM appointments a JOIN appointment_slots sl USING (slot_id) JOIN v_latest_eta v USING (shipment_id)
WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED') AND v.effective_eta_ts > sl.slot_start_ts;
```

- **Result:** APT1004, APT1005, APT1006, APT1014A, APT1015A, APT1017.
- **Why it matters:** Capacity that is already lost looks taken, so other late trucks are told "no slot".
- **What our design does:** Every new ETA re-checks the current booking; if it no longer fits, the agent offers new options and releases the old slot (with R1: confirmed slots are only released once the replacement is agreed or the truck is a no-show).

### DA-06 🟠 Dock problems don't reach bookings already in place

- **Category:** Consistency
- **What's wrong:** Slots after a breakdown are BLOCKED, but the slot in progress is not. APT1005 stays CONFIRMED on DOCK-JAI-D3 though the leveller failed at 09:15; on D2 a 70-min unload in a 60-min slot plus a capacity cut pushes APT1004 back.
- **Query:**

```sql
SELECT e.dock_event_id, e.event_type, sl.slot_id, sl.slot_status, a.appointment_id, a.appointment_status
FROM dock_status_events e
JOIN appointment_slots sl ON sl.dock_id = e.dock_id AND e.event_start_ts < sl.slot_end_ts AND e.event_end_ts > sl.slot_start_ts
LEFT JOIN appointments a ON a.slot_id = sl.slot_id AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS');
```

- **Result:** DEVT001 → APT1005 CONFIRMED in an OPEN slot; DEVT003 → APT1002 IN_PROGRESS and APT1004 CONFIRMED.
- **Why it matters:** The truck at the gate (CHK1005, WAITING_DOCK_UNAVAILABLE) holds a "confirmed" slot that can't happen.
- **What our design does:** A dock event triggers a sweep of affected bookings: re-home or escalate each one, and tell the drivers.

### DA-07 🔴 Dock weight limits are not enforced

- **Category:** Validity
- **What's wrong:** Two live bookings put loads heavier than the dock's limit onto standard docks.
- **Query:**

```sql
SELECT a.appointment_id, s.load_weight_kg, d.dock_id, d.max_vehicle_weight_kg
FROM appointments a JOIN shipments s USING (shipment_id) JOIN appointment_slots sl USING (slot_id) JOIN docks d ON d.dock_id = sl.dock_id
WHERE a.is_current = 1 AND s.load_weight_kg > d.max_vehicle_weight_kg;
```

- **Result:** APT1005 (20,500 kg on 20,000) and APT1014A (21,000 kg on 20,000).
- **Why it matters:** The load alone exceeds the limit, before counting the truck. Unsafe and likely refused at the dock.
- **What our design does:** Weight is part of the feasibility check (Q9 asks whether the limit is gross or load weight).

### DA-08 🟠 Unloading time doesn't fit the slot

- **Category:** Validity
- **What's wrong:** Slots are fixed at 60 min (standard, reefer) or 90 (heavy), but standard loads take 45–75 min. 4 live bookings overrun by design.
- **Query:**

```sql
SELECT a.appointment_id, s.expected_unload_min,
       ROUND((julianday(sl.slot_end_ts) - julianday(sl.slot_start_ts)) * 1440) AS slot_min
FROM appointments a JOIN shipments s USING (shipment_id) JOIN appointment_slots sl USING (slot_id)
WHERE a.is_current = 1 AND s.expected_unload_min > ROUND((julianday(sl.slot_end_ts) - julianday(sl.slot_start_ts)) * 1440);
```

- **Result:** APT1002 (70), APT1005 (75), APT1009 (75), APT1014A (75), all in 60-min slots.
- **Why it matters:** Each overrun eats the next truck's slot (exactly what happened on D2). Assumption A4 is false.
- **What our design does:** A load that needs more than one slot books consecutive slots, or the next slot gets a warning (Q10).

### DA-09 🟠 History shows two confirmed bookings on one slot

- **Category:** Duplicates
- **What's wrong:** APT1007 and APT1012A were both CONFIRMED on SLOT-JAI-003 from 1 Aug 11:55 until APT1012A was cancelled on 4 Aug 09:40.
- **Query:**

```sql
SELECT a.appointment_id, b.appointment_id, a.slot_id, a.confirmed_at, b.confirmed_at, b.cancelled_at
FROM appointments a JOIN appointments b ON a.slot_id = b.slot_id AND a.appointment_id < b.appointment_id;
```

- **Result:** 1 pair on SLOT-JAI-003.
- **Why it matters:** Today's unique index would refuse this, so either the history was written after the fact or the index came later. Either way, past data can't prove we never double-booked.
- **What our design does:** The integrity proof is run on our own capacity layer, not inferred from history. Booking changes are logged as events (Q11).

### DA-10 🟠 Shipments in transit with no live booking

- **Category:** Completeness
- **What's wrong:** SHP1012 and SHP1016 are IN_TRANSIT but their bookings were cancelled with no replacement. SHP1021 never had one. `replaced_appointment_id` is never used.
- **Query:**

```sql
SELECT s.shipment_id, s.current_status,
       (SELECT COUNT(*) FROM appointments a WHERE a.shipment_id = s.shipment_id AND a.is_current = 1) AS live
FROM shipments s
WHERE s.current_status NOT IN ('CANCELLED','COMPLETED')
  AND (SELECT COUNT(*) FROM appointments a WHERE a.shipment_id = s.shipment_id AND a.is_current = 1) = 0;
```

- **Result:** SHP1012, SHP1016, SHP1018 (no-show), SHP1021.
- **Why it matters:** These trucks are coming with nowhere to go, and the reschedule chain is lost.
- **What our design does:** A cancellation with the truck still coming creates an open exception until rebooked; reschedules always set `replaced_appointment_id`.

### DA-11 🟠 Driver identity is ambiguous or impossible

- **Category:** Validity
- **What's wrong:** DRV007 is on two IN_TRANSIT shipments in two different trucks (SHP1007, SHP1016). DRV004 has two assignments and THR010 has no shipment.
- **Query:**

```sql
SELECT a.driver_id, a.shipment_id, a.vehicle_id, b.shipment_id, b.vehicle_id
FROM shipments a JOIN shipments b ON a.driver_id = b.driver_id AND a.shipment_id < b.shipment_id
WHERE a.current_status = 'IN_TRANSIT' AND b.current_status = 'IN_TRANSIT';
```

- **Result:** DRV007: SHP1007/VEH008 and SHP1016/VEH007.
- **Why it matters:** A message from DRV007 can't be tied to one load with certainty; the agent could move the wrong booking.
- **What our design does:** If the driver has more than one active shipment, the agent must confirm which one (by vehicle or reference) before acting.

### DA-12 🟡 A duplicate message opened its own thread

- **Category:** Duplicates
- **What's wrong:** MSG015 is correctly flagged `is_duplicate = 1`, but it created THR009 and EXC009 next to the original THR001 / EXC001. Dedupe works at the exception level only.
- **Query:**

```sql
SELECT exception_id, thread_id, severity_code, exception_status, dedupe_key
FROM driver_exceptions WHERE dedupe_key IN (SELECT dedupe_key FROM driver_exceptions GROUP BY 1 HAVING COUNT(*) > 1);
```

- **Result:** EXC001 (HIGH) and EXC009 (LOW, DUPLICATE) share one key.
- **Why it matters:** Retries can double the workload, and duplicates could each request a slot.
- **What our design does:** Every request carries an idempotency key; the same key returns the same result (also part of the integrity proof).

### DA-13 🟡 A failed escalation email looks like a sent one

- **Category:** Completeness
- **What's wrong:** THR005 is ESCALATED, but the email to the warehouse (OM004) FAILED.
- **Query:**

```sql
SELECT operational_message_id, appointment_id, channel, delivery_status FROM operational_messages WHERE delivery_status NOT IN ('SENT','DELIVERED');
```

- **Result:** OM004 FAILED (SHP1015 reefer case), OM002 QUEUED.
- **Why it matters:** Everyone thinks a human is handling the reefer truck; nobody is.
- **What our design does:** An escalation counts as done only when delivered and acknowledged; failures retry and alert a coordinator (M3).

### DA-14 🟡 The operating-hours rule is not applied by the view

- **Category:** Validity
- **What's wrong:** RULE005 sets the last new start at 21:00, yet `v_slot_availability` shows four 21:00 slots as AVAILABLE with no flag.
- **Query:**

```sql
SELECT slot_id, slot_start_ts, availability_status FROM v_slot_availability WHERE substr(slot_start_ts, 12, 5) >= '21:00';
```

- **Result:** 4 AVAILABLE, 1 BLOCKED.
- **Why it matters:** Facility rules live as data that nothing enforces.
- **What our design does:** The capacity layer reads `facility_rules`; slots that need approval route to a human (Q8 asks whether 21:00 itself counts).

---

## 7. Questions for the client (send as one email after the audit)

| # | Question | Why we need it | Blocks build? |
|---|---|---|---|
| Q1 | Does every booking need warehouse confirmation before it is final? (U1/A1) | Decides whether the PENDING step and its timer exist | No: we design for "yes" |
| Q2 | How long should we wait for the warehouse to confirm before a coordinator chases? | Sets the confirmation timeout | No: configurable default |
| Q3 | What should trigger spike mode? (U4) | Switches the hold from 10 to 5 min | No: configurable |
| Q4 | Is 10 min (5 in a spike) a fair hold for drivers? (A2) | Too short loses drivers; too long blocks slots | No: configurable |
| Q5 | How many coordinators are on shift, and when? (A5) | Sizes escalation load | No |
| Q6 | How much buffer should we add between a low-confidence ETA and the slot start? | SHP1013 was booked with zero slack on a vague "late by one hour" | No: configurable |
| Q7 | How many clarifying questions before we give up and book with a buffer or escalate? | Too many annoys drivers (M7); too few risks bad bookings (M2) | No: configurable |
| Q8 | Does a slot starting exactly at 21:00 need manual approval, or only later starts? (RULE005) | Four 21:00 slots show as available (DA-14) | No |
| Q9 | Is `max_vehicle_weight_kg` gross (truck + load) or load only? | Two loads already exceed it (DA-07) | No: we check load weight now |
| Q10 | Should loads longer than a slot book two slots, or overrun into the next? | 4 live bookings overrun (DA-08) | No: we book consecutive slots |
| Q11 | Were APT1007 and APT1012A really both confirmed on SLOT-JAI-003? | History shows a double booking (DA-09) | No |
| Q12 | Is DRV007 really on two trucks, or is one shipment mis-assigned? | Ambiguous driver identity (DA-11) | No |
| Q13 | May coordinators reassign a PENDING (not yet confirmed) booking to protect a CRITICAL load? | D7 replay: SHP1014 waits until 13:00 otherwise | No: alert-only until confirmed |
