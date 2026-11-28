# DockPilot: client readout

*For SetuHaul's head of operations · one page · 28 Nov 2026 · built on the 4 Aug 2026 snapshot (2 warehouses, 21 shipments)*

**In one line:** a driver who is running late messages DockPilot. It offers only slots the truck can really use, holds one safely, gets the warehouse's yes, and hands anything it can't solve to a coordinator with the whole story. It never promises one slot twice: **0 double bookings in 800 competing requests.**

## Policy: how DockPilot decides

| Question | Rule |
|---|---|
| Which slots are offered? | Only slots that pass **15 checks**: dock type, refrigeration, weight, arrival time plus a buffer, unloading time, dock breakdowns, operating hours and more. The best 3 are offered, earliest first. |
| What does the AI do? | **It only reads.** It turns "should make it by half past eleven" into fields. Every decision is plain, tested code. If the AI is down, simple rules read the message. |
| Two trucks want the same slot | **Normal day:** the first valid request wins, at once. **Rush (5+ requests in 10 min):** requests for one slot are collected for 30 seconds and ranked by priority, then harm already done, then who asked first. A near-tie goes to a person. |
| A promise in progress | A slot is **held for 10 minutes** (5 in a rush) while the driver decides. It becomes a booking request on "1", and is **final only when the warehouse confirms**. |
| Bookings already confirmed | Never moved automatically. Only a coordinator can bump one, and never the same truck twice in a day. A critical load that's squeezed out raises an alert. |
| A person takes over when… | no slot fits · the data contradicts itself · safety words · a near-tie · the warehouse hasn't answered in 30 min · the driver goes silent after a question |

## Limits: what it does not do yet

- **The chat is a web page, not WhatsApp or SMS.** Drivers who aren't chatting aren't told when a dock breaks or their booking changes.
- **The demo login shows the code on screen.** Anyone who knows a driver's number could log in as them. Real codes need SMS, WhatsApp or email (Amazon Cognito email is built, but parked by an AWS account restriction). A login limit (3 codes per 15 minutes) stops guessing.
- **Coordinators can see hand-offs but can't close them,** or move another truck's booking, from the tool yet.
- **A driver's own limits** ("not before 12", "must leave by 1:30") are read but not yet used to choose slots.
- **Timings are defaults until you confirm them:** the hold time, the 30-minute warehouse timeout, the 30-minute buffer for vague arrival times and the rush trigger. All are in one settings file.
- **Proven on one day of dummy data.** One server with one SQLite database is right for this scale. More warehouses would need a shared database (the same rules carry over).

## Risks, and how they're handled

| Risk | How likely | What we did | What we need from you |
|---|---|---|---|
| **The data contradicts itself:** a driver on two trucks, a booking breaking its own rules, two confirmed bookings on one slot in history | Seen in the snapshot (14 data issues, each proved by a query) | DockPilot warns the driver and hands the case to a coordinator. **It never edits your data.** | Answers to the 13 client questions (e.g. Q11, Q12) |
| **The AI misreads a message** | Low, but possible | It can only fill in fields; the 15 checks run on every slot anyway; a vague time gets one clear question, not a guess | — |
| **The warehouse never confirms** (a request was never even sent: DA-02) | Seen in the snapshot | A coordinator is alerted at 30 minutes, and the driver is told once | Confirm the 30-minute timeout (Q2) |
| **A critical load waits behind a pending one** | Seen in the replay (SHP1014) | An alert names the booking a coordinator could move | May coordinators reassign pending bookings? (Q13) |
| **Login abuse** (someone using a driver's number) | Real for the demo login | The login limit, and the trip check on every message; a real code route closes the gap | A channel for codes (SMS / WhatsApp / email) |

**Proof behind the numbers:** 197 automated checks; the 14 seeded cases replayed with 0 double bookings, 0 infeasible bookings, 100% of no-slot cases escalated with a complete record, and 71% resolved without a coordinator. The integrity race: 100 rounds of 8 programs racing for one slot gave **exactly one winner every round**; the unprotected version double-booked in every round, and the same check caught it.
