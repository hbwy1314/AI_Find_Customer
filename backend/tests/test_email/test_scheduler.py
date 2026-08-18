from pathlib import Path

import pytest

from emailing.scheduler import run_scheduler_once
from emailing.store import EmailStore


@pytest.mark.asyncio
async def test_scheduler_sends_pending_message(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "email.db"
    store = EmailStore(str(db_path))
    store.init_db()
    store.upsert_account({
        "id": "acct_1",
        "provider_type": "smtp",
        "from_name": "Ai Hunter",
        "from_email": "sales@example.com",
        "reply_to": "sales@example.com",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_username": "sales@example.com",
        "smtp_secret_encrypted": "enc",
        "imap_host": "",
        "imap_port": 993,
        "imap_username": "",
        "imap_secret_encrypted": "",
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
        "email_account_id": "acct_1",
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
        "status": "scheduled",
        "current_step": 0,
        "stop_reason": "",
        "replied_at": "",
        "last_sent_at": "",
        "next_scheduled_at": "2026-03-09T00:00:00Z",
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
        "status": "pending",
        "scheduled_at": "2026-03-09T00:00:00Z",
        "sent_at": "",
        "provider_message_id": "",
        "thread_key": "",
        "failure_reason": "",
        "created_at": "2026-03-09T00:00:00Z",
        "updated_at": "2026-03-09T00:00:00Z",
    })

    monkeypatch.setattr("emailing.scheduler.load_hunt", lambda hunt_id: {"result": {}})
    monkeypatch.setattr("emailing.scheduler.save_hunt", lambda hunt_id, hunt: None)

    async def fake_sender(*args, **kwargs):
        return {
            "ok": True,
            "provider_message_id": "<mid>",
            "thread_key": "thread-1",
        }

    result = await run_scheduler_once(store, now_iso="2026-03-09T01:00:00Z", sender=fake_sender)
    assert result["sent"] == 1
    assert store.list_pending_messages_ready("2026-03-09T02:00:00Z") == []


@pytest.mark.asyncio
async def test_scheduler_stops_underperforming_template_sequence(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "email.db"
    store = EmailStore(str(db_path))
    store.init_db()
    store.upsert_account({
        "id": "acct_1",
        "provider_type": "smtp",
        "from_name": "Ai Hunter",
        "from_email": "sales@example.com",
        "reply_to": "sales@example.com",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_username": "sales@example.com",
        "smtp_secret_encrypted": "enc",
        "imap_host": "",
        "imap_port": 993,
        "imap_username": "",
        "imap_secret_encrypted": "",
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
        "email_account_id": "acct_1",
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
    for idx in range(1, 11):
        store.create_sequence({
            "id": f"seq_{idx}",
            "campaign_id": "cmp_1",
            "hunt_id": "hunt_1",
            "lead_key": f"lead_{idx}",
            "lead_email": f"buyer{idx}@acme.com",
            "lead_name": f"Acme {idx}",
            "decision_maker_name": "Jane",
            "decision_maker_title": "Purchasing Manager",
            "locale": "en_US",
            "generation_mode": "template_pool",
            "template_id": "tpl_bad",
            "template_group": "en_US|decision_maker_verified|general",
            "template_usage_index": idx,
            "template_max_send_count": 100,
            "status": "scheduled",
            "current_step": 0,
            "stop_reason": "",
            "replied_at": "",
            "last_sent_at": "",
            "next_scheduled_at": "2026-03-09T00:00:00Z",
            "created_at": "2026-03-09T00:00:00Z",
            "updated_at": "2026-03-09T00:00:00Z",
        })
        store.create_message({
            "id": f"msg_{idx}",
            "sequence_id": f"seq_{idx}",
            "step_number": 1,
            "goal": "intro",
            "locale": "en",
            "subject": "Hello",
            "body_text": "Body",
            "status": "pending" if idx == 1 else "sent",
            "scheduled_at": "2026-03-09T00:00:00Z",
            "sent_at": "2026-03-09T00:10:00Z" if idx != 1 else "",
            "provider_message_id": "",
            "thread_key": "",
            "failure_reason": "",
            "created_at": "2026-03-09T00:00:00Z",
            "updated_at": "2026-03-09T00:00:00Z",
        })

    monkeypatch.setattr("emailing.scheduler.load_hunt", lambda hunt_id: {"result": {}})
    monkeypatch.setattr("emailing.scheduler.save_hunt", lambda hunt_id, hunt: None)

    async def fake_sender(*args, **kwargs):
        raise AssertionError("sender should not run for blocked template")

    result = await run_scheduler_once(store, now_iso="2026-03-09T01:00:00Z", sender=fake_sender)

    assert result["sent"] == 0
    assert result["skipped"] == 1
    sequence = store.get_sequence("seq_1")
    assert sequence is not None
    assert sequence["status"] == "stopped"
    assert sequence["stop_reason"] == "template_underperforming"
    message = store.get_message("msg_1")
    assert message is not None
    assert message["status"] == "cancelled"


@pytest.mark.asyncio
async def test_scheduler_skips_role_based_recipient_and_advances(
    tmp_path: Path, monkeypatch
) -> None:
    """A role-based recipient (info@, sales@, …) must never be sent
    to. The scheduler should immediately retire the row, mark the
    message failed with ``recipient_role_based``, and clone the
    pending message for the next candidate so a named decision
    maker is the actual recipient.
    """
    db_path = tmp_path / "email.db"
    store = EmailStore(str(db_path))
    store.init_db()
    store.upsert_account({
        "id": "acct_rb",
        "provider_type": "smtp",
        "from_name": "Ai Hunter",
        "from_email": "sales@example.com",
        "reply_to": "sales@example.com",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_username": "sales@example.com",
        "smtp_secret_encrypted": "enc",
        "imap_host": "", "imap_port": 993, "imap_username": "", "imap_secret_encrypted": "",
        "use_tls": 1,
        "status": "active",
        "daily_send_limit": 50, "hourly_send_limit": 10, "last_test_at": "",
        "created_at": "2026-03-09T00:00:00Z", "updated_at": "2026-03-09T00:00:00Z",
    })
    store.create_campaign({
        "id": "cmp_rb", "hunt_id": "hunt_rb", "email_account_id": "acct_rb",
        "name": "Test", "status": "active",
        "language_mode": "auto_by_region", "default_language": "en",
        "fallback_language": "en", "tone": "professional",
        "step1_delay_days": 0, "step2_delay_days": 3, "step3_delay_days": 3,
        "min_fit_score": 0.6, "min_contactability_score": 0.45,
        "created_at": "2026-03-09T00:00:00Z", "updated_at": "2026-03-09T00:00:00Z",
    })
    store.create_sequence({
        "id": "seq_rb", "campaign_id": "cmp_rb", "hunt_id": "hunt_rb",
        "lead_key": "w:acme.com", "lead_email": "info@acme.com",
        "lead_name": "Acme", "decision_maker_name": "", "decision_maker_title": "",
        "locale": "en", "status": "scheduled", "current_step": 0,
        "stop_reason": "", "replied_at": "", "last_sent_at": "",
        "next_scheduled_at": "2026-03-09T00:00:00Z",
        "created_at": "2026-03-09T00:00:00Z", "updated_at": "2026-03-09T00:00:00Z",
    })
    store.create_message({
        "id": "msg_rb", "sequence_id": "seq_rb", "step_number": 1,
        "goal": "intro", "locale": "en", "subject": "Hello", "body_text": "Body",
        "status": "pending", "scheduled_at": "2026-03-09T00:00:00Z",
        "sent_at": "", "provider_message_id": "", "thread_key": "",
        "failure_reason": "",
        "created_at": "2026-03-09T00:00:00Z", "updated_at": "2026-03-09T00:00:00Z",
    })
    # Two candidates: a role-based inbox first, then a named DM.
    store.add_recipients(
        "seq_rb",
        ["info@acme.com", "buyer@acme.com"],
        is_role_based_per_email={
            "info@acme.com": True,
            "buyer@acme.com": False,
        },
    )

    monkeypatch.setattr("emailing.scheduler.load_hunt", lambda hunt_id: {"result": {}})
    monkeypatch.setattr("emailing.scheduler.save_hunt", lambda hunt_id, hunt: None)

    sent_to: list[str] = []

    async def fake_sender(*args, **kwargs):
        sent_to.append(kwargs.get("to_email", ""))
        return {
            "ok": True,
            "provider_message_id": "<mid>",
            "thread_key": "thread-rb",
        }

    result = await run_scheduler_once(
        store, now_iso="2026-03-09T01:00:00Z", sender=fake_sender,
    )

    # The role-based address was never sent to; only the named DM.
    assert sent_to == ["buyer@acme.com"]
    assert result["sent"] == 1
    assert result["failed"] == 0
    recipients = {r["email"]: r for r in store.list_recipients("seq_rb")}
    assert recipients["info@acme.com"]["status"] == "skipped"
    assert recipients["info@acme.com"]["failure_reason"] == "recipient_role_based"
    # The next message in the chain targets the named recipient.
    sequence = store.get_sequence("seq_rb")
    assert sequence is not None
    assert sequence["lead_email"] == "buyer@acme.com"
    # The original message was marked failed; the cloned one already
    # sent to the named DM within the same scheduler pass, so the
    # pending list is empty and a new email_messages row exists in
    # `sent` status.
    pending_after = store.list_pending_messages_ready("2026-03-09T02:00:00Z")
    assert pending_after == []
    sent_messages = [
        m for m in store.list_messages_for_sequence("seq_rb")
        if m["status"] == "sent"
    ]
    assert len(sent_messages) == 1
    assert sent_messages[0]["subject"] == "Hello"
    # The original is marked failed.
    original = store.get_message("msg_rb")
    assert original is not None
    assert original["status"] == "failed"
    assert original["failure_reason"] == "recipient_role_based"


@pytest.mark.asyncio
async def test_scheduler_marks_exhausted_when_all_recipients_role_based(
    tmp_path: Path, monkeypatch
) -> None:
    """If every candidate in the pool is role-based (or the only
    lead_email fallback is role-based), the sequence ends in
    ``exhausted`` with stop_reason ``all_recipients_role_based`` so
    the operator sees why nothing was sent.
    """
    db_path = tmp_path / "email2.db"
    store = EmailStore(str(db_path))
    store.init_db()
    store.upsert_account({
        "id": "acct_rb2", "provider_type": "smtp",
        "from_name": "Ai Hunter", "from_email": "sales@example.com",
        "reply_to": "sales@example.com",
        "smtp_host": "smtp.example.com", "smtp_port": 587,
        "smtp_username": "sales@example.com", "smtp_secret_encrypted": "enc",
        "imap_host": "", "imap_port": 993, "imap_username": "",
        "imap_secret_encrypted": "", "use_tls": 1,
        "status": "active", "daily_send_limit": 50, "hourly_send_limit": 10,
        "last_test_at": "",
        "created_at": "2026-03-09T00:00:00Z", "updated_at": "2026-03-09T00:00:00Z",
    })
    store.create_campaign({
        "id": "cmp_rb2", "hunt_id": "hunt_rb2", "email_account_id": "acct_rb2",
        "name": "Test", "status": "active",
        "language_mode": "auto_by_region", "default_language": "en",
        "fallback_language": "en", "tone": "professional",
        "step1_delay_days": 0, "step2_delay_days": 3, "step3_delay_days": 3,
        "min_fit_score": 0.6, "min_contactability_score": 0.45,
        "created_at": "2026-03-09T00:00:00Z", "updated_at": "2026-03-09T00:00:00Z",
    })
    store.create_sequence({
        "id": "seq_rb2", "campaign_id": "cmp_rb2", "hunt_id": "hunt_rb2",
        "lead_key": "w:rb.com", "lead_email": "sales@rb.com",
        "lead_name": "RB Inc", "decision_maker_name": "",
        "decision_maker_title": "",
        "locale": "en", "status": "scheduled", "current_step": 0,
        "stop_reason": "", "replied_at": "", "last_sent_at": "",
        "next_scheduled_at": "2026-03-09T00:00:00Z",
        "created_at": "2026-03-09T00:00:00Z", "updated_at": "2026-03-09T00:00:00Z",
    })
    store.create_message({
        "id": "msg_rb2", "sequence_id": "seq_rb2", "step_number": 1,
        "goal": "intro", "locale": "en", "subject": "Hello", "body_text": "Body",
        "status": "pending", "scheduled_at": "2026-03-09T00:00:00Z",
        "sent_at": "", "provider_message_id": "", "thread_key": "",
        "failure_reason": "",
        "created_at": "2026-03-09T00:00:00Z", "updated_at": "2026-03-09T00:00:00Z",
    })
    # Only role-based candidates — no fallback path possible.
    store.add_recipients(
        "seq_rb2",
        ["info@rb.com", "sales@rb.com"],
        is_role_based_per_email={
            "info@rb.com": True,
            "sales@rb.com": True,
        },
    )

    monkeypatch.setattr("emailing.scheduler.load_hunt", lambda hunt_id: {"result": {}})
    monkeypatch.setattr("emailing.scheduler.save_hunt", lambda hunt_id, hunt: None)

    sent_to: list[str] = []

    async def fake_sender(*args, **kwargs):
        sent_to.append(kwargs.get("to_email", ""))
        return {"ok": True, "provider_message_id": "<mid>", "thread_key": "t"}

    result = await run_scheduler_once(
        store, now_iso="2026-03-09T01:00:00Z", sender=fake_sender,
    )

    assert sent_to == []
    assert result["sent"] == 0
    assert result["failed"] == 1
    sequence = store.get_sequence("seq_rb2")
    assert sequence is not None
    assert sequence["status"] == "exhausted"
    assert sequence["stop_reason"] == "all_recipients_role_based"
    # Both recipients are skipped; the message is failed.
    recipients = {r["email"]: r for r in store.list_recipients("seq_rb2")}
    assert all(r["status"] == "skipped" for r in recipients.values())
    msg = store.get_message("msg_rb2")
    assert msg is not None
    assert msg["status"] == "failed"
    assert msg["failure_reason"] == "recipient_role_based"
