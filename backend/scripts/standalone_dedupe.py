#!/usr/bin/env python3
"""Standalone Hunt leads deduplication - no external dependencies."""

import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path


def normalize_company_name(name: str) -> str:
    """Normalize company name for comparison."""
    if not name or not isinstance(name, str):
        return ""
    s = name.strip().lower()
    # Remove common suffixes
    for suffix in [
        "ltd", "limited", "llc", "inc", "incorporated", "corp", "corporation",
        "gmbh", "ag", "sa", "bv", "nv", "pty", "co", "company", "enterprises",
        "international", "group", "holdings",
    ]:
        s = re.sub(rf"\b{suffix}\b\.?", "", s, flags=re.IGNORECASE)
    # Remove punctuation and extra spaces
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def dedupe_leads(leads: list) -> list:
    """Remove duplicate leads based on company name."""
    seen_names = set()
    deduped = []
    for lead in leads:
        if not isinstance(lead, dict):
            continue
        company = normalize_company_name(lead.get("company_name", ""))
        if not company:
            deduped.append(lead)
            continue
        if company in seen_names:
            continue
        seen_names.add(company)
        deduped.append(lead)
    return deduped


def write_json_atomic(path: Path, data: dict) -> None:
    """Atomic JSON write with permission preservation."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        # Inherit original file permissions
        if path.exists():
            stat_info = path.stat()
            os.chown(tmp_name, stat_info.st_uid, stat_info.st_gid)
            os.chmod(tmp_name, stat_info.st_mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def dedupe_hunt_file(path: Path, apply: bool) -> tuple[int, int]:
    """Dedupe a single Hunt file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    result = data.get("result", {})
    if not isinstance(result, dict):
        return 0, 0
    
    leads = result.get("leads", [])
    if not isinstance(leads, list):
        return 0, 0
    
    deduped = dedupe_leads(leads)
    removed = len(leads) - len(deduped)
    
    if removed <= 0:
        return len(leads), 0
    
    if not apply:
        return len(leads), removed
    
    # Apply changes
    result = dict(result)
    result["leads"] = deduped
    data = dict(data)
    data["result"] = result
    data["leads_count"] = len(deduped)
    write_json_atomic(path, data)
    return len(leads), removed


def rebuild_registry(hunts_dir: Path, db_path: str) -> int:
    """Rebuild global lead registry with company names only."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    
    conn = sqlite3.connect(str(db_path))
    
    # Create table if not exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lead_registry (
            key TEXT PRIMARY KEY,
            hunt_id TEXT NOT NULL,
            first_seen TEXT NOT NULL
        )
    """)
    
    # Clear existing entries
    conn.execute("DELETE FROM lead_registry")
    conn.commit()
    
    reserved = 0
    for path in sorted(hunts_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"Failed to read {path.name}: {e}", file=sys.stderr)
            continue
        
        hunt_id = str(data.get("hunt_id", path.stem))
        result = data.get("result", {})
        if not isinstance(result, dict):
            continue
        
        leads = result.get("leads", [])
        if not isinstance(leads, list):
            continue
        
        first_seen = data.get("completed_at") or data.get("created_at") or ""
        
        for lead in leads:
            if not isinstance(lead, dict):
                continue
            company = normalize_company_name(lead.get("company_name", ""))
            if not company:
                continue
            
            key = f"company:{company}"
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO lead_registry (key, hunt_id, first_seen) VALUES (?, ?, ?)",
                    (key, hunt_id, first_seen),
                )
                reserved += 1
            except Exception:
                pass
    
    conn.commit()
    conn.close()
    return reserved


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Dedupe Hunt leads by company name")
    parser.add_argument("--hunts-dir", required=True, help="Hunt JSON directory")
    parser.add_argument("--db-path", default="/opt/ai-hunter/repo/backend/data/email.db", help="Registry DB path")
    parser.add_argument("--apply", action="store_true", help="Write changes")
    parser.add_argument("--rebuild-registry", action="store_true", help="Rebuild global registry")
    args = parser.parse_args()
    
    hunts_dir = Path(args.hunts_dir)
    if not hunts_dir.exists():
        print(f"Error: {hunts_dir} does not exist", file=sys.stderr)
        return 1
    
    files = sorted(hunts_dir.glob("*.json"))
    total_leads = 0
    total_removed = 0
    changed_files = 0
    
    for path in files:
        before, removed = dedupe_hunt_file(path, args.apply)
        total_leads += before
        total_removed += removed
        if removed:
            changed_files += 1
            print(f"{path.name}: {before} -> {before - removed} (removed {removed})")
    
    print(f"files={len(files)} changed={changed_files} leads={total_leads} removed={total_removed}")
    
    if args.rebuild_registry:
        reserved = rebuild_registry(hunts_dir, args.db_path)
        print(f"registry_reserved={reserved}")
    
    if not args.apply and total_removed:
        print("dry-run only; rerun with --apply to write changes")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
