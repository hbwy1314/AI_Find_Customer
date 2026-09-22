"""P1.2 — Backfill hunt JSON files into the SQLite ``hunt_db``.

Wave 2 of the P1 Hunt JSON → SQLite migration.  The script is fully
independent of the running app: it opens ``settings.hunt_db_path``
directly, runs the P0 migration engine to create the 002-005 schema if
missing, then walks ``hunts_dir/*.json`` and writes one set of rows per
hunt into the SQLite tables.

Modes
=====

* ``--dry-run`` (default) — *validates* that every JSON can be parsed and
  that the row we *would* write matches the row currently in SQLite.
  No writes.  Differences are appended to ``scripts/data/migrate_hunts_audit.csv``.
* ``--apply`` — performs the writes (``INSERT OR IGNORE`` so re-runs are
  idempotent) AND writes the audit CSV.  ``INSERT OR IGNORE`` semantics
  match the plan §3.2 rollback note: re-running on top of an already-
  backfilled DB is safe.

What gets written
=================

Four tables per the P1 plan:

* ``hunts`` — top-level hunt metadata (002).
* ``hunt_leads`` — one row per accepted lead (003).  Lead identity uses
  ``agents.lead_identity.lead_identity_keys`` so the keys are
  bit-for-bit identical to what ``accept_new_leads`` would write at
  runtime.
* ``hunt_stage_snapshots`` — one row per stage snapshot in
  ``stage_snapshots`` (004).  ``captured_at`` falls back to
  ``hunt.created_at`` because the JSON payload never records per-
  snapshot timestamps.
* ``hunt_cost_events`` — one row per ``(agent_name, model_name)``
  aggregate in ``cost_summary.by_agent`` (005).  We deliberately do NOT
  inflate call_count into N rows; one aggregate row keeps the JSON
  information loss explicit.  ``provider`` is inferred from the agent
  name (LLM agents → ``openai``; search/serper agents → ``tavily``/``serper``).

CLI
===

.. code-block:: bash

    # default dry-run + audit CSV
    uv run python backend/scripts/migrate_hunts_to_sqlite.py

    # actually write
    uv run python backend/scripts/migrate_hunts_to_sqlite.py --apply

    # point at a staging directory
    uv run python backend/scripts/migrate_hunts_to_sqlite.py \\
        --hunts-dir /tmp/hunts-staging --db /tmp/hunts.db --apply
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import uuid
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Make ``backend/`` importable when the script is invoked directly
# (``uv run python scripts/migrate_hunts_to_sqlite.py`` from repo root).
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR.parent))

from agents.lead_identity import dedupe_leads, lead_identity_keys  # noqa: E402
from api.hunt_store import current_leads  # noqa: E402
from config.settings import get_settings  # noqa: E402
from migrations.runner import apply_pending_migrations  # noqa: E402

AUDIT_COLUMNS = [
    "hunt_id",
    "table",
    "field",
    "json_value",
    "sqlite_value",
    "severity",
]
AUDIT_PATH_DEFAULT = _SCRIPT_DIR / "data" / "migrate_hunts_audit.csv"

# Agent → provider mapping for hunt_cost_events backfill. Conservative:
# only assign providers we have run logs for; everything else stays as
# the literal agent name so the audit row surfaces the unknown.
_AGENT_PROVIDER_HINTS = {
    "parse_description": "openai",
    "insight": "openai",
    "keyword_gen": "openai",
    "lead_extract": "openai",
    "evaluate": "openai",
    "search": "tavily",
    "serper_search": "serper",
    "google_maps": "google_maps",
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _uuid4_hex() -> str:
    return uuid.uuid4().hex


def _encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _iter_hunt_files(hunts_dir: Path) -> list[Path]:
    """Return hunt JSON files in the directory, excluding tombstoned/stale
    artefacts — mirrors :func:`api.hunt_store._dedup_hunt_paths`."""
    paths: list[Path] = []
    for p in sorted(hunts_dir.glob("*.json")):
        name = p.name
        if name.startswith("."):
            # tombstone / hidden file
            continue
        if name.endswith(".deduped.json") or name.endswith(".merged.json"):
            continue
        paths.append(p)
    return paths


def _row_from_hunt(hunt: dict[str, Any]) -> dict[str, Any]:
    """Project a JSON hunt dict to the ``hunts`` row columns (002).

    ``leads_count`` is *derived* from the deduped lead list — not copied
    from the JSON's ``leads_count`` field — because the JSON metadata is
    known to drift (multiple ``existing_leads`` rows can collapse into
    one after the Wave-1 ``accept_new_leads`` dedup pass).  Trusting the
    JSON value here would mean the column disagrees with the row count
    of ``hunt_leads`` and every subsequent audit run flags a phantom
    diff.  The same logic is what the SQLiteBackend ``accept_new_leads``
    performs at runtime; the migration mirrors it.
    """
    result_value = hunt.get("result", "") or ""
    result_blob = _encode_json(result_value) if not isinstance(result_value, str) else result_value
    deduped_lead_count = len(dedupe_leads(current_leads(hunt)))
    return {
        "hunt_id": str(hunt.get("hunt_id", "")),
        "status": str(hunt.get("status", "pending")),
        "current_stage": str(hunt.get("current_stage", "")),
        "hunt_round": int(hunt.get("hunt_round", 0) or 0),
        "leads_count": deduped_lead_count,
        "email_sequences_count": int(hunt.get("email_sequences_count", 0) or 0),
        "result": result_blob,
        "error": str(hunt.get("error", "")),
        "website_url": str(hunt.get("website_url", "")),
        "product_keywords": _encode_json(hunt.get("product_keywords", [])),
        "target_customer_profile": _encode_json(hunt.get("target_customer_profile", {})),
        "target_regions": _encode_json(hunt.get("target_regions", [])),
        "email_template_examples": _encode_json(hunt.get("email_template_examples", [])),
        "email_template_notes": str(hunt.get("email_template_notes", "")),
        "created_at": str(hunt.get("created_at", _utc_now())),
        "completed_at": str(hunt.get("completed_at", "")),
    }


def _iter_lead_rows(hunt_id: str, hunt: dict[str, Any], now: str) -> Iterable[dict[str, Any]]:
    """Yield ``hunt_leads`` rows (003) for every lead in the JSON hunt.

    The dedup pass here is *required* — it matches what
    :func:`api.hunt_store.SQLiteBackend.accept_new_leads` does at runtime
    (see ``agents/lead_identity.dedupe_leads``).  Without it, two leads
    sharing a normalized company name within the same hunt would hit the
    ``UNIQUE(hunt_id, lead_key)`` constraint and one would be dropped —
    exactly the Wave-2 audit goal of "row counts match the runtime".
    """
    for lead in dedupe_leads(current_leads(hunt)):
        keys = lead_identity_keys(lead)
        if keys:
            primary_key = sorted(keys)[0]
        else:
            primary_key = f"anon-{_uuid4_hex()[:12]}"
        yield {
            "lead_id": _uuid4_hex(),
            "hunt_id": hunt_id,
            "lead_key": primary_key,
            "identity_json": _encode_json(lead),
            "status": "new",
            "round_introduced": 0,
            "created_at": now,
        }


def _iter_stage_snapshot_rows(hunt_id: str, hunt: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield ``hunt_stage_snapshots`` rows (004).

    The JSON ``stage_snapshots`` is a dict keyed by stage name; each value
    is either a single snapshot dict (insight/keyword_gen) or a list of
    per-round snapshots (search/lead_extract/evaluate).  We expand both
    into one row per snapshot.  ``captured_at`` falls back to
    ``hunt.created_at`` — JSON has no per-snapshot timestamp.
    """
    snapshots = hunt.get("stage_snapshots") or {}
    if not isinstance(snapshots, dict):
        return
    captured_at = str(hunt.get("created_at") or _utc_now())
    for stage_name, payload in snapshots.items():
        if isinstance(payload, list):
            for _idx, item in enumerate(payload):
                if not isinstance(item, dict):
                    continue
                yield {
                    "snapshot_id": _uuid4_hex(),
                    "hunt_id": hunt_id,
                    "stage": str(item.get("stage", stage_name)),
                    "state_json": _encode_json(item),
                    "captured_at": captured_at,
                }
        elif isinstance(payload, dict):
            yield {
                "snapshot_id": _uuid4_hex(),
                "hunt_id": hunt_id,
                "stage": str(payload.get("stage", stage_name)),
                "state_json": _encode_json(payload),
                "captured_at": captured_at,
            }


