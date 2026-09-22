"""Read-only customer index audit; --clear-legacy explicitly backs up and retires old keys."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.lead_identity import lead_identity_keys
from api.hunt_store import current_leads
from config.settings import get_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clear-legacy", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    sources = Counter()
    tasks = rows = 0
    for path in sorted(Path(settings.hunts_dir).glob("*.json")):
        leads = current_leads(json.loads(path.read_text(encoding="utf-8")))
        tasks += 1
        rows += len(leads)
        sources.update({key for lead in leads for key in lead_identity_keys(lead)})
    db = Path(settings.email_db_path).resolve()
    legacy = set()
    if db.exists():
        with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='lead_registry'").fetchone():
                legacy = {row[0] for row in conn.execute("SELECT dedupe_key FROM lead_registry")}
    print(json.dumps({"tasks": tasks, "lead_rows": rows, "current_keys": len(sources),
                      "multi_task_keys": sum(n > 1 for n in sources.values()),
                      "legacy_keys": len(legacy), "orphan_legacy_keys": len(legacy - sources.keys())}))
    if args.clear_legacy and legacy:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_dir = Path(settings.hunts_dir).parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{db.name}.bak-dedup-{stamp}"
        with sqlite3.connect(db) as conn, sqlite3.connect(backup) as dst:
            conn.backup(dst)
            conn.execute("DELETE FROM lead_registry")
        print(f"Legacy keys cleared; backup={backup}. Hunt and email history were not changed.")


if __name__ == "__main__":
    main()
