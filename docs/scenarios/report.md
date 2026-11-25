# DockPilot scenario run: M1–M9

Snapshot 4 Aug 2026, Jaipur DC. 14 seeded cases on one timeline (08:22 → 16:55). Reader: rules.

## Hard targets

| # | Measure | Target | Result |
|---|---|---|---|
| M1 | Double bookings (check after every step) | 0 | **0** |
| M2 | Confirmed bookings infeasible at confirmation | 0 | **0** |
| M3 | No-slot cases escalated / hand-off complete | 100% / 100% | **100% / 100%** (1 of 1) |
| M4 | Priority-policy violations | 0 | **0** |

## Measured (targets set later by the head of operations)

| Case | Title | Outcome | M5 first message → outcome | M7 questions (defects) | Coordinator involved |
|---|---|---|---|---|---|
| [S01](S01.md) | Late ETA reported before arrival | CONFIRMED | 6.5 min, 2 turns | 0 (0) | no |
| [S02](S02.md) | Tyre repaired, must leave by 13:30 | CONFIRMED | 5.5 min, 2 turns | 0 (0) | no |
| [S03](S03.md) | Uncertain ETA (R7) and a silent driver | CONFIRMED | 32.0 min, 4 turns | 2 (0) | yes |
| [S04](S04.md) | Critical load competing for standard docks | CONFIRMED | 5.5 min, 2 turns | 0 (0) | no |
| [S05](S05.md) | Reefer dock unavailable: no feasible slot | ESCALATED | 0.0 min, 1 turns | 0 (0) | yes |
| [S06](S06.md) | Heavy vehicle, driver with two trucks | ESCALATED | 31.0 min, 3 turns | 1 (0) | yes |
| [S07](S07.md) | Early arrival does not jump the queue | BOOKING_STANDS | 0.0 min, 1 turns | 0 (0) | no |
| [S08](S08.md) | Late arrival already in the yard | ANSWERED | 0.0 min, 1 turns | 0 (0) | no |
| [S09](S09.md) | Duplicate driver message (messaging retry) | RESEND_RECOGNISED | — | 0 (0) | no |
| [S10](S10.md) | Ambiguous shipment: two trips | CONFIRMED | 6.5 min, 4 turns | 2 (0) | no |
| [S11](S11.md) | Ask-only conversation | DECLINED | 1.0 min, 2 turns | 0 (0) | no |
| [S12](S12.md) | Message about a cancelled shipment | REFUSED | 0.0 min, 1 turns | 0 (0) | no |
| [S13](S13.md) | Dock breakdown hits a confirmed booking | CONFIRMED | 0.0 min, 0 turns | 0 (0) | no |
| [S14](S14.md) | No-show check after the grace time | HANDLED | 0.0 min, 0 turns | 0 (0) | no |

- **M6** resolved with no coordinator: **10 of 14** cases (71%). Required escalations are never skipped to raise this.
- **M7** clarifying questions: **5** in all, **0** about a fact we already held.
- **M8** planned wait (slot start − declared arrival; *planned, not measured*: a replay has no real arrivals): average **102 min** over 6 rebooked trucks (SHP1005 175 min, SHP1012 50 min, SHP1020 75 min, SHP1006 100 min, SHP1014 95 min, SHP1013 120 min).
- **M9** Jaipur DC slots in use (booked or done, of open slots on active docks): **21% → 25%** (15 → 18 of 72). The audit's 14% counted all usable slots across both facilities.

## Details

- No problems found for M1, M2 or M4.