def _iter_cost_event_rows(hunt_id: str, hunt: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield ``hunt_cost_events`` rows (005) from the aggregated
    ``cost_summary.by_agent`` dict.  One row per ``(agent, model)`` pair
    to preserve fidelity at the model grain (per-call rows are not
    recoverable from the JSON payload)."""
    cost_summary = hunt.get("cost_summary") or {}
    by_agent = cost_summary.get("by_agent") or {}
    if not isinstance(by_agent, dict):
        return
    captured_at = str(hunt.get("completed_at") or hunt.get("created_at") or _utc_now())
    for agent_name, payload in by_agent.items():
        if not isinstance(payload, dict):
            continue
        models = payload.get("models") or {}
        # Per-model rows are the atomic grain available in the JSON.
        if isinstance(models, dict) and models:
            for model_name, model_payload in models.items():
                if not isinstance(model_payload, dict):
                    continue
                yield {
                    "event_id": _uuid4_hex(),
                    "hunt_id": hunt_id,
                    "provider": _AGENT_PROVIDER_HINTS.get(agent_name, agent_name),
                    "model": str(model_name),
                    "input_tokens": int(model_payload.get("prompt_tokens", 0) or 0),
                    "output_tokens": int(model_payload.get("completion_tokens", 0) or 0),
                    "cost_usd": float(model_payload.get("cost_usd", 0.0) or 0.0),
                    "captured_at": captured_at,
                }
        else:
            # Aggregate row without per-model breakdown — fall back to
            # the agent-level totals.
            yield {
                "event_id": _uuid4_hex(),
                "hunt_id": hunt_id,
                "provider": _AGENT_PROVIDER_HINTS.get(agent_name, agent_name),
                "model": "",
                "input_tokens": int(payload.get("prompt_tokens", 0) or 0),
                "output_tokens": int(payload.get("completion_tokens", 0) or 0),
                "cost_usd": float(payload.get("cost_usd", 0.0) or 0.0),
                "captured_at": captured_at,
            }


# ----------------------------------------------------------------------
# SQL helpers
# ----------------------------------------------------------------------


def _ensure_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row  # allow row["column"] lookups
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    apply_pending_migrations(conn)
    conn.commit()
    return conn


def _fetch_hunt_row(conn: sqlite3.Connection, hunt_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM hunts WHERE hunt_id = ?", (hunt_id,)).fetchone()
    if row is None:
        return None
    cols = [c[1] for c in conn.execute("PRAGMA table_info(hunts)").fetchall()]
    return dict(zip(cols, row, strict=True))


def _fetch_lead_rows(conn: sqlite3.Connection, hunt_id: str) -> list[dict[str, Any]]:
    cols = [c[1] for c in conn.execute("PRAGMA table_info(hunt_leads)").fetchall()]
    rows = conn.execute(
        "SELECT * FROM hunt_leads WHERE hunt_id = ? ORDER BY lead_key", (hunt_id,)
    ).fetchall()
    return [dict(zip(cols, r, strict=True)) for r in rows]


def _fetch_snapshot_rows(conn: sqlite3.Connection, hunt_id: str) -> list[dict[str, Any]]:
    cols = [c[1] for c in conn.execute("PRAGMA table_info(hunt_stage_snapshots)").fetchall()]
    rows = conn.execute(
        "SELECT * FROM hunt_stage_snapshots WHERE hunt_id = ? ORDER BY captured_at",
        (hunt_id,),
    ).fetchall()
    return [dict(zip(cols, r, strict=True)) for r in rows]


def _fetch_cost_rows(conn: sqlite3.Connection, hunt_id: str) -> list[dict[str, Any]]:
    cols = [c[1] for c in conn.execute("PRAGMA table_info(hunt_cost_events)").fetchall()]
    rows = conn.execute(
        "SELECT * FROM hunt_cost_events WHERE hunt_id = ? ORDER BY captured_at",
        (hunt_id,),
    ).fetchall()
    return [dict(zip(cols, r, strict=True)) for r in rows]


# ----------------------------------------------------------------------
# Audit comparators
# ----------------------------------------------------------------------


def _compare_hunt(
    hunt_id: str, json_row: dict[str, Any], db_row: dict[str, Any] | None
) -> list[dict[str, str]]:
    if db_row is None:
        return [
            {
                "hunt_id": hunt_id,
                "table": "hunts",
                "field": "*",
                "json_value": "<row>",
                "sqlite_value": "<missing>",
                "severity": "critical",
            }
        ]
    diffs: list[dict[str, str]] = []
    for field, json_val in json_row.items():
        db_val = db_row.get(field, "")
        if json_val == db_val:
            continue
        diffs.append(
            {
                "hunt_id": hunt_id,
                "table": "hunts",
                "field": field,
                "json_value": str(json_val)[:240],
                "sqlite_value": str(db_val)[:240],
                "severity": "warning",
            }
        )
    return diffs


def _compare_lead_counts(hunt_id: str, json_count: int, db_count: int) -> list[dict[str, str]]:
    if json_count == db_count:
        return []
    return [
        {
            "hunt_id": hunt_id,
            "table": "hunt_leads",
            "field": "count",
            "json_value": str(json_count),
            "sqlite_value": str(db_count),
            "severity": "warning",
        }
    ]


def _compare_table_counts(
    hunt_id: str,
    table: str,
    json_count: int,
    db_count: int,
    severity: str = "warning",
) -> list[dict[str, str]]:
    if json_count == db_count:
        return []
    return [
        {
            "hunt_id": hunt_id,
            "table": table,
            "field": "count",
            "json_value": str(json_count),
            "sqlite_value": str(db_count),
            "severity": severity,
        }
    ]


# ----------------------------------------------------------------------
# Main migration pipeline
# ----------------------------------------------------------------------


def _apply_hunt_rows(
    conn: sqlite3.Connection,
    hunt_id: str,
    hunt: dict[str, Any],
    audit_rows: list[dict[str, str]],
) -> dict[str, int]:
    now = _utc_now()
    hunt_row = _row_from_hunt(hunt)
    conn.execute(
        """
        INSERT INTO hunts (
            hunt_id, status, current_stage, hunt_round, leads_count,
            email_sequences_count, result, error, website_url,
            product_keywords, target_customer_profile, target_regions,
            email_template_examples, email_template_notes,
            created_at, completed_at
        ) VALUES (
            :hunt_id, :status, :current_stage, :hunt_round, :leads_count,
            :email_sequences_count, :result, :error, :website_url,
            :product_keywords, :target_customer_profile, :target_regions,
            :email_template_examples, :email_template_notes,
            :created_at, :completed_at
        )
        ON CONFLICT(hunt_id) DO UPDATE SET
            status=excluded.status,
            current_stage=excluded.current_stage,
            hunt_round=excluded.hunt_round,
            leads_count=excluded.leads_count,
            email_sequences_count=excluded.email_sequences_count,
            result=excluded.result,
            error=excluded.error,
            website_url=excluded.website_url,
            product_keywords=excluded.product_keywords,
            target_customer_profile=excluded.target_customer_profile,
            target_regions=excluded.target_regions,
            email_template_examples=excluded.email_template_examples,
            email_template_notes=excluded.email_template_notes,
            completed_at=excluded.completed_at,
            updated_at=datetime('now')
        """,
        hunt_row,
    )

    # Leads
    json_lead_rows = list(_iter_lead_rows(hunt_id, hunt, now))
    for r in json_lead_rows:
        conn.execute(
            """
            INSERT INTO hunt_leads (
                lead_id, hunt_id, lead_key, identity_json, status,
                round_introduced, created_at
            ) VALUES (
                :lead_id, :hunt_id, :lead_key, :identity_json, :status,
                :round_introduced, :created_at
            )
            ON CONFLICT(hunt_id, lead_key) DO NOTHING
            """,
            r,
        )

    # Stage snapshots
    json_snapshot_rows = list(_iter_stage_snapshot_rows(hunt_id, hunt))
    for r in json_snapshot_rows:
        conn.execute(
            """
            INSERT INTO hunt_stage_snapshots (
                snapshot_id, hunt_id, stage, state_json, captured_at
            ) VALUES (
                :snapshot_id, :hunt_id, :stage, :state_json, :captured_at
            )
            ON CONFLICT(snapshot_id) DO NOTHING
            """,
            r,
        )

    # Cost events
    json_cost_rows = list(_iter_cost_event_rows(hunt_id, hunt))
    for r in json_cost_rows:
        conn.execute(
            """
            INSERT INTO hunt_cost_events (
                event_id, hunt_id, provider, model,
                input_tokens, output_tokens, cost_usd, captured_at
            ) VALUES (
                :event_id, :hunt_id, :provider, :model,
                :input_tokens, :output_tokens, :cost_usd, :captured_at
            )
            ON CONFLICT(event_id) DO NOTHING
            """,
            r,
        )

    # Always refresh leads_count to reflect the latest row count in SQLite
    # (we may have inserted previously on a different run).
    db_lead_count = conn.execute(
        "SELECT COUNT(*) AS c FROM hunt_leads WHERE hunt_id = ?", (hunt_id,)
    ).fetchone()["c"]
    conn.execute(
        "UPDATE hunts SET leads_count = ?, updated_at = datetime('now') WHERE hunt_id = ?",
        (db_lead_count, hunt_id),
    )
    conn.commit()

    # Audit: compare what we *would* have written vs what is *now* in the DB.
    db_hunt = _fetch_hunt_row(conn, hunt_id)
    audit_rows.extend(_compare_hunt(hunt_id, hunt_row, db_hunt))
    db_leads = _fetch_lead_rows(conn, hunt_id)
    audit_rows.extend(_compare_lead_counts(hunt_id, len(json_lead_rows), len(db_leads)))
    db_snaps = _fetch_snapshot_rows(conn, hunt_id)
    audit_rows.extend(
        _compare_table_counts(
            hunt_id, "hunt_stage_snapshots", len(json_snapshot_rows), len(db_snaps)
        )
    )
    db_costs = _fetch_cost_rows(conn, hunt_id)
    audit_rows.extend(
        _compare_table_counts(hunt_id, "hunt_cost_events", len(json_cost_rows), len(db_costs))
    )
    return {
        "leads": len(json_lead_rows),
        "snapshots": len(json_snapshot_rows),
        "cost_events": len(json_cost_rows),
    }


def _audit_only(conn: sqlite3.Connection, hunt_id: str, hunt: dict[str, Any]) -> dict[str, int]:
    """Compare JSON vs SQLite without writing."""
    audit_rows: list[dict[str, str]] = []
    hunt_row = _row_from_hunt(hunt)
    db_hunt = _fetch_hunt_row(conn, hunt_id)
    audit_rows.extend(_compare_hunt(hunt_id, hunt_row, db_hunt))

    json_leads = list(dedupe_leads(current_leads(hunt)))
    db_leads = _fetch_lead_rows(conn, hunt_id)
    audit_rows.extend(_compare_lead_counts(hunt_id, len(json_leads), len(db_leads)))

    json_snaps = list(_iter_stage_snapshot_rows(hunt_id, hunt))
    db_snaps = _fetch_snapshot_rows(conn, hunt_id)
    audit_rows.extend(
        _compare_table_counts(hunt_id, "hunt_stage_snapshots", len(json_snaps), len(db_snaps))
    )

    json_costs = list(_iter_cost_event_rows(hunt_id, hunt))
    db_costs = _fetch_cost_rows(conn, hunt_id)
    audit_rows.extend(
        _compare_table_counts(hunt_id, "hunt_cost_events", len(json_costs), len(db_costs))
    )
    return {
        "leads": len(json_leads),
        "snapshots": len(json_snaps),
        "cost_events": len(json_costs),
        "_audit_buffer": audit_rows,
    }


def _write_audit_csv(audit_path: Path, rows: list[dict[str, str]]) -> None:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=AUDIT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def run(
    *,
    hunts_dir: Path,
    db_path: Path,
    audit_path: Path,
    apply: bool,
) -> int:
    conn = _ensure_db(db_path)
    paths = _iter_hunt_files(hunts_dir)
    audit_rows: list[dict[str, str]] = []
    totals: Counter[str] = Counter()
    for path in paths:
        try:
            hunt = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            audit_rows.append(
                {
                    "hunt_id": path.stem,
                    "table": "<parse>",
                    "field": "json",
                    "json_value": f"<unparseable: {exc}>",
                    "sqlite_value": "",
                    "severity": "critical",
                }
            )
            continue
        hunt_id = str(hunt.get("hunt_id", path.stem))
        if apply:
            counts = _apply_hunt_rows(conn, hunt_id, hunt, audit_rows)
            totals["hunts"] += 1
            for k, v in counts.items():
                totals[k] += v
        else:
            counts = _audit_only(conn, hunt_id, hunt)
            audit_rows.extend(counts.pop("_audit_buffer"))
            totals["hunts"] += 1
            for k, v in counts.items():
                totals[k] += v
    _write_audit_csv(audit_path, audit_rows)
    print(
        json.dumps(
            {
                "mode": "apply" if apply else "dry-run",
                "hunts_dir": str(hunts_dir),
                "db_path": str(db_path),
                "audit_csv": str(audit_path),
                "totals": dict(totals),
                "audit_rows": len(audit_rows),
            },
            indent=2,
        )
    )
    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform writes (default: dry-run)",
    )
    parser.add_argument(
        "--hunts-dir",
        type=Path,
        default=Path(settings.hunts_dir),
        help="Directory of hunt JSON files",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(settings.hunt_db_path),
        help="SQLite database file",
    )
    parser.add_argument(
        "--audit-path",
        type=Path,
        default=AUDIT_PATH_DEFAULT,
        help="Output CSV for audit differences",
    )
    args = parser.parse_args(argv)
    return run(
        hunts_dir=args.hunts_dir,
        db_path=args.db,
        audit_path=args.audit_path,
        apply=args.apply,
    )


if __name__ == "__main__":
    raise SystemExit(main())
