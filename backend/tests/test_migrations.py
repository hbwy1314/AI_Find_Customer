"""Tests for the SQLite migration engine (P0.3).

Covers:
* Empty DB: applies 001, sets user_version=1, _migrations has one row.
* Idempotency: calling init_db twice on the same DB does nothing the second
  time and never errors.
* Legacy user_version: a DB that already has user_version=N (e.g. from a
  manual ``PRAGMA user_version = 5``) gets those versions marked as legacy
  in the ledger and the runner doesn't try to replay them.
* End-to-end via store.init_db(): the EmailStore's init_db path runs the
  migration runner and produces the expected schema state.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from migrations.runner import (
    MIGRATION_FILENAME_RE,
    apply_pending_migrations,
    list_migration_files,
)


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "migrations_test.db")


def _open(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_migration_filename_regex_accepts_valid_names() -> None:
    match = MIGRATION_FILENAME_RE.match("001_create_ledger.sql")
    assert match is not None
    assert int(match.group(1)) == 1
    assert match.group("name") == "create_ledger"


def test_migration_filename_regex_rejects_invalid_names() -> None:
    for bad in [
        "1_foo.sql",        # not zero-padded
        "001.sql",          # no name
        "001_FOO.sql",      # uppercase
        "001_foo bar.sql",  # space
        "001_foo.txt",      # wrong ext
    ]:
        assert MIGRATION_FILENAME_RE.match(bad) is None, bad


def test_list_migration_files_returns_sorted() -> None:
    versions = [v for v, _ in list_migration_files()]
    assert versions == sorted(versions)
    assert 1 in versions  # 001_create_migrations_ledger.sql always present


def test_apply_pending_on_empty_db_creates_ledger_and_bumps_user_version(
    tmp_db_path: str,
) -> None:
    conn = _open(tmp_db_path)
    applied = apply_pending_migrations(conn)
    conn.close()

    assert applied == ["001_create_migrations_ledger.sql"]

    # Reopen and inspect
    conn = sqlite3.connect(tmp_db_path)
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    rows = conn.execute("SELECT version, name FROM _migrations").fetchall()
    conn.close()

    assert version == 1
    assert rows == [(1, "001_create_migrations_ledger.sql")]


def test_apply_pending_is_idempotent(tmp_db_path: str) -> None:
    # Run twice; second call is a no-op
    for _ in range(2):
        conn = _open(tmp_db_path)
        apply_pending_migrations(conn)
        conn.close()
    # First run applies 001, second run applies nothing
    # (we just need both runs to complete without raising)

    conn = sqlite3.connect(tmp_db_path)
    rows = conn.execute("SELECT version FROM _migrations").fetchall()
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    conn.close()
    assert len(rows) == 1
    assert version == 1


def test_legacy_user_version_is_marked_not_replayed(tmp_db_path: str) -> None:
    # Simulate a DB that someone manually bumped to user_version=1
    # (e.g. via PRAGMA user_version = 1) before this engine existed.
    conn = _open(tmp_db_path)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()

    applied = apply_pending_migrations(conn)
    conn.close()

    # Nothing new applied (001 was "legacy" relative to user_version=1)
    assert applied == []

    conn = sqlite3.connect(tmp_db_path)
    rows = conn.execute(
        "SELECT version, name FROM _migrations ORDER BY version"
    ).fetchall()
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    conn.close()
    # user_version untouched
    assert version == 1
    # ledger has the legacy marker for v1
    assert rows == [(1, "legacy-001")]


def test_email_store_init_db_runs_migrations(tmp_db_path: str) -> None:
    """End-to-end: EmailStore.init_db() must call the migration runner
    before creating its own tables."""
    from emailing.store import EmailStore

    store = EmailStore(db_path=tmp_db_path)
    store.init_db()
    store.init_db()  # idempotent

    conn = sqlite3.connect(tmp_db_path)
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()

    assert version == 1
    assert "_migrations" in tables
    # spot-check that the legacy DDL still ran (it should)
    assert "email_accounts" in tables


def test_job_queue_init_db_runs_migrations(tmp_db_path: str) -> None:
    """End-to-end: HuntJobQueue.init_db() must also call the migration runner."""
    from automation.job_queue import HuntJobQueue

    queue = HuntJobQueue(db_path=tmp_db_path)
    queue.init_db()
    queue.init_db()  # idempotent

    conn = sqlite3.connect(tmp_db_path)
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()

    assert version == 1
    assert "_migrations" in tables
    assert "hunt_jobs" in tables
