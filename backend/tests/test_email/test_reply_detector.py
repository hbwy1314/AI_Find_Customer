from pathlib import Path

import pytest

from emailing.reply_detector import run_reply_detection_once
from emailing.store import EmailStore


def _seed_store(store: EmailStore) -> None:
    store.init_db()
    store.upsert_account({
        "id": "default",
        "provider_type": "smtp",
        "from_name": "Ai Hunter",
        "from_email": "sales@example.com",
        "reply_to": "sales@example.com",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_username": "sales@example.com",
        "smtp_secret_encrypted": "enc",
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "imap_username": "sales@example.com",
        "imap_secret_encrypted": "imap-secret",
        "use_tls": 1,
        "status": "active",
        "daily_send_limit": 50,
        "hourly_send_limit": 10,
        "last_test_at": "",
        "created_at": "2026-03-09T00:00:00Z",
        "updated_at": "2026-03-09T00:00:00Z",
    })
    store.create_campaign({
        "id": "cmp_1",
        "hunt_id": "hunt_1",
        "email_account_id": "default",
        "name": "Test",
        "status": "active",
        "language_mode": "auto_by_region",
        "default_language": "en",
        "fallback_language": "en",
        "tone": "professional",
        "step1_delay_days": 0,
        "step2_delay_days": 3,
        "step3_delay_days": 3,
        "min_fit_score": 0.6,
        "min_contactability_score": 0.45,
        "created_at": "2026-03-09T00:00:00Z",
        "updated_at": "2026-03-09T00:00:00Z",
    })
    store.create_sequence({
        "id": "seq_1",
        "campaign_id": "cmp_1",
        "hunt_id": "hunt_1",
        "lead_key": "w:acme.com",
        "lead_email": "buyer@acme.com",
        "lead_name": "Acme",
        "decision_maker_name": "Jane",
        "decision_maker_title": "Purchasing Manager",
        "locale": "en",
        "status": "running",
        "current_step": 1,
        "stop_reason": "",
        "replied_at": "",
        "last_sent_at": "2026-03-09T01:00:00Z",
        "next_scheduled_at": "2026-03-12T01:00:00Z",
        "created_at": "2026-03-09T00:00:00Z",
        "updated_at": "2026-03-09T00:00:00Z",
    })
    store.create_message({
        "id": "msg_1",
        "sequence_id": "seq_1",
        "step_number": 1,
        "goal": "intro",
        "locale": "en",
        "subject": "Hello",
        "body_text": "Body",
        "status": "sent",
        "scheduled_at": "2026-03-09T00:00:00Z",
        "sent_at": "2026-03-09T01:00:00Z",
        "provider_message_id": "<mid-1@example.com>",
        "thread_key": "thread-1",
        "failure_reason": "",
        "created_at": "2026-03-09T00:00:00Z",
        "updated_at": "2026-03-09T01:00:00Z",
    })
    store.create_message({
        "id": "msg_2",
        "sequence_id": "seq_1",
        "step_number": 2,
        "goal": "followup",
        "locale": "en",
        "subject": "Hello again",
        "body_text": "Body 2",
        "status": "pending",
        "scheduled_at": "2026-03-12T01:00:00Z",
        "sent_at": "",
        "provider_message_id": "",
        "thread_key": "",
        "failure_reason": "",
        "created_at": "2026-03-09T00:00:00Z",
        "updated_at": "2026-03-09T00:00:00Z",
    })


@pytest.mark.asyncio
async def test_reply_detector_matches_in_reply_to_and_stops_sequence(tmp_path: Path, monkeypatch):
    store = EmailStore(str(tmp_path / "email.db"))
    _seed_store(store)

    monkeypatch.setattr("emailing.reply_detector.load_hunt", lambda hunt_id: {"result": {}})
    monkeypatch.setattr("emailing.reply_detector.save_hunt", lambda hunt_id, hunt: None)

    def fake_fetcher(account, *, now_iso: str):
        return [{
            "raw_ref": "<reply-1@example.com>",
            "message_id": "<reply-1@example.com>",
            "from_email": "buyer@acme.com",
            "subject": "Re: Hello",
            "in_reply_to": "<mid-1@example.com>",
            "references": ["<mid-1@example.com>"],
            "received_at": "2026-03-10T00:00:00Z",
            "snippet": "Interested, let's talk.",
        }]

    result = await run_reply_detection_once(
        store,
        store.get_account("default") or {},
        now_iso="2026-03-10T00:05:00Z",
        fetcher=fake_fetcher,
    )

    assert {"checked": 1, "matched": 1, "skipped": 0, "ignored": 0} == {k: result[k] for k in ("checked", "matched", "skipped", "ignored")}
    seq = store.get_sequence("seq_1")
    assert seq is not None
    assert seq["status"] == "replied"
    assert seq["stop_reason"] == "reply_detected"
    assert seq["replied_at"] == "2026-03-10T00:00:00Z"
    pending = store.get_message("msg_2")
    assert pending is not None
    assert pending["status"] == "cancelled"
    events = store.list_reply_events_for_sequence("seq_1")
    assert len(events) == 1
    assert events[0]["from_email"] == "buyer@acme.com"


