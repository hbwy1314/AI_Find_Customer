"""Report or clean duplicate leads in persisted Hunt JSON files."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from agents.lead_identity import dedupe_leads, lead_identity_keys
from config.settings import get_settings


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
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


def dedupe_hunt_file(path: Path, *, apply: bool) -> tuple[int, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    leads = result.get("leads") if isinstance(result.get("leads"), list) else []
    deduped = dedupe_leads(leads)
    removed = len(leads) - len(deduped)
    if removed <= 0 or not apply:
        return len(leads), removed

    result = dict(result)
    result["leads"] = deduped
    data = dict(data)
    data["result"] = result
    data["leads_count"] = len(deduped)
    _write_json_atomic(path, data)
    return len(leads), removed


def rebuild_registry(hunts_dir: Path, db_path: str) -> int:
    from emailing.store import EmailStore
    from agents.lead_identity import normalize_company_name

    def _company_name_key(lead: dict) -> list[str]:
        company = normalize_company_name(str(lead.get("company_name", "") or ""))
        return [f"company:{company}"] if company else []

    store = EmailStore(db_path)
    store.init_db()

    # Clear all existing registry entries and rebuild with company names only.
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM lead_registry")

    reserved = 0
    for path in sorted(hunts_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        hunt_id = str(data.get("hunt_id") or path.stem)
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        leads = result.get("leads") if isinstance(result.get("leads"), list) else []
        accepted = store.ensure_lead_keys(
            [lead for lead in leads if isinstance(lead, dict)],
            hunt_id=hunt_id,
            key_fn=_company_name_key,
            now_iso=str(
                data.get("completed_at")
                or data.get("created_at")
                or datetime.now(timezone.utc).isoformat()
            ),
        )
        reserved += accepted
    return reserved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hunts-dir", default="", help="Hunt JSON directory")
    parser.add_argument("--apply", action="store_true", help="Write deduplicated files")
    parser.add_argument("--rebuild-registry", action="store_true", help="Backfill the global lead registry")
    args = parser.parse_args()

    settings = get_settings()
    hunts_dir = Path(args.hunts_dir or settings.hunts_dir)
    files = sorted(hunts_dir.glob("*.json"))
    total_leads = 0
    total_removed = 0
    changed_files = 0
    for path in files:
        before, removed = dedupe_hunt_file(path, apply=args.apply)
        total_leads += before
        total_removed += removed
        if removed:
            changed_files += 1
            print(f"{path.name}: {before} -> {before - removed} (removed {removed})")

    print(f"files={len(files)} changed={changed_files} leads={total_leads} removed={total_removed}")
    if args.rebuild_registry:
        reserved = rebuild_registry(hunts_dir, settings.email_db_path)
        print(f"registry_reserved={reserved}")
    if not args.apply and total_removed:
        print("dry-run only; rerun with --apply to write changes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
