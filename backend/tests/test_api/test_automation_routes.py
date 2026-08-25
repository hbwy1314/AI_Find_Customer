from fastapi.testclient import TestClient

from api.app import create_app


def test_automation_routes(monkeypatch):
    app = create_app()
    client = TestClient(app)

    monkeypatch.setattr(
        "api.automation_routes.collect_automation_status",
        lambda hunts=None: {
            "hunt_jobs": {"queued": 1, "running": 2, "failed": 0},
            "hunts": {"running": 1, "pending": 0},
            "email_queue": {"pending": 3, "sent": 4, "failed": 1, "cancelled": 0},
            "features": {"email_auto_send_enabled": True, "email_reply_detection_enabled": True, "automation_summary_enabled": True, "automation_alerts_enabled": True},
        },
    )
    monkeypatch.setattr(
        "api.automation_routes.collect_automation_metrics",
        lambda hours=24, hunts=None: {
            "window_hours": hours,
            "emails": {"failed": 2},
            "hunt_jobs": {"queued": 1},
            "recent_failures": [],
        },
    )

    status = client.get("/api/v1/automation/status")
    metrics = client.get("/api/v1/automation/metrics?hours=2")
    health = client.get("/api/v1/automation/health")

    assert status.status_code == 200
    assert status.json()["hunt_jobs"]["queued"] == 1
    assert metrics.status_code == 200
    assert metrics.json()["window_hours"] == 2
    assert health.status_code == 200
    assert health.json()["backlog_email_messages"] == 3


def test_automation_job_routes(monkeypatch):
    app = create_app()
    client = TestClient(app)

    fake_job = {
        "id": "job-1",
        "status": "queued",
        "created_at": "2026-04-05T00:00:00+00:00",
        "updated_at": "2026-04-05T00:00:00+00:00",
        "started_at": "",
        "finished_at": "",
        "attempt_count": 1,
        "last_error": "",
        "last_hunt_id": "",
        "payload": {
            "website_url": "https://www.gdushun.com/",
            "description": "Find distributors",
            "product_keywords": ["micro switch"],
            "target_regions": ["United States"],
            "target_lead_count": 100,
            "enable_email_craft": True,
            "template_seed": {
                "source": "pre_generated",
                "template_profile": {"tone": "professional"},
                "template_plan": {"cta_strategy": "Ask a qualification question"},
            },
        },
    }

    class FakeQueue:
        def init_db(self):
            return None

        def enqueue(self, payload, now_iso):
            return "job-1"

        def get(self, job_id):
            if job_id == "missing":
                return None
            return fake_job

        def get_by_hunt_id(self, hunt_id):
            if hunt_id == "missing-hunt":
                return None
            job = dict(fake_job)
            job["last_hunt_id"] = hunt_id
            return job

        def list_jobs(self, limit=100):
            return [fake_job]

    monkeypatch.setattr("api.automation_routes._queue", lambda: FakeQueue())
    monkeypatch.setattr("api.automation_routes.load_hunt", lambda hunt_id: None)

    created = client.post("/api/v1/automation/jobs", json={
        "website_url": "https://www.gdushun.com/",
        "description": "Find distributors",
        "product_keywords": ["micro switch"],
        "target_regions": ["United States"],
        "target_lead_count": 100,
        "enable_email_craft": True,
    })
    listed = client.get("/api/v1/automation/jobs")
    detail = client.get("/api/v1/automation/jobs/job-1")
    by_hunt = client.get("/api/v1/automation/jobs/by-hunt/hunt-1")
    missing = client.get("/api/v1/automation/jobs/missing")
    missing_by_hunt = client.get("/api/v1/automation/jobs/by-hunt/missing-hunt")

    assert created.status_code == 200
    assert created.json()["job_id"] == "job-1"
    assert listed.status_code == 200
    assert listed.json()[0]["website_url"] == "https://www.gdushun.com/"
    assert listed.json()[0]["template_seed"]["template_profile"]["tone"] == "professional"
    assert detail.status_code == 200
    assert detail.json()["target_lead_count"] == 100
    assert detail.json()["template_seed"]["template_plan"]["cta_strategy"] == "Ask a qualification question"
    assert by_hunt.status_code == 200
    assert by_hunt.json()["last_hunt_id"] == "hunt-1"
    assert missing.status_code == 404
    assert missing_by_hunt.status_code == 404


