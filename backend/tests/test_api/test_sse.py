"""Tests for api/sse.py — SSE streaming, event format, hunt lifecycle events."""

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from api.routes import _hunts, _sse_queues
from api.sse import _broadcast_reply, _event_generator, _reply_subscribers, _sse_event


@pytest.fixture
def app():
    _hunts.clear()
    return create_app()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestSseEventFormat:
    def test_event_format(self):
        result = _sse_event("stage_change", {"stage": "insight"})
        assert result.startswith("event: stage_change\n")
        assert "data: " in result
        assert result.endswith("\n\n")

        data_line = result.split("data: ")[1].strip()
        parsed = json.loads(data_line)
        assert parsed["stage"] == "insight"

    def test_event_unicode(self):
        result = _sse_event("progress", {"message": "Verarbeitung läuft"})
        assert "Verarbeitung läuft" in result


class TestEventGenerator:
    @pytest.mark.asyncio
    async def test_completed_hunt_emits_completed_event(self):
        _hunts["done-1"] = {
            "status": "completed",
            "result": {
                "leads": [{"company": "A"}, {"company": "B"}],
                "email_sequences": [{"locale": "en"}],
                "hunt_round": 3,
                "used_keywords": ["kw1", "kw2"],
            },
            "current_stage": "email_craft",
            "hunt_round": 3,
            "leads_count": 2,
            "email_sequences_count": 1,
            "error": None,
        }

        queue: asyncio.Queue = asyncio.Queue()
        events = []
        async for event in _event_generator("done-1", queue):
            events.append(event)

        # heartbeat + completed
        assert len(events) == 2
        assert "event: heartbeat" in events[0]
        assert "event: completed" in events[1]
        data = json.loads(events[1].split("data: ")[1].strip())
        assert data["leads_count"] == 2
        assert data["email_sequences_count"] == 1

    @pytest.mark.asyncio
    async def test_failed_hunt_emits_failed_event(self):
        _hunts["fail-1"] = {
            "status": "failed",
            "result": None,
            "current_stage": None,
            "hunt_round": 0,
            "leads_count": 0,
            "email_sequences_count": 0,
            "error": "API key invalid",
        }

        queue: asyncio.Queue = asyncio.Queue()
        events = []
        async for event in _event_generator("fail-1", queue):
            events.append(event)

        # heartbeat + failed
        assert len(events) == 2
        assert "event: heartbeat" in events[0]
        assert "event: failed" in events[1]
        data = json.loads(events[1].split("data: ")[1].strip())
        assert data["error"] == "API key invalid"

    @pytest.mark.asyncio
    async def test_queue_receives_broadcast_events(self):
        """Test that events pushed to the queue are yielded by the generator."""
        _hunts["running-1"] = {
            "status": "running",
            "result": None,
            "current_stage": "search",
            "hunt_round": 1,
            "leads_count": 5,
            "email_sequences_count": 0,
            "error": None,
        }

        queue: asyncio.Queue = asyncio.Queue()
        # Pre-load events into the queue
        queue.put_nowait(("stage_change", {"stage": "lead_extract", "hunt_round": 1, "leads_count": 5}))
        queue.put_nowait(("completed", {"leads_count": 10, "email_sequences_count": 0, "hunt_round": 2}))

        events = []
        async for event in _event_generator("running-1", queue):
            events.append(event)

        # heartbeat + stage_change + completed
        assert len(events) == 3
        assert "event: heartbeat" in events[0]
        assert "event: stage_change" in events[1]
        assert "event: completed" in events[2]


