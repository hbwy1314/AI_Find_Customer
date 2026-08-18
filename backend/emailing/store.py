"""SQLite store for email automation state."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from auth import secrets as secret_cipher

logger = logging.getLogger(__name__)


_DDL = """
CREATE TABLE IF NOT EXISTS email_accounts (
  id TEXT PRIMARY KEY,
  provider_type TEXT NOT NULL,
  from_name TEXT NOT NULL,
  from_email TEXT NOT NULL,
  reply_to TEXT DEFAULT '',
  smtp_host TEXT DEFAULT '',
  smtp_port INTEGER DEFAULT 587,
  smtp_username TEXT DEFAULT '',
  smtp_secret_encrypted TEXT DEFAULT '',
  imap_host TEXT DEFAULT '',
  imap_port INTEGER DEFAULT 993,
  imap_username TEXT DEFAULT '',
  imap_secret_encrypted TEXT DEFAULT '',
  use_tls INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'active',
  daily_send_limit INTEGER NOT NULL DEFAULT 50,
  hourly_send_limit INTEGER NOT NULL DEFAULT 10,
  last_test_at TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS email_campaigns (
  id TEXT PRIMARY KEY,
  hunt_id TEXT NOT NULL,
  email_account_id TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  language_mode TEXT NOT NULL DEFAULT 'auto_by_region',
  default_language TEXT NOT NULL DEFAULT 'en',
  fallback_language TEXT NOT NULL DEFAULT 'en',
  tone TEXT NOT NULL DEFAULT 'professional',
  step1_delay_days INTEGER NOT NULL DEFAULT 0,
  step2_delay_days INTEGER NOT NULL DEFAULT 3,
  step3_delay_days INTEGER NOT NULL DEFAULT 3,
  min_fit_score REAL NOT NULL DEFAULT 0.6,
  min_contactability_score REAL NOT NULL DEFAULT 0.45,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lead_email_sequences (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL,
  hunt_id TEXT NOT NULL,
  lead_key TEXT NOT NULL,
  lead_email TEXT NOT NULL,
  lead_name TEXT DEFAULT '',
  decision_maker_name TEXT DEFAULT '',
  decision_maker_title TEXT DEFAULT '',
  locale TEXT NOT NULL DEFAULT 'en',
  generation_mode TEXT NOT NULL DEFAULT 'personalized',
  template_id TEXT DEFAULT '',
  template_group TEXT DEFAULT '',
  template_usage_index INTEGER NOT NULL DEFAULT 0,
  template_max_send_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'draft',
  current_step INTEGER NOT NULL DEFAULT 0,
  stop_reason TEXT DEFAULT '',
  replied_at TEXT DEFAULT '',
  last_sent_at TEXT DEFAULT '',
  next_scheduled_at TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sequence_campaign_lead ON lead_email_sequences(campaign_id, lead_key);
-- Per-sequence recipient pool for the "waterfall" send strategy.
-- A sequence can have 1..N recipients; the scheduler picks the next
-- `pending` one, sends, marks it `waiting_reply`. After
-- `email_recipient_waterfall_days` with no reply, the recipient is
-- marked `skipped` and the scheduler advances to the next `pending`
-- row. A reply from ANY recipient flips the whole sequence to
-- `replied`. `position` is the order to try recipients in (lower
-- first); callers pass the candidate list in the order they want
-- them tried.
CREATE TABLE IF NOT EXISTS lead_email_recipients (
  id TEXT PRIMARY KEY,
  sequence_id TEXT NOT NULL,
  email TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',
  sent_at TEXT DEFAULT '',
  last_attempt_at TEXT DEFAULT '',
  replied_at TEXT DEFAULT '',
  failure_reason TEXT DEFAULT '',
  is_role_based INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recipient_sequence ON lead_email_recipients(sequence_id);
CREATE INDEX IF NOT EXISTS idx_recipient_status_pos ON lead_email_recipients(sequence_id, status, position);
CREATE TABLE IF NOT EXISTS email_messages (
  id TEXT PRIMARY KEY,
  sequence_id TEXT NOT NULL,
  step_number INTEGER NOT NULL,
  goal TEXT NOT NULL,
  locale TEXT NOT NULL,
  subject TEXT NOT NULL,
  body_text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  scheduled_at TEXT NOT NULL,
  sent_at TEXT DEFAULT '',
  provider_message_id TEXT DEFAULT '',
  thread_key TEXT DEFAULT '',
  failure_reason TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
-- Not unique per (sequence_id, step_number): waterfall creates a fresh
-- message row for each recipient attempt of the same step. The unique
-- index that used to be here was removed when the waterfall was
-- introduced (see commit history).
CREATE INDEX IF NOT EXISTS idx_email_message_sequence_step ON email_messages(sequence_id, step_number);
CREATE INDEX IF NOT EXISTS idx_email_message_status_schedule ON email_messages(status, scheduled_at);
-- Test sends go through a separate table because they have no
-- `lead_email_sequences` row (so we can't satisfy email_messages'
-- implicit sequence_id link) and we don't want them mixing into
-- reply detection. The `count_sent_today_for_account` helper folds
-- this table's count into the daily total so test sends count
-- against the user's daily_send_limit.
CREATE TABLE IF NOT EXISTS email_test_send_log (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  to_email TEXT NOT NULL,
  subject TEXT NOT NULL,
  body_text TEXT NOT NULL,
  provider TEXT NOT NULL,
  provider_message_id TEXT DEFAULT '',
  thread_key TEXT DEFAULT '',
  ok INTEGER NOT NULL DEFAULT 1,
  failure_reason TEXT DEFAULT '',
  sent_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_test_send_account_sent ON email_test_send_log(account_id, sent_at);
CREATE TABLE IF NOT EXISTS email_reply_events (
  id TEXT PRIMARY KEY,
  sequence_id TEXT NOT NULL,
  message_id TEXT DEFAULT '',
  from_email TEXT NOT NULL,
  subject TEXT DEFAULT '',
  snippet TEXT DEFAULT '',
  received_at TEXT NOT NULL,
  raw_ref TEXT DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reply_sequence_id ON email_reply_events(sequence_id);
-- Unsubscribe records. `email` is the recipient address (lowercased).
-- `scope` is one of: 'all', 'campaign:{id}', 'sequence:{id}'. A
-- row with scope='all' acts as a global block; rows with finer scope
-- only block that specific campaign/sequence. `token_hash` is the
-- SHA256 of the unsubscribe token; we keep the hash (not the raw
-- token) so a leaked DB doesn't let attackers forge valid unsubscribe
-- requests on behalf of other recipients.
CREATE TABLE IF NOT EXISTS email_unsubscribes (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'all',
  token_hash TEXT DEFAULT '',
  source TEXT NOT NULL DEFAULT 'link',
  unsubscribed_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_unsubscribe_email ON email_unsubscribes(email);
CREATE INDEX IF NOT EXISTS idx_unsubscribe_scope ON email_unsubscribes(scope);
-- Lightweight key-value store for app-level secrets we don't want to
-- bake into the .env file (e.g. the unsubscribe-token HMAC secret,
-- which is auto-generated on first use and must survive restarts).
CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- ====================================================================
-- Tables added for user auth + Microsoft Graph integration
-- ====================================================================
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  csrf_token TEXT NOT NULL,
  ip TEXT DEFAULT '',
  user_agent TEXT DEFAULT '',
  expires_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS app_bootstrap (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  initialized INTEGER NOT NULL DEFAULT 0,
  last_admin_at TEXT DEFAULT ''
);
"""


class EmailStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_DDL)
            # Migration: the legacy DDL had `CREATE UNIQUE INDEX` on
            # `email_messages(sequence_id, step_number)`. The waterfall
            # feature intentionally creates multiple rows per (sequence,
            # step) — one per recipient attempt — so the unique index
            # must be downgraded. `CREATE INDEX IF NOT EXISTS` is a no-op
            # when the index already exists, so we have to drop first.
            legacy_unique = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' "
                "AND name='idx_email_message_sequence_step' "
                "AND sql LIKE '%UNIQUE%'"
            ).fetchone()
            if legacy_unique:
                conn.execute("DROP INDEX idx_email_message_sequence_step")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_email_message_sequence_step "
                    "ON email_messages(sequence_id, step_number)"
                )
            self._ensure_column(conn, "lead_email_sequences", "generation_mode", "TEXT NOT NULL DEFAULT 'personalized'")
            self._ensure_column(conn, "lead_email_sequences", "template_id", "TEXT DEFAULT ''")
            self._ensure_column(conn, "lead_email_sequences", "template_group", "TEXT DEFAULT ''")
            self._ensure_column(conn, "lead_email_sequences", "template_usage_index", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "lead_email_sequences", "template_max_send_count", "INTEGER NOT NULL DEFAULT 0")
            # Per-sequence outbound account. Set by campaign creation so a
            # single campaign's volume rotates across all connected
            # mailboxes; empty falls back to the campaign-level account.
            self._ensure_column(conn, "lead_email_sequences", "email_account_id", "TEXT NOT NULL DEFAULT ''")
            # Per-recipient role-based flag: marks shared inboxes
            # (info@/sales@/support@) so the scheduler can refuse to
            # send and advance the waterfall immediately.
            self._ensure_column(conn, "lead_email_recipients", "is_role_based", "INTEGER NOT NULL DEFAULT 0")
            # New columns for encrypted secrets + Graph account metadata
            self._ensure_column(conn, "email_accounts", "secrets_ciphertext", "BLOB DEFAULT X''")
            self._ensure_column(conn, "email_accounts", "graph_tenant_id", "TEXT DEFAULT ''")
            self._ensure_column(conn, "email_accounts", "graph_user_principal_name", "TEXT DEFAULT ''")
            # Manual rotation order set from the quotas page. Existing rows
            # default to 0; new rows are appended at MAX(sort_order)+1.
            self._ensure_column(conn, "email_accounts", "sort_order", "INTEGER NOT NULL DEFAULT 0")
            # Ensure the singleton app_bootstrap row exists
            conn.execute(
                "INSERT OR IGNORE INTO app_bootstrap (id, initialized, last_admin_at) VALUES (1, 0, '')"
            )
        # Idempotent migration: move any pre-existing plaintext secrets into the
        # encrypted blob so SMTP/IMAP keep working after the encryption column is added.
        self._migrate_plaintext_secrets_to_ciphertext()

    def _migrate_plaintext_secrets_to_ciphertext(self) -> None:
        """One-shot migration: any row that still has plaintext
        `smtp_secret_encrypted` / `imap_secret_encrypted` and an empty
        `secrets_ciphertext` is migrated in-place.
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, smtp_secret_encrypted, imap_secret_encrypted, secrets_ciphertext
                    FROM email_accounts
                    WHERE (smtp_secret_encrypted != '' OR imap_secret_encrypted != '')
                      AND (secrets_ciphertext IS NULL OR length(secrets_ciphertext) = 0)
                    """
                ).fetchall()
                for row in rows:
                    blob = secret_cipher.encrypt_dict(
                        {
                            "smtp_secret": str(row["smtp_secret_encrypted"] or ""),
                            "imap_secret": str(row["imap_secret_encrypted"] or ""),
                        }
                    )
                    conn.execute(
                        """
                        UPDATE email_accounts
                        SET secrets_ciphertext = ?,
                            smtp_secret_encrypted = '',
                            imap_secret_encrypted = '',
                            updated_at = updated_at
                        WHERE id = ?
                        """,
                        (blob, row["id"]),
                    )
                if rows:
                    logger.info("Migrated %d email_accounts rows to encrypted secrets_ciphertext", len(rows))
        except Exception:  # noqa: BLE001
            logger.exception("Failed to migrate plaintext secrets to encrypted ciphertext")

    def _ensure_column(self, conn: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name not in columns:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

    def upsert_account(self, payload: dict[str, Any]) -> None:
        cols = [
            "id", "provider_type", "from_name", "from_email", "reply_to",
            "smtp_host", "smtp_port", "smtp_username", "smtp_secret_encrypted",
            "imap_host", "imap_port", "imap_username", "imap_secret_encrypted",
            "use_tls", "status", "daily_send_limit", "hourly_send_limit",
            "last_test_at", "created_at", "updated_at",
            "secrets_ciphertext", "graph_tenant_id", "graph_user_principal_name",
            "sort_order",
        ]
        # Per-column defaults: every text column defaults to "" (matches the
        # existing convention) but sort_order needs an explicit int default
        # so MAX(sort_order) doesn't end up as the empty string.
        int_defaults = {"sort_order"}
        values: list[Any] = []
        for col in cols:
            if col == "sort_order" and not payload.get(col) and payload.get(col) != 0:
                values.append(0)
            elif col in int_defaults:
                values.append(int(payload.get(col) or 0))
            else:
                values.append(payload.get(col, ""))
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{col}=excluded.{col}" for col in cols[1:])
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO email_accounts ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                values,
            )

    def list_accounts(self) -> list[dict[str, Any]]:
        # Manual sort_order wins; created_at is the stable tiebreaker so
        # accounts that have never been reordered keep their original order.
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM email_accounts ORDER BY sort_order ASC, created_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def list_accounts_by_provider(self, provider_type: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM email_accounts WHERE provider_type = ? "
                "ORDER BY sort_order ASC, created_at ASC",
                (provider_type,),
            ).fetchall()
        return [dict(r) for r in rows]

    def reorder_accounts(self, account_ids: list[str]) -> None:
        """Rewrite `sort_order` so the i-th id in the list has sort_order=i.

        Atomic: either every id in the list gets a fresh index or none does.
        Accounts that the caller didn't include keep their current value;
        we only touch the rows that were sent. The caller is expected to
        include every account in the new order — passing a partial list will
        just leave the unmentioned rows at the tail of the rotation.
        """
        if not account_ids:
            return
        # De-dupe while preserving order (the caller already chose the order,
        # but defensive dedupe avoids a UNIQUE / PK clash if duplicates slip in).
        seen: set[str] = set()
        ordered: list[str] = []
        for raw in account_ids:
            aid = str(raw or "").strip()
            if not aid or aid in seen:
                continue
            seen.add(aid)
            ordered.append(aid)
        if not ordered:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            for idx, aid in enumerate(ordered):
                conn.execute(
                    "UPDATE email_accounts SET sort_order = ?, updated_at = ? WHERE id = ?",
                    (idx, now, aid),
                )

    def next_sort_order(self) -> int:
        """Return MAX(sort_order)+1 (or 0 if the table is empty).

        Coerce defensively because rows inserted before the column existed
        (or rows that the caller inserted without setting sort_order) may
        store it as an empty string after a round-trip through upsert.
        """
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(sort_order) AS m FROM email_accounts").fetchone()
        if not row or row["m"] is None or row["m"] == "":
            return 0
        try:
            return int(row["m"]) + 1
        except (TypeError, ValueError):
            return 0

    def delete_account(self, account_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM email_accounts WHERE id = ?", (account_id,))

    def set_account_secrets(self, account_id: str, secrets_dict: dict[str, Any]) -> None:
        """Encrypt `secrets_dict` and write into `email_accounts.secrets_ciphertext`.

        Pass an empty dict to clear.
        """
        blob = secret_cipher.encrypt_dict(secrets_dict) if secrets_dict else b""
        with self._connect() as conn:
            conn.execute(
                "UPDATE email_accounts SET secrets_ciphertext = ? WHERE id = ?",
                (blob, account_id),
            )

    def get_account_secrets(self, account_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT secrets_ciphertext FROM email_accounts WHERE id = ?", (account_id,)
            ).fetchone()
        if not row:
            return {}
        return secret_cipher.decrypt_dict(row["secrets_ciphertext"])

    def set_account_secret_value(self, account_id: str, key: str, value: str) -> None:
        """Update a single key inside the encrypted secrets blob (read-modify-write)."""
        if not key:
            return
        existing = self.get_account_secrets(account_id)
        if value:
            existing[key] = value
        else:
            existing.pop(key, None)
        self.set_account_secrets(account_id, existing)

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM email_accounts WHERE id = ?", (account_id,)).fetchone()
        return dict(row) if row else None

    def create_campaign(self, payload: dict[str, Any]) -> None:
        cols = list(payload.keys())
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO email_campaigns ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                [payload[c] for c in cols],
            )

    def get_campaign(self, campaign_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM email_campaigns WHERE id = ?", (campaign_id,)).fetchone()
        return dict(row) if row else None

    def list_campaigns_for_hunt(self, hunt_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM email_campaigns WHERE hunt_id = ? ORDER BY created_at DESC",
                (hunt_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_campaign_status(self, campaign_id: str, status: str, *, updated_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE email_campaigns SET status = ?, updated_at = ? WHERE id = ?",
                (status, updated_at, campaign_id),
            )

    def create_sequence(self, payload: dict[str, Any]) -> None:
        cols = list(payload.keys())
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO lead_email_sequences ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                [payload[c] for c in cols],
            )

    # --- waterfall recipient pool (per-sequence multi-email) ---------

    def add_recipients(
        self,
        sequence_id: str,
        emails: list[str],
        *,
        is_role_based_per_email: dict[str, bool] | None = None,
    ) -> list[dict[str, Any]]:
        """Insert one row per email into lead_email_recipients for the
        given sequence. Position is the list order (0 = first try).
        Returns the inserted rows.

        ``is_role_based_per_email`` (optional) maps an email address
        (lowercased) to its role-based flag. The scheduler uses that
        flag to skip shared inboxes (info@/sales@/...) and advance
        to the next named recipient. Missing keys default to False.

        Empty / dedup-safe: skipped duplicates within the same input
        and existing rows for the sequence.
        """
        seq_id = str(sequence_id or "").strip()
        if not seq_id:
            raise ValueError("sequence_id is required")
        flags = {str(k or "").strip().lower(): bool(v) for k, v in (is_role_based_per_email or {}).items()}
        norm: list[str] = []
        seen: set[str] = set()
        for raw in emails or []:
            e = (raw or "").strip().lower()
            if not e or e in seen:
                continue
            seen.add(e)
            norm.append(e)
        if not norm:
            return []
        now = datetime.now(timezone.utc).isoformat()
        inserted: list[dict[str, Any]] = []
        with self._connect() as conn:
            existing = {
                row["email"]
                for row in conn.execute(
                    "SELECT email FROM lead_email_recipients WHERE sequence_id = ?",
                    (seq_id,),
                ).fetchall()
            }
            for pos, email in enumerate(norm):
                if email in existing:
                    continue
                row_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO lead_email_recipients "
                    "(id, sequence_id, email, position, status, is_role_based, "
                    " created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
                    (
                        row_id,
                        seq_id,
                        email,
                        pos,
                        1 if flags.get(email) else 0,
                        now,
                        now,
                    ),
                )
                inserted.append(
                    {
                        "id": row_id,
                        "sequence_id": seq_id,
                        "email": email,
                        "position": pos,
                        "status": "pending",
                    }
                )
        return inserted

    def list_recipients(self, sequence_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM lead_email_recipients "
                "WHERE sequence_id = ? ORDER BY position ASC, created_at ASC",
                (sequence_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def next_pending_recipient(self, sequence_id: str) -> dict[str, Any] | None:
        """Return the lowest-position pending recipient, or None.

        If a recipient is `waiting_reply` (already sent) and we have
        no other `pending` ones, we DON'T pick the waiting one again
        — the scheduler will time-out that one and flip it to
        `skipped` before continuing. This is the "waterfall" loop.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM lead_email_recipients "
                "WHERE sequence_id = ? AND status = 'pending' "
                "ORDER BY position ASC, created_at ASC LIMIT 1",
                (sequence_id,),
            ).fetchone()
        return dict(row) if row else None

    def mark_recipient_sent(
        self, recipient_id: str, *, sent_at: str
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE lead_email_recipients "
                "SET status = 'waiting_reply', sent_at = ?, last_attempt_at = ?, updated_at = ? "
                "WHERE id = ?",
                (sent_at, sent_at, sent_at, recipient_id),
            )

    def mark_recipient_failed(
        self, recipient_id: str, *, reason: str, updated_at: str
    ) -> None:
        """A send failed (transient or permanent). Park the recipient
        in `failed` so we don't retry it on the same day. The scheduler
        will pick the next pending one (or skip if this was the last)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE lead_email_recipients "
                "SET status = 'failed', failure_reason = ?, last_attempt_at = ?, updated_at = ? "
                "WHERE id = ?",
                (reason, updated_at, updated_at, recipient_id),
            )

    def mark_recipient_skipped(
        self, recipient_id: str, *, reason: str, updated_at: str
    ) -> None:
        """A recipient was rejected before any send attempt (e.g. a
        role-based shared inbox). Park the recipient in ``skipped`` so
        the waterfall can advance to the next candidate without
        waiting for the 3-day timeout. ``reason`` is recorded for
        audit (``recipient_role_based`` / ``recipient_unsubscribed``
        / etc.) so the UI can surface *why* the address was skipped.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE lead_email_recipients "
                "SET status = 'skipped', failure_reason = ?, last_attempt_at = ?, updated_at = ? "
                "WHERE id = ?",
                (reason, updated_at, updated_at, recipient_id),
            )

    def advance_waiting_recipient(
        self, recipient_id: str, *, updated_at: str
    ) -> None:
        """A waiting_reply recipient has been waiting too long with no
        reply — mark it `skipped` so the scheduler moves to the next."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE lead_email_recipients "
                "SET status = 'skipped', updated_at = ? WHERE id = ?",
                (updated_at, recipient_id),
            )

    def mark_recipient_replied(
        self, recipient_id: str, *, replied_at: str
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE lead_email_recipients "
                "SET status = 'replied', replied_at = ?, updated_at = ? WHERE id = ?",
                (replied_at, replied_at, recipient_id),
            )

    def find_recipient_by_email(
        self, sequence_id: str, email: str
    ) -> dict[str, Any] | None:
        """Look up a recipient row by email (for reply-detector)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM lead_email_recipients "
                "WHERE sequence_id = ? AND email = ? LIMIT 1",
                (sequence_id, (email or "").strip().lower()),
            ).fetchone()
        return dict(row) if row else None

    def waiting_recipients_older_than(
        self, threshold_iso: str
    ) -> list[dict[str, Any]]:
        """Return all waiting_reply recipients whose ``sent_at`` is
        before the threshold — these are due to be flipped to ``skipped``
        (waterfall window elapsed, no reply).

        We use ``sent_at`` (not ``last_attempt_at``) because the
        waterfall window measures "time since we sent" — failed
        recipients have a stale ``last_attempt_at`` but no actual
        delivery to time out from, and they're already retired via
        ``mark_recipient_failed``.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM lead_email_recipients "
                "WHERE status = 'waiting_reply' "
                "AND sent_at != '' AND sent_at < ? "
                "ORDER BY sent_at ASC",
                (threshold_iso,),
            ).fetchall()
        return [dict(r) for r in rows]

    def is_sequence_exhausted(self, sequence_id: str) -> bool:
        """True when no `pending` or `waiting_reply` rows remain
        (i.e. all recipients have been sent, replied, or skipped)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT "
                "  SUM(CASE WHEN status IN ('pending','waiting_reply') THEN 1 ELSE 0 END) AS active "
                "FROM lead_email_recipients WHERE sequence_id = ?",
                (sequence_id,),
            ).fetchone()
        return not (row and (row["active"] or 0))

    def get_sequence(self, sequence_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM lead_email_sequences WHERE id = ?", (sequence_id,)).fetchone()
        return dict(row) if row else None

    def list_sequences_for_campaign(self, campaign_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM lead_email_sequences WHERE campaign_id = ? ORDER BY created_at ASC",
                (campaign_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def has_contact_history_for_lead_key(self, lead_key: str) -> bool:
        """Return whether a lead/email pair was already queued or contacted before.

        Purely failed sequences with no sent messages do not block retry.
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM lead_email_sequences seq
                LEFT JOIN email_messages msg
                  ON msg.sequence_id = seq.id
                 AND msg.status = 'sent'
                WHERE seq.lead_key = ?
                  AND (
                    seq.status != 'failed'
                    OR msg.id IS NOT NULL
                  )
                LIMIT 1
                """,
                (lead_key,),
            ).fetchone()
        return row is not None

    def update_sequence_status(
        self,
        sequence_id: str,
        *,
        status: str,
        updated_at: str,
        current_step: int | None = None,
        stop_reason: str | None = None,
        replied_at: str | None = None,
        last_sent_at: str | None = None,
        next_scheduled_at: str | None = None,
    ) -> None:
        fields = ["status = ?", "updated_at = ?"]
        values: list[Any] = [status, updated_at]
        for name, value in [
            ("current_step", current_step),
            ("stop_reason", stop_reason),
            ("replied_at", replied_at),
            ("last_sent_at", last_sent_at),
            ("next_scheduled_at", next_scheduled_at),
        ]:
            if value is not None:
                fields.append(f"{name} = ?")
                values.append(value)
        values.append(sequence_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE lead_email_sequences SET {', '.join(fields)} WHERE id = ?",
                values,
            )

    def update_sequence_lead_email(
        self, sequence_id: str, *, lead_email: str, updated_at: str
    ) -> None:
        """Set the sequence's primary recipient. Used by the waterfall
        to point step 1 (and any subsequent steps) at the new
        recipient after a previous one was skipped."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE lead_email_sequences SET lead_email = ?, updated_at = ? "
                "WHERE id = ?",
                (lead_email, updated_at, sequence_id),
            )

    def clone_pending_message_after(
        self, source_message_id: str, *, scheduled_at: str
    ) -> str | None:
        """Copy a sent/failed message into a fresh pending row for the
        same step but the sequence's CURRENT lead_email (which the
        caller has already updated to the next recipient). Returns
        the new message id, or None if the source is missing.

        Used by the waterfall advancement: the previous message stays
        as `sent` (history), and a new `pending` row is created so
        the scheduler picks it up on the next pass.
        """
        with self._connect() as conn:
            src = conn.execute(
                "SELECT * FROM email_messages WHERE id = ?",
                (source_message_id,),
            ).fetchone()
            if not src:
                return None
            new_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO email_messages "
                "(id, sequence_id, step_number, goal, locale, subject, body_text, "
                " status, scheduled_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (
                    new_id,
                    src["sequence_id"],
                    src["step_number"],
                    src["goal"],
                    src["locale"],
                    src["subject"],
                    src["body_text"],
                    scheduled_at,
                    scheduled_at,
                    scheduled_at,
                ),
            )
        return new_id

    def latest_message_for_sequence(self, sequence_id: str) -> dict[str, Any] | None:
        """Return the most-recently-created message for the sequence,
        regardless of status. Used by the waterfall to find the row
        that was sent to the now-skipped recipient (so we can clone
        it for the next recipient)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM email_messages WHERE sequence_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (sequence_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_message(self, payload: dict[str, Any]) -> None:
        cols = list(payload.keys())
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO email_messages ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                [payload[c] for c in cols],
            )

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM email_messages WHERE id = ?", (message_id,)).fetchone()
        return dict(row) if row else None

    def find_message_by_provider_message_id(self, provider_message_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM email_messages WHERE provider_message_id = ? LIMIT 1",
                (provider_message_id,),
            ).fetchone()
        return dict(row) if row else None

    def find_message_by_thread_key(self, thread_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM email_messages WHERE thread_key = ? AND status = 'sent' "
                "ORDER BY sent_at DESC LIMIT 1",
                (thread_key,),
            ).fetchone()
        return dict(row) if row else None

    def get_message_for_step(self, sequence_id: str, step_number: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM email_messages WHERE sequence_id = ? AND step_number = ?",
                (sequence_id, step_number),
            ).fetchone()
        return dict(row) if row else None

    def list_messages_for_sequence(self, sequence_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM email_messages WHERE sequence_id = ? ORDER BY step_number ASC",
                (sequence_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_pending_messages_ready(self, now_iso: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM email_messages WHERE status = 'pending' AND scheduled_at <= ? "
                "ORDER BY scheduled_at ASC LIMIT ?",
                (now_iso, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_message_sent(self, message_id: str, *, provider_message_id: str, thread_key: str, sent_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE email_messages SET status = 'sent', provider_message_id = ?, thread_key = ?, sent_at = ?, updated_at = ? WHERE id = ?",
                (provider_message_id, thread_key, sent_at, sent_at, message_id),
            )

    def mark_message_failed(self, message_id: str, *, failure_reason: str, updated_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE email_messages SET status = 'failed', failure_reason = ?, updated_at = ? WHERE id = ?",
                (failure_reason, updated_at, message_id),
            )

    def cancel_future_pending_messages(self, sequence_id: str, *, updated_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE email_messages SET status = 'cancelled', updated_at = ? WHERE sequence_id = ? AND status = 'pending'",
                (updated_at, sequence_id),
            )

    def count_messages_for_campaign(self, campaign_id: str, *, status: str | None = None) -> int:
        query = (
            "SELECT COUNT(*) FROM email_messages m "
            "JOIN lead_email_sequences s ON s.id = m.sequence_id "
            "WHERE s.campaign_id = ?"
        )
        params: list[Any] = [campaign_id]
        if status:
            query += " AND m.status = ?"
            params.append(status)
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row[0]) if row else 0

    def count_messages_by_status(self, status: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM email_messages WHERE status = ?",
                (status,),
            ).fetchone()
        return int(row[0]) if row else 0

    def count_sent_today_for_account(self, account_id: str, *, now_iso: str) -> int:
        """Count outbound traffic on this account since 00:00 UTC today.

        Folds in both production sends (via email_messages → campaign
        join) and test sends (via email_test_send_log). The UI's
        "今日 / 上限" bar and the scheduler's per-account daily cap
        both call this, so test sends correctly burn through the
        same quota — important for shared mailboxes where you don't
        want a flood of test-sends to eat the real campaign budget.
        """
        try:
            now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        except ValueError:
            now = datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        start_of_day = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        start_iso = start_of_day.isoformat()
        with self._connect() as conn:
            prod_row = conn.execute(
                """
                SELECT COUNT(*)
                FROM email_messages m
                JOIN lead_email_sequences s ON s.id = m.sequence_id
                JOIN email_campaigns c ON c.id = s.campaign_id
                WHERE COALESCE(NULLIF(s.email_account_id, ''), c.email_account_id) = ?
                  AND m.status = 'sent'
                  AND m.sent_at >= ?
                """,
                (account_id, start_iso),
            ).fetchone()
            test_row = conn.execute(
                """
                SELECT COUNT(*)
                FROM email_test_send_log
                WHERE account_id = ?
                  AND sent_at >= ?
                  AND ok = 1
                """,
                (account_id, start_iso),
            ).fetchone()
        return int(prod_row[0] if prod_row else 0) + int(test_row[0] if test_row else 0)

    def sent_today_by_account(self, *, now_iso: str) -> dict[str, int]:
        """Per-account sent-today map (prod sends + test sends).

        Uses the same effective-account rule as
        `count_sent_today_for_account`: a sequence-level
        `email_account_id` wins over the campaign-level one.
        """
        try:
            now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        except ValueError:
            now = datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        start_iso = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        counts: dict[str, int] = {}
        with self._connect() as conn:
            for row in conn.execute(
                """
                SELECT COALESCE(NULLIF(s.email_account_id, ''), c.email_account_id) AS acct,
                       COUNT(*) AS c
                FROM email_messages m
                JOIN lead_email_sequences s ON s.id = m.sequence_id
                JOIN email_campaigns c ON c.id = s.campaign_id
                WHERE m.status = 'sent' AND m.sent_at >= ?
                GROUP BY acct
                """,
                (start_iso,),
            ).fetchall():
                counts[str(row["acct"])] = int(row["c"])
            for row in conn.execute(
                """
                SELECT account_id, COUNT(*) AS c
                FROM email_test_send_log
                WHERE sent_at >= ? AND ok = 1
                GROUP BY account_id
                """,
                (start_iso,),
            ).fetchall():
                key = str(row["account_id"])
                counts[key] = counts.get(key, 0) + int(row["c"])
        return counts

    def rebind_sequence_account(self, sequence_id: str, *, email_account_id: str, updated_at: str) -> None:
        """Point a sequence at a different outbound account (quota rotation)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE lead_email_sequences SET email_account_id = ?, updated_at = ? WHERE id = ?",
                (email_account_id, updated_at, sequence_id),
            )

    def record_test_send(
        self,
        *,
        account_id: str,
        to_email: str,
        subject: str,
        body_text: str,
        provider: str,
        provider_message_id: str,
        thread_key: str,
        ok: bool,
        failure_reason: str,
        sent_at: str,
    ) -> None:
        """Insert a row into `email_test_send_log` after a test-send attempt.

        Called from the test-send endpoint so the daily quota counter
        picks it up. `ok=False` rows are still recorded (so the user
        can see the failure in audit) but the count helper ignores
        them via `WHERE ok = 1` — only successful test sends should
        burn through the daily limit.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO email_test_send_log
                  (id, account_id, to_email, subject, body_text, provider,
                   provider_message_id, thread_key, ok, failure_reason,
                   sent_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    account_id,
                    to_email,
                    subject,
                    body_text,
                    provider,
                    provider_message_id,
                    thread_key,
                    1 if ok else 0,
                    failure_reason,
                    sent_at,
                    sent_at,
                ),
            )

    def count_sequences_by_status(self, *statuses: str) -> int:
        if not statuses:
            return 0
        placeholders = ", ".join("?" for _ in statuses)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM lead_email_sequences WHERE status IN ({placeholders})",
                list(statuses),
            ).fetchone()
        return int(row[0]) if row else 0

    def count_campaigns_by_status(self, *statuses: str) -> int:
        if not statuses:
            return 0
        placeholders = ", ".join("?" for _ in statuses)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM email_campaigns WHERE status IN ({placeholders})",
                list(statuses),
            ).fetchone()
        return int(row[0]) if row else 0

    def count_messages_since(self, status: str, *, since_iso: str, time_field: str = "updated_at") -> int:
        if time_field not in {"created_at", "updated_at", "scheduled_at", "sent_at"}:
            raise ValueError("Unsupported time field")
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM email_messages WHERE status = ? AND {time_field} >= ?",
                (status, since_iso),
            ).fetchone()
        return int(row[0]) if row else 0

    def find_sent_message_by_lead_email_and_subject(self, lead_email: str, subject: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT m.* FROM email_messages m "
                "JOIN lead_email_sequences s ON s.id = m.sequence_id "
                "WHERE m.status = 'sent' AND lower(s.lead_email) = lower(?) AND lower(m.subject) = lower(?) "
                "ORDER BY m.sent_at DESC LIMIT 1",
                (lead_email, subject),
            ).fetchone()
        return dict(row) if row else None

    def has_reply_event(self, raw_ref: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM email_reply_events WHERE raw_ref = ? LIMIT 1",
                (raw_ref,),
            ).fetchone()
        return row is not None

    def create_reply_event(self, payload: dict[str, Any]) -> None:
        cols = list(payload.keys())
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO email_reply_events ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                [payload[c] for c in cols],
            )

    def list_reply_events_for_sequence(self, sequence_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM email_reply_events WHERE sequence_id = ? ORDER BY received_at DESC",
                (sequence_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_reply_events_since(self, since_iso: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM email_reply_events WHERE received_at >= ?",
                (since_iso,),
            ).fetchone()
        return int(row[0]) if row else 0

    def list_recent_message_failures(self, *, since_iso: str, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT m.subject, m.failure_reason, m.updated_at, s.lead_email
                FROM email_messages m
                JOIN lead_email_sequences s ON s.id = m.sequence_id
                WHERE m.status = 'failed' AND m.updated_at >= ?
                ORDER BY m.updated_at DESC
                LIMIT ?
                """,
                (since_iso, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_sent_messages_since(self, *, since_iso: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                  m.id,
                  m.subject,
                  m.sent_at,
                  s.lead_email,
                  s.lead_name,
                  s.hunt_id,
                  s.campaign_id
                FROM email_messages m
                JOIN lead_email_sequences s ON s.id = m.sequence_id
                WHERE m.status = 'sent' AND m.sent_at >= ?
                ORDER BY m.sent_at ASC, m.id ASC
                LIMIT ?
                """,
                (since_iso, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_reply_events_since(self, *, since_iso: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                  r.id,
                  r.from_email,
                  r.subject,
                  r.snippet,
                  r.received_at,
                  s.lead_name,
                  s.hunt_id,
                  s.campaign_id
                FROM email_reply_events r
                JOIN lead_email_sequences s ON s.id = r.sequence_id
                WHERE r.received_at >= ?
                ORDER BY r.received_at DESC, r.id DESC
                LIMIT ?
                """,
                (since_iso, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_message_failure_reasons(self, *, since_iso: str, limit: int = 5) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT failure_reason, COUNT(*) AS count
                FROM email_messages
                WHERE status = 'failed' AND updated_at >= ?
                GROUP BY failure_reason
                ORDER BY count DESC, failure_reason ASC
                LIMIT ?
                """,
                (since_iso, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_template_performance_for_campaign(
        self,
        campaign_id: str,
        *,
        underperforming_min_assigned: int = 10,
        underperforming_min_reply_rate: float = 1.0,
    ) -> dict[str, dict[str, Any]]:
        sequences = [seq for seq in self.list_sequences_for_campaign(campaign_id) if seq.get("template_id")]
        if not sequences:
            return {}

        template_summary: dict[str, dict[str, Any]] = {}
        with self._connect() as conn:
            # One GROUP BY instead of one COUNT per sequence — this method
            # runs on every campaign-summary poll, so the old per-sequence
            # query turned a 200-lead campaign into 200+ round trips.
            sent_by_sequence: dict[str, int] = {}
            sequence_ids = [str(seq["id"]) for seq in sequences]
            for chunk_start in range(0, len(sequence_ids), 500):
                chunk = sequence_ids[chunk_start:chunk_start + 500]
                placeholders = ", ".join("?" for _ in chunk)
                for row in conn.execute(
                    f"SELECT sequence_id, COUNT(*) AS c FROM email_messages "
                    f"WHERE status = 'sent' AND sequence_id IN ({placeholders}) "
                    f"GROUP BY sequence_id",
                    chunk,
                ).fetchall():
                    sent_by_sequence[str(row["sequence_id"])] = int(row["c"])

            for sequence in sequences:
                template_id = str(sequence.get("template_id") or "")
                if not template_id:
                    continue
                summary = template_summary.setdefault(
                    template_id,
                    {
                        "template_id": template_id,
                        "template_group": str(sequence.get("template_group") or ""),
                        "generation_mode": str(sequence.get("generation_mode") or "template_pool"),
                        "assigned_count": 0,
                        "max_send_count": int(sequence.get("template_max_send_count") or 0),
                        "sent_count": 0,
                        "replied_count": 0,
                        "reply_rate": 0.0,
                        "remaining_capacity": 0,
                        "status": "warming_up",
                        "optimization_needed": False,
                        "recommended_action": "keep_collecting_data",
                        "reason": "Not enough delivery/reply data yet.",
                    },
                )
                summary["assigned_count"] += 1
                if sequence.get("status") == "replied":
                    summary["replied_count"] += 1
                summary["sent_count"] += sent_by_sequence.get(str(sequence["id"]), 0)

        for summary in template_summary.values():
            assigned = int(summary["assigned_count"])
            replied = int(summary["replied_count"])
            max_send_count = int(summary["max_send_count"])
            summary["reply_rate"] = round((replied / assigned) * 100, 2) if assigned else 0.0
            summary["remaining_capacity"] = max(max_send_count - assigned, 0)
            if max_send_count and assigned >= max_send_count:
                summary["status"] = "exhausted"
                summary["optimization_needed"] = True
                summary["recommended_action"] = "create_new_template_version"
                summary["reason"] = "Template reached the configured assignment cap."
            elif assigned >= underperforming_min_assigned and summary["reply_rate"] < underperforming_min_reply_rate:
                summary["status"] = "underperforming"
                summary["optimization_needed"] = True
                summary["recommended_action"] = "optimize_template_before_more_sends"
                summary["reason"] = (
                    f"Reply rate {summary['reply_rate']}% is below the threshold "
                    f"{underperforming_min_reply_rate}% after {assigned} assignments."
                )
            else:
                summary["status"] = "warming_up"
                summary["optimization_needed"] = False
                summary["recommended_action"] = "keep_collecting_data"
                summary["reason"] = "Continue sending until enough reply data accumulates."
        return template_summary

    # ====================================================================
    # User auth (users / sessions / app_bootstrap) — single-tenant login
    # ====================================================================

    def count_users(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return int(row[0]) if row else 0

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, email, password_hash, role, created_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, email, password_hash, role, created_at FROM users WHERE lower(email) = lower(?)",
                (email,),
            ).fetchone()
        return dict(row) if row else None

    def create_user(self, *, email: str, password_hash: str, role: str, created_at: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                (email, password_hash, role, created_at),
            )
            return int(cur.lastrowid)

    def create_session(
        self,
        *,
        session_id: str,
        user_id: int,
        csrf_token: str,
        ip: str,
        user_agent: str,
        expires_at: str,
        last_seen_at: str,
        created_at: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions
                  (id, user_id, csrf_token, ip, user_agent, expires_at, last_seen_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, user_id, csrf_token, ip, user_agent, expires_at, last_seen_at, created_at),
            )

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT s.id, s.user_id, s.csrf_token, s.ip, s.user_agent, s.expires_at, s.last_seen_at, s.created_at,
                       u.email AS user_email, u.role AS user_role
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.id = ?
                """,
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def touch_session(self, session_id: str, last_seen_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE id = ?",
                (last_seen_at, session_id),
            )

    def delete_session(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def purge_expired_sessions(self, now_iso: str) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso,))
            return int(cur.rowcount or 0)

    # --- app_bootstrap singleton ----------------------------------------

    # --- unsubscribe records --------------------------------------------

    def record_unsubscribe(
        self,
        *,
        email: str,
        scope: str = "all",
        token_hash: str = "",
        source: str = "link",
    ) -> str:
        """Record that ``email`` has unsubscribed.

        Idempotent on (email, scope) — re-clicking the link is a no-op.
        Returns the unsubscribe row id.
        """
        import secrets as _secrets

        email_norm = (email or "").strip().lower()
        scope_norm = (scope or "all").strip() or "all"
        if not email_norm:
            raise ValueError("email is required")
        now = datetime.now(timezone.utc).isoformat()
        row_id = _secrets.token_urlsafe(16)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM email_unsubscribes WHERE email = ? AND scope = ?",
                (email_norm, scope_norm),
            ).fetchone()
            if existing:
                return str(existing["id"])
            conn.execute(
                "INSERT INTO email_unsubscribes "
                "(id, email, scope, token_hash, source, unsubscribed_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row_id, email_norm, scope_norm, token_hash, source, now, now),
            )
        return row_id

    def is_unsubscribed(
        self,
        email: str,
        *,
        scope: str | None = None,
    ) -> bool:
        """Return True if the recipient is blocked from receiving future mail.

        The check matches BOTH a global 'all' block (which overrides any
        finer scope) AND a block whose scope equals the requested
        ``scope`` (e.g. ``'campaign:abc'`` when sending for campaign abc).
        Pass ``scope=None`` to check only the global block.
        """
        email_norm = (email or "").strip().lower()
        if not email_norm:
            return False
        with self._connect() as conn:
            # Global block wins regardless of scope.
            row = conn.execute(
                "SELECT 1 FROM email_unsubscribes WHERE email = ? AND scope = 'all' LIMIT 1",
                (email_norm,),
            ).fetchone()
            if row:
                return True
            if scope:
                row = conn.execute(
                    "SELECT 1 FROM email_unsubscribes WHERE email = ? AND scope = ? LIMIT 1",
                    (email_norm, scope),
                ).fetchone()
                if row:
                    return True
        return False

    def list_unsubscribes(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Return recent unsubscribe records (most recent first)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM email_unsubscribes ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    # --- app_settings (key-value, app-level secrets) ------------------

    def get_app_setting(self, key: str, default: str = "") -> str:
        """Read a string value from the `app_settings` table.

        Returns `default` when the key is missing.
        """
        if not key:
            return default
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (key,),
            ).fetchone()
        if not row:
            return default
        return str(row["value"] or "")

    def set_app_setting(self, key: str, value: str) -> None:
        """Upsert a key-value pair in `app_settings`."""
        if not key:
            raise ValueError("key is required")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, now),
            )

    def is_signup_open(self) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT initialized FROM app_bootstrap WHERE id = 1").fetchone()
        if not row:
            return True  # bootstrap row missing → still open
        return int(row["initialized"] or 0) == 0

    def mark_signup_closed(self, *, last_admin_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE app_bootstrap SET initialized = 1, last_admin_at = ? WHERE id = 1",
                (last_admin_at,),
            )


# ---------------------------------------------------------------------------
# Module-level singleton accessor (breaks circular import between api.app and
# route modules that need the same store instance).
# ---------------------------------------------------------------------------

_email_store_singleton: EmailStore | None = None


def get_email_store() -> EmailStore:
    """Return the long-lived EmailStore singleton (creates one on first call)."""
    global _email_store_singleton
    if _email_store_singleton is None:
        # Late import to avoid pulling settings at module-import time.
        from config.settings import get_settings
        settings = get_settings()
        store = EmailStore(settings.email_db_path)
        store.init_db()
        _email_store_singleton = store
    return _email_store_singleton


def set_email_store(store: EmailStore) -> None:
    """Override the singleton (used by FastAPI lifespan in api.app)."""
    global _email_store_singleton
    _email_store_singleton = store
