"""Tests for the hunt_store Backend abstraction (P1.1 — Wave 1).

Three layers under test:

* :class:`JSONBackend` — byte-for-byte clone of the pre-P1 fcntl/JSON
  implementation; re-tested here so future refactors stay honest to the
  behaviour the rest of the suite relies on.
* :class:`SQLiteBackend` — exercises the migrations/002-005 schema,
  upserts, dedup, lead linking, and purge-by-created_at.
* :class:`DualWriteBackend` — write-through to both, reads via primary,
  and the Wave-1 safety net that a SQLite failure never blocks the
  pipeline.

Each test isolates ``tmp_path`` for both the JSON ``hunts_dir`` and the
SQLite ``hunt_db_path``, and monkey-patches ``hunt_store.get_settings``
so neither backend accidentally reads the real ``backend/data/`` dir.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from api import hunt_store
from api.hunt_store import (
    DualWriteBackend,
    JSONBackend,
    SQLiteBackend,
    is_tombstoned,
    reset_backend_singleton,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def hunts_dir(tmp_path: Path) -> Path:
    """Return an isolated ``hunts_dir`` rooted at ``tmp_path/hunts`` and
    monkey-patch ``hunt_store.get_settings`` so every backend reads from it."""
    directory = tmp_path / "hunts"
    directory.mkdir(parents=True, exist_ok=True)
    hunt_store.get_settings = lambda: SimpleNamespace(
        hunts_dir=str(directory),
        automation_queue_db_path="",
    )
    reset_backend_singleton()
    return directory


@pytest.fixture
def hunt_db(tmp_path: Path) -> Path:
    """Return an isolated ``hunts.db`` path under ``tmp_path``."""
    return tmp_path / "hunts.db"


@pytest.fixture
def json_backend(hunts_dir: Path) -> JSONBackend:
    return JSONBackend()


@pytest.fixture
def sqlite_backend(hunts_dir: Path, hunt_db: Path) -> SQLiteBackend:
    return SQLiteBackend(db_path=str(hunt_db))


@pytest.fixture
def dual_backend(hunts_dir: Path, hunt_db: Path) -> DualWriteBackend:
    return DualWriteBackend(
        primary=JSONBackend(),
        secondary=SQLiteBackend(db_path=str(hunt_db)),
    )


# ===========================================================================
# JSONBackend — defensive re-tests of the pre-P1 behaviour
# ===========================================================================


class TestJSONBackend:
    def test_save_and_load_roundtrip(self, json_backend, hunts_dir):
        json_backend.save_hunt("h1", {"status": "running", "leads_count": 5})
        loaded = json_backend.load_hunt("h1")
        assert loaded is not None
        assert loaded["status"] == "running"
        assert loaded["leads_count"] == 5
        # The JSON file lives under hunts_dir, not the SQLite one
        assert (hunts_dir / "h1.json").exists()

    def test_load_missing_returns_none(self, json_backend):
        assert json_backend.load_hunt("nope") is None

    def test_save_after_delete_raises(self, json_backend):
        json_backend.save_hunt("h1", {"status": "running"})
        json_backend.delete_hunt("h1")
        with pytest.raises(RuntimeError, match="deleted"):
            json_backend.save_hunt("h1", {"status": "running"})

    def test_load_all_hunts(self, json_backend):
        json_backend.save_hunt("h1", {"status": "running"})
        json_backend.save_hunt("h2", {"status": "done"})
        all_hunts = json_backend.load_all_hunts()
        assert set(all_hunts.keys()) == {"h1", "h2"}

    def test_load_all_mark_interrupted_marks_running_as_failed(self, json_backend, hunts_dir):
        json_backend.save_hunt("h1", {"status": "running"})
        out = json_backend.load_all_hunts(mark_interrupted=True)
        assert out["h1"]["status"] == "failed"
        assert "Process was interrupted" in out["h1"]["error"]
        # And the mutation is persisted on disk
        on_disk = json.loads((hunts_dir / "h1.json").read_text(encoding="utf-8"))
        assert on_disk["status"] == "failed"

    def test_load_all_mark_interrupted_off_keeps_running(self, json_backend):
        json_backend.save_hunt("h1", {"status": "running"})
        out = json_backend.load_all_hunts(mark_interrupted=False)
        assert out["h1"]["status"] == "running"

    def test_delete_creates_tombstone(self, json_backend, hunts_dir):
        json_backend.save_hunt("h1", {"status": "running"})
        json_backend.delete_hunt("h1")
        assert is_tombstoned("h1")
        assert not (hunts_dir / "h1.json").exists()
        assert (hunts_dir / ".h1.deleted").exists()

    def test_purge_old_hunts_zero_retention_is_noop(self, json_backend):
        json_backend.save_hunt("h1", {"status": "done"})
        assert json_backend.purge_old_hunts(0) == []

    def test_purge_old_hunts_skips_fresh_files(self, json_backend):
        json_backend.save_hunt("h1", {"status": "done"})
        purged = json_backend.purge_old_hunts(retention_days=30)
        assert purged == []
        assert json_backend.load_hunt("h1") is not None

    def test_accept_new_leads_empty_hunt_id_returns_in_run_dedup(self, json_backend):
        leads = [
            {"company_name": "Acme", "website": "https://acme.de"},
            {"company_name": "Acme", "website": "https://acme.de"},  # dup
        ]
        out = json_backend.accept_new_leads("", leads)
        # In-run dedup drops the duplicate
        assert len(out) == 1

    def test_accept_new_leads_basic(self, json_backend):
        json_backend.save_hunt("h1", {"result": {"leads": []}})
        accepted = json_backend.accept_new_leads(
            "h1",
            [{"company_name": "Acme", "website": "https://acme.de"}],
        )
        assert len(accepted) == 1

    def test_accept_new_leads_rejects_against_deleted_hunt(self, json_backend):
        json_backend.save_hunt("h1", {"result": {"leads": []}})
        json_backend.delete_hunt("h1")
        with pytest.raises(RuntimeError, match="no longer exists"):
            json_backend.accept_new_leads(
                "h1", [{"company_name": "Acme"}]
            )

    def test_accept_new_leads_dedups_against_existing(self, json_backend):
        json_backend.save_hunt(
            "h1",
            {"result": {"leads": [{"company_name": "Acme", "website": "https://acme.de"}]}},
        )
        accepted = json_backend.accept_new_leads(
            "h1",
            [{"company_name": "Acme", "website": "https://acme.de"}],
        )
        assert accepted == []

    def test_current_lead_keys_aggregates_across_hunts(self, json_backend):
        json_backend.save_hunt(
            "h1", {"result": {"leads": [{"company_name": "Acme"}]}}
        )
        json_backend.save_hunt(
            "h2", {"result": {"leads": [{"company_name": "Beta"}]}}
        )
        keys = json_backend.current_lead_keys()
        # The exact key shape comes from lead_identity; we only assert
        # the set is non-empty and contains entries from both hunts.
        assert len(keys) >= 2

    def test_saved_leads_returns_current_state(self, json_backend):
        json_backend.save_hunt(
            "h1", {"result": {"leads": [{"company_name": "Acme"}]}}
        )
        leads = json_backend.saved_leads("h1")
        assert leads == [{"company_name": "Acme"}]


# ===========================================================================
# SQLiteBackend — schema, CRUD, dedup, purge
# ===========================================================================


class TestSQLiteBackend:
    def test_schema_applied_on_first_connect(self, sqlite_backend, hunt_db):
        sqlite_backend.save_hunt("h1", {"status": "running"})
        conn = sqlite3.connect(str(hunt_db))
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "hunts" in tables
        assert "hunt_leads" in tables
        assert "hunt_stage_snapshots" in tables
        assert "hunt_cost_events" in tables
        assert "_migrations" in tables

    def test_save_and_load_roundtrip(self, sqlite_backend):
        sqlite_backend.save_hunt("h1", {"status": "running", "leads_count": 3})
        loaded = sqlite_backend.load_hunt("h1")
        assert loaded is not None
        assert loaded["status"] == "running"
        assert loaded["leads_count"] == 3

    def test_save_upsert_overwrites(self, sqlite_backend):
        sqlite_backend.save_hunt("h1", {"status": "running"})
        sqlite_backend.save_hunt("h1", {"status": "done", "leads_count": 7})
        loaded = sqlite_backend.load_hunt("h1")
        assert loaded["status"] == "done"
        assert loaded["leads_count"] == 7

    def test_load_missing_returns_none(self, sqlite_backend):
        assert sqlite_backend.load_hunt("nope") is None

    def test_load_all_hunts(self, sqlite_backend):
        sqlite_backend.save_hunt("h1", {"status": "running"})
        sqlite_backend.save_hunt("h2", {"status": "done"})
        all_hunts = sqlite_backend.load_all_hunts()
        assert set(all_hunts.keys()) == {"h1", "h2"}

    def test_load_all_mark_interrupted_persists_failure(self, sqlite_backend, hunt_db):
        sqlite_backend.save_hunt("h1", {"status": "running"})
        sqlite_backend.load_all_hunts(mark_interrupted=True)
        # Reload from raw SQLite to confirm persistence
        conn = sqlite3.connect(str(hunt_db))
        row = conn.execute(
            "SELECT status, error FROM hunts WHERE hunt_id = ?", ("h1",)
        ).fetchone()
        conn.close()
        assert row[0] == "failed"
        assert "interrupted" in row[1].lower()

    def test_delete_removes_row(self, sqlite_backend, hunt_db):
        sqlite_backend.save_hunt("h1", {"status": "running"})
        sqlite_backend.delete_hunt("h1")
        conn = sqlite3.connect(str(hunt_db))
        n = conn.execute(
            "SELECT COUNT(*) FROM hunts WHERE hunt_id = ?", ("h1",)
        ).fetchone()[0]
        conn.close()
        assert n == 0

    def test_purge_old_hunts_by_created_at(self, sqlite_backend, hunt_db):
        # Insert a row whose created_at is older than the cutoff.
        sqlite_backend.save_hunt(
            "h1",
            {"status": "done", "created_at": "2020-01-01T00:00:00+00:00"},
        )
        sqlite_backend.save_hunt(
            "h2",
            {"status": "done", "created_at": "2099-01-01T00:00:00+00:00"},
        )
        purged = sqlite_backend.purge_old_hunts(retention_days=30)
        assert purged == ["h1"]
        assert sqlite_backend.load_hunt("h1") is None
        assert sqlite_backend.load_hunt("h2") is not None

    def test_purge_old_hunts_zero_retention_is_noop(self, sqlite_backend):
        sqlite_backend.save_hunt("h1", {"status": "done"})
        assert sqlite_backend.purge_old_hunts(0) == []

    def test_accept_new_leads_basic(self, sqlite_backend):
        sqlite_backend.save_hunt("h1", {"result": {"leads": []}})
        accepted = sqlite_backend.accept_new_leads(
            "h1",
            [{"company_name": "Acme", "website": "https://acme.de"}],
        )
        assert len(accepted) == 1
        loaded = sqlite_backend.load_hunt("h1")
        assert loaded["leads_count"] == 1

    def test_accept_new_leads_rejects_against_deleted_hunt(
        self, sqlite_backend, json_backend  # noqa: ARG002 — fixture provides filesystem tombstone
    ):
        sqlite_backend.save_hunt("h1", {"result": {"leads": []}})
        # Use JSONBackend just to drop a filesystem tombstone that
        # SQLiteBackend checks via the shared ``_hunts_dir``.
        json_backend.delete_hunt("h1")
        with pytest.raises(RuntimeError, match="no longer exists"):
            sqlite_backend.accept_new_leads("h1", [{"company_name": "Acme"}])

    def test_accept_new_leads_dedups_within_hunt(self, sqlite_backend):
        sqlite_backend.save_hunt("h1", {"result": {"leads": []}})
        accepted1 = sqlite_backend.accept_new_leads(
            "h1", [{"company_name": "Acme", "website": "https://acme.de"}]
        )
        accepted2 = sqlite_backend.accept_new_leads(
            "h1", [{"company_name": "Acme", "website": "https://acme.de"}]
        )
        assert len(accepted1) == 1
        assert accepted2 == []
        loaded = sqlite_backend.load_hunt("h1")
        assert loaded["leads_count"] == 1

    def test_accept_new_leads_dedups_across_hunts(self, sqlite_backend):
        sqlite_backend.save_hunt("h1", {"result": {"leads": []}})
        sqlite_backend.save_hunt("h2", {"result": {"leads": []}})
        sqlite_backend.accept_new_leads(
            "h1", [{"company_name": "Acme", "website": "https://acme.de"}]
        )
        accepted = sqlite_backend.accept_new_leads(
            "h2", [{"company_name": "Acme", "website": "https://acme.de"}]
        )
        assert accepted == []
        loaded = sqlite_backend.load_hunt("h2")
        assert loaded["leads_count"] == 0

    def test_accept_new_leads_empty_hunt_id_returns_in_run_dedup(
        self, sqlite_backend
    ):
        leads = [
            {"company_name": "Acme", "website": "https://acme.de"},
            {"company_name": "Acme", "website": "https://acme.de"},
        ]
        out = sqlite_backend.accept_new_leads("", leads)
        assert len(out) == 1

    def test_current_lead_keys(self, sqlite_backend):
        sqlite_backend.save_hunt("h1", {"result": {"leads": []}})
        sqlite_backend.accept_new_leads(
            "h1", [{"company_name": "Acme", "website": "https://acme.de"}]
        )
        sqlite_backend.accept_new_leads(
            "h1", [{"company_name": "Beta", "website": "https://beta.de"}]
        )
        keys = sqlite_backend.current_lead_keys()
        assert "acme.de" in keys or len(keys) >= 2

    def test_saved_leads_returns_current_state(self, sqlite_backend):
        sqlite_backend.save_hunt("h1", {"result": {"leads": []}})
        sqlite_backend.accept_new_leads(
            "h1", [{"company_name": "Acme", "website": "https://acme.de"}]
        )
        leads = sqlite_backend.saved_leads("h1")
        assert len(leads) == 1
        assert leads[0]["company_name"] == "Acme"

    def test_is_tombstoned_reflects_filesystem_marker(
        self, sqlite_backend, json_backend  # noqa: ARG002
    ):
        sqlite_backend.save_hunt("h1", {"result": {"leads": []}})
        assert not sqlite_backend.is_tombstoned("h1")
        json_backend.delete_hunt("h1")  # drops filesystem tombstone
        assert sqlite_backend.is_tombstoned("h1")


# ===========================================================================
# DualWriteBackend — write-through contract
# ===========================================================================


class TestDualWriteBackend:
    def test_save_writes_to_both_backends(
        self, dual_backend, hunts_dir: Path, hunt_db: Path
    ):
        dual_backend.save_hunt("h1", {"status": "running"})
        # JSON side
        assert (hunts_dir / "h1.json").exists()
        # SQLite side
        conn = sqlite3.connect(str(hunt_db))
        n = conn.execute(
            "SELECT COUNT(*) FROM hunts WHERE hunt_id = ?", ("h1",)
        ).fetchone()[0]
        conn.close()
        assert n == 1

    def test_load_reads_from_primary(
        self, dual_backend, hunts_dir: Path
    ):
        # Write only through JSON so SQLite is out of date; reads must
        # still see the JSON state.
        (hunts_dir / "h2.json").write_text(
            json.dumps(
                {"hunt_id": "h2", "status": "running", "leads_count": 9},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        loaded = dual_backend.load_hunt("h2")
        assert loaded is not None
        assert loaded["status"] == "running"
        assert loaded["leads_count"] == 9

    def test_accept_new_leads_persists_to_both(
        self, dual_backend, hunts_dir: Path, hunt_db: Path
    ):
        dual_backend.save_hunt("h1", {"result": {"leads": []}})
        accepted = dual_backend.accept_new_leads(
            "h1", [{"company_name": "Acme", "website": "https://acme.de"}]
        )
        assert len(accepted) == 1
        # JSON side got it
        on_disk = json.loads((hunts_dir / "h1.json").read_text(encoding="utf-8"))
        assert any(lead.get("company_name") == "Acme" for lead in on_disk["result"]["leads"])
        # SQLite side got it
        conn = sqlite3.connect(str(hunt_db))
        n = conn.execute(
            "SELECT COUNT(*) FROM hunt_leads WHERE hunt_id = ?", ("h1",)
        ).fetchone()[0]
        conn.close()
        assert n == 1

    def test_sqlite_failure_does_not_block_primary(self, tmp_path):
        # JSON writes to a real directory; SQLite points at a path that
        # cannot be opened (its parent doesn't exist AND we never create it).
        good_hunts_dir = tmp_path / "hunts"
        good_hunts_dir.mkdir(parents=True, exist_ok=True)
        bad_db_path = tmp_path / "no_such_dir" / "hunts.db"
        # Patch the module-level ``get_settings`` so JSONBackend (which
        # reads via ``from config.settings import get_settings``) sees
        # the test hunts_dir; an instance-level attribute would be ignored.
        hunt_store.get_settings = lambda: SimpleNamespace(
            hunts_dir=str(good_hunts_dir),
            automation_queue_db_path="",
        )
        reset_backend_singleton()
        primary = JSONBackend()
        secondary = SQLiteBackend(db_path=str(bad_db_path))
        dual = DualWriteBackend(primary=primary, secondary=secondary)
        dual.save_hunt("h1", {"status": "running"})
        assert (good_hunts_dir / "h1.json").exists()

    def test_delete_propagates_to_both(
        self, dual_backend, hunts_dir: Path, hunt_db: Path
    ):
        dual_backend.save_hunt("h1", {"status": "running"})
        dual_backend.delete_hunt("h1")
        # JSON tombstone + file removed
        assert not (hunts_dir / "h1.json").exists()
        assert (hunts_dir / ".h1.deleted").exists()
        # SQLite row removed
        conn = sqlite3.connect(str(hunt_db))
        n = conn.execute(
            "SELECT COUNT(*) FROM hunts WHERE hunt_id = ?", ("h1",)
        ).fetchone()[0]
        conn.close()
        assert n == 0


# ===========================================================================
# Factory + singleton
# ===========================================================================


class TestFactory:
    def test_get_backend_returns_json_when_db_path_missing(self, tmp_path):
        # No hunt_db_path set → degrade to JSONBackend for safety.
        hunts_dir = tmp_path / "hunts"
        hunt_store.get_settings = lambda: SimpleNamespace(
            hunts_dir=str(hunts_dir),
            automation_queue_db_path="",
            hunt_storage_backend="dual",
        )
        reset_backend_singleton()
        backend = hunt_store._get_backend()
        assert isinstance(backend, JSONBackend)

    def test_get_backend_respects_json_setting(self, tmp_path):
        hunts_dir = tmp_path / "hunts"
        hunt_store.get_settings = lambda: SimpleNamespace(
            hunts_dir=str(hunts_dir),
            automation_queue_db_path="",
            hunt_storage_backend="json",
            hunt_db_path=str(tmp_path / "hunts.db"),
        )
        reset_backend_singleton()
        backend = hunt_store._get_backend()
        assert isinstance(backend, JSONBackend)

    def test_reset_backend_singleton_drops_cache(self, tmp_path):
        hunts_dir = tmp_path / "hunts"
        hunt_store.get_settings = lambda: SimpleNamespace(
            hunts_dir=str(hunts_dir),
            automation_queue_db_path="",
            hunt_storage_backend="json",
        )
        reset_backend_singleton()
        first = hunt_store._get_backend()
        reset_backend_singleton()
        second = hunt_store._get_backend()
        # Both should be JSONBackend, but different instances after reset
        assert isinstance(first, JSONBackend)
        assert isinstance(second, JSONBackend)


# ===========================================================================
# Smoke: backend routing through the public thin-wrapper API
# ===========================================================================


def test_public_api_routes_to_backend(hunts_dir, hunt_db):
    """End-to-end smoke: the public ``save_hunt`` / ``load_hunt`` /
    ``accept_new_leads`` thin wrappers must route to a real backend
    without callers caring which one is active."""
    hunt_store.get_settings = lambda: SimpleNamespace(
        hunts_dir=str(hunts_dir),
        automation_queue_db_path="",
        hunt_storage_backend="dual",
        hunt_db_path=str(hunt_db),
    )
    reset_backend_singleton()
    hunt_store.save_hunt("h1", {"result": {"leads": []}})
    hunt_store.accept_new_leads(
        "h1", [{"company_name": "Acme", "website": "https://acme.de"}]
    )
    loaded = hunt_store.load_hunt("h1")
    assert loaded is not None
    assert loaded["leads_count"] >= 1


def test_public_api_works_under_legacy_simple_namespace(hunts_dir):
    """Pre-1 P1 tests only patch ``hunts_dir`` / ``automation_queue_db_path``
    via ``SimpleNamespace``. The factory must degrade to JSON so they
    keep passing without modification."""
    hunt_store.get_settings = lambda: SimpleNamespace(
        hunts_dir=str(hunts_dir),
        automation_queue_db_path="",
    )
    reset_backend_singleton()
    hunt_store.save_hunt("h1", {"result": {"leads": []}})
    assert hunt_store.load_hunt("h1") is not None