class TestSseEndpoint:
    @pytest.mark.asyncio
    async def test_stream_not_found(self, client):
        resp = await client.get("/api/v1/hunts/nonexistent/stream")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_stream_completed_hunt(self, client):
        _hunts["stream-1"] = {
            "status": "completed",
            "result": {
                "leads": [],
                "email_sequences": [],
                "hunt_round": 1,
                "used_keywords": [],
            },
            "current_stage": "email_craft",
            "hunt_round": 1,
            "leads_count": 0,
            "email_sequences_count": 0,
            "error": None,
        }

        resp = await client.get("/api/v1/hunts/stream-1/stream")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

        body = resp.text
        assert "event: completed" in body

    @pytest.mark.asyncio
    async def test_stream_automation_job(self, client, monkeypatch):
        fake_job = {
            "id": "job-1",
            "status": "queued",
            "created_at": "2026-04-05T00:00:00+00:00",
            "updated_at": "2026-04-05T00:00:00+00:00",
            "started_at": "",
            "finished_at": "",
            "claimed_by": "",
            "attempt_count": 0,
            "last_error": "",
            "last_hunt_id": "",
            "progress_stage": "queued",
            "progress_message": "Waiting for consumer to claim",
            "template_seed_status": "pending",
            "template_seed_source": "",
            "payload": {
                "website_url": "https://www.gdushun.com/",
                "description": "Find distributors",
                "product_keywords": ["micro switch"],
                "target_regions": ["United States"],
                "target_lead_count": 100,
                "enable_email_craft": True,
            },
        }

        class FakeQueue:
            def __init__(self):
                self.calls = 0

            def get(self, job_id):
                if job_id != "job-1":
                    return None
                self.calls += 1
                if self.calls == 1:
                    return fake_job
                completed = dict(fake_job)
                completed["status"] = "completed"
                completed["progress_stage"] = "completed"
                completed["progress_message"] = "Queue job completed successfully"
                completed["finished_at"] = "2026-04-05T00:01:00+00:00"
                return completed

        async def _fast_sleep(_: float):
            return None

        monkeypatch.setattr("api.sse._automation_job_queue", lambda: FakeQueue())
        monkeypatch.setattr("api.sse.asyncio.sleep", _fast_sleep)

        resp = await client.get("/api/v1/automation/jobs/job-1/stream")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        assert "event: heartbeat" in resp.text
        assert "Waiting for consumer to claim" in resp.text


class TestReplyBroadcast:
    """Unit tests for `_broadcast_reply` — the in-process fan-out used
    by the reply-detection loop to push `reply` events to every open
    /replies/stream subscriber.
    """

    def setup_method(self):
        # Each test gets a clean subscriber list so it can't leak
        # state into the next one.
        _reply_subscribers.clear()

    def teardown_method(self):
        _reply_subscribers.clear()

    def test_broadcast_fans_out_to_all_subscribers(self):
        q1: asyncio.Queue = asyncio.Queue()
        q2: asyncio.Queue = asyncio.Queue()
        _reply_subscribers.extend([q1, q2])

        payload = {"id": "abc-123", "from_email": "buyer@acme.com", "subject": "Re: hello"}
        _broadcast_reply(payload)

        assert q1.get_nowait() == payload
        assert q2.get_nowait() == payload

    def test_broadcast_drops_oldest_on_full_queue(self):
        # Simulate a slow client: queue is full to the brim before we
        # try to push. The fan-out must drop the oldest event rather
        # than block, otherwise the reply-detection loop would hang
        # on a stuck browser tab.
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        _reply_subscribers.append(q)
        q.put_nowait({"id": "old-1"})
        q.put_nowait({"id": "old-2"})

        _broadcast_reply({"id": "new-1"})

        # Oldest entry is gone, the new event is at the back, and
        # exactly two events are in the queue.
        drained = []
        while not q.empty():
            drained.append(q.get_nowait())
        assert drained == [{"id": "old-2"}, {"id": "new-1"}]

    def test_broadcast_no_subscribers_is_noop(self):
        # No one listening — should not raise, should not allocate.
        _broadcast_reply({"id": "x"})
        assert _reply_subscribers == []


class TestReplyStreamEndpoint:
    """Integration tests for GET /api/v1/replies/stream."""

    @pytest.mark.asyncio
    async def test_stream_returns_event_stream(self, client, monkeypatch):
        # The reply-event generator normally blocks forever (heartbeat
        # loop). Replace it with a deterministic two-event sequence
        # so the test can finish quickly.
        async def fake_generator():
            yield _sse_event("heartbeat", {"connected_at": "2026-08-25T00:00:00+00:00"})
            yield _sse_event("reply", {"id": "evt-1", "from_email": "buyer@acme.com"})

        monkeypatch.setattr("api.sse._reply_event_generator", fake_generator)

        # Need a session for the auth dep; the ASGI client uses
        # testclient which is in _LOCAL_HOSTS so require_api_access
        # short-circuits when API_ACCESS_TOKEN is unset. Make sure
        # no token is configured in this test.
        from config import settings as settings_mod
        original = settings_mod.get_settings()
        monkeypatch.setattr(original, "api_access_token", "")

        resp = await client.get("/api/v1/replies/stream")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        body = resp.text
        assert "event: heartbeat" in body
        assert "event: reply" in body
        assert "buyer@acme.com" in body