def test_create_automation_job_from_hunt(monkeypatch):
    app = create_app()
    client = TestClient(app)

    queued_payloads = []

    class FakeQueue:
        def init_db(self):
            return None

        def enqueue(self, payload, now_iso):
            queued_payloads.append(payload)
            return "job-2"

        def get(self, job_id):
            return {
                "id": "job-2",
                "status": "queued",
                "created_at": "2026-04-05T00:00:00+00:00",
                "updated_at": "2026-04-05T00:00:00+00:00",
                "started_at": "",
                "finished_at": "",
                "attempt_count": 0,
                "last_error": "",
                "last_hunt_id": "",
                "payload": queued_payloads[-1] if queued_payloads else {},
            }

    monkeypatch.setattr("api.automation_routes._queue", lambda: FakeQueue())
    monkeypatch.setattr(
        "api.automation_routes.load_hunt",
        lambda hunt_id: {
            "payload": {
                "website_url": "https://www.gdushun.com/",
                "description": "Find distributors",
                "product_keywords": ["micro switch"],
                "target_customer_profile": "Distributors",
                "target_regions": ["United States"],
                "uploaded_file_ids": ["file-1"],
            }
        } if hunt_id == "hunt-1" else None,
    )

    resp = client.post("/api/v1/automation/jobs/from-hunt/hunt-1", json={
        "target_lead_count": 300,
        "max_rounds": 12,
        "min_new_leads_threshold": 2,
        "enable_email_craft": True,
        "email_template_examples": ["Dear Sir/Madam"],
        "email_template_notes": "Keep it concise",
    })
    missing = client.post("/api/v1/automation/jobs/from-hunt/missing", json={})

    assert resp.status_code == 200
    assert resp.json()["job_id"] == "job-2"
    assert queued_payloads[0]["website_url"] == "https://www.gdushun.com/"
    assert queued_payloads[0]["target_lead_count"] == 300
    assert queued_payloads[0]["enable_email_craft"] is True
    assert missing.status_code == 404


def test_create_automation_job_from_hunt_carries_prior_leads(monkeypatch):
    """Regression for the "提交后续任务 = 重新从 0 挖" bug.

    Before this fix, `create_automation_job_from_hunt` only carried
    the *config* fields (website_url, keywords, ...) through to the
    new job, but dropped the prior hunt's `result.leads`. The consumer
    then created a fresh `HuntRequest` with `leads=[]`, so the operator's
    "continue mining" effectively discarded all the work the previous
    hunt had done. The fix forwards the prior lead dicts as
    `existing_leads` so the new hunt's initial state starts from the
    real lead count and the lead-extract agent's dedup baseline is
    correct.
    """
    app = create_app()
    client = TestClient(app)

    queued_payloads: list[dict] = []

    class FakeQueue:
        def init_db(self):
            return None

        def enqueue(self, payload, now_iso):
            queued_payloads.append(payload)
            return "job-x"

        def get(self, job_id):
            return {
                "id": "job-x",
                "status": "queued",
                "created_at": "2026-04-05T00:00:00+00:00",
                "updated_at": "2026-04-05T00:00:00+00:00",
                "started_at": "",
                "finished_at": "",
                "attempt_count": 0,
                "last_error": "",
                "last_hunt_id": "",
                "payload": queued_payloads[-1] if queued_payloads else {},
            }

    prior_leads = [
        {
            "company_name": "ACME Vape",
            "website": "https://acme.example.com/",
            "emails": ["sales@acme.example.com"],
            "fit_score": 0.9,
        },
        {
            "company_name": "Beta Wholesale",
            "website": "https://beta.example.com/",
            "emails": ["hello@beta.example.com"],
            "fit_score": 0.7,
        },
    ]

    monkeypatch.setattr("api.automation_routes._queue", lambda: FakeQueue())
    monkeypatch.setattr(
        "api.automation_routes.load_hunt",
        lambda hunt_id: {
            "payload": {
                "website_url": "https://www.gdushun.com/",
                "product_keywords": ["micro switch"],
                "target_customer_profile": "Distributors",
                "target_regions": ["United States"],
            },
            # This is the completed-hunt state on disk; the bug
            # was that we never read this even though we had it.
            "result": {
                "leads": prior_leads,
                "hunt_round": 3,
            },
        } if hunt_id == "hunt-prior" else None,
    )

    resp = client.post("/api/v1/automation/jobs/from-hunt/hunt-prior", json={
        "target_lead_count": 300,
        "max_rounds": 5,
        "min_new_leads_threshold": 3,
        "enable_email_craft": False,
        "email_template_examples": [],
        "email_template_notes": "",
    })
    assert resp.status_code == 200

    # The new job's payload MUST carry the prior lead dicts so the
    # consumer can seed them into the new hunt's initial state.
    assert queued_payloads, "enqueue was never called"
    assert "existing_leads" in queued_payloads[0]
    assert queued_payloads[0]["existing_leads"] == prior_leads
    # Sanity: the full lead dict (with scoring fields) is preserved,
    # not just email addresses.
    assert queued_payloads[0]["existing_leads"][0]["fit_score"] == 0.9

    # If the prior hunt has no leads yet (just started), we still
    # need an empty list — not `None` or a missing key — so the
    # downstream Pydantic validator doesn't choke.
    queued_payloads.clear()
    monkeypatch.setattr(
        "api.automation_routes.load_hunt",
        lambda hunt_id: {
            "payload": {"website_url": "https://x.example.com/"},
            "result": {},  # no leads yet
        } if hunt_id == "hunt-empty" else None,
    )
    resp2 = client.post("/api/v1/automation/jobs/from-hunt/hunt-empty", json={
        "target_lead_count": 50, "max_rounds": 3, "min_new_leads_threshold": 5,
        "enable_email_craft": False, "email_template_examples": [], "email_template_notes": "",
    })
    assert resp2.status_code == 200
    assert queued_payloads[0]["existing_leads"] == []


