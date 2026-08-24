"""Tests for the email unsubscribe flow (token + store + API + scheduler)."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from config.settings import get_settings
from emailing.scheduler import run_scheduler_once
from emailing.store import EmailStore
from emailing.unsubscribe import (
    build_mailto_unsubscribe,
    build_unsubscribe_url,
    issue_token,
    token_hash,
    verify_token,
)


@pytest.fixture
def store(tmp_path) -> EmailStore:
    # Use a per-test tmp DB. The route tests below build a real
    # FastAPI app via `create_app()` which reads
    # `get_settings().email_db_path` — so they monkeypatch the
    # `get_settings` symbol that `create_app` resolves against to
    # also point at this same tmp DB. This gives every test
    # isolation without breaking the shared-state contract that the
    # API tests rely on.
    s = EmailStore(str(tmp_path / "email.db"))
    s.init_db()
    return s


def test_token_sign_and_verify() -> None:
    t = issue_token("a@b.com", scope="campaign:abc")
    payload = verify_token(t)
    assert payload is not None
    assert payload["email"] == "a@b.com"
    assert payload["scope"] == "campaign:abc"
    assert payload["ttl"] == 90 * 24 * 60 * 60


def test_bad_token_returns_none() -> None:
    # right shape (aGVsbG8= is base64 for "hello") but signature is garbage
    assert verify_token("aGVsbG8=.Zm9v") is None


def test_expired_token_returns_none() -> None:
    t = issue_token("a@b.com", ttl_seconds=-1)
    # issued_at + ttl is already in the past, so verify should reject
    assert verify_token(t) is None


def test_token_hash_is_deterministic_and_not_reversible() -> None:
    t = issue_token("a@b.com")
    h1 = token_hash(t)
    h2 = token_hash(t)
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex
    # the raw token is not in the hash
    assert t not in h1


def test_build_unsubscribe_url_and_mailto() -> None:
    url = build_unsubscribe_url("https://api.x.example/", "tok123")
    assert url == "https://api.x.example/api/unsubscribe/tok123"
    mailto = build_mailto_unsubscribe("user@example.com")
    assert mailto == "mailto:unsubscribe@example.com?subject=unsubscribe"


def test_record_is_idempotent(store: EmailStore) -> None:
    rid1 = store.record_unsubscribe(
        email="dup@example.com", scope="campaign:abc", token_hash="h1", source="link"
    )
    rid2 = store.record_unsubscribe(
        email="dup@example.com", scope="campaign:abc", token_hash="h2", source="link"
    )
    assert rid1 == rid2


def test_is_unsubscribed_global_blocks_all_scopes(store: EmailStore) -> None:
    store.record_unsubscribe(email="global-block@example.com", scope="all")
    assert store.is_unsubscribed("global-block@example.com")
    assert store.is_unsubscribed("global-block@example.com", scope="campaign:abc")
    assert store.is_unsubscribed("global-block@example.com", scope="campaign:other")
    assert not store.is_unsubscribed("nobody@nowhere.com")


def test_is_unsubscribed_campaign_only_blocks_that_campaign(store: EmailStore) -> None:
    store.record_unsubscribe(email="campaign-only@example.com", scope="campaign:abc")
    assert not store.is_unsubscribed("campaign-only@example.com")  # no global
    assert store.is_unsubscribed("campaign-only@example.com", scope="campaign:abc")
    assert not store.is_unsubscribed("campaign-only@example.com", scope="campaign:other")


@pytest.fixture
def client(store: EmailStore, monkeypatch) -> TestClient:
    # `create_app()` reads `get_settings().email_db_path` to wire up
    # the FastAPI dependency that hands the email store to route
    # handlers. We need the route's writes to land in the same DB
    # the test's `store` is reading from, so patch the settings the
    # app actually resolves against. The route handlers also call
    # `get_email_store()` which is a process-wide singleton — reset
    # it so the next call picks up our patched settings.
    import config.settings as settings_mod
    import emailing.store as email_store_mod
    fake = type("S", (), {
        "email_db_path": store.db_path,
        "email_reply_check_interval_seconds": 180,
        "email_reply_detection_enabled": False,
        "email_provider_type": "graph",
        "graph_tenant_id": "",
        "graph_client_id": "",
        "graph_client_secret": "",
        "graph_mailbox_upn": "",
    })()
    monkeypatch.setattr(settings_mod, "get_settings", lambda: fake)
    monkeypatch.setattr(email_store_mod, "_email_store_singleton", None)
    return TestClient(create_app())


def test_route_get_bad_token_returns_400_html(client: TestClient) -> None:
    r = client.get("/api/unsubscribe/aGVsbG8=.Zm9v")
    assert r.status_code == 400
    assert "text/html" in r.headers.get("content-type", "")


def test_route_get_real_token_records_and_returns_200_html(
    client: TestClient, store: EmailStore
) -> None:
    email = "route-real@example.com"
    token = issue_token(email, scope="all")
    try:
        r = client.get(f"/api/unsubscribe/{token}")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
        assert "已退订" in r.text
        assert email in r.text
        assert store.is_unsubscribed(email)
    finally:
        with sqlite3.connect(store.db_path) as conn:
            conn.execute("DELETE FROM email_unsubscribes WHERE email = ?", (email,))
            conn.commit()


def test_route_post_is_one_click(client: TestClient, store: EmailStore) -> None:
    email = "oneclick@example.com"
    token = issue_token(email, scope="all")
    try:
        r = client.post(f"/api/unsubscribe/{token}")
        assert r.status_code == 200
        # one-click returns plain (no HTML body) so mail clients don't render
        assert "text/plain" in r.headers.get("content-type", "") or r.text == ""
        assert store.is_unsubscribed(email)
    finally:
        with sqlite3.connect(store.db_path) as conn:
            conn.execute("DELETE FROM email_unsubscribes WHERE email = ?", (email,))
            conn.commit()


@pytest.mark.asyncio
async def test_scheduler_skips_unsubscribed_recipient(store: EmailStore) -> None:
    """Integration: scheduler should mark message failed + sequence stopped
    when the recipient has unsubscribed."""
    from datetime import datetime, timezone

    email = "sched-unsub@example.com"
    store.record_unsubscribe(email=email, scope="all")

    sent_called = False

    async def fake_send(*args, **kwargs):
        nonlocal sent_called
        sent_called = True
        return {
            "ok": True,
            "provider": "smtp",
            "provider_message_id": "x",
            "thread_key": "y",
            "sent_at": "",
            "error": "",
            "error_type": "",
        }

    seq_id = "seq-unsub-test"
    camp_id = "camp-unsub-test"
    hunt_id = "hunt-unsub-test"
    acc_id = "acc-unsub-test"
    now = datetime.now(timezone.utc).isoformat()

    try:
        with store._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO email_accounts
                (id, provider_type, from_name, from_email, reply_to, status,
                 daily_send_limit, hourly_send_limit, last_test_at,
                 created_at, updated_at)
                VALUES (?, 'graph', 'Sales', 'sales@test.com', '', 'active',
                        100, 100, '', ?, ?)""",
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
                (seq_id, camp_id, hunt_id, "lead-1", email, "Test", now, now),
            )
            conn.execute(
                "INSERT OR REPLACE INTO email_messages "
                "(id, sequence_id, step_number, goal, locale, subject, body_text, status, "
                " scheduled_at, created_at, updated_at) "
                "VALUES (?, ?, 1, 'intro', 'en', 'Hi', 'Hello', 'pending', ?, ?, ?)",
                ("msg-unsub-test", seq_id, now, now, now),
            )

        result = await run_scheduler_once(store, now_iso=now, sender=fake_send)
        assert result["skipped"] == 1
        assert not sent_called, "send_email must not be called for unsubscribed recipient"

        with sqlite3.connect(store.db_path) as conn:
            msg = conn.execute(
                "SELECT status, failure_reason FROM email_messages WHERE id='msg-unsub-test'"
            ).fetchone()
            seq = conn.execute(
                "SELECT status, stop_reason FROM lead_email_sequences WHERE id='seq-unsub-test'"
            ).fetchone()
        assert msg[0] == "failed"
        assert msg[1] == "recipient_unsubscribed"
        assert seq[0] == "stopped"
        assert seq[1] == "unsubscribed"
    finally:
        with sqlite3.connect(store.db_path) as conn:
            conn.execute("DELETE FROM email_messages WHERE id='msg-unsub-test'")
            conn.execute("DELETE FROM lead_email_sequences WHERE id='seq-unsub-test'")
            conn.execute("DELETE FROM email_campaigns WHERE id='camp-unsub-test'")
            conn.execute("DELETE FROM email_accounts WHERE id='acc-unsub-test'")
            conn.execute("DELETE FROM email_unsubscribes WHERE email=?", (email,))
            conn.commit()
