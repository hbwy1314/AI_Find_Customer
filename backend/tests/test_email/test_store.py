from pathlib import Path

from emailing.store import EmailStore


def test_global_lead_registry_reserves_aliases_once(tmp_path: Path):
    store = EmailStore(str(tmp_path / "email.db"))
    store.init_db()
    lead = {"company_name": "Acme Ltd", "website": "https://www.acme.com/about", "emails": ["sales@acme.com"]}
    assert store.reserve_lead_keys(
        [lead], hunt_id="hunt-1", key_fn=lambda item: ["domain:acme.com", "email:sales@acme.com"], now_iso="2026-01-01"
    ) == [lead]
    assert store.reserve_lead_keys(
        [{"company_name": "Acme", "website": "https://acme.com"}],
        hunt_id="hunt-2",
        key_fn=lambda item: ["domain:acme.com"],
        now_iso="2026-01-02",
    ) == []


def test_global_lead_registry_backfills_missing_aliases(tmp_path: Path):
    store = EmailStore(str(tmp_path / "email.db"))
    store.init_db()
    lead = {"company_name": "Acme GmbH", "website": "https://acme.com"}
    assert store.reserve_lead_keys(
        [lead],
        hunt_id="hunt-1",
        key_fn=lambda item: ["legacy:acme"],
        now_iso="2026-01-01",
    ) == [lead]

    inserted = store.ensure_lead_keys(
        [lead],
        hunt_id="hunt-1",
        key_fn=lambda item: ["legacy:acme", "domain:acme.com"],
        now_iso="2026-01-01",
    )

    assert inserted == 1
    assert {"legacy:acme", "domain:acme.com"} <= store.list_lead_registry_keys()


def test_get_hunt_id_for_key(tmp_path: Path):
    """Test retrieving the hunt_id that originally registered a key."""
    store = EmailStore(str(tmp_path / "email.db"))
    store.init_db()
    
    # Register keys for different hunts
    lead1 = {"company_name": "Acme", "website": "https://acme.com"}
    lead2 = {"company_name": "Beta Corp", "emails": ["info@beta.com"]}
    
    store.reserve_lead_keys(
        [lead1],
        hunt_id="hunt-alpha",
        key_fn=lambda item: ["domain:acme.com", "company:acme"],
        now_iso="2026-01-01",
    )
    
    store.reserve_lead_keys(
        [lead2],
        hunt_id="hunt-beta",
        key_fn=lambda item: ["email:info@beta.com", "company:beta corp"],
        now_iso="2026-01-02",
    )
    
    # Check hunt_id retrieval
    assert store.get_hunt_id_for_key("domain:acme.com") == "hunt-alpha"
    assert store.get_hunt_id_for_key("company:acme") == "hunt-alpha"
    assert store.get_hunt_id_for_key("email:info@beta.com") == "hunt-beta"
    assert store.get_hunt_id_for_key("company:beta corp") == "hunt-beta"
    assert store.get_hunt_id_for_key("nonexistent:key") is None


def test_email_store_init_and_account_roundtrip(tmp_path: Path):
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
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "imap_username": "sales@example.com",
        "imap_secret_encrypted": "enc2",
        "use_tls": 1,
        "status": "active",
        "daily_send_limit": 50,
        "hourly_send_limit": 10,
        "last_test_at": "",
        "created_at": "2026-03-09T00:00:00Z",
        "updated_at": "2026-03-09T00:00:00Z",
    })
    account = store.get_account("acct_1")
    assert account is not None
    assert account["from_email"] == "sales@example.com"