def test_create_automation_job_from_hunt_falls_back_to_top_level_fields(monkeypatch):
    """Regression for "Hunt has no reusable payload" 422 on legacy hunts.

    Old hunts written by `_initialize_hunt` only set top-level fields
    (`hunt['website_url']`, `hunt['product_keywords']`, ...) without
    nesting them under a `payload` key. The previous implementation
    looked at `hunt.get('payload')` exclusively, so every legacy hunt
    422'd on resume — even hunts that clearly had a valid config. The
    fix falls back to top-level fields plus the LangGraph state under
    `hunt['result']`.
    """
    app = create_app()
    client = TestClient(app)

    queued_payloads: list[dict] = []

    class FakeQueue:
        def init_db(self):
            return None

        def enqueue(self, payload, now_iso):
            queued_payloads.append(payload)
            return "job-fb"

        def get(self, job_id):
            return {
                "id": "job-fb",
                "status": "queued",
                "created_at": "2026-08-24T00:00:00+00:00",
                "updated_at": "2026-08-24T00:00:00+00:00",
                "started_at": "",
                "finished_at": "",
                "attempt_count": 0,
                "last_error": "",
                "last_hunt_id": "",
                "payload": queued_payloads[-1] if queued_payloads else {},
            }

    monkeypatch.setattr("api.automation_routes._queue", lambda: FakeQueue())
    # Legacy layout: top-level fields, NO `payload` key, plus a
    # `result` with the LangGraph state.
    monkeypatch.setattr(
        "api.automation_routes.load_hunt",
        lambda hunt_id: {
            "website_url": "https://romioecig.com/",
            "product_keywords": ["vape", "e-cigarette"],
            "target_customer_profile": "wholesale distributors",
            "target_regions": ["United Kingdom"],
            "result": {
                "description": "Target UK-based E-cigarette Wholesale Distributors",
                "leads": [
                    {"company_name": "ACME", "website": "https://acme.example.com/"}
                ],
            },
        } if hunt_id == "hunt-legacy" else None,
    )

    resp = client.post("/api/v1/automation/jobs/from-hunt/hunt-legacy", json={
        "target_lead_count": 250, "max_rounds": 5, "min_new_leads_threshold": 3,
        "enable_email_craft": False, "email_template_examples": [], "email_template_notes": "",
    })
    assert resp.status_code == 200
    assert queued_payloads, "enqueue was never called"

    p = queued_payloads[0]
    # Top-level config fields were carried through, not 422'd.
    assert p["website_url"] == "https://romioecig.com/"
    assert p["product_keywords"] == ["vape", "e-cigarette"]
    assert p["target_customer_profile"] == "wholesale distributors"
    assert p["target_regions"] == ["United Kingdom"]
    # The result-layer description filled in the missing key.
    assert p["description"] == "Target UK-based E-cigarette Wholesale Distributors"
    # And the result-layer leads were carried through as before.
    assert p["existing_leads"] == [
        {"company_name": "ACME", "website": "https://acme.example.com/"}
    ]

    # And the truly-empty case (no payload, no top-level fields, no
    # result) still gets a clean 422 — the fallback must not silently
    # enqueue a no-op job.
    queued_payloads.clear()
    monkeypatch.setattr(
        "api.automation_routes.load_hunt",
        lambda hunt_id: {"status": "completed"} if hunt_id == "hunt-bare" else None,
    )
    resp_empty = client.post("/api/v1/automation/jobs/from-hunt/hunt-bare", json={
        "target_lead_count": 50, "max_rounds": 3, "min_new_leads_threshold": 5,
        "enable_email_craft": False, "email_template_examples": [], "email_template_notes": "",
    })
    assert resp_empty.status_code == 422
    assert queued_payloads == []


