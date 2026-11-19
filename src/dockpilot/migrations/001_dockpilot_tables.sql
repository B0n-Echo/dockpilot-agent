-- DockPilot's own tables, added to the working copy of the client database.
-- Additive only: creates new tables and indexes; never alters or drops the client's 18 tables.
-- Timestamps are ISO-8601 text with offset, matching the client data (e.g. 2026-08-04T09:45:00+05:30).

-- A slot held for one driver while they decide (decision D5).
-- A long unload holds consecutive slots: one row per slot, sharing group_id.
-- After confirmation the extra slot's hold stays CONVERTED and points at the same appointment,
-- so it keeps occupying that slot while the appointment is active.
CREATE TABLE slot_holds (
    hold_id        TEXT PRIMARY KEY,
    group_id       TEXT NOT NULL,
    slot_id        TEXT NOT NULL REFERENCES appointment_slots(slot_id),
    shipment_id    TEXT NOT NULL REFERENCES shipments(shipment_id),
    driver_id      TEXT NOT NULL REFERENCES drivers(driver_id),
    thread_id      TEXT REFERENCES chat_threads(thread_id),
    hold_status    TEXT NOT NULL CHECK (hold_status IN ('ACTIVE', 'CONVERTED', 'EXPIRED')),
    created_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    appointment_id TEXT REFERENCES appointments(appointment_id),
    request_key    TEXT NOT NULL UNIQUE
);

-- The database itself refuses a second ACTIVE hold on the same slot.
CREATE UNIQUE INDEX ux_active_hold_per_slot
    ON slot_holds(slot_id)
    WHERE hold_status = 'ACTIVE';

-- The hand-off record a coordinator receives (decisions D10, D12; measure M3).
CREATE TABLE escalations (
    escalation_id      TEXT PRIMARY KEY,
    created_at         TEXT NOT NULL,
    reason_code        TEXT NOT NULL CHECK (reason_code IN (
                           'NO_FEASIBLE_SLOT', 'NEAR_TIE', 'CRITICAL_WAITING', 'WAREHOUSE_TIMEOUT',
                           'DELIVERY_FAILED', 'ETA_UNRESOLVED', 'BUMP_REQUEST', 'CONTRADICTORY_DATA',
                           'APPROVAL_REQUIRED', 'SAFETY')),
    driver_id          TEXT REFERENCES drivers(driver_id),
    shipment_id        TEXT REFERENCES shipments(shipment_id),
    thread_id          TEXT REFERENCES chat_threads(thread_id),
    appointment_id     TEXT REFERENCES appointments(appointment_id),
    latest_eta_ts      TEXT,
    eta_source         TEXT,
    eta_confidence     TEXT,
    options_checked    TEXT,  -- JSON list: [{"slot_id": ..., "failed_because": [...]}]
    driver_constraints TEXT,
    summary            TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'OPEN'
                           CHECK (status IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVED')),
    delivery_status    TEXT NOT NULL DEFAULT 'PENDING'
                           CHECK (delivery_status IN ('PENDING', 'DELIVERED', 'FAILED')),
    acknowledged_at    TEXT,
    resolved_at        TEXT,
    resolution_note    TEXT
);

-- Append-only diary of every state change (audit trail; see DA-09).
CREATE TABLE events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts    TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    shipment_id TEXT REFERENCES shipments(shipment_id),
    actor       TEXT NOT NULL CHECK (actor IN ('DRIVER', 'AGENT', 'WAREHOUSE', 'COORDINATOR', 'TIMER', 'SYSTEM')),
    payload     TEXT  -- JSON details
);
