-- Driver login (dp-18): one-time codes and sessions. Working copy only, additive.

-- One row per code sent. Only a hash of the code is stored, never the code itself.
CREATE TABLE login_codes (
    code_id      TEXT PRIMARY KEY,
    driver_id    TEXT NOT NULL REFERENCES drivers(driver_id),
    code_hash    TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    wrong_tries  INTEGER NOT NULL DEFAULT 0,
    used_at      TEXT,
    cancelled_at TEXT
);

-- One row per login. shipment_id stays empty until the driver's trip is known.
CREATE TABLE driver_sessions (
    session_id  TEXT PRIMARY KEY,
    driver_id   TEXT NOT NULL REFERENCES drivers(driver_id),
    shipment_id TEXT REFERENCES shipments(shipment_id),
    thread_id   TEXT REFERENCES chat_threads(thread_id),
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    ended_at    TEXT
);
