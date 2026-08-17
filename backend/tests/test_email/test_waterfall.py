"""Tests for the multi-email waterfall recipient pool.

A sequence can have 1..N candidate emails. The scheduler picks the
next ``pending`` recipient, sends, marks it ``waiting_reply``. After
``email_recipient_waterfall_days`` with no reply, the recipient is
flipped to ``skipped`` and the scheduler advances to the next pending
one. A reply on ANY recipient stops the whole sequence.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from emailing.scheduler import run_scheduler_once
from emailing.store import EmailStore
from config.settings import get_settings


@pytest.fixture
def store() -> EmailStore:
    s = EmailStore(get_settings().email_db_path)
    s.init_db()
    return s


def _seed_full_setup(
    store: EmailStore,
    *,
    seq_id: str = "seq-waterfall",
    camp_id: str = "camp-waterfall",
    hunt_id: str = "hunt-waterfall",
    acc_id: str = "acc-waterfall",
) -> str:
    """Insert a minimal campaign+sequence+account and return the
    primary ``lead_email`` to use (so legacy single-recipient
    sequences still work in tests)."""
    now = datetime.now(timezone.utc).isoformat()
    with store._connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO email_accounts
            (id, provider_type, from_name, from_email, reply_to, smtp_host, smtp_port,
             smtp_username, smtp_secret_encrypted, imap_host, imap_port, imap_username,
             imap_secret_encrypted, use_tls, status, daily_send_limit, hourly_send_limit,
             last_test_at, created_at, updated_at)
            VALUES (?, 'smtp', 'Sales', 'sales@test.com', '', 'smtp.test.com', 587,
                    'u', 'pw', '', 993, '', '', 1, 'active', 100, 100, '', ?, ?)""",
            (acc_id, now, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO email_campaigns "
            "(id, hunt_id, email_account_id, name, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?)",
            (camp_id, hunt_id, acc_id, "Test", now, now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO lead_email_sequences "
            "(id, campaign_id, hunt_id, lead_key, lead_email, lead_name, status, "
            " current_step, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'running', 1, ?, ?)",
            (seq_id, camp_id, hunt_id, "lead-1", "primary@test.com", "Test", now, now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO email_messages "
            "(id, sequence_id, step_number, goal, locale, subject, body_text, status, "
            " scheduled_at, created_at, updated_at) "
            "VALUES (?, ?, 1, 'intro', 'en', 'Hi', 'Hello', 'pending', ?, ?, ?)",
            (f"msg-{seq_id}", seq_id, now, now, now),
        )
    return "primary@test.com"


def _cleanup(store: EmailStore, *seq_ids: str) -> None:
    with sqlite3.connect(store.db_path) as conn:
        for seq_id in seq_ids:
            conn.execute("DELETE FROM email_messages WHERE sequence_id = ?", (seq_id,))
            conn.execute(
                "DELETE FROM lead_email_recipients WHERE sequence_id = ?", (seq_id,)
            )
            conn.execute("DELETE FROM lead_email_sequences WHERE id = ?", (seq_id,))
        conn.execute("DELETE FROM email_campaigns WHERE id = 'camp-waterfall'")
        conn.execute("DELETE FROM email_accounts WHERE id = 'acc-waterfall'")
        conn.commit()


def test_add_recipients_creates_pending_rows(store: EmailStore) -> None:
    seq_id = "seq-ar-1"
    _seed_full_setup(store, seq_id=seq_id)
    try:
        added = store.add_recipients(seq_id, ["a@test.com", "B@Test.com", "a@test.com"])
        assert len(added) == 2  # 3 input → 2 unique (case + dup)
        rows = store.list_recipients(seq_id)
        assert len(rows) == 2
        assert rows[0]["email"] == "a@test.com"  # lowercased
        assert rows[0]["position"] == 0
        assert rows[1]["email"] == "b@test.com"
        assert rows[1]["position"] == 1
        assert all(r["status"] == "pending" for r in rows)
    finally:
        _cleanup(store, seq_id)


def test_next_pending_returns_lowest_position(store: EmailStore) -> None:
    seq_id = "seq-np-1"
    _seed_full_setup(store, seq_id=seq_id)
    try:
        store.add_recipients(seq_id, ["first@test.com", "second@test.com"])
        nxt = store.next_pending_recipient(seq_id)
        assert nxt is not None
        assert nxt["email"] == "first@test.com"
    finally:
        _cleanup(store, seq_id)


def test_scheduler_uses_pooled_recipient(store: EmailStore) -> None:
    """When a sequence has a pool, the scheduler should send to the
    first pending recipient (not sequence.lead_email)."""
    seq_id = "seq-pool-1"
    _seed_full_setup(store, seq_id=seq_id)
    try:
        # Pool with 2 candidates; the lead_email is "primary@test.com"
        # (legacy field) but we expect the scheduler to use the pool.
        store.add_recipients(seq_id, ["pool-a@test.com", "pool-b@test.com"])
        sent_to: list[str] = []

        async def fake_send(*args, **kwargs):
            sent_to.append(kwargs.get("to_email", ""))
            return {
                "ok": True,
                "provider": "smtp",
                "provider_message_id": "x",
                "thread_key": "y",
                "sent_at": "",
                "error": "",
                "error_type": "",
            }

        result = asyncio.run(run_scheduler_once(store, sender=fake_send))
        assert result["sent"] == 1
        assert sent_to == ["pool-a@test.com"]

        rows = store.list_recipients(seq_id)
        assert rows[0]["status"] == "waiting_reply"
        assert rows[0]["sent_at"] != ""

        # The sequence is NOT yet exhausted (1 waiting + 1 pending remain)
        assert not store.is_sequence_exhausted(seq_id)
    finally:
        _cleanup(store, seq_id)


def test_scheduler_advances_after_waterfall_window(store: EmailStore) -> None:
    """A waiting_reply recipient past the window flips to skipped,
    and the next pending becomes the active recipient."""
    seq_id = "seq-adv-1"
    _seed_full_setup(store, seq_id=seq_id)
    try:
        # Force waterfall window = 1 day for speed
        s = get_settings()
        s.email_recipient_waterfall_days = 1
        try:
            store.add_recipients(seq_id, ["first@test.com", "second@test.com"])
            sent_to: list[str] = []

            async def fake_send(*args, **kwargs):
                sent_to.append(kwargs.get("to_email", ""))
                return {
                    "ok": True,
                    "provider": "smtp",
                    "provider_message_id": "x",
                    "thread_key": "y",
                    "sent_at": "",
                    "error": "",
                    "error_type": "",
                }

            # Pass 1: send to first@test.com
            asyncio.run(run_scheduler_once(store, sender=fake_send))
            assert sent_to == ["first@test.com"]

            # Manually rewind the recipient's sent_at so it counts as
            # past the 1-day waterfall window.
            past = (
                datetime.now(timezone.utc) - timedelta(days=2)
            ).isoformat()
            with store._connect() as conn:
                conn.execute(
                    "UPDATE lead_email_recipients SET sent_at = ? WHERE sequence_id = ?",
                    (past, seq_id),
                )

            # Pass 2: scheduler should advance first → skipped, then
            # send to second@test.com.
            sent_to.clear()
            asyncio.run(run_scheduler_once(store, sender=fake_send))
            assert sent_to == ["second@test.com"], f"expected second, got {sent_to}"

            rows = {r["email"]: r for r in store.list_recipients(seq_id)}
            assert rows["first@test.com"]["status"] == "skipped"
            assert rows["second@test.com"]["status"] == "waiting_reply"
        finally:
            s.email_recipient_waterfall_days = 3  # restore default
    finally:
        _cleanup(store, seq_id)


def test_scheduler_marks_sequence_exhausted_when_pool_empty(store: EmailStore) -> None:
    """When the last recipient's waterfall window elapses and the
    advance logic finds no next pending recipient, the sequence flips
    to ``exhausted`` so the UI surfaces it correctly (vs. leaving it
    in a stale ``running`` state forever)."""
    seq_id = "seq-exh-1"
    _seed_full_setup(store, seq_id=seq_id)
    try:
        s = get_settings()
        s.email_recipient_waterfall_days = 1
        try:
            # Single recipient — will be the only one to ever try.
            store.add_recipients(seq_id, ["only@test.com"])
            sent_to: list[str] = []

            async def fake_send(*args, **kwargs):
                sent_to.append(kwargs.get("to_email", ""))
                return {
                    "ok": True,
                    "provider": "smtp",
                    "provider_message_id": "x",
                    "thread_key": "y",
                    "sent_at": "",
                    "error": "",
                    "error_type": "",
                }

            # Pass 1: send. After this, the recipient is `waiting_reply`
            # and the sequence is `running` (we still hope for a reply).
            asyncio.run(run_scheduler_once(store, sender=fake_send))
            assert sent_to == ["only@test.com"]
            seq = store.get_sequence(seq_id)
            assert seq["status"] == "running"
            assert seq["stop_reason"] in (None, "")

            # Rewind the recipient's sent_at past the 1-day window so
            # the next scheduler pass triggers advance + exhaustion.
            past = (
                datetime.now(timezone.utc) - timedelta(days=2)
            ).isoformat()
            with store._connect() as conn:
                conn.execute(
                    "UPDATE lead_email_recipients SET sent_at = ? WHERE sequence_id = ?",
                    (past, seq_id),
                )

            # Pass 2: advance flips only→skipped, finds no next
            # pending, marks the sequence exhausted.
            asyncio.run(run_scheduler_once(store, sender=fake_send))
            seq = store.get_sequence(seq_id)
            assert seq["status"] == "exhausted", f"expected exhausted, got {seq['status']}"
            assert seq["stop_reason"] == "all_recipients_tried"

            rows = {r["email"]: r for r in store.list_recipients(seq_id)}
            assert rows["only@test.com"]["status"] == "skipped"
        finally:
            s.email_recipient_waterfall_days = 3
    finally:
        _cleanup(store, seq_id)


def test_scheduler_retires_failed_recipient_not_sequence(store: EmailStore) -> None:
    """A send failure on one recipient should not park the whole
    sequence — just retire that recipient and continue."""
    seq_id = "seq-fail-1"
    _seed_full_setup(store, seq_id=seq_id)
    try:
        store.add_recipients(seq_id, ["bad@test.com", "good@test.com"])
        attempt = [0]
        sent_to: list[str] = []

        async def fake_send(*args, **kwargs):
            attempt[0] += 1
            sent_to.append(kwargs.get("to_email", ""))
            if attempt[0] == 1:
                return {
                    "ok": False,
                    "provider": "smtp",
                    "provider_message_id": "",
                    "thread_key": "",
                    "sent_at": "",
                    "error": "address_rejected",
                    "error_type": "permanent_failure",
                }
            return {
                "ok": True,
                "provider": "smtp",
                "provider_message_id": "x",
                "thread_key": "y",
                "sent_at": "",
                "error": "",
                "error_type": "",
            }

        result = asyncio.run(run_scheduler_once(store, sender=fake_send))
        # First call: bad → failed, good → waiting_reply (no exhaustion)
        # Both attempted in a single pass.
        assert sent_to == ["bad@test.com", "good@test.com"]

        seq = store.get_sequence(seq_id)
        assert seq["status"] in ("running", "exhausted")
        # Only 1 of 2 recipients is "active" so not exhausted yet
        rows = {r["email"]: r for r in store.list_recipients(seq_id)}
        assert rows["bad@test.com"]["status"] == "failed"
        assert rows["good@test.com"]["status"] == "waiting_reply"
    finally:
        _cleanup(store, seq_id)


def test_waterfall_disabled_when_days_is_zero(store: EmailStore) -> None:
    """Setting email_recipient_waterfall_days = 0 should disable the
    advancement (legacy single-recipient behavior)."""
    seq_id = "seq-wd-1"
    _seed_full_setup(store, seq_id=seq_id)
    try:
        s = get_settings()
        s.email_recipient_waterfall_days = 0
        try:
            store.add_recipients(seq_id, ["first@test.com", "second@test.com"])
            sent_to: list[str] = []

            async def fake_send(*args, **kwargs):
                sent_to.append(kwargs.get("to_email", ""))
                return {
                    "ok": True,
                    "provider": "smtp",
                    "provider_message_id": "x",
                    "thread_key": "y",
                    "sent_at": "",
                    "error": "",
                    "error_type": "",
                }

            # Send to first
            asyncio.run(run_scheduler_once(store, sender=fake_send))
            assert sent_to == ["first@test.com"]

            # Even after 100 days, the first recipient stays waiting_reply
            # (no advancement)
            very_past = (
                datetime.now(timezone.utc) - timedelta(days=100)
            ).isoformat()
            with store._connect() as conn:
                conn.execute(
                    "UPDATE lead_email_recipients SET sent_at = ? WHERE sequence_id = ?",
                    (very_past, seq_id),
                )

            sent_to.clear()
            asyncio.run(run_scheduler_once(store, sender=fake_send))
            # No second send happened (scheduler didn't advance)
            assert sent_to == [], f"expected no second send, got {sent_to}"
        finally:
            s.email_recipient_waterfall_days = 3
    finally:
        _cleanup(store, seq_id)
