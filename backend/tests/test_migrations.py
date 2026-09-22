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


def _all_migration_filenames() -> list[str]:
    """All migration filenames in version order. Tests compute their expected
    ``applied``/``user_version``/``_migrations`` count dynamically from this
    so adding 002, 003, ... doesn't break the suite."""
    return [path.name for _, path in list_migration_files()]


def _all_migration_versions() -> list[int]:
    return [v for v, _ in list_migration_files()]


def _latest_migration_version() -> int:
    versions = _all_migration_versions()
    return max(versions) if versions else 0


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

    assert applied == _all_migration_filenames()

    # Reopen and inspect
    conn = sqlite3.connect(tmp_db_path)
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    rows = conn.execute("SELECT version, name FROM _migrations").fetchall()
    conn.close()

    assert version == _latest_migration_version()
    assert rows == [(v, name) for v, name in zip(_all_migration_versions(), _all_migration_filenames(), strict=False)]


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
    assert len(rows) == len(_all_migration_versions())
    assert version == _latest_migration_version()


def test_legacy_user_version_is_marked_not_replayed(tmp_db_path: str) -> None:
    # Simulate a DB that someone manually bumped to user_version=1
    # (e.g. via PRAGMA user_version = 1) before this engine existed.
    conn = _open(tmp_db_path)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()

    applied = apply_pending_migrations(conn)
    conn.close()

    # 001 is "legacy" relative to user_version=1; 002-005 are above it and
    # must be applied.
    legacy_versions = [v for v in _all_migration_versions() if v <= 1]
    pending_versions = [v for v in _all_migration_versions() if v > 1]
    expected_applied = [
        name for v, name in zip(_all_migration_versions(), _all_migration_filenames(), strict=False)
        if v in pending_versions
    ]
    assert applied == expected_applied
    assert len(legacy_versions) == 1  # only 001 is legacy here

    conn = sqlite3.connect(tmp_db_path)
    rows = conn.execute(
        "SELECT version, name FROM _migrations ORDER BY version"
    ).fetchall()
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    conn.close()
    # user_version now reflects the highest applied migration (5 after
    # the 002-005 hunt schema migrations ship). What we care about is
    # that no migration with version <= the original user_version got
    # replayed: 001 is marked legacy in the ledger, not re-run.
    assert version == _latest_migration_version()
    # ledger must contain exactly one legacy-001 entry + 4 real applied entries
    legacy_rows = [(v, f"legacy-{v:03d}") for v in _all_migration_versions() if v <= 1]
    real_rows = [
        (v, name) for v, name in zip(_all_migration_versions(), _all_migration_filenames(), strict=False)
        if v > 1
    ]
    assert rows == legacy_rows + real_rows


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

    assert version == _latest_migration_version()
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

    assert version == _latest_migration_version()
    assert "_migrations" in tables
    assert "hunt_jobs" in tables
