-- US-003: Persist per-session self-reports against a follow-up commitment.
--
-- The reflection captures whether the user feels the session lived up to
-- the active commitment (yes / no / partial / skip) plus an optional
-- free-text note. follow_up_id references follow_ups(id) which became
-- the primary key on follow_ups in US-002, so the FK resolves cleanly
-- whether the follow-up row was created pre- or post-US-002.

CREATE TABLE IF NOT EXISTS session_reflections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_stable_id TEXT NOT NULL,
    follow_up_id INTEGER NOT NULL REFERENCES follow_ups(id),
    self_report TEXT NOT NULL CHECK (self_report IN ('yes','no','partial','skip')),
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reflections_follow_up ON session_reflections(follow_up_id);
