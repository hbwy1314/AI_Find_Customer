-- 002 — Create the `hunts` table (hunt metadata + workflow state).
--
-- Replaces the JSON-per-hunt pattern (`backend/data/hunts/{hunt_id}.json`)
-- with a single SQLite table. The Wave-1 refactor keeps JSON as the read
-- source-of-truth (DualWriteBackend writes both, reads via JSON), so this
-- table is a write-through shadow that the Wave-3 read flip will promote
-- to source-of-truth. Wave-4 retires the JSON files entirely.
--
-- Design notes:
-- * `hunt_id` is the UUID string already used in JSON filenames — no mapping.
-- * Fields that were JSON arrays/objects in the old payload
--   (`product_keywords`, `target_customer_profile`, `target_regions`,
--    `email_template_examples`, `cost_summary`) are stored as JSON text
--   columns for now — Wave 2 may extract the hottest one (cost_summary)
--   into `hunt_cost_events` (005).
-- * `status` uses the same string vocabulary as before
--   (pending / running / done / failed / cancelled).
-- * `updated_at` is set on every UPSERT in SQLiteBackend so callers can
--   cheaply compute "hunt modified in the last N minutes".
-- * Indexes target the three known hot paths:
--     - active hunts by created_at DESC (status filters)
--     - completed-hunt purge by completed_at
--     - dedup/lookup by website_url (deals with the same company twice)

CREATE TABLE hunts (
    hunt_id              TEXT PRIMARY KEY,
    status               TEXT NOT NULL DEFAULT 'pending',
    current_stage        TEXT NOT NULL DEFAULT '',
    hunt_round           INTEGER NOT NULL DEFAULT 0,
    leads_count          INTEGER NOT NULL DEFAULT 0,
    email_sequences_count INTEGER NOT NULL DEFAULT 0,
    result               TEXT NOT NULL DEFAULT '',
    error                TEXT NOT NULL DEFAULT '',
    website_url          TEXT NOT NULL DEFAULT '',
    product_keywords     TEXT NOT NULL DEFAULT '[]',
    target_customer_profile TEXT NOT NULL DEFAULT '{}',
    target_regions       TEXT NOT NULL DEFAULT '[]',
    email_template_examples TEXT NOT NULL DEFAULT '[]',
    email_template_notes TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL,
    completed_at         TEXT NOT NULL DEFAULT '',
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_hunts_status_created
    ON hunts(status, created_at DESC);

CREATE INDEX idx_hunts_completed_at
    ON hunts(completed_at)
    WHERE status = 'done';

CREATE INDEX idx_hunts_website
    ON hunts(website_url)
    WHERE website_url != '';