-- 005 — Create the `hunt_cost_events` table (per-LLM-call cost ledger).
--
-- Replaces the denormalized `cost_summary` JSON object inside each hunt
-- document. Today the only granularity available is "total per hunt" —
-- the JSON file has no place to store per-call token counts, so a
-- provider-cost audit requires re-parsing the LangGraph checkpoint.
--
-- Design notes:
-- * One row per LLM/Tavily/Serp/etc. call that produces a billable event.
-- * `cost_usd` is stored as REAL (SQLite REAL = IEEE-754 binary64). For
--   OpenAI/Anthropic prices in the $0.000001-$0.10 range, double precision
--   is well below rounding noise. If the platform ever supports sub-cent
--   token pricing, switch to TEXT and store micros (cost_micros INTEGER).
-- * `provider` is a free-form label (openai / anthropic / tavily / ...) —
--   index targets provider-level analytics queries
--   ("LLM spend last 7d grouped by provider").
-- * Composite index on `(hunt_id, captured_at DESC)` keeps per-hunt
--   aggregations and "latest cost event" lookups cheap.

CREATE TABLE hunt_cost_events (
    event_id    TEXT PRIMARY KEY,
    hunt_id     TEXT NOT NULL REFERENCES hunts(hunt_id) ON DELETE CASCADE,
    provider    TEXT NOT NULL,
    model       TEXT NOT NULL DEFAULT '',
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL NOT NULL DEFAULT 0.0,
    captured_at TEXT NOT NULL
);

CREATE INDEX idx_cost_hunt_time
    ON hunt_cost_events(hunt_id, captured_at DESC);

CREATE INDEX idx_cost_provider
    ON hunt_cost_events(provider, captured_at DESC);