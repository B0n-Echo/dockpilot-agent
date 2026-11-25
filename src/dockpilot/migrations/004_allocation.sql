-- Allocation policy (dp-10, D7): every slot request the agent makes. Working copy only, additive.
-- Counts requests (spike mode) and queues rivals during the collection window.
CREATE TABLE slot_requests (
    request_id    TEXT PRIMARY KEY,
    shipment_id   TEXT NOT NULL REFERENCES shipments(shipment_id),
    facility_id   TEXT NOT NULL REFERENCES facilities(facility_id),
    slot_ids      TEXT NOT NULL,  -- JSON list, in time order
    thread_id     TEXT REFERENCES chat_threads(thread_id),
    request_key   TEXT NOT NULL UNIQUE,
    requested_at  TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('HELD', 'WAITING', 'WON', 'LOST', 'NEAR_TIE', 'REFUSED')),
    decided_at    TEXT,
    hold_group    TEXT,
    reason        TEXT,
    escalation_id TEXT REFERENCES escalations(escalation_id)
);

CREATE INDEX ix_slot_requests_facility_time ON slot_requests(facility_id, requested_at);
