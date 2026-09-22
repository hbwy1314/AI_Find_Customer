-- 001 — Create the _migrations audit ledger.
--
-- This migration exists so the engine can be activated even before any
-- schema has been extracted into migrations/*.sql. It bumps user_version
-- from 0 to 1. Future migrations (002+) will own real schema changes;
-- the legacy CREATE TABLE blocks in emailing/store.py and
-- automation/job_queue.py continue to guard those tables for now.
--
-- P1 (data-layer migration) will replace the legacy blocks with
-- migrations/002_initial_emailing_schema.sql etc. When that lands, the
-- IF NOT EXISTS guards will become no-ops on upgraded DBs.

CREATE TABLE IF NOT EXISTS _migrations (
    id INTEGER PRIMARY KEY,
    version INTEGER NOT NULL UNIQUE,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);