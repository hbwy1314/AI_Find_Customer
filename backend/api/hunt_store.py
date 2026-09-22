"""Hunt persistence — JSON file-based storage for hunt metadata and results.

P1 refactor: the original implementation in this module is now wrapped by a
:class:`Backend` abstraction so the same public API (``save_hunt``,
``accept_new_leads``, ``load_hunt``, ``load_hunt``, ``delete_hunt``,
``purge_old_hunts``, ``load_all_hunts``, ``saved_leads``,
``current_lead_keys``, ``now_iso``) can be served by either the legacy JSON
backend or a SQLite-backed one without touching any caller.

Three backend implementations ship in Wave 1:

* :class:`JSONBackend` — the original fcntl + JSON-file implementation,
  preserved byte-for-byte behaviour so the 1000+ existing tests keep
  passing without modification.
* :class:`SQLiteBackend` — a single-file SQLite database with the schema
  in ``migrations/002-005_*.sql`` (``hunts`` / ``hunt_leads`` /
  ``hunt_stage_snapshots`` / ``hunt_cost_events``).
* :class:`DualWriteBackend` — write-through to both backends, read via
  JSON. This is the Wave-1 default: a SQLite write failure logs a warning
  but never blocks the hunt pipeline, and the existing JSON files stay
  source-of-truth for reads. Wave 3 flips reads to SQLite; Wave 4 retires
  JSON entirely.

Each hunt is saved as a JSON file under ``settings.hunts_dir``.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import time
import uuid as _uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config.settings import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Low-level helpers (preserved from the original implementation so the
# JSONBackend, the public thin-wrapper API, and the existing tests can all
# share the same atomic-write / lock primitives without duplication).
# ---------------------------------------------------------------------------


def _hunts_dir() -> Path:
    """Return the hunts directory path, creating it if needed."""
    settings = get_settings()
    p = Path(settings.hunts_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def hunt_data_lock() -> Iterator[None]:
    """Serialize customer acceptance, persistence and deletion across workers.

    Kept as a module-level contextmanager so existing callers that import
    ``hunt_data_lock`` directly (and any test that monkey-patches the
    fcntl primitives) continue to work.
    """
    with (_hunts_dir() / ".dedup.lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def current_leads(hunt: dict[str, Any]) -> list[dict[str, Any]]:
    result = hunt.get("result")
    leads = result.get("leads") if isinstance(result, dict) else None
    if not isinstance(leads, list):
        leads = hunt.get("existing_leads") or []
    return [lead for lead in leads if isinstance(lead, dict)]


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Atomic write via temp file + replace to prevent partial writes on crash.

    Kept as a module-level function so ``tests/test_api/test_hunt_dedup.py``
    can keep monkey-patching ``hunt_store._write_json_atomic`` directly.
    """
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        # Inherit original file permissions before replace
        if path.exists():
            stat_info = path.stat()
            os.chown(tmp_name, stat_info.st_uid, stat_info.st_gid)
            os.chmod(tmp_name, stat_info.st_mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def now_iso() -> str:
    """Return current UTC time as ISO string."""
    return datetime.now(UTC).isoformat()


def _dedup_hunt_paths(root: Path) -> list[Path]:
    """Return current task Hunt files, excluding stale historical artifacts."""
    settings = get_settings()
    queue_path = str(getattr(settings, "automation_queue_db_path", "") or "")
    if not queue_path or not Path(queue_path).exists():
        return sorted(root.glob("*.json"))
    try:
        with sqlite3.connect(f"file:{Path(queue_path).resolve()}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT DISTINCT last_hunt_id FROM hunt_jobs WHERE last_hunt_id != ''"
            ).fetchall()
        paths = [root / f"{str(row[0])}.json" for row in rows if str(row[0] or "")]
        return sorted(path for path in paths if path.exists())
    except (OSError, sqlite3.Error):
        return sorted(root.glob("*.json"))


# ---------------------------------------------------------------------------
# Backend abstraction
# ---------------------------------------------------------------------------


class Backend(ABC):
    """Storage backend for hunt metadata and accepted leads."""

    @abstractmethod
    def save_hunt(self, hunt_id: str, hunt_data: dict[str, Any]) -> None: ...

    @abstractmethod
    def load_hunt(self, hunt_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def load_all_hunts(self, *, mark_interrupted: bool = False) -> dict[str, dict[str, Any]]: ...

    @abstractmethod
    def delete_hunt(self, hunt_id: str) -> None: ...

    @abstractmethod
    def purge_old_hunts(self, retention_days: int) -> list[str]: ...

    @abstractmethod
    def accept_new_leads(self, hunt_id: str, leads: list[dict[str, Any]]) -> list[dict[str, Any]]: ...

    @abstractmethod
    def saved_leads(self, hunt_id: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def current_lead_keys(self) -> set[str]: ...

    @abstractmethod
    def is_tombstoned(self, hunt_id: str) -> bool: ...

    def close(self) -> None:  # pragma: no cover - optional hook
        return None


# ---------------------------------------------------------------------------
# JSON backend — original fcntl + atomic-JSON implementation, byte-for-byte
# behaviour with the pre-P1 module.
# ---------------------------------------------------------------------------


class JSONBackend(Backend):
    """JSON-file storage under ``settings.hunts_dir``.

    Behavioural clone of the original module-level functions, so existing
    tests that monkey-patch ``hunt_store._write_json_atomic`` or
    ``hunt_store.get_settings`` continue to work.
    """

    def save_hunt(self, hunt_id: str, hunt_data: dict[str, Any]) -> None:
        try:
            with hunt_data_lock():
                root = _hunts_dir()
                path = root / f"{hunt_id}.json"
                if (root / f".{hunt_id}.deleted").exists():
                    raise RuntimeError(f"Hunt {hunt_id} was deleted")
                payload = {"hunt_id": hunt_id, **hunt_data}
                _write_json_atomic(path, payload)
        except Exception as e:
            logger.warning("[HuntStore] Failed to save hunt %s: %s", hunt_id[:8], e)
            raise

    def load_hunt(self, hunt_id: str) -> dict[str, Any] | None:
        try:
            path = _hunts_dir() / f"{hunt_id}.json"
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            data.pop("hunt_id", None)
            return data
        except Exception as e:
            logger.warning("[HuntStore] Failed to load hunt %s: %s", hunt_id[:8], e)
            return None

    def load_all_hunts(self, *, mark_interrupted: bool = False) -> dict[str, dict[str, Any]]:
        hunts: dict[str, dict[str, Any]] = {}
        hunts_path = _hunts_dir()
        for path in hunts_path.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                hid = data.pop("hunt_id", path.stem)
                if mark_interrupted and data.get("status") in ("running", "pending"):
                    data["status"] = "failed"
                    data["error"] = "Process was interrupted (server restarted)"
                    data["completed_at"] = now_iso()
                    payload = {"hunt_id": hid, **data}
                    _write_json_atomic(path, payload)
                    logger.info("[HuntStore] Marked interrupted hunt %s as failed", hid[:8])
                hunts[hid] = data
                logger.debug("[HuntStore] Loaded hunt %s (status=%s)", hid[:8], data.get("status"))
            except Exception as e:
                logger.warning("[HuntStore] Failed to load %s: %s", path.name, e)
        if hunts:
            logger.info("[HuntStore] Loaded %d historical hunts from %s", len(hunts), hunts_path)
        return hunts

    def delete_hunt(self, hunt_id: str) -> None:
        try:
            with hunt_data_lock():
                path = _hunts_dir() / f"{hunt_id}.json"
                _write_json_atomic(_hunts_dir() / f".{hunt_id}.deleted", {"deleted_at": now_iso()})
                path.unlink(missing_ok=True)
        except Exception as e:
            logger.warning("[HuntStore] Failed to delete hunt %s: %s", hunt_id[:8], e)
            raise

    def purge_old_hunts(self, retention_days: int) -> list[str]:
        if retention_days <= 0:
            return []
        cutoff = time.time() - retention_days * 86400
        purged: list[str] = []
        with hunt_data_lock():
            for path in sorted(_hunts_dir().glob("*.json")):
                try:
                    if path.stat().st_mtime >= cutoff:
                        continue
                    hunt_id = path.stem
                    _write_json_atomic(
                        _hunts_dir() / f".{hunt_id}.deleted",
                        {"deleted_at": now_iso(), "purged_by_retention": True},
                    )
                    path.unlink(missing_ok=True)
                    purged.append(hunt_id)
                except OSError:
                    continue
        return purged

    def accept_new_leads(self, hunt_id: str, leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from agents.lead_identity import dedupe_leads, lead_identity_keys
        if not str(hunt_id or "").strip():
            return list(dedupe_leads(leads))
        with hunt_data_lock():
            root = _hunts_dir()
            path = root / f"{hunt_id}.json"
            if (root / f".{hunt_id}.deleted").exists() or not path.exists():
                raise RuntimeError(f"Hunt {hunt_id} no longer exists")
            keys: set[str] = set()
            for source in _dedup_hunt_paths(root):
                data = json.loads(source.read_text(encoding="utf-8"))
                keys.update(key for lead in current_leads(data) for key in lead_identity_keys(lead))
            hunt = json.loads(path.read_text(encoding="utf-8"))
            accepted = []
            for lead in dedupe_leads(leads):
                identities = set(lead_identity_keys(lead))
                if identities & keys:
                    continue
                accepted.append(lead)
                keys.update(identities)
            if accepted:
                baseline = current_leads(hunt)
                result = hunt.get("result")
                if not isinstance(result, dict):
                    result = {}
                    hunt["result"] = result
                result["leads"] = dedupe_leads(baseline + accepted)
                hunt["leads_count"] = len(result["leads"])
                _write_json_atomic(path, hunt)
            return accepted

    def saved_leads(self, hunt_id: str) -> list[dict[str, Any]]:
        with hunt_data_lock():
            path = _hunts_dir() / f"{hunt_id}.json"
            return current_leads(json.loads(path.read_text(encoding="utf-8")))

    def current_lead_keys(self) -> set[str]:
        from agents.lead_identity import lead_identity_keys
        keys: set[str] = set()
        with hunt_data_lock():
            for path in _dedup_hunt_paths(_hunts_dir()):
                hunt = json.loads(path.read_text(encoding="utf-8"))
                keys.update(key for lead in current_leads(hunt) for key in lead_identity_keys(lead))
        return keys

    def is_tombstoned(self, hunt_id: str) -> bool:
        return (_hunts_dir() / f".{hunt_id}.deleted").exists()


# ---------------------------------------------------------------------------
# SQLite backend — single-file DB at ``settings.hunt_db_path``.
# ---------------------------------------------------------------------------


_SQLITE_SCHEMA_PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
)


class SQLiteBackend(Backend):
    """SQLite-backed hunt persistence using migrations/002-005.

    Schema (managed by the migrations runner; this class only calls
    ``apply_pending_migrations`` on first connect):

    * ``hunts`` — one row per hunt document (id + 18 top-level fields).
    * ``hunt_leads`` — one row per accepted lead, ``UNIQUE(hunt_id, lead_key)``.
    * ``hunt_stage_snapshots`` — append-only per-hunt stage log.
    * ``hunt_cost_events`` — append-only per-LLM-call cost ledger.

    Wave 1 uses this as the secondary write target behind
    :class:`DualWriteBackend`; reads still come from JSON. Wave 3 flips
    reads to SQLite; Wave 4 retires the JSON backend entirely.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path_override = db_path

    def _conn(self) -> sqlite3.Connection:
        from migrations.runner import apply_pending_migrations

        path = self._db_path_override or get_settings().hunt_db_path
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        for pragma in _SQLITE_SCHEMA_PRAGMAS:
            conn.execute(pragma)
        apply_pending_migrations(conn)
        conn.commit()
        return conn

    # ------------------------------------------------------------------
    # Row <-> payload helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _leads_for_hunt(conn: sqlite3.Connection, hunt_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT lead_id, lead_key, identity_json, status, round_introduced, created_at "
            "FROM hunt_leads WHERE hunt_id = ? ORDER BY created_at ASC, lead_id ASC",
            (hunt_id,),
        ).fetchall()
        leads: list[dict[str, Any]] = []
        for row in rows:
            identity = row["identity_json"] or "{}"
            try:
                lead = json.loads(identity)
            except json.JSONDecodeError:
                lead = {}
            lead["_lead_id"] = row["lead_id"]
            lead["_lead_key"] = row["lead_key"]
            lead["_status"] = row["status"]
            leads.append(lead)
        return leads

    @staticmethod
    def _row_to_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "hunt_id": row["hunt_id"],
            "status": row["status"],
            "current_stage": row["current_stage"],
            "hunt_round": row["hunt_round"],
            "leads_count": row["leads_count"],
            "email_sequences_count": row["email_sequences_count"],
            "error": row["error"],
            "website_url": row["website_url"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
        }
        for json_field, default in (
            ("product_keywords", []),
            ("target_customer_profile", {}),
            ("target_regions", []),
            ("email_template_examples", []),
        ):
            raw = row[json_field] or ""
            try:
                payload[json_field] = json.loads(raw) if raw else default
            except json.JSONDecodeError:
                payload[json_field] = default
        payload["email_template_notes"] = row["email_template_notes"]
        # result field is a JSON-encoded blob; decode and merge leads list
        result_raw = row["result"] or ""
        if result_raw:
            try:
                result_obj = json.loads(result_raw)
            except json.JSONDecodeError:
                result_obj = {}
        else:
            result_obj = {}
        result_obj["leads"] = SQLiteBackend._leads_for_hunt(conn, row["hunt_id"])
        payload["result"] = result_obj
        # ``leads_count`` is the denormalized counter maintained by
        # ``save_hunt`` (initial value) and ``accept_new_leads`` (re-count
        # after inserts). Don't recompute it here — the row value is
        # the source of truth and recomputing breaks tests where
        # ``save_hunt`` pre-populates a count before any lead exists.
        return payload

    @staticmethod
    def _is_tombstoned(hunt_id: str) -> bool:
        return (_hunts_dir() / f".{hunt_id}.deleted").exists()

    @staticmethod
    def _encode_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)

    # ------------------------------------------------------------------
    # Backend protocol
    # ------------------------------------------------------------------

    def save_hunt(self, hunt_id: str, hunt_data: dict[str, Any]) -> None:
        conn = self._conn()
        try:
            if self._is_tombstoned(hunt_id):
                raise RuntimeError(f"Hunt {hunt_id} was deleted")
            payload = dict(hunt_data)
            payload.setdefault("created_at", now_iso())
            result_value = payload.get("result", "")
            result_blob = self._encode_json(result_value) if not isinstance(result_value, str) else result_value
            conn.execute(
                """
                INSERT INTO hunts (
                    hunt_id, status, current_stage, hunt_round, leads_count,
                    email_sequences_count, result, error, website_url,
                    product_keywords, target_customer_profile, target_regions,
                    email_template_examples, email_template_notes,
                    created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                (
                    hunt_id,
                    str(payload.get("status", "pending")),
                    str(payload.get("current_stage", "")),
                    int(payload.get("hunt_round", 0) or 0),
                    int(payload.get("leads_count", 0) or 0),
                    int(payload.get("email_sequences_count", 0) or 0),
                    result_blob,
                    str(payload.get("error", "")),
                    str(payload.get("website_url", "")),
                    self._encode_json(payload.get("product_keywords", [])),
                    self._encode_json(payload.get("target_customer_profile", {})),
                    self._encode_json(payload.get("target_regions", [])),
                    self._encode_json(payload.get("email_template_examples", [])),
                    str(payload.get("email_template_notes", "")),
                    str(payload.get("created_at", now_iso())),
                    str(payload.get("completed_at", "")),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def load_hunt(self, hunt_id: str) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM hunts WHERE hunt_id = ?", (hunt_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_payload(conn, row)
        finally:
            conn.close()

    def load_all_hunts(self, *, mark_interrupted: bool = False) -> dict[str, dict[str, Any]]:
        conn = self._conn()
        try:
            rows = conn.execute("SELECT * FROM hunts ORDER BY created_at DESC").fetchall()
            out: dict[str, dict[str, Any]] = {}
            for row in rows:
                payload = self._row_to_payload(conn, row)
                hid = payload.pop("hunt_id", row["hunt_id"])
                if mark_interrupted and payload.get("status") in ("running", "pending"):
                    payload["status"] = "failed"
                    payload["error"] = "Process was interrupted (server restarted)"
                    payload["completed_at"] = now_iso()
                    conn.execute(
                        "UPDATE hunts SET status=?, error=?, completed_at=?, "
                        "updated_at=datetime('now') WHERE hunt_id=?",
                        (payload["status"], payload["error"], payload["completed_at"], hid),
                    )
                    conn.commit()
                out[hid] = payload
            return out
        finally:
            conn.close()

    def delete_hunt(self, hunt_id: str) -> None:
        conn = self._conn()
        try:
            conn.execute("DELETE FROM hunts WHERE hunt_id = ?", (hunt_id,))
            conn.commit()
        finally:
            conn.close()

    def purge_old_hunts(self, retention_days: int) -> list[str]:
        if retention_days <= 0:
            return []
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT hunt_id FROM hunts WHERE created_at != '' "
                "AND julianday(created_at) < julianday('now', ?)",
                (f"-{retention_days} days",),
            ).fetchall()
            ids = [row["hunt_id"] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(f"DELETE FROM hunts WHERE hunt_id IN ({placeholders})", ids)
                conn.commit()
            return ids
        finally:
            conn.close()

    def accept_new_leads(self, hunt_id: str, leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from agents.lead_identity import dedupe_leads, lead_identity_keys
        if not str(hunt_id or "").strip():
            return list(dedupe_leads(leads))
        conn = self._conn()
        try:
            if self._is_tombstoned(hunt_id):
                raise RuntimeError(f"Hunt {hunt_id} no longer exists")
            existing = conn.execute(
                "SELECT hunt_id FROM hunts WHERE hunt_id = ?", (hunt_id,)
            ).fetchone()
            if existing is None:
                raise RuntimeError(f"Hunt {hunt_id} no longer exists")
            keys: set[str] = set()
            for kr in conn.execute("SELECT lead_key FROM hunt_leads").fetchall():
                keys.add(kr["lead_key"])
            accepted: list[dict[str, Any]] = []
            for lead in dedupe_leads(leads):
                identities = set(lead_identity_keys(lead))
                if identities & keys:
                    continue
                accepted.append(lead)
                keys.update(identities)
            if accepted:
                now = now_iso()
                for lead in accepted:
                    lead_keys = sorted(lead_identity_keys(lead))
                    primary_key = lead_keys[0] if lead_keys else f"anon-{_uuid.uuid4().hex[:12]}"
                    conn.execute(
                        """
                        INSERT INTO hunt_leads
                            (lead_id, hunt_id, lead_key, identity_json, status, round_introduced, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(hunt_id, lead_key) DO NOTHING
                        """,
                        (
                            _uuid.uuid4().hex,
                            hunt_id,
                            primary_key,
                            self._encode_json(lead),
                            "new",
                            0,
                            now,
                        ),
                    )
                count = conn.execute(
                    "SELECT COUNT(*) AS c FROM hunt_leads WHERE hunt_id = ?", (hunt_id,)
                ).fetchone()["c"]
                conn.execute(
                    "UPDATE hunts SET leads_count=?, updated_at=datetime('now') WHERE hunt_id=?",
                    (count, hunt_id),
                )
                conn.commit()
            return accepted
        finally:
            conn.close()

    def saved_leads(self, hunt_id: str) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            return self._leads_for_hunt(conn, hunt_id)
        finally:
            conn.close()

    def current_lead_keys(self) -> set[str]:
        conn = self._conn()
        try:
            rows = conn.execute("SELECT DISTINCT lead_key FROM hunt_leads").fetchall()
            return {row["lead_key"] for row in rows}
        finally:
            conn.close()

    def is_tombstoned(self, hunt_id: str) -> bool:
        return self._is_tombstoned(hunt_id)


# ---------------------------------------------------------------------------
# Dual-write backend (Wave 1 default): write-through to JSON + SQLite,
# reads via JSON. A SQLite failure is logged but never blocks the pipeline
# so the existing 1000 tests keep passing without modification.
# ---------------------------------------------------------------------------


class DualWriteBackend(Backend):
    """Write-through JSON + SQLite, read via JSON.

    Writes call ``primary`` (JSON) first; if it raises the exception
    propagates and SQLite is not touched. If primary succeeds and SQLite
    raises, the exception is logged and swallowed — the JSON file already
    holds the canonical state. Reads always go through ``primary``.
    """

    def __init__(self, primary: Backend, secondary: Backend) -> None:
        self._primary = primary
        self._secondary = secondary

    def _safe_secondary(self, method: str, *args: Any, **kwargs: Any) -> None:
        try:
            getattr(self._secondary, method)(*args, **kwargs)
        except Exception as exc:  # pragma: no cover - defensive best-effort
            logger.warning(
                "[HuntStore] secondary backend %s.%s failed: %s — "
                "JSON file remains source-of-truth; will reconcile on next backfill",
                type(self._secondary).__name__, method, exc,
            )

    # Writes
    def save_hunt(self, hunt_id: str, hunt_data: dict[str, Any]) -> None:
        self._primary.save_hunt(hunt_id, hunt_data)
        self._safe_secondary("save_hunt", hunt_id, hunt_data)

    def delete_hunt(self, hunt_id: str) -> None:
        self._primary.delete_hunt(hunt_id)
        self._safe_secondary("delete_hunt", hunt_id)

    def purge_old_hunts(self, retention_days: int) -> list[str]:
        purged = self._primary.purge_old_hunts(retention_days)
        self._safe_secondary("purge_old_hunts", retention_days)
        return purged

    def accept_new_leads(self, hunt_id: str, leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        accepted = self._primary.accept_new_leads(hunt_id, leads)
        if accepted:
            self._safe_secondary("accept_new_leads", hunt_id, accepted)
        return accepted

    def saved_leads(self, hunt_id: str) -> list[dict[str, Any]]:
        return self._primary.saved_leads(hunt_id)

    def load_all_hunts(self, *, mark_interrupted: bool = False) -> dict[str, dict[str, Any]]:
        return self._primary.load_all_hunts(mark_interrupted=mark_interrupted)

    # Reads — primary only
    def load_hunt(self, hunt_id: str) -> dict[str, Any] | None:
        return self._primary.load_hunt(hunt_id)

    def current_lead_keys(self) -> set[str]:
        return self._primary.current_lead_keys()

    def is_tombstoned(self, hunt_id: str) -> bool:
        return self._primary.is_tombstoned(hunt_id)

    def close(self) -> None:
        self._primary.close()
        self._secondary.close()


# ---------------------------------------------------------------------------
# Backend factory + singleton
# ---------------------------------------------------------------------------


_VALID_BACKENDS = {"json", "sqlite", "dual"}

_backend_singleton: Backend | None = None


def _build_backend() -> Backend:
    """Build a :class:`Backend` based on ``settings.hunt_storage_backend``.

    Settings that pre-date P1 (e.g. the ``SimpleNamespace`` test fixtures
    in :mod:`tests.test_api.test_hunt_dedup`) only expose ``hunts_dir`` /
    ``automation_queue_db_path``. When ``hunt_db_path`` is missing we
    gracefully degrade to the legacy JSON backend so existing tests keep
    passing without modification.
    """
    settings = get_settings()
    name = getattr(settings, "hunt_storage_backend", None) or "dual"
    name = str(name).lower()
    if name not in _VALID_BACKENDS:
        logger.warning(
            "[HuntStore] unknown hunt_storage_backend=%r, falling back to 'dual'",
            name,
        )
        name = "dual"
    json_backend = JSONBackend()
    db_path = getattr(settings, "hunt_db_path", None)
    if name == "json" or not db_path:
        if name != "json":
            logger.debug(
                "[HuntStore] hunt_db_path not configured; using JSON backend only"
            )
        return json_backend
    sqlite_backend = SQLiteBackend()
    if name == "sqlite":
        return sqlite_backend
    return DualWriteBackend(primary=json_backend, secondary=sqlite_backend)


def _get_backend() -> Backend:
    """Return the singleton :class:`Backend` selected by settings."""
    global _backend_singleton
    if _backend_singleton is None:
        _backend_singleton = _build_backend()
    return _backend_singleton


def reset_backend_singleton() -> None:
    """Drop the cached backend (used by tests after monkey-patching settings)."""
    global _backend_singleton
    _backend_singleton = None


# ---------------------------------------------------------------------------
# Public thin-wrapper API. Signatures and semantics are byte-for-byte
# identical to the pre-P1 module-level functions so every existing caller
# and test keeps working without modification.
# ---------------------------------------------------------------------------


def save_hunt(hunt_id: str, hunt_data: dict[str, Any]) -> None:
    """Persist a hunt document."""
    _get_backend().save_hunt(hunt_id, hunt_data)


def load_hunt(hunt_id: str) -> dict[str, Any] | None:
    """Load a single hunt by id."""
    return _get_backend().load_hunt(hunt_id)


def load_all_hunts(*, mark_interrupted: bool = False) -> dict[str, dict[str, Any]]:
    """Load every persisted hunt into a ``{hunt_id: payload}`` dict."""
    return _get_backend().load_all_hunts(mark_interrupted=mark_interrupted)


def delete_hunt(hunt_id: str) -> None:
    """Tombstone + remove a hunt."""
    _get_backend().delete_hunt(hunt_id)


def purge_old_hunts(retention_days: int) -> list[str]:
    """Delete hunt documents older than ``retention_days``."""
    return _get_backend().purge_old_hunts(retention_days)


def accept_new_leads(hunt_id: str, leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Atomically dedup and persist accepted leads on a hunt."""
    return _get_backend().accept_new_leads(hunt_id, leads)


def saved_leads(hunt_id: str) -> list[dict[str, Any]]:
    """Read accepted progress strictly before persisting a failure/cancellation."""
    return _get_backend().saved_leads(hunt_id)


def current_lead_keys() -> set[str]:
    """Read company names from currently queued/visible automation Hunts."""
    return _get_backend().current_lead_keys()


def is_tombstoned(hunt_id: str) -> bool:
    """True if a tombstone marker exists for ``hunt_id``."""
    return _get_backend().is_tombstoned(hunt_id)
