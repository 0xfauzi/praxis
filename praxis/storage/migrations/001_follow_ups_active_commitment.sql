-- US-002: Allow multiple follow-up commitments per week so a future
-- user-picks-from-alternatives flow (user_chosen + display_text) can
-- store every suggestion and then mark all-but-one as superseded.
--
-- The v0.2 follow_ups table used `week_iso TEXT PRIMARY KEY`, which
-- caps it at one row per week. We rebuild it with `id INTEGER PRIMARY
-- KEY AUTOINCREMENT`, copy the existing rows over, and add the three
-- new columns (user_chosen, display_text, superseded_by) plus the
-- partial-unique index that allows only one "active" (outcome='pending'
-- AND superseded_by IS NULL) row per week_iso.

CREATE TABLE follow_ups_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_iso TEXT NOT NULL,
    dim_key TEXT NOT NULL,
    commitment_text TEXT NOT NULL,
    target_metric TEXT NOT NULL,
    baseline_value REAL NOT NULL,
    measured_value REAL,
    outcome TEXT NOT NULL CHECK (outcome IN ('improved','unchanged','worse','pending')),
    user_chosen INTEGER NOT NULL DEFAULT 0,
    display_text TEXT,
    superseded_by INTEGER REFERENCES follow_ups(id)
);

INSERT INTO follow_ups_v2
    (week_iso, dim_key, commitment_text, target_metric,
     baseline_value, measured_value, outcome)
SELECT
    week_iso, dim_key, commitment_text, target_metric,
    baseline_value, measured_value, outcome
FROM follow_ups;

DROP TABLE follow_ups;

ALTER TABLE follow_ups_v2 RENAME TO follow_ups;

CREATE UNIQUE INDEX idx_follow_ups_one_active_per_week
    ON follow_ups(week_iso)
    WHERE outcome='pending' AND superseded_by IS NULL;
