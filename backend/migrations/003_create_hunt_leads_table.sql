-- 003 — Create the `hunt_leads` table (one row per accepted lead per hunt).
--
-- Replaces the nested `result.leads[]` array inside each hunt JSON file.
-- Extracting this into a relational table enables:
--   * O(1) dedup lookup by `lead_key` (composite index on hunt_id+lead_key).
--   * Cross-hunt dedup via plain `lead_key` index.
--   * Foreign-key cascade delete when a hunt is purged.
--   * Future status transitions per-lead (e.g. emailed/bounced) without
--     rewriting the whole hunt document.
--
-- Design notes:
-- * `lead_key` is the normalized identity key produced by
--   `agents.lead_identity.lead_identity_keys(lead)` — same key used for
--   in-process dedup today. Storing it as a separate column (in addition
--   to the JSON blob in `identity_json`) makes dedup an indexed lookup
--   instead of a JSON parse + Python set.
-- * `identity_json` retains the full lead dict for round-trip fidelity
--   (the lead schema has already drifted across versions; we don't try
--   to project every field into columns here).
-- * `UNIQUE(hunt_id, lead_key)` enforces the per-hunt dedup invariant at
--   the storage layer instead of relying solely on
--   `accept_new_leads()`'s in-memory set.
-- * `ON DELETE CASCADE` ties lead lifetime to hunt lifetime so purges
--   don't leak orphan leads.

CREATE TABLE hunt_leads (
    lead_id        TEXT PRIMARY KEY,
    hunt_id        TEXT NOT NULL REFERENCES hunts(hunt_id) ON DELETE CASCADE,
    lead_key       TEXT NOT NULL,
    identity_json  TEXT NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL DEFAULT 'new',
    round_introduced INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    UNIQUE(hunt_id, lead_key)
);

CREATE INDEX idx_hunt_leads_hunt_id
    ON hunt_leads(hunt_id);

CREATE INDEX idx_hunt_leads_status
    ON hunt_leads(hunt_id, status);

CREATE INDEX idx_hunt_leads_key
    ON hunt_leads(lead_key);