def test_account_sort_order_and_reorder(tmp_path: Path):
    """Drag-to-reorder in the quotas page relies on `sort_order`.

    - Existing rows default to 0 (ties broken by created_at asc)
    - `reorder_accounts` rewrites indices so the i-th id in the list has
      sort_order = i
    - `next_sort_order` returns MAX(sort_order)+1 so newly created
      accounts append to the end of the rotation
    """
    db_path = tmp_path / "email.db"
    store = EmailStore(str(db_path))
    store.init_db()

    base = {
        "provider_type": "smtp",
        "from_name": "Ai Hunter",
        "from_email": "sales@example.com",
        "reply_to": "sales@example.com",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_username": "sales@example.com",
        "smtp_secret_encrypted": "",
        "imap_host": "",
        "imap_port": 993,
        "imap_username": "",
        "imap_secret_encrypted": "",
        "use_tls": 1,
        "status": "active",
        "daily_send_limit": 50,
        "hourly_send_limit": 10,
        "last_test_at": "",
        "secrets_ciphertext": b"",
        "graph_tenant_id": "",
        "graph_user_principal_name": "",
    }
    for i, created_at in enumerate(["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"]):
        store.upsert_account({
            **base,
            "id": f"acct_{i+1}",
            "from_email": f"a{i+1}@example.com",
            "created_at": created_at,
            "updated_at": created_at,
        })
    # All three should come back in created_at order (all sort_order=0
    # tiebreaker), so the rotation list is stable on first load.
    assert [a["id"] for a in store.list_accounts()] == ["acct_1", "acct_2", "acct_3"]

    # next_sort_order starts at 0 (empty would also be 0, but with rows
    # all at 0 the next is 1).
    assert store.next_sort_order() == 1

    # Drag acct_2 to the top, then acct_1 to the bottom.
    store.reorder_accounts(["acct_2", "acct_3", "acct_1"])
    assert [a["id"] for a in store.list_accounts()] == ["acct_2", "acct_3", "acct_1"]
    assert [a["sort_order"] for a in store.list_accounts()] == [0, 1, 2]

    # Duplicates are silently de-duped (defensive).
    store.reorder_accounts(["acct_1", "acct_1", "acct_2", "acct_3"])
    assert [a["sort_order"] for a in store.list_accounts()] == [0, 1, 2]

    # Empty list is a no-op.
    before = [a["sort_order"] for a in store.list_accounts()]
    store.reorder_accounts([])
    assert [a["sort_order"] for a in store.list_accounts()] == before


