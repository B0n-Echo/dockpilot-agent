-- Conversation agent (dp-14): what the agent waits for in each chat thread. Working copy only, additive.
-- Chats themselves use the client's chat_threads / chat_messages tables.
CREATE TABLE conversation_state (
    thread_id   TEXT PRIMARY KEY REFERENCES chat_threads(thread_id),
    awaiting    TEXT NOT NULL DEFAULT 'NOTHING'
                    CHECK (awaiting IN ('NOTHING', 'SHIPMENT', 'ETA', 'CHOICE', 'WAREHOUSE')),
    note        TEXT NOT NULL DEFAULT '{}',  -- JSON: options offered, held slot, parked message, best guess
    times_asked INTEGER NOT NULL DEFAULT 0,
    asked_at    TEXT,                        -- when the open question was asked (silence timer)
    nudged_at   TEXT,                        -- when the one nudge was sent
    updated_at  TEXT NOT NULL
);
