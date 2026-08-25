from automation.notifier import (
    render_alert_text,
    render_discovery_batch_text,
    render_hunt_completed_text,
    render_hunt_failed_text,
    render_hunt_started_text,
    render_reply_detected_text,
    render_send_batch_text,
    render_summary_text,
)


def test_render_summary_text_includes_key_counts():
    text = render_summary_text(
        {
            "window_hours": 2,
            "hunt_jobs": {"completed": 3, "failed": 1, "queued": 2, "running": 1, "retrying": 2},
            "hunts": {"created": 4, "completed": 3, "failed": 1, "new_leads": 120, "generated_email_sequences": 44},
            "emails": {"queued": 20, "sent": 18, "failed": 2, "replied": 1, "active_campaigns": 2, "active_sequences": 14, "replied_sequences": 1},
            "status_snapshot": {"hunts": {"running_details": [{"website_url": "https://www.gdushun.com/", "current_stage": "lead_extract", "leads_count": 4, "email_sequences_count": 0}]}},
            "recent_completed_hunts": [{"website_url": "https://www.gdushun.com/", "lead_count": 88, "email_sequence_count": 30}],
            "recent_failed_hunts": [{"website_url": "https://retry.example.com", "current_stage": "lead_extract", "error": "minimax 429", "retry_status": "queued_retry", "retry_attempts": 2}],
            "top_failure_reasons": [{"failure_reason": "smtp_timeout", "count": 2}],
            "recent_failures": [{"lead_email": "buyer@example.com", "subject": "Hello", "failure_reason": "smtp_timeout"}],
        }
    )
    assert "最近 2 小时" in text
    assert "Hunt 已创建 4" in text
    assert "新增企业 120" in text
    assert "已发送 18" in text
    assert "队列重试中 2" in text
    assert "当前运行中:" in text
    assert "最近失败Hunt:" in text
    assert "失败原因Top:" in text


def test_render_alert_text():
    text = render_alert_text(
        {"hunt_jobs": {"queued": 25}, "email_queue": {"pending": 30}},
        {"emails": {"failed": 12}},
    )
    assert "待执行 hunt_jobs: 25" in text
    assert "待发送邮件: 30" in text
    assert "最近窗口失败发送: 12" in text


def test_render_hunt_started_and_completed_text():
    started = render_hunt_started_text(
        {
            "website_url": "https://www.gdushun.com/",
            "target_regions": ["United States"],
            "target_lead_count": 100,
            "enable_email_craft": True,
            "description": "Find distributors",
        },
        hunt_id="hunt-123",
    )
    completed = render_hunt_completed_text(
        {
            "hunt_id": "hunt-123",
            "website_url": "https://www.gdushun.com/",
            "lead_count": 88,
            "email_sequence_count": 30,
            "campaign": {"campaign_id": "camp-1", "status": "active"},
        }
    )
    failed = render_hunt_failed_text(
        {"website_url": "https://www.gdushun.com/", "target_lead_count": 100, "description": "Find distributors"},
        error_message="timeout",
    )
    assert "任务开始" in started
    assert "官网: https://www.gdushun.com/" in started
    assert "任务结束" in completed
    assert "新增企业: 88" in completed
    assert "主要目标官网: https://www.gdushun.com/" in completed
    assert "任务失败" in failed


def test_render_discovery_and_send_batch_text():
    discovery = render_discovery_batch_text([
        {"company_name": "Acme", "website": "https://acme.com", "email_count": 2},
        {"company_name": "Beta", "website": "https://beta.com", "email_count": 1},
    ])
    sending = render_send_batch_text([
        {"company_name": "Acme", "lead_email": "buyer@acme.com", "subject": "Hello"},
        {"company_name": "Beta", "lead_email": "sales@beta.com", "subject": "Offer"},
    ])
    assert "新增企业" in discovery
    assert "Acme" in discovery
    assert "邮件已发送" in sending
    assert "buyer@acme.com" in sending


# ── render_reply_detected_text ──────────────────────────────────
# Feishu push notification body for inbound replies matched to a
# sent message. Renders up to 10 matches per batch; an N+1 line lists
# the remaining count so the Feishu text stays under the 4KB limit.


def test_render_reply_detected_text_empty_returns_empty_string():
    assert render_reply_detected_text([]) == ""


def test_render_reply_detected_text_basic():
    text = render_reply_detected_text([
        {
            "lead_email": "buyer@acme.com",
            "lead_name": "Alice from Acme",
            "subject": "Re: Hello",
            "snippet": "Thanks for reaching out — let's talk next week.",
        },
    ])
    assert "AI Hunter 收到回信 | 本轮 1 条" in text
    assert "Alice from Acme <buyer@acme.com>" in text
    assert "Re: Hello" in text
    assert "Thanks for reaching out" in text
    assert "已自动停止后续跟进邮件" in text


def test_render_reply_detected_text_caps_at_10_and_announces_remainder():
    matches = [
        {
            "lead_email": f"buyer{i}@acme.com",
            "lead_name": f"Buyer {i}",
            "subject": f"Re: Quote #{i}",
            "snippet": f"snippet {i}",
        }
        for i in range(12)
    ]
    text = render_reply_detected_text(matches)
    assert "本轮 12 条" in text
    # 10 leads in the body, 12th line is the remainder footer, 13th is
    # the auto-stop reminder.
    assert "其余 2 条已省略" in text
    # The 11th and 12th lead are dropped from the body.
    assert "buyer10@acme.com" not in text
    assert "buyer11@acme.com" not in text
    # The first 10 still made it.
    assert "buyer0@acme.com" in text
    assert "buyer9@acme.com" in text


def test_render_reply_detected_text_falls_back_to_local_part_when_no_name():
    text = render_reply_detected_text([
        {
            "lead_email": "sales@beta.com",
            "lead_name": "",
            "subject": "Re: Quote",
            "snippet": "",
        },
    ])
    # No name — fall back to the local part of the address (no angle brackets).
    assert "sales" in text
    assert "<sales@beta.com>" not in text
    # Empty snippet must not produce a dangling "  " line — verify
    # the lead line is immediately followed by the auto-stop footer
    # (no extra line in between).
    body_lines = text.splitlines()
    lead_line = next(l for l in body_lines if l.startswith("- sales"))
    footer_line = "已自动停止后续跟进邮件，可到 AI Hunter 详情页查看对话。"
    assert body_lines[body_lines.index(lead_line) + 1] == footer_line


def test_render_reply_detected_text_truncates_long_subject_and_snippet():
    long_subject = "A" * 200
    long_snippet = "B" * 300
    text = render_reply_detected_text([
        {"lead_email": "x@y.com", "lead_name": "X", "subject": long_subject, "snippet": long_snippet},
    ])
    # 60-char cap on subject, 120-char cap on snippet.
    assert "A" * 60 in text
    assert "A" * 61 not in text
    assert "B" * 120 in text
    assert "B" * 121 not in text
