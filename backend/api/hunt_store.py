"""Hunt persistence — JSON file-based storage for hunt metadata and results.

Each hunt is saved as a JSON file: {hunts_dir}/{hunt_id}.json
On server startup, all existing hunt files are loaded into the in-memory _hunts dict.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import get_settings

logger = logging.getLogger(__name__)


@contextmanager
def hunt_data_lock():
    """Serialize customer acceptance, persistence and deletion across workers."""
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


def current_lead_keys() -> set[str]:
    """Read company names from currently queued/visible automation Hunts."""
    from agents.lead_identity import lead_identity_keys
    keys: set[str] = set()
    with hunt_data_lock():
        for path in _dedup_hunt_paths():
            hunt = json.loads(path.read_text(encoding="utf-8"))
            keys.update(key for lead in current_leads(hunt) for key in lead_identity_keys(lead))
    return keys


def _dedup_hunt_paths() -> list[Path]:
    """Return current task Hunts, excluding stale historical job artifacts."""
    root = _hunts_dir()
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
        # A queue audit failure must not make a running hunt silently ignore
        # all deduplication. Fall back to the strict file-based scope.
        return sorted(root.glob("*.json"))


def saved_leads(hunt_id: str) -> list[dict[str, Any]]:
    """Read accepted progress strictly before persisting a failure/cancellation."""
    with hunt_data_lock():
        path = _hunts_dir() / f"{hunt_id}.json"
        return current_leads(json.loads(path.read_text(encoding="utf-8")))


def accept_new_leads(hunt_id: str, leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Check and persist accepted customers atomically against all existing tasks."""
    from agents.lead_identity import dedupe_leads, lead_identity_keys
    if not str(hunt_id or "").strip():
        # No hunt is bound to this run (e.g. direct graph invocation in
        # tests): nothing to persist against and no cross-hunt dedup keys
        # to check — return in-run deduped leads instead of killing the
        # whole pipeline.
        return list(dedupe_leads(leads))
    with hunt_data_lock():
        root = _hunts_dir()
        path = root / f"{hunt_id}.json"
        if (root / f".{hunt_id}.deleted").exists() or not path.exists():
            raise RuntimeError(f"Hunt {hunt_id} no longer exists")
        keys: set[str] = set()
        for source in _dedup_hunt_paths():
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


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Atomic write via temp file + replace to prevent partial writes on crash."""
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
        except FileNotFoundError:
            pass
        raise


def _hunts_dir() -> Path:
    """Return the hunts directory path, creating it if needed."""
    settings = get_settings()
    p = Path(settings.hunts_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_hunt(hunt_id: str, hunt_data: dict[str, Any]) -> None:
    """Persist a hunt to disk as JSON."""
    try:
        with hunt_data_lock():
            path = _hunts_dir() / f"{hunt_id}.json"
            if (_hunts_dir() / f".{hunt_id}.deleted").exists():
                raise RuntimeError(f"Hunt {hunt_id} was deleted")
            payload = {"hunt_id": hunt_id, **hunt_data}
            _write_json_atomic(path, payload)
    except Exception as e:
        logger.warning("[HuntStore] Failed to save hunt %s: %s", hunt_id[:8], e)
        raise


def load_all_hunts(*, mark_interrupted: bool = False) -> dict[str, dict[str, Any]]:
    """Load all hunts from disk into a dict keyed by hunt_id.

    `mark_interrupted=True` should only be used during process startup recovery.
    Runtime readers such as metrics/notifiers must not mutate running hunts.
    """
    hunts: dict[str, dict[str, Any]] = {}
    hunts_path = _hunts_dir()

    for path in hunts_path.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            hid = data.pop("hunt_id", path.stem)
            # Any hunt that was running/pending when the process died is now interrupted.
            # This mutation is only safe during startup recovery, not during runtime reads.
            if mark_interrupted and data.get("status") in ("running", "pending"):
                data["status"] = "failed"
                data["error"] = "Process was interrupted (server restarted)"
                data["completed_at"] = now_iso()
                # Persist the updated status so it survives future restarts
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


def delete_hunt(hunt_id: str) -> None:
    """Delete a hunt file from disk."""
    try:
        with hunt_data_lock():
            path = _hunts_dir() / f"{hunt_id}.json"
            _write_json_atomic(_hunts_dir() / f".{hunt_id}.deleted", {"deleted_at": now_iso()})
            path.unlink(missing_ok=True)
    except Exception as e:
        logger.warning("[HuntStore] Failed to delete hunt %s: %s", hunt_id[:8], e)
        raise


def load_hunt(hunt_id: str) -> dict[str, Any] | None:
    """Load a single hunt from disk."""
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


def now_iso() -> str:
    """Return current UTC time as ISO string."""
    return datetime.now(timezone.utc).isoformat()


def purge_old_hunts(retention_days: int) -> list[str]:
    """Delete hunt JSON files older than `retention_days` (by file mtime).

    Returns the purged hunt ids. Uses the same tombstone protocol as
    `delete_hunt()` so `accept_new_leads()` and `save_hunt()` correctly
    reject purged hunts. Callers should also evict purged ids from the
    in-memory `_hunts` dict and clean up the hunt's checkpoint rows.
    """
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