def test_cancel_and_retry_automation_job(monkeypatch):
    app = create_app()
    client = TestClient(app)

    state = {
        "id": "job-3",
        "status": "queued",
        "created_at": "2026-04-05T00:00:00+00:00",
        "updated_at": "2026-04-05T00:00:00+00:00",
        "started_at": "",
        "finished_at": "",
        "attempt_count": 1,
        "last_error": "",
        "last_hunt_id": "",
        "payload": {"website_url": "https://www.gdushun.com/"},
    }

    class FakeQueue:
        def init_db(self):
            return None

        def get(self, job_id):
            if job_id != "job-3":
                return None
            return state.copy()

        def cancel(self, job_id, updated_at):
            state["status"] = "failed"
            state["finished_at"] = updated_at
            state["updated_at"] = updated_at
            state["last_error"] = "Cancelled by user"

        def retry_now(self, job_id, updated_at):
            state["status"] = "queued"
            state["updated_at"] = updated_at
            state["finished_at"] = ""

    monkeypatch.setattr("api.automation_routes._queue", lambda: FakeQueue())
    monkeypatch.setattr("api.automation_routes.load_hunt", lambda hunt_id: None)
    requested = []
    monkeypatch.setattr("api.automation_routes.request_hunt_cancel", lambda hunt_id, reason="": requested.append((hunt_id, reason)) or True)

    cancelled = client.post("/api/v1/automation/jobs/job-3/cancel")
    retried = client.post("/api/v1/automation/jobs/job-3/retry")
    missing = client.post("/api/v1/automation/jobs/missing/retry")

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "failed"
    assert "Cancelled by user" in cancelled.json()["last_error"]
    assert requested == []
    assert retried.status_code == 200
    assert retried.json()["status"] == "queued"
    assert missing.status_code == 404


def test_cancel_running_job_requests_hunt_cancel(monkeypatch):
    app = create_app()
    client = TestClient(app)

    state = {
        "id": "job-4",
        "status": "running",
        "created_at": "2026-04-05T00:00:00+00:00",
        "updated_at": "2026-04-05T00:00:00+00:00",
        "started_at": "2026-04-05T00:00:10+00:00",
        "finished_at": "",
        "attempt_count": 1,
        "last_error": "",
        "last_hunt_id": "hunt-123",
        "payload": {"website_url": "https://www.gdushun.com/"},
    }

    class FakeQueue:
        def init_db(self):
            return None

        def get(self, job_id):
            if job_id != "job-4":
                return None
            return state.copy()

        def cancel(self, job_id, updated_at):
            state["status"] = "failed"
            state["finished_at"] = updated_at
            state["updated_at"] = updated_at
            state["last_error"] = "Cancelled by user"
            state["progress_stage"] = "cancelled"

    requested = []
    monkeypatch.setattr("api.automation_routes._queue", lambda: FakeQueue())
    monkeypatch.setattr("api.automation_routes.load_hunt", lambda hunt_id: None)
    monkeypatch.setattr("api.automation_routes.request_hunt_cancel", lambda hunt_id, reason="": requested.append((hunt_id, reason)) or True)

    cancelled = client.post("/api/v1/automation/jobs/job-4/cancel")

    assert cancelled.status_code == 200
    assert requested == [("hunt-123", "Cancelled by user via automation job")]


