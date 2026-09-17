#!/usr/bin/env python3
"""Clean weak identity keys from the global lead registry.

Company-name-only keys (company:...) should not exist in the registry as standalone
identities because they cause same-name companies in different regions to incorrectly
dedupe each other.

This script removes all company:... keys from the registry that are not accompanied
by strong identity keys (domain, email, phone, place_id, source_url) for the same hunt_id.

Usage:
    python scripts/clean_weak_identity_keys.py --dry-run  # Preview changes
    python scripts/clean_weak_identity_keys.py            # Apply changes
"""

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

# Add backend to path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

from config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean weak identity keys from global registry")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without applying")
    args = parser.parse_args()

    settings = get_settings()
    db_path = Path(settings.email_db_path)
    if not db_path.exists():
        print(f"✗ Database not found: {db_path}")
        return

    print("==> Analyzing global lead registry...")
    
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    try:
        # Get all keys
        rows = conn.execute("SELECT dedupe_key, hunt_id, lead_json FROM lead_registry").fetchall()
        
        if not rows:
            print("✓ Registry is empty")
            return
        
        print(f"Found {len(rows)} total keys in registry")
        
        # Group keys by hunt_id and their associated lead
        # The challenge: each dedupe_key is a PRIMARY KEY, so one row = one key.
        # Multiple keys for the same lead are stored in separate rows.
        # We need to find leads that ONLY have company: keys.
        
        # Step 1: Extract all keys and group by hunt + lead content
        hunt_lead_keys = defaultdict(lambda: {"keys": [], "lead_json": None})
        
        for row in rows:
            key = str(row["dedupe_key"])
            hunt_id = str(row["hunt_id"])
            lead_json = str(row["lead_json"])
            
            # Use hunt_id + lead_json as identity (same lead = same JSON in registry)
            identity = f"{hunt_id}:{lead_json}"
            hunt_lead_keys[identity]["keys"].append(key)
            hunt_lead_keys[identity]["lead_json"] = lead_json
            hunt_lead_keys[identity]["hunt_id"] = hunt_id
        
        print(f"Found {len(hunt_lead_keys)} unique leads across all hunts")
        
        # Step 2: Find leads with ONLY company: keys
        weak_keys_to_remove = []
        
        for identity, data in hunt_lead_keys.items():
            keys = data["keys"]
            
            # Check if there are any strong keys
            strong_keys = [
                k for k in keys
                if k.startswith(("domain:", "email:", "phone:", "place:", "source_url:"))
            ]
            
            company_keys = [k for k in keys if k.startswith("company:")]
            
            if company_keys and not strong_keys:
                # This lead only has company: keys - remove them
                weak_keys_to_remove.extend(company_keys)
        
        # Deduplicate
        weak_keys_to_remove = list(dict.fromkeys(weak_keys_to_remove))
        
        if not weak_keys_to_remove:
            print("✓ No weak company keys to remove")
            return
        
        print(f"\nFound {len(weak_keys_to_remove)} weak company keys to remove:")
        for key in weak_keys_to_remove[:10]:
            print(f"  - {key}")
        if len(weak_keys_to_remove) > 10:
            print(f"  ... and {len(weak_keys_to_remove) - 10} more")
        
        if args.dry_run:
            print("\n✓ Dry-run complete. Use without --dry-run to apply changes.")
            return
        
        # Step 3: Delete weak keys
        print("\nRemoving weak keys...")
        cursor = conn.cursor()
        for key in weak_keys_to_remove:
            cursor.execute("DELETE FROM lead_registry WHERE dedupe_key = ?", (key,))
        
        conn.commit()
        
        remaining = conn.execute("SELECT COUNT(*) FROM lead_registry").fetchone()[0]
        
        print(f"✓ Removed {len(weak_keys_to_remove)} weak company keys")
        print(f"✓ Registry now has {remaining} total keys")
        
    finally:
        conn.close()


if __name__ == "__main__":
    main()
