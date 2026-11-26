-- Trace mode (dp-20): what each driver message or timer tick went through. Working copy only, additive.
CREATE TABLE trace_turns (
    turn_id    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,            -- "driver message", "timer tick", "warehouse confirms"…
    label      TEXT,                     -- the driver's text or the booking, phone numbers masked
    thread_id  TEXT,                     -- the chat it belongs to (for the driver's own web panel)
    started_at TEXT NOT NULL,            -- real time, UTC
    ms         REAL NOT NULL
);

CREATE TABLE trace_spans (
    span_id     TEXT PRIMARY KEY,
    turn_id     TEXT NOT NULL REFERENCES trace_turns(turn_id),
    parent_id   TEXT,                    -- the step this one ran inside
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('turn', 'step', 'decision', 'db', 'ai', 'timer')),
    started_at  TEXT NOT NULL,
    ms          REAL NOT NULL,
    summary_in  TEXT,                    -- short; for the AI step: the full input sent to the model
    summary_out TEXT,                    -- short; for the AI step: the model's raw answer
    notes       TEXT,                    -- e.g. why the AI fell back to the rules
    error       TEXT
);

CREATE INDEX ix_trace_turns_thread ON trace_turns(thread_id, started_at);