def test_delete_completed_job_removes_row_without_cancel(monkeypatch):
    """Deleting a terminal-status job should not touch the cancel path."""
    app = create_app()
    client = TestClient(app)

    state = {
        "id": "job-done",
        "status": "completed",
        "created_at": "2026-04-05T00:00:00+00:00",
        "updated_at": "2026-04-05T01:00:00+00:00",
        "started_at": "2026-04-05T00:00:10+00:00",
        "finished_at": "2026-04-05T01:00:00+00:00",
        "attempt_count": 1,
        "last_error": "",
        "last_hunt_id": "hunt-done",
        "payload": {"website_url": "https://x.example.com/"},
    }

    cancelled_calls: list[str] = []
    deleted_ids: list[str] = []

    class FakeQueue:
        def init_db(self):
            return None

        def get(self, job_id):
            return state.copy() if job_id == "job-done" else None

        def cancel(self, job_id, updated_at):
            cancelled_calls.append(job_id)

        def delete_job(self, job_id):
            deleted_ids.append(job_id)
            return True

    requested = []
    monkeypatch.setattr("api.automation_routes._queue", lambda: FakeQueue())
    monkeypatch.setattr("api.automation_routes.request_hunt_cancel", lambda hunt_id, reason="": requested.append((hunt_id, reason)) or True)

    resp = client.delete("/api/v1/automation/jobs/job-done")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "job_id": "job-done", "deleted": True, "cancelled_first": False}
    # A completed job must NOT go through cancel — that would clobber
    # finished_at and re-open the chance of a duplicate worker.
    assert cancelled_calls == []
    # We did call delete_job, and we did NOT ask the hunt to cancel.
    assert deleted_ids == ["job-done"]
    assert requested == []


def test_delete_running_job_cancels_hunt_first(monkeypatch):
    """Deleting a still-running job must cancel it (and its hunt) first,
    otherwise the consumer's next mark_completed / mark_failed write
    against a deleted row would surface as an integrity error."""
    app = create_app()
    client = TestClient(app)

    state = {
        "id": "job-live",
        "status": "running",
        "created_at": "2026-04-05T00:00:00+00:00",
        "updated_at": "2026-04-05T00:01:00+00:00",
        "started_at": "2026-04-05T00:00:10+00:00",
        "finished_at": "",
        "attempt_count": 1,
        "last_error": "",
        "last_hunt_id": "hunt-live",
        "payload": {"website_url": "https://x.example.com/"},
    }

    cancelled = []
    deleted = []
    hunt_cancels = []

    class FakeQueue:
        def init_db(self):
            return None

        def get(self, job_id):
            return state.copy() if job_id == "job-live" else None

        def cancel(self, job_id, updated_at):
            cancelled.append(job_id)
            state["status"] = "failed"
            state["finished_at"] = updated_at
            state["last_error"] = "Cancelled by user"
            state["progress_stage"] = "cancelled"

        def delete_job(self, job_id):
            deleted.append(job_id)
            return True

    monkeypatch.setattr("api.automation_routes._queue", lambda: FakeQueue())
    monkeypatch.setattr(
        "api.automation_routes.request_hunt_cancel",
        lambda hunt_id, reason="": hunt_cancels.append((hunt_id, reason)) or True,
    )

    resp = client.delete("/api/v1/automation/jobs/job-live")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["cancelled_first"] is True
    assert body["deleted"] is True
    # Order matters: cancel must complete before delete.
    assert cancelled == ["job-live"]
    assert deleted == ["job-live"]
    assert hunt_cancels == [("hunt-live", "Cancelled by user via job delete")]


def test_delete_missing_job_returns_404(monkeypatch):
    app = create_app()
    client = TestClient(app)

    class FakeQueue:
        def init_db(self):
            return None

        def get(self, job_id):
            return None

        def cancel(self, job_id, updated_at):
            raise AssertionError("cancel must not be called for missing job")

        def delete_job(self, job_id):
            raise AssertionError("delete_job must not be called for missing job")

    monkeypatch.setattr("api.automation_routes._queue", lambda: FakeQueue())
    resp = client.delete("/api/v1/automation/jobs/never-existed")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()