def test_message_lifecycle(tmp_path: Path):
    db_path = tmp_path / "email.db"
    store = EmailStore(str(db_path))
    store.init_db()
    store.upsert_account({
        "id": "acct_1",
        "provider_type": "graph",
        "from_name": "Sales",
        "from_email": "sales@test.com",
        "reply_to": "",
        "status": "active",
        "daily_send_limit": 100,
        "hourly_send_limit": 10,
        "last_test_at": "",
        "created_at": "2026-08-14T00:00:00+00:00",
        "updated_at": "2026-08-14T00:00:00+00:00",
        "secrets_ciphertext": b"",
        "graph_tenant_id": "",
        "graph_user_principal_name": "",
        "sort_order": 0,
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
    ready = store.list_pending_messages_ready("2026-03-09T01:00:00Z")
    assert len(ready) == 1
    store.mark_message_sent(
        "msg_1",
        provider_message_id="<mid>",
        thread_key="thread-1",
        sent_at="2026-03-09T01:05:00Z",
    )
    assert store.list_pending_messages_ready("2026-03-09T02:00:00Z") == []


def test_template_performance_aggregation(tmp_path: Path):
    db_path = tmp_path / "email.db"
    store = EmailStore(str(db_path))
    store.init_db()
    store.upsert_account({
        "id": "acct_1",
        "provider_type": "graph",
        "from_name": "Sales",
        "from_email": "sales@test.com",
        "reply_to": "",
        "status": "active",
        "daily_send_limit": 100,
        "hourly_send_limit": 10,
        "last_test_at": "",
        "created_at": "2026-08-14T00:00:00+00:00",
        "updated_at": "2026-08-14T00:00:00+00:00",
        "secrets_ciphertext": b"",
        "graph_tenant_id": "",
        "graph_user_principal_name": "",
        "sort_order": 0,
    })
    store.create_campaign({
        "id": "cmp_tpl",
        "hunt_id": "hunt_tpl",
        "email_account_id": "acct_1",
        "name": "Template Campaign",
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
    for idx, status in [(1, "scheduled"), (2, "replied")]:
        store.create_sequence({
            "id": f"seq_tpl_{idx}",
            "campaign_id": "cmp_tpl",
            "hunt_id": "hunt_tpl",
            "lead_key": f"lead_{idx}",
            "lead_email": f"buyer{idx}@acme.com",
            "lead_name": f"Lead {idx}",
            "decision_maker_name": "Jane",
            "decision_maker_title": "Buyer",
            "locale": "en_US",
            "generation_mode": "template_pool",
            "template_id": "tpl_123",
            "template_group": "en_US|decision_maker_verified|industrial_supply",
            "template_usage_index": idx,
            "template_max_send_count": 100,
            "status": status,
            "current_step": 0,
            "stop_reason": "",
            "replied_at": "2026-03-09T01:00:00Z" if status == "replied" else "",
            "last_sent_at": "",
            "next_scheduled_at": "2026-03-09T00:00:00Z",
            "created_at": "2026-03-09T00:00:00Z",
            "updated_at": "2026-03-09T00:00:00Z",
        })
    for idx in [1, 2]:
        store.create_message({
            "id": f"msg_tpl_{idx}",
            "sequence_id": f"seq_tpl_{idx}",
            "step_number": 1,
            "goal": "intro",
            "locale": "en_US",
            "subject": f"Hello {idx}",
            "body_text": "Body",
            "status": "sent",
            "scheduled_at": "2026-03-09T00:00:00Z",
            "sent_at": "2026-03-09T00:10:00Z",
            "provider_message_id": "",
            "thread_key": "",
            "failure_reason": "",
            "created_at": "2026-03-09T00:00:00Z",
            "updated_at": "2026-03-09T00:10:00Z",
        })

    summary = store.get_template_performance_for_campaign("cmp_tpl")

    assert summary["tpl_123"]["assigned_count"] == 2
    assert summary["tpl_123"]["sent_count"] == 2
    assert summary["tpl_123"]["replied_count"] == 1
    assert summary["tpl_123"]["reply_rate"] == 50.0
    assert summary["tpl_123"]["remaining_capacity"] == 98


def test_template_performance_uses_custom_thresholds(tmp_path: Path):
    db_path = tmp_path / "email.db"
    store = EmailStore(str(db_path))
    store.init_db()
    store.upsert_account({
        "id": "acct_1",
        "provider_type": "graph",
        "from_name": "Sales",
        "from_email": "sales@test.com",
        "reply_to": "",
        "status": "active",
        "daily_send_limit": 100,
        "hourly_send_limit": 10,
        "last_test_at": "",
        "created_at": "2026-08-14T00:00:00+00:00",
        "updated_at": "2026-08-14T00:00:00+00:00",
        "secrets_ciphertext": b"",
        "graph_tenant_id": "",
        "graph_user_principal_name": "",
        "sort_order": 0,
    })
    store.create_campaign({
        "id": "cmp_custom",
        "hunt_id": "hunt_custom",
        "email_account_id": "acct_1",
        "name": "Template Campaign",
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
    for idx in range(1, 4):
        store.create_sequence({
            "id": f"seq_custom_{idx}",
            "campaign_id": "cmp_custom",
            "hunt_id": "hunt_custom",
            "lead_key": f"lead_{idx}",
            "lead_email": f"buyer{idx}@acme.com",
            "lead_name": f"Lead {idx}",
            "decision_maker_name": "Jane",
            "decision_maker_title": "Buyer",
            "locale": "en_US",
            "generation_mode": "template_pool",
            "template_id": "tpl_custom",
            "template_group": "en_US|decision_maker_verified|industrial_supply",
            "template_usage_index": idx,
            "template_max_send_count": 5,
            "status": "scheduled",
            "current_step": 0,
            "stop_reason": "",
            "replied_at": "",
            "last_sent_at": "",
            "next_scheduled_at": "2026-03-09T00:00:00Z",
            "created_at": "2026-03-09T00:00:00Z",
            "updated_at": "2026-03-09T00:00:00Z",
        })
    summary = store.get_template_performance_for_campaign(
        "cmp_custom",
        underperforming_min_assigned=3,
        underperforming_min_reply_rate=5.0,
    )
    assert summary["tpl_custom"]["status"] == "underperforming"
    assert summary["tpl_custom"]["remaining_capacity"] == 2


def test_test_send_log_counts_toward_daily_quota(tmp_path: Path):
    """The quotas page bar and the scheduler's daily cap both call
    `count_sent_today_for_account`; that helper now folds in the
    `email_test_send_log` table so operator-driven test sends burn
    through the same per-account daily budget as production sends.
    """
    db_path = tmp_path / "email.db"
    store = EmailStore(str(db_path))
    store.init_db()
    # FK requires the account row to exist before any email_test_send_log
    # rows reference it.
    store.upsert_account({
        "id": "acct_a",
        "provider_type": "graph",
        "from_name": "Sales",
        "from_email": "sales@test.com",
        "reply_to": "",
        "status": "active",
        "daily_send_limit": 100,
        "hourly_send_limit": 10,
        "last_test_at": "",
        "created_at": "2026-08-14T00:00:00+00:00",
        "updated_at": "2026-08-14T00:00:00+00:00",
        "secrets_ciphertext": b"",
        "graph_tenant_id": "",
        "graph_user_principal_name": "",
        "sort_order": 0,
    })
    now_iso = "2026-08-14T10:00:00+00:00"
    # Empty to start with.
    assert store.count_sent_today_for_account("acct_a", now_iso=now_iso) == 0

    # A successful test send on `acct_a` ticks the counter by 1.
    store.record_test_send(
        account_id="acct_a",
        to_email="qa@example.com",
        subject="ping",
        body_text="body",
        provider="smtp",
        provider_message_id="<m1@x>",
        thread_key="t1",
        ok=True,
        failure_reason="",
        sent_at=now_iso,
    )
    assert store.count_sent_today_for_account("acct_a", now_iso=now_iso) == 1

    # A second one stacks.
    store.record_test_send(
        account_id="acct_a",
        to_email="qa@example.com",
        subject="ping 2",
        body_text="body",
        provider="smtp",
        provider_message_id="<m2@x>",
        thread_key="t2",
        ok=True,
        failure_reason="",
        sent_at=now_iso,
    )
    assert store.count_sent_today_for_account("acct_a", now_iso=now_iso) == 2
    # Failed attempts are recorded for audit but DON'T count toward
    # the budget — the user might hit a transient SMTP auth error and
    # we don't want to charge them for it.
    store.record_test_send(
        account_id="acct_a",
        to_email="qa@example.com",
        subject="ping failed",
        body_text="body",
        provider="smtp",
        provider_message_id="",
        thread_key="t3",
        ok=False,
        failure_reason="smtp_account_incomplete",
        sent_at=now_iso,
    )
    assert store.count_sent_today_for_account("acct_a", now_iso=now_iso) == 2

    # A different account has its own bucket.
    assert store.count_sent_today_for_account("acct_b", now_iso=now_iso) == 0

    # Old sends (before today's 00:00 UTC) are excluded.
    yesterday = "2026-08-13T23:59:59+00:00"
    store.record_test_send(
        account_id="acct_a",
        to_email="qa@example.com",
        subject="yesterday",
        body_text="body",
        provider="smtp",
        provider_message_id="<y@x>",
        thread_key="t4",
        ok=True,
        failure_reason="",
        sent_at=yesterday,
    )
    assert store.count_sent_today_for_account("acct_a", now_iso=now_iso) == 2


def test_manual_send_claim_uses_stable_recipient_identity(tmp_path: Path):
    store = EmailStore(str(tmp_path / "email.db"))
    store.init_db()
    now = "2026-09-21T12:30:00+00:00"

    first, _ = store.claim_manual_send(
        "hunt-1",
        0,
        1,
        recipient_email="Buyer@Example.com",
        claim_id="claim-1",
        now_iso=now,
    )
    moved, existing = store.claim_manual_send(
        "hunt-1",
        4,
        1,
        recipient_email="buyer@example.com",
        claim_id="claim-2",
        now_iso=now,
    )
    replacement, _ = store.claim_manual_send(
        "hunt-1",
        0,
        1,
        recipient_email="other@example.com",
        claim_id="claim-3",
        now_iso=now,
    )

    assert first is True
    assert moved is False
    assert existing and existing["id"] == "claim-1"
    assert replacement is True

    assert store.reserve_send_quota(
        "acct-1",
        "claim-1",
        daily_limit=20,
        hourly_limit=10,
        now_iso=now,
    ) is True
    store.release_manual_send_preparation(
        "claim-1",
        message_id="not-created",
        updated_at=now,
    )
    retried, _ = store.claim_manual_send(
        "hunt-1",
        4,
        1,
        recipient_email="buyer@example.com",
        claim_id="claim-4",
        now_iso=now,
    )
    assert retried is True
    with store._connect() as conn:
        quota_status = conn.execute(
            "SELECT status FROM email_quota_reservations WHERE message_id = 'claim-1'"
        ).fetchone()[0]
    assert quota_status == "released"


def test_manual_send_migration_prefers_confirmed_send(tmp_path: Path):
    store = EmailStore(str(tmp_path / "email.db"))
    store.init_db()
    now = "2026-09-21T12:30:00+00:00"
    store.upsert_account({
        "id": "acct-1",
        "provider_type": "graph",
        "from_name": "Sales",
        "from_email": "sales@example.com",
        "created_at": now,
        "updated_at": now,
    })
    store.create_campaign({
        "id": "campaign-1",
        "hunt_id": "hunt-1",
        "email_account_id": "acct-1",
        "name": "Manual",
        "created_at": now,
        "updated_at": now,
    })
    for suffix in ("failed", "sent"):
        sequence_id = f"sequence-{suffix}"
        message_id = f"message-{suffix}"
        store.create_sequence({
            "id": sequence_id,
            "campaign_id": "campaign-1",
            "hunt_id": "hunt-1",
            "lead_key": f"lead-{suffix}",
            "lead_email": "buyer@example.com",
            "created_at": now,
            "updated_at": now,
        })
        store.create_message({
            "id": message_id,
            "sequence_id": sequence_id,
            "step_number": 1,
            "goal": "intro",
            "locale": "en",
            "subject": suffix,
            "body_text": suffix,
            "status": suffix,
            "scheduled_at": now,
            "sent_at": now if suffix == "sent" else "",
            "created_at": now,
            "updated_at": now,
        })

    with store._connect() as conn:
        conn.execute("DELETE FROM manual_send_recipient_claims")
        conn.execute(
            "INSERT INTO manual_send_claims "
            "(id, hunt_id, sequence_index, sequence_number, status, message_id, created_at, updated_at) "
            "VALUES ('claim-failed', 'hunt-1', 0, 1, 'failed', 'message-failed', ?, ?)",
            (now, "2026-09-21T12:31:00+00:00"),
        )
        conn.execute(
            "INSERT INTO manual_send_claims "
            "(id, hunt_id, sequence_index, sequence_number, status, message_id, created_at, updated_at) "
            "VALUES ('claim-sent', 'hunt-1', 3, 1, 'sent', 'message-sent', ?, ?)",
            (now, now),
        )

    store.init_db()

    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, status FROM manual_send_recipient_claims "
            "WHERE hunt_id = 'hunt-1' AND recipient_email = 'buyer@example.com'"
        ).fetchall()
    assert [tuple(row) for row in rows] == [("claim-sent", "sent")]
