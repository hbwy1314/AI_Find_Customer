"""Tests for the P1.2 — Hunt JSON → SQLite migration script.

Covers:

* ``--dry-run`` (default) — never writes, only audits.
* ``--apply`` — writes rows for hunts / hunt_leads / hunt_stage_snapshots /
  hunt_cost_events, idempotent under ``INSERT OR IGNORE``.
* ``scripts/data/migrate_hunts_audit.csv`` is empty (header-only) when
  the SQLite store is in sync with the JSON files.
* ``audit.csv`` surfaces real mismatches (hunt field drift, missing
  leads, lead_count mismatch).

Each test isolates ``tmp_path`` for both the JSON ``hunts_dir`` and the
SQLite ``hunt_db_path``, and monkey-patches ``hunt_store.get_settings``
so the script doesn't accidentally read the real ``backend/data/`` dir.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from api import hunt_store
from api.hunt_store import reset_backend_singleton
from scripts import migrate_hunts_to_sqlite as mhm

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def hunts_dir(tmp_path: Path) -> Path:
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
    return tmp_path / "hunts.db"


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.csv"


# ---------------------------------------------------------------------------
# Builders — produce realistic JSON hunt payloads for fixture files
# ---------------------------------------------------------------------------


def _write_hunt(directory: Path, hunt: dict) -> Path:
    path = directory / f"{hunt['hunt_id']}.json"
    path.write_text(json.dumps(hunt), encoding="utf-8")
    return path


def _make_hunt(
    hunt_id: str,
    *,
    status: str = "completed",
    leads: list[dict] | None = None,
    existing_leads: list[dict] | None = None,
    cost_summary: dict | None = None,
    stage_snapshots: dict | None = None,
) -> dict:
    # ``current_leads()`` prefers ``result.leads`` when present (and a list).
    # Keep the two lead fields *mutually exclusive* in fixtures so each
    # test exercises exactly one code path; mixing them was masking a
    # zero-lead surprise on the first run.
    if existing_leads is not None and leads is None:
        result_leads: list[dict] = []
        top_level_existing = existing_leads
    elif leads is not None and existing_leads is None:
        result_leads = leads
        top_level_existing = []
    elif existing_leads is None and leads is None:
        result_leads = []
        top_level_existing = []
    else:
        # Caller explicitly passed both — honor that.
        result_leads = leads or []
        top_level_existing = existing_leads or []
    payload: dict = {
        "hunt_id": hunt_id,
        "status": status,
        "current_stage": "evaluate",
        "hunt_round": 2,
        "leads_count": len(result_leads) + len(top_level_existing),
        "email_sequences_count": 0,
        "error": "",
        "created_at": "2026-09-01T10:00:00+00:00",
        "website_url": "",
        "product_keywords": ["vape", "e-cigarette"],
        "target_customer_profile": "UK vape wholesale distributors",
        "target_regions": ["UK", "Europe"],
        "email_template_examples": [],
        "email_template_notes": "",
        "existing_leads": top_level_existing,
        "completed_at": "2026-09-01T10:05:00+00:00",
        "result": {
            "description": "Test hunt",
            "leads": result_leads,
            "keywords": [],
            "used_keywords": [],
            "insight": None,
        },
    }
    if cost_summary is not None:
        payload["cost_summary"] = cost_summary
    if stage_snapshots is not None:
        payload["stage_snapshots"] = stage_snapshots
    return payload


def _make_lead(company: str, *, score: float = 8.0, website: str = "") -> dict:
    return {
        "company_name": company,
        "website": website,
        "industry": "vape wholesale",
        "match_score": score,
        "emails": [f"info@{company.lower().replace(' ', '')}.com"],
    }


# ---------------------------------------------------------------------------
# Defaults & dry-run
# ---------------------------------------------------------------------------


def test_dry_run_is_default_mode(hunts_dir: Path, hunt_db: Path, audit_path: Path) -> None:
    _write_hunt(
        hunts_dir,
        _make_hunt(
            "h1",
            leads=[_make_lead("Acme Vapes")],
            cost_summary={
                "total_cost_usd": 0.01,
                "total_tokens": 100,
                "total_llm_calls": 2,
                "rounds_completed": 1,
                "avg_cost_per_round_usd": 0.01,
                "by_agent": {
                    "insight": {
                        "prompt_tokens": 50,
                        "completion_tokens": 50,
                        "total_tokens": 100,
                        "cost_usd": 0.01,
                        "call_count": 2,
                        "models": {
                            "gpt-4o-mini": {
                                "prompt_tokens": 50,
                                "completion_tokens": 50,
                                "total_tokens": 100,
                                "cost_usd": 0.01,
                                "call_count": 2,
                            }
                        },
                    }
                },
            },
        ),
    )
    exit_code = mhm.run(
        hunts_dir=hunts_dir,
        db_path=hunt_db,
        audit_path=audit_path,
        apply=False,
    )
    assert exit_code == 0
    # Schema exists but no rows were written — dry-run must not touch data.
    conn = sqlite3.connect(str(hunt_db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM hunts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM hunt_leads").fetchone()[0] == 0
    finally:
        conn.close()


def test_dry_run_audit_header_only_when_in_sync(
    hunts_dir: Path, hunt_db: Path, audit_path: Path
) -> None:
    """After a successful apply, a follow-up dry-run should emit an empty
    audit (header-only CSV)."""
    _write_hunt(hunts_dir, _make_hunt("h1", leads=[_make_lead("A")]))
    _write_hunt(hunts_dir, _make_hunt("h2", leads=[_make_lead("B")]))
    mhm.run(hunts_dir=hunts_dir, db_path=hunt_db, audit_path=audit_path, apply=True)
    audit_path.unlink()
    mhm.run(hunts_dir=hunts_dir, db_path=hunt_db, audit_path=audit_path, apply=False)
    with audit_path.open() as fh:
        rows = list(csv.DictReader(fh))
    assert rows == []


def test_dry_run_audit_surfaces_missing_hunt(
    hunts_dir: Path, hunt_db: Path, audit_path: Path
) -> None:
    _write_hunt(hunts_dir, _make_hunt("h1"))
    # Run dry-run without applying — DB is empty so audit must flag missing rows.
    mhm.run(hunts_dir=hunts_dir, db_path=hunt_db, audit_path=audit_path, apply=False)
    with audit_path.open() as fh:
        rows = list(csv.DictReader(fh))
    assert any(r["table"] == "hunts" and r["severity"] == "critical" for r in rows)


# ---------------------------------------------------------------------------
# Apply — happy path
# ---------------------------------------------------------------------------


def test_apply_writes_all_four_tables(hunts_dir: Path, hunt_db: Path, audit_path: Path) -> None:
    _write_hunt(
        hunts_dir,
        _make_hunt(
            "h1",
            leads=[_make_lead("A"), _make_lead("B")],
            stage_snapshots={
                "insight": {"stage": "insight", "company_name": "Market Overview"},
                "search": [
                    {"stage": "search", "result_count": 5, "hunt_round": 1},
                    {"stage": "search", "result_count": 7, "hunt_round": 2},
                ],
            },
            cost_summary={
                "total_cost_usd": 0.05,
                "total_tokens": 500,
                "total_llm_calls": 4,
                "rounds_completed": 2,
                "avg_cost_per_round_usd": 0.025,
                "by_agent": {
                    "insight": {
                        "prompt_tokens": 200,
                        "completion_tokens": 100,
                        "total_tokens": 300,
                        "cost_usd": 0.02,
                        "call_count": 1,
                        "models": {
                            "gpt-4o-mini": {
                                "prompt_tokens": 200,
                                "completion_tokens": 100,
                                "total_tokens": 300,
                                "cost_usd": 0.02,
                                "call_count": 1,
                            }
                        },
                    },
                    "search": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "cost_usd": 0.03,
                        "call_count": 3,
                        "models": {},
                    },
                },
            },
        ),
    )
    mhm.run(hunts_dir=hunts_dir, db_path=hunt_db, audit_path=audit_path, apply=True)
    conn = sqlite3.connect(str(hunt_db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM hunts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM hunt_leads").fetchone()[0] == 2
        # 1 insight snapshot + 2 search snapshots = 3
        assert conn.execute("SELECT COUNT(*) FROM hunt_stage_snapshots").fetchone()[0] == 3
        # 1 model-level row + 1 agent-level fallback row = 2
        assert conn.execute("SELECT COUNT(*) FROM hunt_cost_events").fetchone()[0] == 2
    finally:
        conn.close()


def test_apply_is_idempotent(hunts_dir: Path, hunt_db: Path, audit_path: Path) -> None:
    _write_hunt(hunts_dir, _make_hunt("h1", leads=[_make_lead("A")]))
    mhm.run(hunts_dir=hunts_dir, db_path=hunt_db, audit_path=audit_path, apply=True)
    counts_first = _row_counts(hunt_db)
    # Re-run with apply again — must be no-op thanks to ON CONFLICT DO NOTHING.
    mhm.run(hunts_dir=hunts_dir, db_path=hunt_db, audit_path=audit_path, apply=True)
    counts_second = _row_counts(hunt_db)
    assert counts_first == counts_second


def test_apply_uses_dedup_for_leads(hunts_dir: Path, hunt_db: Path, audit_path: Path) -> None:
    """Two leads with the same normalized company name within one hunt
    must collapse to a single row — matches ``accept_new_leads`` at
    runtime."""
    _write_hunt(
        hunts_dir,
        _make_hunt(
            "h1",
            leads=[_make_lead("  Acme Vapes  "), _make_lead("acme vapes")],
        ),
    )
    mhm.run(hunts_dir=hunts_dir, db_path=hunt_db, audit_path=audit_path, apply=True)
    conn = sqlite3.connect(str(hunt_db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM hunt_leads").fetchone()[0] == 1
    finally:
        conn.close()


def test_apply_dedup_lead_keys_match_runtime(
    hunts_dir: Path, hunt_db: Path, audit_path: Path
) -> None:
    """The lead_key written by the script must equal what
    ``lead_identity_keys`` produces at runtime — that's the contract
    that makes a follow-up Wave-3 read-switch safe."""
    _write_hunt(
        hunts_dir,
        _make_hunt("h1", leads=[_make_lead("Acme Vapes", website="acme.example")]),
    )
    mhm.run(hunts_dir=hunts_dir, db_path=hunt_db, audit_path=audit_path, apply=True)
    conn = sqlite3.connect(str(hunt_db))
    try:
        row = conn.execute("SELECT lead_key FROM hunt_leads WHERE hunt_id = ?", ("h1",)).fetchone()
    finally:
        conn.close()
    assert row[0] == "company:acme vapes"


def test_apply_hunt_leads_count_overrides_stale_json_metadata(
    hunts_dir: Path, hunt_db: Path, audit_path: Path
) -> None:
    """JSON ``leads_count`` is unreliable (drifts from the dedup pass).
    The migration must write the actual deduped row count into the
    ``hunts.leads_count`` column."""
    payload = _make_hunt(
        "h1",
        leads=[_make_lead("A"), _make_lead("B"), _make_lead("  A  ")],
    )
    payload["leads_count"] = 999  # clearly bogus
    _write_hunt(hunts_dir, payload)
    mhm.run(hunts_dir=hunts_dir, db_path=hunt_db, audit_path=audit_path, apply=True)
    conn = sqlite3.connect(str(hunt_db))
    try:
        row = conn.execute("SELECT leads_count FROM hunts WHERE hunt_id = ?", ("h1",)).fetchone()
    finally:
        conn.close()
    assert row[0] == 2  # deduped count of unique companies


# ---------------------------------------------------------------------------
# Cost event splitting
# ---------------------------------------------------------------------------


def test_cost_events_split_per_model(hunts_dir: Path, hunt_db: Path, audit_path: Path) -> None:
    _write_hunt(
        hunts_dir,
        _make_hunt(
            "h1",
            cost_summary={
                "total_cost_usd": 0.03,
                "total_tokens": 300,
                "total_llm_calls": 3,
                "rounds_completed": 1,
                "avg_cost_per_round_usd": 0.03,
                "by_agent": {
                    "insight": {
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "total_tokens": 150,
                        "cost_usd": 0.01,
                        "call_count": 1,
                        "models": {
                            "gpt-4o": {
                                "prompt_tokens": 100,
                                "completion_tokens": 50,
                                "total_tokens": 150,
                                "cost_usd": 0.01,
                                "call_count": 1,
                            },
                            "gpt-4o-mini": {
                                "prompt_tokens": 100,
                                "completion_tokens": 50,
                                "total_tokens": 150,
                                "cost_usd": 0.02,
                                "call_count": 2,
                            },
                        },
                    }
                },
            },
        ),
    )
    mhm.run(hunts_dir=hunts_dir, db_path=hunt_db, audit_path=audit_path, apply=True)
    conn = sqlite3.connect(str(hunt_db))
    try:
        rows = conn.execute(
            "SELECT model, cost_usd FROM hunt_cost_events WHERE hunt_id = ? ORDER BY model",
            ("h1",),
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("gpt-4o", 0.01), ("gpt-4o-mini", 0.02)]


def test_cost_event_provider_inferred_from_agent(
    hunts_dir: Path, hunt_db: Path, audit_path: Path
) -> None:
    _write_hunt(
        hunts_dir,
        _make_hunt(
            "h1",
            cost_summary={
                "total_cost_usd": 0.01,
                "total_tokens": 50,
                "total_llm_calls": 1,
                "rounds_completed": 1,
                "avg_cost_per_round_usd": 0.01,
                "by_agent": {
                    "search": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "cost_usd": 0.01,
                        "call_count": 1,
                        "models": {
                            "tavily": {
                                "prompt_tokens": 0,
                                "completion_tokens": 0,
                                "total_tokens": 0,
                                "cost_usd": 0.01,
                                "call_count": 1,
                            }
                        },
                    }
                },
            },
        ),
    )
    mhm.run(hunts_dir=hunts_dir, db_path=hunt_db, audit_path=audit_path, apply=True)
    conn = sqlite3.connect(str(hunt_db))
    try:
        provider = conn.execute(
            "SELECT provider FROM hunt_cost_events WHERE hunt_id = ?", ("h1",)
        ).fetchone()[0]
    finally:
        conn.close()
    assert provider == "tavily"


# ---------------------------------------------------------------------------
# Audit semantics
# ---------------------------------------------------------------------------


def test_audit_csv_has_canonical_columns(hunts_dir: Path, hunt_db: Path, audit_path: Path) -> None:
    _write_hunt(hunts_dir, _make_hunt("h1"))
    mhm.run(hunts_dir=hunts_dir, db_path=hunt_db, audit_path=audit_path, apply=False)
    with audit_path.open() as fh:
        reader = csv.reader(fh)
        header = next(reader)
    assert header == mhm.AUDIT_COLUMNS


def test_apply_audit_path_is_overwritten_each_run(
    hunts_dir: Path, hunt_db: Path, audit_path: Path
) -> None:
    audit_path.write_text("stale,leftover\n1,2\n", encoding="utf-8")
    _write_hunt(hunts_dir, _make_hunt("h1"))
    mhm.run(hunts_dir=hunts_dir, db_path=hunt_db, audit_path=audit_path, apply=True)
    with audit_path.open() as fh:
        header = next(csv.reader(fh))
    assert header == mhm.AUDIT_COLUMNS


def test_empty_hunts_dir_produces_empty_audit(
    hunts_dir: Path, hunt_db: Path, audit_path: Path
) -> None:
    # No JSON files written.
    mhm.run(hunts_dir=hunts_dir, db_path=hunt_db, audit_path=audit_path, apply=False)
    with audit_path.open() as fh:
        rows = list(csv.DictReader(fh))
    assert rows == []


def test_hunt_with_no_leads_still_writes_hunt_row(
    hunts_dir: Path, hunt_db: Path, audit_path: Path
) -> None:
    _write_hunt(hunts_dir, _make_hunt("h1"))  # no leads, no cost, no snapshots
    mhm.run(hunts_dir=hunts_dir, db_path=hunt_db, audit_path=audit_path, apply=True)
    conn = sqlite3.connect(str(hunt_db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM hunts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM hunt_leads").fetchone()[0] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in (
                "hunts",
                "hunt_leads",
                "hunt_stage_snapshots",
                "hunt_cost_events",
            )
        }
    finally:
        conn.close()
