import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from api import hunt_store
from automation.job_queue import HuntJobQueue


def _accept_in_process(args):
    directory, hid = args
    hunt_store.get_settings = lambda: SimpleNamespace(hunts_dir=directory)
    return len(hunt_store.accept_new_leads(hid, [{"company_name": "Acme"}]))


@pytest.fixture
def tasks(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hunt_store,
        "get_settings",
        lambda: SimpleNamespace(
            hunts_dir=str(tmp_path),
            automation_queue_db_path=str(tmp_path / "queue.db"),
        ),
    )
    return tmp_path


def save(hid, leads):
    hunt_store.save_hunt(hid, {"result": {"leads": leads}})


def test_deleted_last_source_releases_customer(tasks):
    lead = {"company_name": "Acme", "website": "https://acme.de"}
    save("old", [lead])
    save("other", [lead])
    save("new", [])
    hunt_store.delete_hunt("old")
    assert hunt_store.accept_new_leads("new", [lead]) == []
    hunt_store.delete_hunt("other")
    assert hunt_store.accept_new_leads("new", [lead]) == [lead]


def test_explicit_empty_result_does_not_restore_seed(tasks):
    hunt_store.save_hunt("a", {"existing_leads": [{"company_name": "Old"}], "result": {"leads": []}})
    assert hunt_store.current_lead_keys() == set()
    save("b", [])
    assert hunt_store.accept_new_leads("b", [{"company_name": "Old"}])


def test_inherited_baseline_is_preserved(tasks):
    baseline = [{"company_name": f"Customer {i}"} for i in range(19)]
    save("old", baseline)
    hunt_store.save_hunt("retry", {"existing_leads": baseline, "result": None})
    assert hunt_store.accept_new_leads("retry", [{"company_name": "New"}])
    assert len(hunt_store.current_leads(hunt_store.load_hunt("retry"))) == 20


def test_deleted_worker_cannot_resurrect_task(tasks):
    save("old", [])
    hunt_store.delete_hunt("old")
    with pytest.raises(RuntimeError):
        save("old", [{"company_name": "Acme"}])
    with pytest.raises(RuntimeError):
        hunt_store.accept_new_leads("old", [{"company_name": "Acme"}])
    assert not (tasks / "old.json").exists()


def test_parallel_acceptance_has_one_winner(tasks):
    save("a", [])
    save("b", [])
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda hid: hunt_store.accept_new_leads(hid, [{"company_name": "Acme"}]), ["a", "b"]))
    assert sum(len(result) for result in results) == 1


def test_corrupt_task_fails_closed(tasks):
    save("a", [])
    (tasks / "broken.json").write_text("{")
    with pytest.raises(json.JSONDecodeError):
        hunt_store.accept_new_leads("a", [{"company_name": "Acme"}])
    assert hunt_store.current_leads(hunt_store.load_hunt("a")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_pass", [False, True])
async def test_retry_node_preserves_baseline(tasks, monkeypatch, gate_pass):
    from unittest.mock import AsyncMock
    from agents import lead_extract_agent as agent

    baseline = [{"company_name": f"Customer {i}"} for i in range(19)]
    save("old", baseline)
    hunt_store.save_hunt("retry", {"result": None, "existing_leads": baseline})
    monkeypatch.setattr(agent, "get_settings", lambda: SimpleNamespace(scrape_concurrency=2))
    for name in ("JinaReaderTool", "LLMTool", "GoogleSearchTool"):
        monkeypatch.setattr(agent, name, lambda **kwargs: AsyncMock())
    monkeypatch.setattr(agent, "_quick_gate_candidate", AsyncMock(return_value=(gate_pass, {})))
    monkeypatch.setattr(agent, "_scrape_and_extract", AsyncMock(return_value={"company_name": "New"}))
    result = await agent.lead_extract_node({
        "hunt_id": "retry", "leads": baseline, "target_lead_count": 200,
        "search_results": [{"link": "https://new.de", "title": "New"}],
    })
    assert len(result["leads"]) == (20 if gate_pass else 19)
    assert all(lead in result["leads"] for lead in baseline)


def test_removed_lead_releases_without_deleting_task(tasks):
    save("a", [{"company_name": "Acme"}])
    save("a", [])
    save("b", [])
    assert hunt_store.accept_new_leads("b", [{"company_name": "Acme"}])


def test_legacy_registry_is_not_a_source(tasks):
    import sqlite3
    with sqlite3.connect(tasks / "legacy.db") as conn:
        conn.execute("CREATE TABLE lead_registry (dedupe_key TEXT)")
        conn.execute("INSERT INTO lead_registry VALUES ('company:acme')")
    save("new", [])
    assert hunt_store.accept_new_leads("new", [{"company_name": "Acme"}])


def test_historical_hunts_outside_visible_jobs_do_not_block(tasks):
    save("historical", [{"company_name": "Acme"}])
    save("visible", [])
    save("new", [])
    queue = HuntJobQueue(str(tasks / "queue.db"))
    queue.init_db()
    job_id = queue.enqueue({"owner_user_id": 1}, now_iso="2026-01-01T00:00:00+00:00")
    queue.mark_completed(job_id, hunt_id="visible", finished_at="2026-01-01T00:01:00+00:00")

    assert hunt_store.accept_new_leads("new", [{"company_name": "Acme"}])


def test_visible_job_company_name_still_blocks_duplicate(tasks):
    save("visible", [{"company_name": "Acme"}])
    save("new", [])
    queue = HuntJobQueue(str(tasks / "queue.db"))
    queue.init_db()
    job_id = queue.enqueue({"owner_user_id": 1}, now_iso="2026-01-01T00:00:00+00:00")
    queue.mark_completed(job_id, hunt_id="visible", finished_at="2026-01-01T00:01:00+00:00")

    assert hunt_store.accept_new_leads("new", [{"company_name": " ACME "}]) == []


def test_processes_share_acceptance_lock(tasks):
    from concurrent.futures import ProcessPoolExecutor
    save("a", [])
    save("b", [])
    with ProcessPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(_accept_in_process, [(str(tasks), hid) for hid in ("a", "b")]))
    assert sum(results) == 1


def test_failed_write_does_not_claim_identity(tasks, monkeypatch):
    save("a", [])
    save("b", [])
    original = hunt_store._write_json_atomic
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(hunt_store, "_write_json_atomic", fail)
    with pytest.raises(OSError):
        hunt_store.accept_new_leads("a", [{"company_name": "Acme"}])
    monkeypatch.setattr(hunt_store, "_write_json_atomic", original)
    assert hunt_store.accept_new_leads("b", [{"company_name": "Acme"}])