@pytest.mark.asyncio
async def test_reply_detector_deduplicates_raw_ref(tmp_path: Path, monkeypatch):
    store = EmailStore(str(tmp_path / "email.db"))
    _seed_store(store)

    monkeypatch.setattr("emailing.reply_detector.load_hunt", lambda hunt_id: {"result": {}})
    monkeypatch.setattr("emailing.reply_detector.save_hunt", lambda hunt_id, hunt: None)

    def fake_fetcher(account, *, now_iso: str):
        return [{
            "raw_ref": "<reply-1@example.com>",
            "message_id": "<reply-1@example.com>",
            "from_email": "buyer@acme.com",
            "subject": "Re: Hello",
            "in_reply_to": "<mid-1@example.com>",
            "references": [],
            "received_at": "2026-03-10T00:00:00Z",
            "snippet": "Ping",
        }]

    await run_reply_detection_once(store, store.get_account("default") or {}, now_iso="2026-03-10T00:05:00Z", fetcher=fake_fetcher)
    result = await run_reply_detection_once(store, store.get_account("default") or {}, now_iso="2026-03-10T00:06:00Z", fetcher=fake_fetcher)
    assert {"checked": 1, "matched": 0, "skipped": 1, "ignored": 0} == {k: result[k] for k in ("checked", "matched", "skipped", "ignored")}


@pytest.mark.asyncio
async def test_reply_detector_ignores_out_of_office(tmp_path: Path, monkeypatch):
    store = EmailStore(str(tmp_path / "email.db"))
    _seed_store(store)

    monkeypatch.setattr("emailing.reply_detector.load_hunt", lambda hunt_id: {"result": {}})
    monkeypatch.setattr("emailing.reply_detector.save_hunt", lambda hunt_id, hunt: None)

    def fake_fetcher(account, *, now_iso: str):
        return [{
            "raw_ref": "<ooo-1@example.com>",
            "message_id": "<ooo-1@example.com>",
            "from_email": "buyer@acme.com",
            "subject": "Automatic reply: Hello",
            "in_reply_to": "<mid-1@example.com>",
            "references": ["<mid-1@example.com>"],
            "received_at": "2026-03-10T00:00:00Z",
            "snippet": "I am currently out of office until next week.",
            "headers": {"Auto-Submitted": "auto-replied"},
        }]

    result = await run_reply_detection_once(
        store,
        store.get_account("default") or {},
        now_iso="2026-03-10T00:05:00Z",
        fetcher=fake_fetcher,
    )

    assert {"checked": 1, "matched": 0, "skipped": 0, "ignored": 1} == {k: result[k] for k in ("checked", "matched", "skipped", "ignored")}
    seq = store.get_sequence("seq_1")
    assert seq is not None
    assert seq["status"] == "running"
    assert store.list_reply_events_for_sequence("seq_1") == []


@pytest.mark.asyncio
async def test_reply_detector_ignores_bounce_from_mailer_daemon(tmp_path: Path, monkeypatch):
    store = EmailStore(str(tmp_path / "email.db"))
    _seed_store(store)

    monkeypatch.setattr("emailing.reply_detector.load_hunt", lambda hunt_id: {"result": {}})
    monkeypatch.setattr("emailing.reply_detector.save_hunt", lambda hunt_id, hunt: None)

    def fake_fetcher(account, *, now_iso: str):
        return [{
            "raw_ref": "<bounce-1@example.com>",
            "message_id": "<bounce-1@example.com>",
            "from_email": "mailer-daemon@example.com",
            "subject": "Delivery Status Notification (Failure)",
            "in_reply_to": "<mid-1@example.com>",
            "references": ["<mid-1@example.com>"],
            "received_at": "2026-03-10T00:00:00Z",
            "snippet": "Delivery has failed to these recipients or groups.",
            "headers": {"X-Failed-Recipients": "buyer@acme.com"},
        }]

    result = await run_reply_detection_once(
        store,
        store.get_account("default") or {},
        now_iso="2026-03-10T00:05:00Z",
        fetcher=fake_fetcher,
    )

    assert {"checked": 1, "matched": 0, "skipped": 0, "ignored": 1} == {k: result[k] for k in ("checked", "matched", "skipped", "ignored")}
    seq = store.get_sequence("seq_1")
    assert seq is not None
    assert seq["status"] == "running"


@pytest.mark.asyncio
async def test_graph_reply_detection_matches_conversation_and_stops_sequence(tmp_path: Path, monkeypatch):
    """Graph path: replies arrive via fetch_graph_replies and match on
    conversationId == sent message thread_key, stopping the sequence."""
    from emailing.reply_detector import run_graph_reply_detection_once

    store = EmailStore(str(tmp_path / "email.db"))
    _seed_store(store)

    monkeypatch.setattr("emailing.reply_detector.load_hunt", lambda hunt_id: {"result": {}})
    monkeypatch.setattr("emailing.reply_detector.save_hunt", lambda hunt_id, hunt: None)

    async def fake_fetch_graph_replies(account, *, now_iso, recent_days=14, limit=100):
        return [{
            "raw_ref": "graph:conv-1",
            "message_id": "<graph-reply-1@example.com>",
            "conversation_id": "thread-1",  # matches msg_1 thread_key
            "in_reply_to": "",
            "references": [],
            "from_email": "buyer@acme.com",
            "from_name": "Jane",
            "subject": "Re: Hello",
            "received_at": "2026-03-10T00:00:00Z",
            "snippet": "Interested, let's talk.",
            "headers": {},
        }]

    monkeypatch.setattr(
        "emailing.graph_client.fetch_graph_replies", fake_fetch_graph_replies
    )

    result = await run_graph_reply_detection_once(store, now_iso="2026-03-10T00:05:00Z")

    assert {"checked": 1, "matched": 1, "skipped": 0, "ignored": 0} == {k: result[k] for k in ("checked", "matched", "skipped", "ignored")}
    seq = store.get_sequence("seq_1")
    assert seq is not None
    assert seq["status"] == "replied"
    assert seq["stop_reason"] == "reply_detected"
    pending = store.get_message("msg_2")
    assert pending is not None
    assert pending["status"] == "cancelled"
    events = store.list_reply_events_for_sequence("seq_1")
    assert len(events) == 1
    assert events[0]["from_email"] == "buyer@acme.com"
