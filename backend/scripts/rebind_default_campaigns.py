#!/usr/bin/env python3
"""Rebind legacy campaigns with email_account_id='default' to real accounts.

Usage:
    python3 scripts/rebind_default_campaigns.py [--dry-run]

This script finds all campaigns (any status) with email_account_id='default'
and rebinds them to the first available Graph account with remaining quota.
Prevents the scheduler from hitting 'inactive_email_account' errors when
trying to send from the empty 'default' account row.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

# Add backend to path so we can import config/emailing modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import get_settings
from emailing.store import EmailStore


def _pick_first_available_account(store: EmailStore) -> dict | None:
    """Pick the first active Graph account (by sort_order).
    
    For this one-time rebind script, we don't check quotas — we just
    want to bind to any real account so the scheduler's own quota
    rotation logic can take over.
    """
    for row in store.list_accounts():
        if str(row.get("status", "active")) != "active":
            continue
        if str(row.get("provider_type", "graph")) != "graph":
            continue
        account_id = str(row.get("id", "") or "")
        if not account_id or account_id == "default":
            continue
        return row
    return None


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    settings = get_settings()
    store = EmailStore(settings.email_db_path)
    store.init_db()
    
    # Find all campaigns with email_account_id='default'
    campaigns = []
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, status, email_account_id, name, created_at "
            "FROM email_campaigns "
            "WHERE email_account_id = 'default' "
            "ORDER BY created_at DESC"
        ).fetchall()
        campaigns = [dict(r) for r in rows]
    
    if not campaigns:
        print("✓ No campaigns with email_account_id='default' found.")
        return 0
    
    print(f"Found {len(campaigns)} campaigns with email_account_id='default':")
    for c in campaigns:
        print(f"  - {c['id'][:8]}... ({c['status']}) created at {c['created_at']}")
    print()
    
    # Pick a real account to rebind to
    target_account = _pick_first_available_account(store)
    if not target_account:
        print("✗ No active Graph accounts found. Cannot rebind.")
        return 1
    
    target_id = str(target_account["id"])
    target_email = str(target_account.get("from_email", ""))
    print(f"Target account: {target_id} ({target_email})")
    print()
    
    if dry_run:
        print("DRY RUN: Would rebind the following campaigns:")
        for c in campaigns:
            print(f"  - {c['id']} → {target_id}")
        print("\nRun without --dry-run to apply changes.")
        return 0
    
    # Rebind all campaigns
    updated_at = datetime.now(timezone.utc).isoformat()
    with store._connect() as conn:
        for c in campaigns:
            conn.execute(
                "UPDATE email_campaigns SET email_account_id = ?, updated_at = ? WHERE id = ?",
                (target_id, updated_at, c["id"]),
            )
            print(f"✓ Rebound campaign {c['id'][:8]}... from 'default' to {target_id}")
    
    print(f"\n✓ Successfully rebound {len(campaigns)} campaigns to {target_id} ({target_email})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
