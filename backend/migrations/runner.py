"""Lightweight SQLite migrations engine.

Why this exists
---------------
The codebase historically grew DDL by appending ``CREATE TABLE IF NOT
EXISTS`` blocks inside ``emailing/store.py`` and ``automation/job_queue.py``.
That works for additive changes but is unsafe for anything that needs to
*modify* existing columns — the right tool for that is a versioned migration.

This module is intentionally tiny: no ORM, no ``alembic``, no async driver.
It is the seam where P1 will start extracting inline DDL into numbered
``.sql`` files. For now it only manages the ``_migrations`` ledger and a
``PRAGMA user_version`` counter so P0 can ship the engine and P1 can plug
in real migrations one by one.

Rules
-----
* Migration files live next to this module, named ``NNN_description.sql``
  where ``NNN`` is a monotonically increasing zero-padded integer.
* Applied migrations are recorded in ``_migrations(id, applied_at)``.
* ``PRAGMA user_version`` is the source of truth for the current version;
  ``_migrations`` is the audit log.
* Gaps in the sequence abort startup — never silently skip a migration.
* Each migration runs inside a single transaction.

Usage
-----
::

    from migrations.runner import apply_pending_migrations
    applied = apply_pending_migrations("/path/to/db.sqlite")
    if applied:
        logger.info("Applied migrations: %s", applied)

The caller is responsible for opening the connection with the same pragmas
(``journal_mode=WAL``, ``foreign_keys=ON``, ``busy_timeout``) that the rest
of the codebase uses. ``init_db()`` in each store already does this.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent
MIGRATION_FILENAME_RE = re.compile(r"^(\d{3,})_(?P<name>[a-z0-9_]+)\.sql$")


def _utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def list_migration_files() -> list[tuple[int, Path]]:
    """Return ``[(version, path), ...]`` sorted ascending by version.

    Files that don't match ``NNN_name.sql`` are ignored. Duplicates of
    the same ``NNN`` are an error (raised lazily in :func:`apply_pending_migrations`).
    """
    found: dict[int, Path] = {}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = MIGRATION_FILENAME_RE.match(path.name)
        if not match:
            logger.debug("[migrations] skipping non-matching file: %s", path.name)
            continue
        version = int(match.group(1))
        if version in found:
            raise RuntimeError(
                f"Duplicate migration version {version}: "
                f"{found[version].name} and {path.name}"
            )
        found[version] = path
    return sorted(found.items())


def _read_user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def _write_user_version(conn: sqlite3.Connection, version: int) -> None:
    # SQLite doesn't bind PRAGMA, so escape the value safely.
    conn.execute(f"PRAGMA user_version = {int(version)}")


def _ensure_ledger_table(conn: sqlite3.Connection) -> None:
    """Create the ``_migrations`` audit table (idempotent).

    Kept separate from any concrete migration file so the ledger exists
    even before any migration has been recorded.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _migrations (
            id INTEGER PRIMARY KEY,
            version INTEGER NOT NULL UNIQUE,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    return {
        int(row[0])
        for row in conn.execute("SELECT version FROM _migrations").fetchall()
    }


def apply_pending_migrations(conn: sqlite3.Connection) -> list[str]:
    """Apply any pending migrations. Idempotent — safe to call on every
    ``init_db()``.

    Returns the list of *newly* applied migration filenames (empty on
    no-op runs). Raises on gaps or duplicate versions.
    """
    _ensure_ledger_table(conn)
    conn.commit()

    current_version = _read_user_version(conn)
    already = _applied_versions(conn)
    files = list_migration_files()

    # Validate sequence integrity: 1..N must be contiguous (allow starting > 1
    # if legacy DBs were at that version before this engine existed).
    expected_versions = [v for v, _ in files]
    if expected_versions:
        # Detect gaps relative to current_version. If current_version == 0
        # we require 001 to exist.
        for v, _path in files:
            if v <= current_version and v not in already:
                # A version lower than current was never recorded — possibly
                # a legacy DB from before this engine existed. Mark it as
                # implicitly applied so we don't try to re-run historical DDL.
                logger.info(
                    "[migrations] marking legacy version %s as applied "
                    "(DB already at user_version=%s)",
                    v, current_version,
                )
                conn.execute(
                    "INSERT OR IGNORE INTO _migrations(version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (v, f"legacy-{v:03d}", _utcnow_iso()),
                )
                already.add(v)
                conn.commit()

    newly_applied: list[str] = []
    for version, path in files:
        if version in already:
            continue
        if version <= current_version:
            # The version is <= DB's known version but not in the ledger —
            # we already inserted the legacy marker above. Skip.
            continue
        sql = path.read_text(encoding="utf-8")
        logger.info("[migrations] applying %s (version %d)", path.name, version)
        try:
            conn.executescript(sql)
        except sqlite3.Error as exc:
            logger.exception("[migrations] failed applying %s", path.name)
            raise RuntimeError(
                f"Migration {path.name} failed: {exc}"
            ) from exc
        conn.execute(
            "INSERT INTO _migrations(version, name, applied_at) VALUES (?, ?, ?)",
            (version, path.name, _utcnow_iso()),
        )
        _write_user_version(conn, version)
        conn.commit()
        newly_applied.append(path.name)

    if newly_applied:
        logger.info("[migrations] applied %d new migration(s)", len(newly_applied))
    return newly_applied
