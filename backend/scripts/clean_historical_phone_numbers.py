"""Validate and normalize phone numbers in persisted historical leads."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import get_settings
from tools.contact_extractor import sanitize_phone_list


def _clean_tree(value: Any, stats: dict[str, int]) -> bool:
    changed = False
    if isinstance(value, dict):
        phones = value.get("phone_numbers")
        if isinstance(phones, list):
            original = [phone for phone in phones if isinstance(phone, str)]
            cleaned = sanitize_phone_list(
                original,
                country_code=str(value.get("country_code", "") or ""),
                address=str(value.get("address", "") or ""),
                website=str(value.get("website", value.get("source_url", "")) or ""),
            )
            stats["lead_records"] += 1
            stats["phones_before"] += len(original)
            stats["phones_after"] += len(cleaned)
            stats["phones_removed"] += max(len(original) - len(cleaned), 0)
            stats["phones_reformatted"] += sum(phone not in original for phone in cleaned)
            if phones != cleaned:
                value["phone_numbers"] = cleaned
                stats["changed_lead_records"] += 1
                changed = True
        for nested in value.values():
            changed = _clean_tree(nested, stats) or changed
    elif isinstance(value, list):
        for nested in value:
            changed = _clean_tree(nested, stats) or changed
    return changed


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    mode = path.stat().st_mode
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _backup_sqlite(source_path: Path, target_path: Path) -> None:
    with sqlite3.connect(str(source_path)) as source, sqlite3.connect(str(target_path)) as target:
        source.backup(target)


def _assert_idle(settings: Any) -> None:
    with sqlite3.connect(settings.automation_queue_db_path) as conn:
        active_jobs = conn.execute(
            "SELECT COUNT(*) FROM hunt_jobs WHERE status IN (?, ?)",
            ("queued", "running"),
        ).fetchone()[0]
    if active_jobs:
        raise RuntimeError(f"Found {active_jobs} queued/running automation jobs")

    running_hunts = []
    for path in Path(settings.hunts_dir).glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if str(data.get("status", "") or "") in {"pending", "running"}:
            running_hunts.append(path.stem)
    if running_hunts:
        raise RuntimeError(f"Found pending/running Hunt files: {running_hunts[:5]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    hunt_root = Path(settings.hunts_dir)
    queue_db = Path(settings.automation_queue_db_path)
    stats = {
        "hunt_files_scanned": 0,
        "hunt_files_changed": 0,
        "queue_rows_scanned": 0,
        "queue_rows_changed": 0,
        "lead_records": 0,
        "changed_lead_records": 0,
        "phones_before": 0,
        "phones_after": 0,
        "phones_removed": 0,
        "phones_reformatted": 0,
    }
    changed_hunts: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(hunt_root.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        stats["hunt_files_scanned"] += 1
        if _clean_tree(data, stats):
            stats["hunt_files_changed"] += 1
            changed_hunts.append((path, data))

    changed_rows: list[tuple[str, str]] = []
    with sqlite3.connect(queue_db) as conn:
        for job_id, payload_json in conn.execute("SELECT id, payload_json FROM hunt_jobs"):
            stats["queue_rows_scanned"] += 1
            payload = json.loads(payload_json or "{}")
            if _clean_tree(payload, stats):
                stats["queue_rows_changed"] += 1
                changed_rows.append((json.dumps(payload, ensure_ascii=False), str(job_id)))

    print(json.dumps({"apply": args.apply, **stats}, ensure_ascii=False))
    if not args.apply:
        return

    _assert_idle(settings)
    backup_root = queue_db.parent / (
        "phone-cleanup-backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    backup_hunts = backup_root / "hunts"
    backup_hunts.mkdir(parents=True, mode=0o700)
    _backup_sqlite(queue_db, backup_root / queue_db.name)
    for path, _ in changed_hunts:
        shutil.copy2(path, backup_hunts / path.name)
    print(f"backup={backup_root}", flush=True)

    for path, data in changed_hunts:
        _atomic_write_json(path, data)
    with sqlite3.connect(queue_db) as conn:
        conn.executemany("UPDATE hunt_jobs SET payload_json = ? WHERE id = ?", changed_rows)
        conn.commit()
    print("cleanup_complete", flush=True)


if __name__ == "__main__":
    main()
