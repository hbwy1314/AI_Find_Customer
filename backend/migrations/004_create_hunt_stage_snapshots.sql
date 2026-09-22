-- 004 — Create the `hunt_stage_snapshots` table (per-stage state history).
--
-- Replaces the in-payload `stage_snapshots` array that today lives inside
-- each hunt JSON. Promoting it to its own table unlocks:
--   * Cheap "what state was this hunt in 10 minutes ago?" queries.
--   * Stage-level time-series analytics (P50 hunt-stage durations).
--   * Decoupling the run log from the live hunt document so write paths
--     don't have to read-modify-write the array on every snapshot.
--
-- Design notes:
-- * `state_json` keeps the full per-stage state blob (mirrors what the
--   LangGraph reducer used to push into `stage_snapshots[-1]`).
-- * `captured_at` is ISO-8601 UTC, same convention as `created_at` /
--   `completed_at` on the parent `hunts` table.
-- * Composite index `(hunt_id, captured_at DESC)` is the only access
--   pattern this table needs in Wave 1 — append-mostly with a tail read
--   for "last 5 snapshots" panels.

CREATE TABLE hunt_stage_snapshots (
    snapshot_id  TEXT PRIMARY KEY,
    hunt_id      TEXT NOT NULL REFERENCES hunts(hunt_id) ON DELETE CASCADE,
    stage        TEXT NOT NULL,
    state_json   TEXT NOT NULL DEFAULT '{}',
    captured_at  TEXT NOT NULL
);

CREATE INDEX idx_snapshots_hunt_time
    ON hunt_stage_snapshots(hunt_id, captured_at DESC);