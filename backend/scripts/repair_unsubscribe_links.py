"""Repair invalid unsubscribe links in archived email HTML.

Dry-run is the default. Run with ``--apply`` only while the API and workers are
stopped; the script verifies that no hunt job or email delivery is in flight.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import stat
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import get_settings
from emailing.html_format import prepare_send_html
from emailing.unsubscribe import build_unsubscribe_url, issue_token, verify_token

_UNSUBSCRIBE_LINK_RE = re.compile(
    r"(https?)://[^\s\"'<>]+/api/unsubscribe/([A-Za-z0-9_.-]+)"
)


def _link_issue(body_html: str, recipient: str) -> str:
    """Return why HTML needs repair, or an empty string when all links are valid."""
    links = _UNSUBSCRIBE_LINK_RE.findall(str(body_html or ""))
    if not links:
        return "missing"
    if any(scheme != "https" for scheme, _token in links):
        return "insecure"
    tokens = [token for _scheme, token in links]
    if "__preview__" in tokens:
        return "placeholder"

    recipient_norm = str(recipient or "").strip().lower()
    for token in tokens:
        payload = verify_token(token)
        if not payload:
            return "invalid_or_expired"
        if str(payload.get("email") or "").strip().lower() != recipient_norm:
            return "recipient_mismatch"
    return ""


def _recipient_for_sequence(sequence: dict) -> str:
    target_email = str((sequence.get("target") or {}).get("target_email") or "").strip()
    if target_email:
        return target_email.lower()
    lead_emails = (sequence.get("lead") or {}).get("emails") or []
    if isinstance(lead_emails, list) and lead_emails:
        return str(lead_emails[0] or "").strip().lower()
    return ""


def _write_hunt_atomic(path: Path, hunt: dict) -> None:
    mode = path.stat().st_mode
    temp = path.with_name(path.name + ".repair-tmp")
    temp.write_text(json.dumps(hunt, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.chmod(stat.S_IMODE(mode))
    temp.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    hunts_root = Path(settings.hunts_dir)
    email_db = Path(settings.email_db_path)
    queue_db = Path(settings.automation_queue_db_path)

    with sqlite3.connect(f"file:{queue_db}?mode=ro", uri=True) as queue:
        active_jobs = queue.execute(
            "SELECT count(*) FROM hunt_jobs WHERE status IN ('queued','running')"
        ).fetchone()[0]
        if active_jobs:
            raise RuntimeError(f"Active hunt jobs ({active_jobs}); aborting repair")

    with sqlite3.connect(f"file:{email_db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        in_flight = conn.execute(
            "SELECT count(*) FROM email_messages WHERE status='sending'"
        ).fetchone()[0]
        if in_flight:
            raise RuntimeError(f"Messages in flight ({in_flight}); aborting repair")
        rows = conn.execute(
            """
            SELECT m.id, m.body_text, m.body_html, m.locale, m.status,
                   s.lead_email, s.campaign_id
            FROM email_messages m
            JOIN lead_email_sequences s ON s.id=m.sequence_id
            WHERE m.status != 'sending'
            """
        ).fetchall()

    db_repairs: list[tuple[sqlite3.Row, str]] = []
    db_issues: Counter[str] = Counter()
    invalid_db_recipients = 0
    for row in rows:
        recipient = str(row["lead_email"] or "").strip().lower()
        if not recipient or "@" not in recipient:
            invalid_db_recipients += 1
            continue
        issue = _link_issue(str(row["body_html"] or ""), recipient)
        if issue:
            db_repairs.append((row, issue))
            db_issues[issue] += 1

    hunt_repairs: list[tuple[dict, str, str, str]] = []
    hunt_issues: Counter[str] = Counter()
    hunt_files: dict[Path, dict] = {}
    scanned_hunt_emails = 0
    invalid_hunt_recipients = 0
    for path in hunts_root.glob("*.json"):
        hunt = json.loads(path.read_text(encoding="utf-8"))
        if hunt.get("status") in {"running", "pending"}:
            raise RuntimeError(f"Active hunt file {path.name}; aborting repair")
        for sequence in (hunt.get("result") or {}).get("email_sequences", []):
            recipient = _recipient_for_sequence(sequence)
            emails = sequence.get("emails") or []
            if not recipient or "@" not in recipient:
                invalid_hunt_recipients += len(emails)
                continue
            locale = str(sequence.get("locale") or "en_US")
            for email in emails:
                if not isinstance(email, dict):
                    continue
                scanned_hunt_emails += 1
                issue = _link_issue(str(email.get("body_html") or ""), recipient)
                if issue:
                    hunt_repairs.append((email, recipient, locale, issue))
                    hunt_issues[issue] += 1
                    hunt_files[path] = hunt

    summary = {
        "apply": args.apply,
        "database": {
            "scanned": len(rows),
            "valid": len(rows) - len(db_repairs) - invalid_db_recipients,
            "repair": len(db_repairs),
            "issues": dict(sorted(db_issues.items())),
            "invalid_recipients": invalid_db_recipients,
        },
        "hunts": {
            "scanned": scanned_hunt_emails,
            "valid": scanned_hunt_emails - len(hunt_repairs),
            "repair": len(hunt_repairs),
            "issues": dict(sorted(hunt_issues.items())),
            "invalid_recipients": invalid_hunt_recipients,
            "files": len(hunt_files),
        },
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    if not args.apply:
        return

    base_url = str(settings.public_base_url or "").strip() or "https://api.nineluan.com"
    if not base_url.startswith("https://"):
        raise RuntimeError("public_base_url must use HTTPS before applying repairs")

    backup = email_db.parent / (
        "unsubscribe-backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    backup.mkdir(mode=0o700)
    if db_repairs:
        with sqlite3.connect(str(email_db)) as source, sqlite3.connect(str(backup / email_db.name)) as target:
            source.backup(target)
    for path in hunt_files:
        shutil.copy2(path, backup / path.name)
    print("backup=" + str(backup), flush=True)

    if db_repairs:
        with sqlite3.connect(str(email_db)) as conn:
            for row, _issue in db_repairs:
                recipient = str(row["lead_email"] or "").strip().lower()
                campaign_id = str(row["campaign_id"] or "").strip()
                scope = f"campaign:{campaign_id}" if campaign_id else "all"
                url = build_unsubscribe_url(base_url, issue_token(recipient, scope=scope))
                body_html = prepare_send_html(
                    str(row["body_text"] or ""),
                    str(row["body_html"] or ""),
                    url,
                    locale=str(row["locale"] or "en_US"),
                )
                conn.execute(
                    "UPDATE email_messages SET body_html=? WHERE id=? AND status != 'sending'",
                    (body_html, row["id"]),
                )
            conn.commit()

    for email, recipient, locale, _issue in hunt_repairs:
        url = build_unsubscribe_url(base_url, issue_token(recipient))
        email["body_html"] = prepare_send_html(
            str(email.get("body_text") or ""),
            str(email.get("body_html") or ""),
            url,
            locale=locale,
        )
    for path, hunt in hunt_files.items():
        _write_hunt_atomic(path, hunt)

    print(json.dumps({"repair_complete": True, "backup": str(backup)}, sort_keys=True))


if __name__ == "__main__":
    main()
