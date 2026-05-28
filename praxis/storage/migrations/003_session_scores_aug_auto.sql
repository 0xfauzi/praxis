-- US-004: Persist the augmentation vs automation classifier output
-- alongside its confidence so the weekly read can surface per-session
-- classification without re-running the classifier.
--
-- Both columns are nullable. Rows written before this migration ran
-- (and rows for which the classifier could not produce a confident
-- value) keep NULL, which the read helper reports as (None, None).

ALTER TABLE session_scores ADD COLUMN aug_auto_classification TEXT;
ALTER TABLE session_scores ADD COLUMN aug_auto_confidence REAL;
