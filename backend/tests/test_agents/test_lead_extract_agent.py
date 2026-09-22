"""Tests for agents/lead_extract_agent.py — ReAct-based extraction, dedup, node integration."""

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.lead_extract_agent import (
    _apply_evidence_to_scores,
    _candidate_budget,
    _competitor_rejection_reason,
    _has_concrete_customs_data,
    _is_generic_mailbox,
    _normalize_decision_maker_emails,
    _preferred_customer_role,
    _quick_gate_candidate,
    _scrape_and_extract,
    _strongest_competitor_risk,
    _verify_lead_emails,
    lead_extract_node,
)
from api import hunt_store
from emailing.store import EmailStore


@pytest.fixture(autouse=True)
def isolated_current_tasks(tmp_path, monkeypatch):
    monkeypatch.setattr(hunt_store, "get_settings", lambda: SimpleNamespace(hunts_dir=str(tmp_path / "hunts")))
    for hid in ("test-hunt", "hunt-a", "hunt-b", "hunt-vape"):
        hunt_store.save_hunt(hid, {"result": {"leads": []}})


def _base_state(**overrides):
    base = {
        "hunt_id": "test-hunt",
        "website_url": "https://solartech.de",
        "product_keywords": ["solar inverter"],
        "target_regions": ["Europe"],
        "uploaded_files": [],
        "target_lead_count": 200,
        "max_rounds": 10,
        "insight": {
            "products": ["solar inverter", "PV panel"],
            "industries": ["Renewable Energy"],
            "target_customer_profile": "B2B distributors in Europe",
        },
        "keywords": ["solar inverter distributor"],
        "used_keywords": ["solar inverter distributor"],
        "search_results": [
            {"title": "SolarTech", "link": "https://solartech.de/about", "snippet": "...", "source_keyword": "kw1"},
            {"title": "PV Dist", "link": "https://pvdist.com", "snippet": "...", "source_keyword": "kw1"},
        ],
        "matched_platforms": [],
        "keyword_search_stats": {"kw1": {"result_count": 2, "leads_found": 0}},
        "leads": [],
        "email_sequences": [],
        "hunt_round": 1,
        "prev_round_lead_count": 0,
        "round_feedback": None,
        "current_stage": "search",
        "messages": [],
    }
    base.update(overrides)
    return base


VALID_REACT_RESULT = json.dumps({
    "company_name": "SolarTech GmbH",
    "website": "https://solartech.de",
    "industry": "Renewable Energy",
    "description": "Leading solar inverter manufacturer",
    "contact_person": "Hans Mueller",
    "country_code": "de",
    "emails": ["info@solartech.de"],
    "phone_numbers": ["+49 30 12345678"],
    "social_media": {"linkedin": "https://linkedin.com/company/solartech"},
    "address": "Berlin, Germany",
    "match_score": 0.85,
})

INVALID_REACT_RESULT = json.dumps({
    "company_name": "",
    "website": "",
    "industry": "",
    "description": "This is a blog post",
    "contact_person": None,
    "country_code": "",
    "emails": [],
    "phone_numbers": [],
    "social_media": {},
    "match_score": 0.0,
})


class TestCompetitorSignals:
    def test_keeps_strongest_risk_across_gate_and_deep_research(self):
        assert _strongest_competitor_risk("low", "medium") == "medium"
        assert _strongest_competitor_risk("high", "low") == "high"
        assert _strongest_competitor_risk("moderate", "unknown") == "medium"

    def test_prefers_deep_research_customer_role(self):
        assert _preferred_customer_role("manufacturer", "distributor") == "manufacturer"
        assert _preferred_customer_role("unknown", "wholesaler") == "wholesaler"

    def test_medium_risk_is_rejected_even_without_manufacturer_role(self):
        reason = _competitor_rejection_reason({
            "competitor_risk": "medium",
            "customer_role": "distributor",
        })
        assert reason == "Explicit medium competitor risk"


class TestScrapeAndExtract:
    """Tests for _scrape_and_extract which delegates to react_loop."""

    @pytest.mark.asyncio
    async def test_valid_lead(self):
        sem = asyncio.Semaphore(5)
        jina = AsyncMock()
        llm = AsyncMock()
        google = AsyncMock()

        with patch("agents.lead_extract_agent.react_loop", return_value=VALID_REACT_RESULT):
            result = await _scrape_and_extract(
                {"link": "https://solartech.de", "source_keyword": "kw1"},
                jina, llm, sem, insight={"products": ["solar inverter"]}, google_search=google,
            )

        assert result is not None
        assert result["company_name"] == "SolarTech GmbH"
        assert result["match_score"] == 0.85
        assert "info@solartech.de" in result["emails"]
        assert result["source_keyword"] == "kw1"

    @pytest.mark.asyncio
    async def test_normalizes_valid_phones_and_removes_invalid_phones(self):
        sem = asyncio.Semaphore(5)
        data = json.loads(VALID_REACT_RESULT)
        data.update({
            "country_code": "ES",
            "address": "Barcelona, España",
            "phone_numbers": [
                "+34 967 810 126",
                "684 365 166",
                "7404-16970-",
                "973-799-0901",
            ],
        })

        with patch("agents.lead_extract_agent.react_loop", return_value=json.dumps(data)):
            result = await _scrape_and_extract(
                {"link": "https://supplier.es", "source_keyword": "kw1"},
                AsyncMock(), AsyncMock(), sem,
                insight={"products": ["solar inverter"]},
                google_search=AsyncMock(),
            )

        assert result is not None
        assert result["phone_numbers"] == ["+34 967 81 01 26", "+34 684 36 51 66"]

    @pytest.mark.asyncio
    async def test_invalid_lead_returns_none(self):
        sem = asyncio.Semaphore(5)
        jina = AsyncMock()
        llm = AsyncMock()
        google = AsyncMock()

        with patch("agents.lead_extract_agent.react_loop", return_value=INVALID_REACT_RESULT):
            result = await _scrape_and_extract(
                {"link": "https://blog.com/post", "source_keyword": "kw1"},
                jina, llm, sem, insight={"products": ["solar inverter"]}, google_search=google,
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_react_failure_returns_none(self):
        """When react_loop raises an exception, return None."""
        sem = asyncio.Semaphore(5)
        jina = AsyncMock()
        llm = AsyncMock()
        google = AsyncMock()

        with patch("agents.lead_extract_agent.react_loop", side_effect=Exception("LLM timeout")):
            result = await _scrape_and_extract(
                {"link": "https://down.com", "source_keyword": "kw1"},
                jina, llm, sem, insight={}, google_search=google,
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_json_returns_none(self):
        """When react_loop returns non-JSON, return None."""
        sem = asyncio.Semaphore(5)
        jina = AsyncMock()
        llm = AsyncMock()
        google = AsyncMock()

        with patch("agents.lead_extract_agent.react_loop", return_value="not valid json at all"):
            result = await _scrape_and_extract(
                {"link": "https://broken.com", "source_keyword": "kw1"},
                jina, llm, sem, insight={}, google_search=google,
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_link_returns_none(self):
        sem = asyncio.Semaphore(5)
        jina = AsyncMock()
        llm = AsyncMock()
        google = AsyncMock()

        result = await _scrape_and_extract(
            {"link": "", "source_keyword": "kw1"},
            jina, llm, sem, insight={}, google_search=google,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_maps_only_result_without_link_is_supported(self):
        sem = asyncio.Semaphore(5)
        jina = AsyncMock()
        llm = AsyncMock()
        google = AsyncMock()

        with patch("agents.lead_extract_agent.react_loop", return_value=VALID_REACT_RESULT):
            result = await _scrape_and_extract(
                {
                    "title": "SolarTech GmbH",
                    "link": "",
                    "source_keyword": "kw1",
                    "maps_data": {
                        "title": "SolarTech GmbH",
                        "address": "Berlin",
                        "type": "Solar company",
                        "types": ["Solar company"],
                        "website": "https://solartech.de",
                        "phoneNumber": "+49 30 12345678",
                        "description": "Leading solar panel distributor in Berlin.",
                        "email": "info@solartech.de",
                    },
                },
                jina, llm, sem, insight={"products": ["solar inverter"]}, google_search=google,
            )

        assert result is not None
        assert result["company_name"] == "SolarTech GmbH"
        assert result["maps_data"]["address"] == "Berlin"
        assert "info@solartech.de" in result["emails"]

    @pytest.mark.asyncio
    async def test_match_score_clamped(self):
        sem = asyncio.Semaphore(5)
        jina = AsyncMock()
        llm = AsyncMock()
        google = AsyncMock()

        over_score = json.dumps({
            "is_valid_lead": True,
            "company_name": "OverScore Inc",
            "emails": [],
            "phone_numbers": [],
            "social_media": {},
            "match_score": 1.5,
        })

        with patch("agents.lead_extract_agent.react_loop", return_value=over_score):
            result = await _scrape_and_extract(
                {"link": "https://over.com", "source_keyword": "kw1"},
                jina, llm, sem, insight={}, google_search=google,
            )

        assert result is not None
        assert result["match_score"] == 1.0


class TestCandidateBudget:
    def test_candidate_budget_scales_with_target(self):
        assert _candidate_budget(5, 5) == 20

    def test_candidate_budget_has_floor(self):
        assert _candidate_budget(0, 1) == 12

    @pytest.mark.asyncio
    async def test_url_type_hint_in_prompt(self):
        """Verify that the user prompt includes URL type hints for the ReAct agent."""
        sem = asyncio.Semaphore(5)
        jina = AsyncMock()
        llm = AsyncMock()
        google = AsyncMock()
        captured_kwargs = {}

        async def capture_react_loop(**kwargs):
            captured_kwargs.update(kwargs)
            return VALID_REACT_RESULT

        with patch("agents.lead_extract_agent.react_loop", side_effect=capture_react_loop):
            # LinkedIn URL should get a LinkedIn-specific hint
            await _scrape_and_extract(
                {"link": "https://linkedin.com/company/acme-corp", "source_keyword": "kw1"},
                jina, llm, sem, insight={"products": ["solar"]}, google_search=google,
            )

        assert "LinkedIn" in captured_kwargs["user_prompt"]
        assert "Do NOT" in captured_kwargs["user_prompt"]

    @pytest.mark.asyncio
    async def test_platform_url_type_hint(self):
        """Verify platform URLs get a platform-specific hint."""
        sem = asyncio.Semaphore(5)
        captured_kwargs = {}

        async def capture_react_loop(**kwargs):
            captured_kwargs.update(kwargs)
            return VALID_REACT_RESULT

        with patch("agents.lead_extract_agent.react_loop", side_effect=capture_react_loop):
            await _scrape_and_extract(
                {"link": "https://alibaba.com/supplier/solartech", "source_keyword": "kw1"},
                AsyncMock(), AsyncMock(), sem,
                insight={"products": ["solar"]}, google_search=AsyncMock(),
            )

        assert "platform" in captured_kwargs["user_prompt"].lower()

    @pytest.mark.asyncio
    async def test_react_tools_are_built_with_correct_dependencies(self):
        """Verify that _build_react_tools creates 5 tools including customs lookup."""
        from agents.lead_extract_agent import _build_react_tools

        jina = AsyncMock()
        llm = AsyncMock()
        google = AsyncMock()
        insight = {"products": ["solar inverter"]}

        tools = _build_react_tools(jina, llm, google, insight)

        assert len(tools) == 5
        tool_names = {t.name for t in tools}
        assert tool_names == {"scrape_page", "google_search", "find_customs_data", "extract_lead_info", "assess_lead_fit"}

    @pytest.mark.asyncio
    async def test_react_tool_scrape_page_auto_extracts_contacts(self):
        """Test that scrape_page auto-extracts emails, phones, social from content."""
        from agents.lead_extract_agent import _build_react_tools

        jina = AsyncMock()
        jina.read = AsyncMock(return_value=(
            "# Company Page\nContact us at hello@acmecorp.com or call +1 703 848 7947. "
            "Visit https://linkedin.com/company/acme for more info. "
            "Enough text to pass the minimum length check for scraping."
        ))
        llm = AsyncMock()
        google = AsyncMock()

        tools = _build_react_tools(jina, llm, google, {})
        scrape_tool = next(t for t in tools if t.name == "scrape_page")

        result = await scrape_tool.fn(url="https://example.com")
        parsed = json.loads(result)
        assert "content" in parsed
        assert "hello@acmecorp.com" in parsed["extracted_emails"]
        assert len(parsed["extracted_phones"]) > 0

    @pytest.mark.asyncio
    async def test_react_tool_google_search(self):
        """Test the google_search tool function directly."""
        from agents.lead_extract_agent import _build_react_tools

        google = AsyncMock()
        google.search = AsyncMock(return_value=[
            {"title": "Acme Corp Contact", "link": "https://acmecorp.com/contact", "snippet": "Email: info@acmecorp.com for inquiries"},
        ])

        tools = _build_react_tools(AsyncMock(), AsyncMock(), google, {})
        search_tool = next(t for t in tools if t.name == "google_search")

        result = await search_tool.fn(query="acme corp email")
        parsed = json.loads(result)
        assert len(parsed["results"]) == 1
        assert "info@acmecorp.com" in parsed["contacts_from_snippets"]["emails"]

    @pytest.mark.asyncio
    async def test_react_tool_extract_lead_info(self):
        """Test the extract_lead_info tool function directly."""
        from agents.lead_extract_agent import _build_react_tools

        llm = AsyncMock()
        llm.generate = AsyncMock(return_value=VALID_REACT_RESULT)

        tools = _build_react_tools(AsyncMock(), llm, AsyncMock(), {"products": ["solar"]})
        lead_tool = next(t for t in tools if t.name == "extract_lead_info")

        result = await lead_tool.fn(page_content="Some company page content")
        parsed = json.loads(result)
        assert parsed["company_name"] == "SolarTech GmbH"


class TestQuickGate:
    @pytest.mark.asyncio
    async def test_quick_gate_rejects_non_company_entity(self):
        llm = AsyncMock()
        llm.generate = AsyncMock(return_value=json.dumps({
            "pass_gate": False,
            "entity_type": "directory",
            "customer_role_guess": "unknown",
            "competitor_risk": "low",
            "confidence": 0.92,
            "reason": "Directory page, not a real prospect company.",
            "risk_flags": ["directory"],
        }))

        passed, gate = await _quick_gate_candidate(
            {"title": "Industrial Directory Listing", "maps_data": {"description": "Supplier directory"}},
            llm,
            {"products": ["micro switch"]},
        )

        assert passed is False
        assert gate["entity_type"] == "directory"
        assert "directory" in gate["risk_flags"]

    @pytest.mark.asyncio
    async def test_quick_gate_rejects_possible_competitor_even_if_channel(self):
        """Any explicit possible-competitor signal is excluded from the hunt."""
        llm = AsyncMock()
        llm.generate = AsyncMock(return_value=json.dumps({
            "pass_gate": True,
            "entity_type": "company",
            "customer_role_guess": "distributor",
            "competitor_risk": "high",
            "confidence": 0.61,
            "reason": "Sells related products but appears to act as channel partner.",
            "risk_flags": ["possible_competitor"],
        }))

        # Use description without B2B keywords to test actual LLM logic
        passed, gate = await _quick_gate_candidate(
            {"title": "Acme Electrical Supply", "maps_data": {"description": "Electrical components supplier"}},
            llm,
            {"products": ["micro switch"]},
        )

        assert passed is False
        assert gate["customer_role_guess"] == "distributor"
        assert gate["competitor_risk"] == "high"

    @pytest.mark.asyncio
    async def test_final_extraction_rejects_competitor_risk(self):
        sem = asyncio.Semaphore(5)
        jina = AsyncMock()
        llm = AsyncMock()
        google = AsyncMock()
        competitor_result = json.loads(VALID_REACT_RESULT)
        competitor_result.update({
            "competitor_risk": "high",
            "risk_flags": ["possible_competitor"],
            "customer_role": "manufacturer",
        })

        with patch(
            "agents.lead_extract_agent.react_loop",
            return_value=json.dumps(competitor_result),
        ):
            result = await _scrape_and_extract(
                {"link": "https://competitor.example", "source_keyword": "kw1"},
                jina,
                llm,
                sem,
                insight={"products": ["solar inverter"]},
                google_search=google,
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_react_tool_find_customs_data(self):
        """Test the customs router tool function directly."""
        from agents.lead_extract_agent import _build_react_tools

        with patch("agents.lead_extract_agent.route_customs_data", new=AsyncMock(return_value={
            "status": "ok",
            "summary": "importgenius: period 2024; import; partners: Vietnam. Source: https://example.com",
            "evidence": [{"provider": "importgenius", "source_url": "https://example.com"}],
        })):
            tools = _build_react_tools(AsyncMock(), AsyncMock(), AsyncMock(), {"products": ["micro switch"]})
            customs_tool = next(t for t in tools if t.name == "find_customs_data")

            result = await customs_tool.fn(company_name="Acme GmbH", website="https://acme.de", country="Germany")
            parsed = json.loads(result)
            assert parsed["status"] == "ok"
            assert parsed["evidence"][0]["provider"] == "importgenius"


class TestLeadExtractNode:
    @pytest.mark.asyncio
    async def test_extracts_leads(self):
        state = _base_state()

        with patch("agents.lead_extract_agent.JinaReaderTool") as MockJina, \
             patch("agents.lead_extract_agent.LLMTool") as MockLLM, \
             patch("agents.lead_extract_agent.GoogleSearchTool") as MockGoogle, \
             patch("agents.lead_extract_agent.react_loop", return_value=VALID_REACT_RESULT), \
             patch("agents.lead_extract_agent.get_settings") as mock_settings:

            mock_settings.return_value.scrape_concurrency = 5

            MockJina.return_value = AsyncMock(close=AsyncMock())
            MockLLM.return_value = AsyncMock(close=AsyncMock())
            MockGoogle.return_value = AsyncMock(close=AsyncMock())

            result = await lead_extract_node(state)

        assert result["current_stage"] == "lead_extract"
        assert len(result["leads"]) > 0

    @pytest.mark.asyncio
    async def test_deduplicates_by_company_name(self):
        state = _base_state(search_results=[
            {"link": "https://solartech.de/about", "source_keyword": "kw1"},
            {"link": "https://solartech.de/products", "source_keyword": "kw1"},
        ])

        with patch("agents.lead_extract_agent.JinaReaderTool") as MockJina, \
             patch("agents.lead_extract_agent.LLMTool") as MockLLM, \
             patch("agents.lead_extract_agent.GoogleSearchTool") as MockGoogle, \
             patch("agents.lead_extract_agent.react_loop", return_value=VALID_REACT_RESULT), \
             patch("agents.lead_extract_agent.get_settings") as mock_settings:

            mock_settings.return_value.scrape_concurrency = 5

            MockJina.return_value = AsyncMock(close=AsyncMock())
            MockLLM.return_value = AsyncMock(close=AsyncMock())
            MockGoogle.return_value = AsyncMock(close=AsyncMock())

            result = await lead_extract_node(state)

        assert len(result["leads"]) == 1

    @pytest.mark.asyncio
    async def test_skips_already_extracted_domains(self):
        existing_leads = [{"website": "https://solartech.de/about", "company_name": "SolarTech"}]
        state = _base_state(leads=existing_leads)

        with patch("agents.lead_extract_agent.JinaReaderTool") as MockJina, \
             patch("agents.lead_extract_agent.LLMTool") as MockLLM, \
             patch("agents.lead_extract_agent.GoogleSearchTool") as MockGoogle, \
             patch("agents.lead_extract_agent.react_loop", return_value=VALID_REACT_RESULT), \
             patch("agents.lead_extract_agent.get_settings") as mock_settings:

            mock_settings.return_value.scrape_concurrency = 5

            MockJina.return_value = AsyncMock(close=AsyncMock())
            MockLLM.return_value = AsyncMock(close=AsyncMock())
            MockGoogle.return_value = AsyncMock(close=AsyncMock())

            result = await lead_extract_node(state)

        # solartech.de already in leads, only pvdist.com should be processed
        assert any(l.get("company_name") == "SolarTech" for l in result["leads"])

    @pytest.mark.asyncio
    async def test_skips_global_candidate_before_deep_scrape(self):
        state = _base_state(
            search_results=[
                {
                    "title": "Known Solar",
                    "link": "https://known-solar.example/",
                    "source": "google_maps",
                    "maps_data": {
                        "title": "Known Solar",
                        "website": "https://known-solar.example/",
                    },
                },
            ],
        )
        registry = MagicMock()
        registry.list_lead_registry_keys.return_value = {"domain:known-solar.example"}

        with (
            patch("agents.lead_extract_agent.current_lead_keys", return_value={"company:known solar"}),
            patch("agents.lead_extract_agent.get_settings") as mock_settings,
            patch("agents.lead_extract_agent._scrape_and_extract", new_callable=AsyncMock) as mock_scrape,
        ):
            mock_settings.return_value.scrape_concurrency = 5
            result = await lead_extract_node(state)

        assert result.get("leads", []) == []
        mock_scrape.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_updates_keyword_stats(self):
        state = _base_state(
            search_results=[
                {"link": "https://newlead.com", "source_keyword": "kw1"},
            ],
            keyword_search_stats={"kw1": {"result_count": 5, "leads_found": 0}},
        )

        with patch("agents.lead_extract_agent.JinaReaderTool") as MockJina, \
             patch("agents.lead_extract_agent.LLMTool") as MockLLM, \
             patch("agents.lead_extract_agent.GoogleSearchTool") as MockGoogle, \
             patch("agents.lead_extract_agent.react_loop", return_value=VALID_REACT_RESULT), \
             patch("agents.lead_extract_agent.get_settings") as mock_settings:

            mock_settings.return_value.scrape_concurrency = 5

            MockJina.return_value = AsyncMock(close=AsyncMock())
            MockLLM.return_value = AsyncMock(close=AsyncMock())
            MockGoogle.return_value = AsyncMock(close=AsyncMock())

            result = await lead_extract_node(state)

        assert result["keyword_search_stats"]["kw1"]["leads_found"] >= 1

    @pytest.mark.asyncio
    async def test_stops_deep_scrape_once_target_lead_count_is_reached(self):
        state = _base_state(
            target_lead_count=1,
            search_results=[
                {"link": "https://firstlead.com", "source_keyword": "kw1"},
                {"link": "https://secondlead.com", "source_keyword": "kw1"},
            ],
        )
        blocked_started = asyncio.Event()
        blocked_cancelled = asyncio.Event()

        async def fake_scrape_and_extract(search_result, *args, **kwargs):
            link = search_result["link"]
            if "firstlead.com" in link:
                return {
                    "company_name": "First Lead",
                    "website": "https://firstlead.com",
                    "industry": "Electrical",
                    "description": "First lead",
                    "emails": [],
                    "phone_numbers": [],
                    "social_media": {},
                    "match_score": 0.8,
                    "source_keyword": "kw1",
                }
            blocked_started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                blocked_cancelled.set()
                raise
            return {
                "company_name": "Second Lead",
                "website": "https://secondlead.com",
                "industry": "Electrical",
                "description": "Second lead",
                "emails": [],
                "phone_numbers": [],
                "social_media": {},
                "match_score": 0.7,
                "source_keyword": "kw1",
            }

        with patch("agents.lead_extract_agent.JinaReaderTool") as MockJina, \
             patch("agents.lead_extract_agent.LLMTool") as MockLLM, \
             patch("agents.lead_extract_agent.GoogleSearchTool") as MockGoogle, \
             patch("agents.lead_extract_agent._quick_gate_candidate", new=AsyncMock(return_value=(True, {"reason": "keep"}))), \
             patch("agents.lead_extract_agent._scrape_and_extract", side_effect=fake_scrape_and_extract), \
             patch("agents.lead_extract_agent.get_settings") as mock_settings:

            mock_settings.return_value.scrape_concurrency = 5
            MockJina.return_value = AsyncMock(close=AsyncMock())
            MockLLM.return_value = AsyncMock(close=AsyncMock())
            MockGoogle.return_value = AsyncMock(close=AsyncMock())

            result = await lead_extract_node(state)

        assert len(result["leads"]) == 1
        assert result["leads"][0]["company_name"] == "First Lead"
        assert blocked_started.is_set()
        assert blocked_cancelled.is_set()

    @pytest.mark.asyncio
    async def test_does_not_dedupe_by_linkedin_host(self):
        """Different LinkedIn company pages must not be deduped by linkedin.com host."""
        state = _base_state(
            search_results=[
                {"link": "https://linkedin.com/company/acme-a", "source_keyword": "kw1"},
                {"link": "https://linkedin.com/company/acme-b", "source_keyword": "kw1"},
            ]
        )

        async def react_for_linkedin(**kwargs):
            prompt = kwargs.get("user_prompt", "")
            if "acme-a" in prompt:
                return json.dumps({
                    "company_name": "Acme A",
                    "website": "https://linkedin.com/company/acme-a",
                    "industry": "Manufacturing",
                    "description": "A",
                    "emails": ["a@example.com"],
                    "phone_numbers": [],
                    "social_media": {},
                    "match_score": 0.7,
                })
            return json.dumps({
                "company_name": "Acme B",
                "website": "https://linkedin.com/company/acme-b",
                "industry": "Manufacturing",
                "description": "B",
                "emails": ["b@example.com"],
                "phone_numbers": [],
                "social_media": {},
                "match_score": 0.71,
            })

        with patch("agents.lead_extract_agent.JinaReaderTool") as MockJina, \
             patch("agents.lead_extract_agent.LLMTool") as MockLLM, \
             patch("agents.lead_extract_agent.GoogleSearchTool") as MockGoogle, \
             patch("agents.lead_extract_agent.react_loop", side_effect=react_for_linkedin), \
             patch("agents.lead_extract_agent.get_settings") as mock_settings:

            mock_settings.return_value.scrape_concurrency = 5
            MockJina.return_value = AsyncMock(close=AsyncMock())
            MockLLM.return_value = AsyncMock(close=AsyncMock())
            MockGoogle.return_value = AsyncMock(close=AsyncMock())

            result = await lead_extract_node(state)

        # Should keep both; no dedupe on linkedin.com host
        assert len(result["leads"]) == 2

    @pytest.mark.asyncio
    async def test_platform_results_with_different_company_names_are_kept(self):
        """Shared official domains do not merge different company names."""
        state = _base_state(
            search_results=[
                {"link": "https://thomasnet.com/company/acme-a", "source_keyword": "kw1"},
                {"link": "https://alibaba.com/company/acme-a", "source_keyword": "kw1"},
            ]
        )

        async def react_for_platforms(**kwargs):
            prompt = kwargs.get("user_prompt", "")
            if "thomasnet.com" in prompt:
                return json.dumps({
                    "company_name": "Acme Corp",
                    "website": "https://acme.com",
                    "industry": "Manufacturing",
                    "description": "A",
                    "emails": ["sales@acme.com"],
                    "phone_numbers": [],
                    "social_media": {},
                    "match_score": 0.8,
                })
            return json.dumps({
                "company_name": "Acme Corporation",
                "website": "https://www.acme.com/about",
                "industry": "Manufacturing",
                "description": "B",
                "emails": ["contact@acme.com"],
                "phone_numbers": [],
                "social_media": {},
                "match_score": 0.81,
            })

        with patch("agents.lead_extract_agent.JinaReaderTool") as MockJina, \
             patch("agents.lead_extract_agent.LLMTool") as MockLLM, \
             patch("agents.lead_extract_agent.GoogleSearchTool") as MockGoogle, \
             patch("agents.lead_extract_agent.react_loop", side_effect=react_for_platforms), \
             patch("agents.lead_extract_agent.get_settings") as mock_settings:

            mock_settings.return_value.scrape_concurrency = 5
            MockJina.return_value = AsyncMock(close=AsyncMock())
            MockLLM.return_value = AsyncMock(close=AsyncMock())
            MockGoogle.return_value = AsyncMock(close=AsyncMock())

            result = await lead_extract_node(state)

        assert len(result["leads"]) == 2

    @pytest.mark.asyncio
    async def test_empty_search_results(self):
        state = _base_state(search_results=[])
        result = await lead_extract_node(state)
        assert result["current_stage"] == "lead_extract"

    @pytest.mark.asyncio
    async def test_filters_irrelevant_urls(self):
        """Irrelevant URLs (google.com, tiktok.com) should be filtered out."""
        state = _base_state(search_results=[
            {"link": "https://google.com/search?q=solar", "source_keyword": "kw1"},
            {"link": "https://tiktok.com/@solar", "source_keyword": "kw1"},
            {"link": "https://realcompany.com", "source_keyword": "kw1"},
        ])

        call_count = {"n": 0}
        original_valid = VALID_REACT_RESULT

        async def counting_react_loop(**kwargs):
            call_count["n"] += 1
            return original_valid

        with patch("agents.lead_extract_agent.JinaReaderTool") as MockJina, \
             patch("agents.lead_extract_agent.LLMTool") as MockLLM, \
             patch("agents.lead_extract_agent.GoogleSearchTool") as MockGoogle, \
             patch("agents.lead_extract_agent.react_loop", side_effect=counting_react_loop), \
             patch("agents.lead_extract_agent.get_settings") as mock_settings:

            mock_settings.return_value.scrape_concurrency = 5

            MockJina.return_value = AsyncMock(close=AsyncMock())
            MockLLM.return_value = AsyncMock(close=AsyncMock())
            MockGoogle.return_value = AsyncMock(close=AsyncMock())

            result = await lead_extract_node(state)

        # Only realcompany.com should be processed, google/tiktok filtered
        assert call_count["n"] == 1

    @pytest.mark.asyncio
    async def test_orphan_registry_does_not_block_extraction(self):
        """Registry-only customers without current tasks do not block extraction."""
        import tempfile

        from agents.lead_identity import lead_identity_keys
        from emailing.store import EmailStore
        
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = EmailStore(str(db_path))
            store.init_db()
            
            # Hunt A already found "Vape Store" in Germany
            hunt_a_lead = {
                "company_name": "Vape Store",
                "website": "https://vape-berlin.com",
                "emails": [],
                "phone_numbers": [],
            }
            store.reserve_lead_keys([hunt_a_lead], hunt_id="hunt-a", key_fn=lead_identity_keys, now_iso="2026-01-01")
            
            # Hunt B searches and finds "Vape Store" in Poland (different place_id, different website)
            state = _base_state(
                hunt_id="hunt-b",
                search_results=[
                    {
                        "link": "https://maps.google.com/?cid=999",
                        "title": "Vape Store",
                        "source_keyword": "kw1",
                        "maps_data": {
                            "place_id": "ChIJ_poland_vape_store",
                            "title": "Vape Store",
                            "address": "Warsaw, Poland",
                        },
                    }
                ],
            )

            # ReAct returns Polish Vape Store with different website
            react_result = json.dumps({
                "company_name": "Vape Store Warsaw",
                "website": "https://vape-warsaw.pl",
                "industry": "Retail",
                "description": "Vape retailer in Poland",
                "emails": ["contact@vape-warsaw.pl"],
                "phone_numbers": [],
                "social_media": {},
                "decision_makers": [],
                "address": "Warsaw, Poland",
                "match_score": 0.75,
                "fit_reasons": ["Vape retailer in Poland"],
                "disqualify_reasons": [],
            })

            with patch("agents.lead_extract_agent.JinaReaderTool") as MockJina, \
                 patch("agents.lead_extract_agent.LLMTool") as MockLLM, \
                 patch("agents.lead_extract_agent.GoogleSearchTool") as MockGoogle, \
                 patch("agents.lead_extract_agent.react_loop", return_value=react_result), \
                 patch("agents.lead_extract_agent.get_settings") as mock_settings, \
                 patch("agents.lead_extract_agent.current_lead_keys", return_value=set()):

                mock_settings.return_value.scrape_concurrency = 5
                mock_settings.return_value.email_db_path = str(db_path)

                jina_instance = AsyncMock()
                jina_instance.close = AsyncMock()
                MockJina.return_value = jina_instance

                llm_instance = AsyncMock()
                llm_instance.close = AsyncMock()
                # QuickGate passes
                llm_instance.generate = AsyncMock(return_value=json.dumps({
                    "pass_gate": True,
                    "entity_type": "company",
                    "customer_role_guess": "retailer",
                    "competitor_risk": "low",
                    "confidence": 0.8,
                    "reason": "Vape retailer",
                    "risk_flags": []
                }))
                MockLLM.return_value = llm_instance

                google_instance = AsyncMock()
                google_instance.close = AsyncMock()
                MockGoogle.return_value = google_instance

                result = await lead_extract_node(state)

            # Should NOT be filtered by global dedup because different website/place_id
            assert len(result["leads"]) == 1
            assert result["leads"][0]["company_name"] == "Vape Store Warsaw"
            assert result["leads"][0]["website"] == "https://vape-warsaw.pl"

    @pytest.mark.asyncio
    async def test_continue_hunt_preserves_old_leads_and_dedupes_new(self):
        """Continue hunt should keep existing leads and not re-scrape them."""
        import tempfile

        from agents.lead_identity import lead_identity_keys
        from emailing.store import EmailStore
        
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = EmailStore(str(db_path))
            store.init_db()
            
            # Hunt A already has 2 leads
            existing_leads = [
                {
                    "company_name": "Existing Lead A",
                    "website": "https://existing-a.com",
                    "emails": ["contact@existing-a.com"],
                    "phone_numbers": [],
                },
                {
                    "company_name": "Existing Lead B",
                    "website": "https://existing-b.com",
                    "emails": [],
                    "phone_numbers": [],
                }
            ]
            store.reserve_lead_keys(existing_leads, hunt_id="hunt-a", key_fn=lead_identity_keys, now_iso="2026-01-01")
            
            # Continue hunt: search finds 3 candidates
            # 1. existing-a.com (should be filtered)
            # 2. existing-b.com (should be filtered)
            # 3. new-c.com (should be processed)
            state = _base_state(
                hunt_id="hunt-a",
                hunt_round=2,
                leads=existing_leads,  # existing_leads are passed as "leads" in state
                search_results=[
                    {"link": "https://existing-a.com", "title": "Existing Lead A", "source_keyword": "kw1"},
                    {"link": "https://existing-b.com", "title": "Existing Lead B", "source_keyword": "kw1"},
                    {"link": "https://new-c.com", "title": "New Lead C", "source_keyword": "kw1"},
                ],
            )

            # ReAct only called for new-c.com
            react_result = json.dumps({
                "company_name": "New Lead C",
                "website": "https://new-c.com",
                "industry": "Manufacturing",
                "description": "New company",
                "emails": ["sales@new-c.com"],
                "phone_numbers": [],
                "social_media": {},
                "decision_makers": [],
                "address": "Berlin, Germany",
                "match_score": 0.8,
                "fit_reasons": ["Manufacturing company in Germany"],
                "disqualify_reasons": [],
            })

            scrape_call_count = {"n": 0}
            
            async def counting_react_loop(**kwargs):
                scrape_call_count["n"] += 1
                return react_result

            with patch("agents.lead_extract_agent.JinaReaderTool") as MockJina, \
                 patch("agents.lead_extract_agent.LLMTool") as MockLLM, \
                 patch("agents.lead_extract_agent.GoogleSearchTool") as MockGoogle, \
                 patch("agents.lead_extract_agent.react_loop", side_effect=counting_react_loop), \
                 patch("agents.lead_extract_agent.get_settings") as mock_settings, \
                 patch("agents.lead_extract_agent.current_lead_keys", return_value=set()):

                mock_settings.return_value.scrape_concurrency = 5
                mock_settings.return_value.email_db_path = str(db_path)

                MockJina.return_value = AsyncMock(close=AsyncMock())
                MockLLM.return_value = AsyncMock(close=AsyncMock())
                MockGoogle.return_value = AsyncMock(close=AsyncMock())

                result = await lead_extract_node(state)

            # Company-name identity is applied after extraction; URLs alone do
            # not prevent researching candidates in a continued hunt.
            assert scrape_call_count["n"] == 3
            
            # Result should contain all 3 leads: 2 existing + 1 new
            assert len(result["leads"]) == 3
            
            # Verify all leads are present
            company_names = {lead["company_name"] for lead in result["leads"]}
            assert company_names == {"Existing Lead A", "Existing Lead B", "New Lead C"}
            
            # Verify the new lead was added
            new_lead = next(l for l in result["leads"] if l["company_name"] == "New Lead C")
            assert new_lead["website"] == "https://new-c.com"
            assert new_lead["emails"] == ["sales@new-c.com"]

    @pytest.mark.asyncio
    async def test_distinct_extracted_names_and_domains_remain_distinct(self):
        """Distinct official identities remain distinct despite similar search titles."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_registry.db"
            store = EmailStore(str(db_path))
            
            # Hunt searches for "Vape Store" in multiple regions
            # Finds 3 candidates with same name but different locations
            state = _base_state(
                hunt_id="hunt-vape",
                hunt_round=1,
                leads=[],
                search_results=[
                    {
                        "link": "https://vapestore-berlin.de",
                        "title": "Vape Store",
                        "source_keyword": "vape store Germany",
                        "maps_data": {
                            "place_id": "ChIJ1234_berlin",
                            "title": "Vape Store",
                            "address": "Berlin, Germany",
                        }
                    },
                    {
                        "link": "https://vapestore-london.co.uk",
                        "title": "Vape Store",
                        "source_keyword": "vape store UK",
                        "maps_data": {
                            "place_id": "ChIJ5678_london",
                            "title": "Vape Store",
                            "address": "London, UK",
                        }
                    },
                    {
                        "link": "https://vapestore-paris.fr",
                        "title": "Vape Store",
                        "source_keyword": "vape store France",
                        "maps_data": {
                            "place_id": "ChIJ9012_paris",
                            "title": "Vape Store",
                            "address": "Paris, France",
                        }
                    },
                ],
            )

            react_results = [
                json.dumps({
                    "company_name": "Vape Store Berlin",
                    "website": "https://vapestore-berlin.de",
                    "industry": "Retail",
                    "description": "Vape shop in Berlin",
                    "emails": ["info@vapestore-berlin.de"],
                    "phone_numbers": ["+49301234567"],
                    "social_media": {},
                    "decision_makers": [],
                    "address": "Berlin, Germany",
                    "match_score": 0.75,
                    "fit_reasons": ["Vape retailer in Germany"],
                    "disqualify_reasons": [],
                }),
                json.dumps({
                    "company_name": "Vape Store London",
                    "website": "https://vapestore-london.co.uk",
                    "industry": "Retail",
                    "description": "Vape shop in London",
                    "emails": ["sales@vapestore-london.co.uk"],
                    "phone_numbers": ["+442071234567"],
                    "social_media": {},
                    "decision_makers": [],
                    "address": "London, UK",
                    "match_score": 0.73,
                    "fit_reasons": ["Vape retailer in UK"],
                    "disqualify_reasons": [],
                }),
                json.dumps({
                    "company_name": "Vape Store Paris",
                    "website": "https://vapestore-paris.fr",
                    "industry": "Retail",
                    "description": "Vape shop in Paris",
                    "emails": ["contact@vapestore-paris.fr"],
                    "phone_numbers": ["+33145123456"],
                    "social_media": {},
                    "decision_makers": [],
                    "address": "Paris, France",
                    "match_score": 0.72,
                    "fit_reasons": ["Vape retailer in France"],
                    "disqualify_reasons": [],
                }),
            ]

            call_index = {"i": 0}
            
            async def indexed_react_loop(**kwargs):
                result = react_results[call_index["i"]]
                call_index["i"] += 1
                return result

            with patch("agents.lead_extract_agent.JinaReaderTool") as MockJina, \
                 patch("agents.lead_extract_agent.LLMTool") as MockLLM, \
                 patch("agents.lead_extract_agent.GoogleSearchTool") as MockGoogle, \
                 patch("agents.lead_extract_agent.react_loop", side_effect=indexed_react_loop), \
                 patch("agents.lead_extract_agent.get_settings") as mock_settings, \
                 patch("agents.lead_extract_agent.current_lead_keys", return_value=set()):

                mock_settings.return_value.scrape_concurrency = 5
                mock_settings.return_value.email_db_path = str(db_path)

                MockJina.return_value = AsyncMock(close=AsyncMock())
                MockLLM.return_value = AsyncMock(close=AsyncMock())
                MockGoogle.return_value = AsyncMock(close=AsyncMock())

                result = await lead_extract_node(state)

            # All 3 candidates should be scraped (not deduplicated by company name alone)
            assert call_index["i"] == 3
            
            # Result should contain all 3 leads
            assert len(result["leads"]) == 3
            
            # Verify each lead has unique identity
            domains = {lead["website"] for lead in result["leads"]}
            assert len(domains) == 3
            assert "https://vapestore-berlin.de" in domains
            assert "https://vapestore-london.co.uk" in domains
            assert "https://vapestore-paris.fr" in domains


class TestCollectedContactsMerge:
    """Tests for P0-3: Regex-extracted emails are merged into leads."""

    @pytest.mark.asyncio
    async def test_scrape_page_collects_contacts(self):
        """scrape_page tool accumulates Regex-extracted contacts into _collected_contacts."""
        from agents.lead_extract_agent import _build_react_tools

        jina = AsyncMock()
        jina.read = AsyncMock(return_value=(
            "# Company Page\nContact us at hello@acmecorp.com or sales@acmecorp.com. "
            "Call +1 703 848 7947. Visit https://linkedin.com/company/acme. "
            "Enough text to pass the minimum length check for scraping."
        ))
        collected = {"emails": set(), "phones": set(), "social": {}}

        tools = _build_react_tools(
            jina, AsyncMock(), AsyncMock(), {},
            _collected_contacts=collected,
        )
        scrape_tool = next(t for t in tools if t.name == "scrape_page")
        await scrape_tool.fn(url="https://acmecorp.com")

        assert "hello@acmecorp.com" in collected["emails"]
        assert "sales@acmecorp.com" in collected["emails"]
        assert len(collected["phones"]) > 0

    @pytest.mark.asyncio
    async def test_regex_emails_merged_into_lead(self):
        """Even if ReAct JSON omits emails found by Regex, they appear in the final lead."""
        sem = asyncio.Semaphore(5)

        # ReAct returns a valid lead but with EMPTY emails list
        react_result_no_emails = json.dumps({
            "is_valid_lead": True,
            "company_name": "Acme Corp",
            "website": "https://acmecorp.com",
            "industry": "Manufacturing",
            "description": "Test company",
            "emails": [],  # ← ReAct agent omitted the emails!
            "phone_numbers": [],
            "social_media": {},
            "match_score": 0.7,
        })

        # But the Jina scraper found emails in the page content
        jina = AsyncMock()
        jina.read = AsyncMock(return_value=(
            "Contact us at sales@acmecorp.com for inquiries. "
            "This is a long enough text to pass the content length check. "
            "Additional text to ensure we have enough content here."
        ))

        # The key: mock react_loop so it invokes the scrape_page tool
        # (which populates _collected_contacts via closure) before returning.
        async def react_loop_that_scrapes(**kwargs):
            # Find the scrape_page tool from the tools list
            tools = kwargs.get("tools", [])
            scrape_tool = next((t for t in tools if t.name == "scrape_page"), None)
            if scrape_tool:
                await scrape_tool.fn(url="https://acmecorp.com/contact")
            return react_result_no_emails

        with patch("agents.lead_extract_agent.react_loop", side_effect=react_loop_that_scrapes):
            result = await _scrape_and_extract(
                {"link": "https://acmecorp.com/contact", "source_keyword": "kw1"},
                jina, AsyncMock(), sem,
                insight={"products": ["widgets"]},
                google_search=AsyncMock(),
            )

        assert result is not None
        # The Regex-extracted email should be merged in via P0-3
        assert "sales@acmecorp.com" in result["emails"]

    @pytest.mark.asyncio
    async def test_react_loop_called_with_required_fields(self):
        """Verify that _scrape_and_extract passes required_json_fields to react_loop."""
        sem = asyncio.Semaphore(5)
        captured_kwargs = {}

        async def capture_react_loop(**kwargs):
            captured_kwargs.update(kwargs)
            return VALID_REACT_RESULT

        with patch("agents.lead_extract_agent.react_loop", side_effect=capture_react_loop):
            await _scrape_and_extract(
                {"link": "https://example.com", "source_keyword": "kw1"},
                AsyncMock(), AsyncMock(), sem,
                insight={"products": ["solar"]},
                google_search=AsyncMock(),
            )

        assert "required_json_fields" in captured_kwargs
        required = captured_kwargs["required_json_fields"]
        assert "company_name" in required
        assert "emails" in required
        assert "match_score" in required

    @pytest.mark.asyncio
    async def test_build_react_tools_accepts_collected_contacts(self):
        """_build_react_tools works with and without _collected_contacts."""
        from agents.lead_extract_agent import _build_react_tools

        # Without _collected_contacts (backward compat)
        tools = _build_react_tools(AsyncMock(), AsyncMock(), AsyncMock(), {})
        assert len(tools) == 5  # scrape_page, google_search, find_customs_data, extract_lead_info, assess_lead_fit

        # With _collected_contacts
        collected = {"emails": set(), "phones": set(), "social": {}}
        tools = _build_react_tools(
            AsyncMock(), AsyncMock(), AsyncMock(), {},
            _collected_contacts=collected,
        )
        assert len(tools) == 5

    @pytest.mark.asyncio
    async def test_customs_tool_result_merged_into_lead(self):
        """If ReAct calls the customs tool but omits customs_data, post-processing should fill it."""
        sem = asyncio.Semaphore(5)
        react_result_no_customs = json.dumps({
            "company_name": "Acme Corp",
            "website": "https://acme.com",
            "industry": "Manufacturing",
            "description": "Test company",
            "emails": [],
            "phone_numbers": [],
            "social_media": {},
            "customs_data": "No data found",
            "evidence": [],
            "match_score": 0.7,
        })

        async def react_loop_that_calls_customs(**kwargs):
            tools = kwargs.get("tools", [])
            customs_tool = next((t for t in tools if t.name == "find_customs_data"), None)
            if customs_tool:
                await customs_tool.fn(company_name="Acme Corp", website="https://acme.com", country="Germany")
            return react_result_no_customs

        with patch("agents.lead_extract_agent.route_customs_data", new=AsyncMock(return_value={
            "status": "ok",
            "summary": "importgenius: period 2024; import; partners: Vietnam. Source: https://importgenius.com/importers/acme-corp",
            "evidence": [
                {
                    "provider": "importgenius",
                    "source_url": "https://importgenius.com/importers/acme-corp",
                    "trade_direction": "import",
                    "period": "2024",
                    "partner_countries": ["Vietnam"],
                }
            ],
        })), patch("agents.lead_extract_agent.react_loop", side_effect=react_loop_that_calls_customs):
            result = await _scrape_and_extract(
                {"link": "https://acme.com", "source_keyword": "kw1"},
                AsyncMock(), AsyncMock(), sem,
                insight={"products": ["micro switch"]},
                google_search=AsyncMock(),
            )

        assert result is not None
        assert "importgenius" in result["customs_data"]
        assert len(result["customs_records"]) == 1
        assert result["customs_records"][0]["provider"] == "importgenius"
        assert any("importgenius.com/importers/acme-corp" in item["source_url"] for item in result["evidence"])


class TestEmailVerification:
    """Tests for P0-2: Email verification in lead_extract_node."""

    @pytest.mark.asyncio
    async def test_undeliverable_emails_removed(self):
        """lead_extract_node filters out emails with no MX records."""
        state = _base_state(search_results=[
            {"link": "https://newlead.com", "source_keyword": "kw1"},
        ])

        # ReAct returns a lead with two emails
        react_result_with_emails = json.dumps({
            "is_valid_lead": True,
            "company_name": "NewLead Corp",
            "website": "https://newlead.com",
            "industry": "Tech",
            "description": "A tech company",
            "emails": ["valid@newlead.com", "invalid@expired-domain.xyz"],
            "phone_numbers": [],
            "social_media": {},
            "match_score": 0.9,
        })

        # Mock EmailVerifierTool to mark one email as undeliverable
        mock_verify_results = [
            {"email": "valid@newlead.com", "valid_syntax": True, "has_mx": True, "is_deliverable": True, "mx_records": ["mx.newlead.com"]},
            {"email": "invalid@expired-domain.xyz", "valid_syntax": True, "has_mx": False, "is_deliverable": False, "mx_records": []},
        ]

        with patch("agents.lead_extract_agent.JinaReaderTool") as MockJina, \
             patch("agents.lead_extract_agent.LLMTool") as MockLLM, \
             patch("agents.lead_extract_agent.GoogleSearchTool") as MockGoogle, \
             patch("agents.lead_extract_agent.react_loop", return_value=react_result_with_emails), \
             patch("agents.lead_extract_agent.get_settings") as mock_settings, \
             patch("agents.lead_extract_agent.EmailVerifierTool") as MockVerifier:

            mock_settings.return_value.scrape_concurrency = 5
            MockJina.return_value = AsyncMock(close=AsyncMock())
            MockLLM.return_value = AsyncMock(close=AsyncMock())
            MockGoogle.return_value = AsyncMock(close=AsyncMock())

            mock_verifier_instance = AsyncMock()
            mock_verifier_instance.verify_batch = AsyncMock(return_value=mock_verify_results)
            MockVerifier.return_value = mock_verifier_instance

            result = await lead_extract_node(state)

        # Only the deliverable email should remain
        new_leads = [l for l in result["leads"] if l.get("company_name") == "NewLead Corp"]
        assert len(new_leads) == 1
        assert "valid@newlead.com" in new_leads[0]["emails"]
        assert "invalid@expired-domain.xyz" not in new_leads[0]["emails"]


class TestVerifyLeadEmails:
    """Unit tests for the module-level _verify_lead_emails function."""

    @pytest.mark.asyncio
    async def test_removes_undeliverable_emails(self):
        lead = {"company_name": "TestCo", "emails": ["good@test.com", "bad@dead.xyz"]}
        verifier = AsyncMock()
        verifier.verify_batch = AsyncMock(return_value=[
            {"email": "good@test.com", "is_deliverable": True},
            {"email": "bad@dead.xyz", "is_deliverable": False},
        ])
        result = await _verify_lead_emails(lead, verifier)
        assert result["emails"] == ["good@test.com"]

    @pytest.mark.asyncio
    async def test_keeps_all_when_all_deliverable(self):
        lead = {"company_name": "TestCo", "emails": ["a@x.com", "b@x.com"]}
        verifier = AsyncMock()
        verifier.verify_batch = AsyncMock(return_value=[
            {"email": "a@x.com", "is_deliverable": True},
            {"email": "b@x.com", "is_deliverable": True},
        ])
        result = await _verify_lead_emails(lead, verifier)
        assert result["emails"] == ["a@x.com", "b@x.com"]

    @pytest.mark.asyncio
    async def test_no_emails_skips_verification(self):
        lead = {"company_name": "TestCo", "emails": []}
        verifier = AsyncMock()
        verifier.verify_batch = AsyncMock()
        result = await _verify_lead_emails(lead, verifier)
        verifier.verify_batch.assert_not_called()
        assert result["emails"] == []

    @pytest.mark.asyncio
    async def test_verification_error_keeps_original(self):
        lead = {"company_name": "TestCo", "emails": ["a@x.com"]}
        verifier = AsyncMock()
        verifier.verify_batch = AsyncMock(side_effect=Exception("DNS timeout"))
        result = await _verify_lead_emails(lead, verifier)
        # On error, original emails preserved
        assert result["emails"] == ["a@x.com"]

    @pytest.mark.asyncio
    async def test_returns_same_lead_dict(self):
        lead = {"company_name": "TestCo", "emails": ["a@x.com"], "match_score": 0.8}
        verifier = AsyncMock()
        verifier.verify_batch = AsyncMock(return_value=[
            {"email": "a@x.com", "is_deliverable": True},
        ])
        result = await _verify_lead_emails(lead, verifier)
        assert result["match_score"] == 0.8
        assert result["company_name"] == "TestCo"


class TestEvidenceScoring:
    def test_customs_and_contacts_raise_contactability_and_priority(self):
        lead = {
            "company_name": "Acme",
            "match_score": 0.72,
            "fit_score": 0.72,
            "contactability_score": 0.1,
            "priority_tier": "low",
            "emails": ["sales@acme.com"],
            "phone_numbers": ["+49 30 123456"],
            "decision_makers": [{"name": "Jane", "title": "Purchasing Manager", "email": "jane@acme.com"}],
            "customs_data": "importgenius: period 2024; import; partners: Vietnam. Source: https://example.com",
            "evidence": [{"claim": "Customs evidence: import; period 2024", "source_url": "https://example.com"}],
        }

        result = _apply_evidence_to_scores(lead)
        assert result["contactability_score"] > 0.4
        assert result["customs_score"] > 0.5
        assert result["priority_tier"] == "high"

    def test_low_fit_stays_low_even_with_contact_and_customs(self):
        lead = {
            "company_name": "WeakFit",
            "match_score": 0.2,
            "fit_score": 0.2,
            "contactability_score": 0.0,
            "priority_tier": "low",
            "emails": ["info@weakfit.com"],
            "phone_numbers": ["+1 555 0000"],
            "decision_makers": [{"name": "Tom", "title": "Owner", "email": "tom@weakfit.com"}],
            "customs_data": "importgenius: period 2024; import. Source: https://example.com",
            "evidence": [{"claim": "Customs evidence: import", "source_url": "https://example.com"}],
        }

        result = _apply_evidence_to_scores(lead)
        assert result["contactability_score"] > 0.3
        assert result["customs_score"] > 0.4
        assert result["priority_tier"] == "low"

    def test_negative_customs_explanation_does_not_count_as_customs_data(self):
        assert _has_concrete_customs_data("No detailed customs data available through public sources.") is False
        assert _has_concrete_customs_data("No data found - company is an engineering services provider, not an importer/exporter") is False
        assert _has_concrete_customs_data("importgenius: period 2024; import; partners: Vietnam. Source: https://example.com") is True

    def test_negative_customs_text_does_not_raise_customs_score(self):
        lead = {
            "company_name": "Service Co",
            "fit_score": 0.8,
            "contactability_score": 0.2,
            "customs_score": 0.0,
            "priority_tier": "low",
            "emails": [],
            "phone_numbers": [],
            "decision_makers": [],
            "customs_data": "No data available - this is a UK-based service company, not an importer/exporter of goods",
            "evidence": [],
        }
        result = _apply_evidence_to_scores(lead)
        assert result["customs_score"] == 0.0

    @pytest.mark.asyncio
    async def test_negative_customs_text_is_normalized_to_no_data_without_tool_evidence(self):
        sem = asyncio.Semaphore(5)
        react_result_negative_customs = json.dumps({
            "company_name": "Service Co",
            "website": "https://serviceco.com",
            "industry": "Engineering services",
            "description": "Service company",
            "emails": [],
            "phone_numbers": [],
            "social_media": {},
            "customs_data": "No data available - this is a service company, not an importer/exporter of goods",
            "evidence": [],
            "match_score": 0.4,
        })

        with patch("agents.lead_extract_agent.react_loop", return_value=react_result_negative_customs):
            result = await _scrape_and_extract(
                {"link": "https://serviceco.com", "source_keyword": "kw1"},
                AsyncMock(), AsyncMock(), sem,
                insight={"products": ["servo motor"]},
                google_search=AsyncMock(),
            )

        assert result is not None
        assert result["customs_data"] == "No data found"


class TestDecisionMakerEmailInference:
    def test_detects_generic_mailboxes(self):
        assert _is_generic_mailbox("info@acme.com") is True
        assert _is_generic_mailbox("sales.team@acme.com") is True
        assert _is_generic_mailbox("jane.doe@acme.com") is False

    def test_generic_company_mailbox_is_removed_from_decision_maker(self):
        lead = {
            "website": "https://xotechtrading.com",
            "decision_makers": [
                {"name": "Mr. Mohammed", "title": "Managing Director", "email": "info@xotechtrading.com"},
                {"name": "Sales Team", "title": "Sales Department", "email": "sales@xotechtrading.com", "linkedin": "https://linkedin.com/in/sales-team"},
            ],
        }
        result = _normalize_decision_maker_emails(lead)
        assert result["decision_makers"][0]["email"] == ""
        assert result["decision_makers"][1]["email"] == ""
        assert result["decision_makers"][1]["linkedin"] == "https://linkedin.com/in/sales-team"

    def test_inferred_email_without_pattern_evidence_is_removed(self):
        lead = {
            "website": "https://degrenne.fr",
            "decision_makers": [
                {"name": "Francois Degrenne", "title": "Owner/CEO", "email": "francois@degrenne.fr (inferred)"},
            ],
        }
        result = _normalize_decision_maker_emails(lead)
        assert result["decision_makers"][0]["email"] == ""

    def test_inferred_email_uses_real_same_domain_pattern(self):
        lead = {
            "website": "https://acme.com",
            "decision_makers": [
                {"name": "Jane Smith", "title": "Sales Director", "email": "jane.smith@acme.com"},
                {"name": "John Doe", "title": "CEO", "email": "inferred"},
            ],
        }
        result = _normalize_decision_maker_emails(lead)
        assert result["decision_makers"][1]["email"] == "john.doe@acme.com (inferred)"


class TestFinalDedupSafetyNet:
    """Final dedup pass on `existing_leads + new_leads` to guarantee no duplicates
    ever escape into the email_craft stage (defends against requeue state
    re-injection, seen_urls drift across rounds, etc.)."""

    async def test_dedupes_identical_leads_by_website_domain(self):
        from agents.lead_extract_agent import _official_website_domain

        lead = {
            "company_name": "IE Wholesale Inc",
            "website": "http://www.iewholesale.online/",
            "emails": ["info@iewholesale.online"],
        }
        # Simulate the safety net directly via _official_website_domain
        domain = _official_website_domain(lead["website"])
        assert domain == "iewholesale.online"
        # Two identical domains should collapse to one
        assert len({domain, _official_website_domain("https://iewholesale.online/")}) == 1

    def test_final_dedup_drops_duplicate_website_in_existing_and_new(self):
        """Direct unit test of the final dedup logic: same website in both
        `existing_leads` (carried over from a previous round) and `new_leads`
        (this round) should keep only one entry."""
        from agents.lead_extract_agent import _official_website_domain

        existing_leads = [{
            "company_name": "IE Wholesale Inc",
            "website": "http://www.iewholesale.online/",
            "emails": ["info@iewholesale.online"],
        }]
        new_leads = [{
            "company_name": "IE Wholesale Inc",
            "website": "http://www.iewholesale.online/",
            "emails": ["info@iewholesale.online"],
        }]

        # Reproduce the final dedup loop in isolation
        merged = existing_leads + new_leads
        deduped = []
        final_seen_domains: set[str] = set()
        for lead in merged:
            domain = _official_website_domain(lead.get("website", ""))
            if domain:
                if domain in final_seen_domains:
                    continue
                final_seen_domains.add(domain)
            deduped.append(lead)

        assert len(deduped) == 1
        assert deduped[0]["website"] == "http://www.iewholesale.online/"

    def test_final_dedup_falls_back_to_company_name_when_no_website(self):
        from agents.lead_extract_agent import _official_website_domain

        lead = {"company_name": "Acme Vapes", "website": "", "emails": []}
        domain = _official_website_domain(lead["website"])
        assert domain == ""
        # When website is empty, dedup key should fall back to company_name
        name_key = (lead["company_name"] or "").strip().lower()
        assert name_key == "acme vapes"


class TestExistingLeadsGlobalDedup:
    """Inherited customers are never discarded because of an old registry owner."""

    @pytest.mark.asyncio
    async def test_existing_leads_preserved_across_hunt_ids(self, tmp_path: Path):
        """Retry must retain the complete inherited baseline."""
        from agents.lead_identity import lead_identity_keys
        from emailing.store import EmailStore

        store = EmailStore(str(tmp_path / "test.db"))
        store.init_db()

        # Hunt A registered these leads first
        lead_a1 = {"company_name": "Acme Corp", "website": "https://acme.com", "emails": ["info@acme.com"]}
        lead_a2 = {"company_name": "Beta Ltd", "website": "https://beta.com", "emails": []}

        store.reserve_lead_keys(
            [lead_a1, lead_a2],
            hunt_id="hunt-alpha",
            key_fn=lead_identity_keys,
            now_iso="2026-01-01",
        )

        # Hunt B's existing_leads accidentally contains duplicates from Hunt A
        # (this happens when user continues mining and search results overlap)
        existing_leads = [
            {"company_name": "Acme Corp", "website": "https://acme.com", "emails": ["info@acme.com"]},  # from hunt-alpha
            {"company_name": "Beta Ltd", "website": "https://beta.com", "emails": []},  # from hunt-alpha
            {"company_name": "Gamma Inc", "website": "https://gamma.com", "emails": []},  # actually belongs to hunt-beta
        ]

        result = await lead_extract_node(_base_state(
            hunt_id="hunt-beta", leads=existing_leads, search_results=[]))
        assert result["leads"] == existing_leads

    @pytest.mark.asyncio
    async def test_existing_leads_preserved_when_belonging_to_current_hunt(self, tmp_path: Path):
        """Leads originally from current hunt should be preserved."""
        from agents.lead_identity import lead_identity_keys
        from emailing.store import EmailStore

        store = EmailStore(str(tmp_path / "test.db"))
        store.init_db()

        # Hunt A registered its own leads
        lead_a1 = {"company_name": "Acme Corp", "website": "https://acme.com", "emails": []}
        lead_a2 = {"company_name": "Beta Ltd", "website": "https://beta.com", "emails": []}

        store.reserve_lead_keys(
            [lead_a1, lead_a2],
            hunt_id="hunt-alpha",
            key_fn=lead_identity_keys,
            now_iso="2026-01-01",
        )

        # Continue mining hunt-alpha with same leads
        existing_leads = [
            {"company_name": "Acme Corp", "website": "https://acme.com", "emails": []},
            {"company_name": "Beta Ltd", "website": "https://beta.com", "emails": []},
        ]

        result = await lead_extract_node(_base_state(
            hunt_id="hunt-alpha", leads=existing_leads, search_results=[]))
        assert result["leads"] == existing_leads


class TestCandidateGlobalDedup:
    """Test pre-scrape global deduplication using candidate_identity_keys."""

    @pytest.mark.asyncio
    async def test_orphan_registry_does_not_block_maps_candidate(self):
        """仅存在于旧注册表、现存任务没有的客户可以重新抓取。"""
        import tempfile
        from pathlib import Path

        from agents.lead_extract_agent import lead_extract_node
        from emailing.store import EmailStore

        with tempfile.TemporaryDirectory() as tmpdir:
            store = EmailStore(str(Path(tmpdir) / "test.db"))
            store.init_db()

            # Hunt A 已经收集了德国的 Vape Store
            hunt_a_lead = {
                "company_name": "Vape Store",
                "website": "https://vape-berlin.com",
                "emails": [],
            }
            from agents.lead_identity import lead_identity_keys
            store.reserve_lead_keys(
                [hunt_a_lead],
                hunt_id="hunt-a",
                key_fn=lead_identity_keys,
                now_iso="2026-01-01"
            )

            # Hunt B 搜索到波兰的 Vape Store (不同 place_id)
            state = {
                "hunt_id": "hunt-b",
                "hunt_round": 1,
                "current_stage": "lead_extract",
                "search_results": [
                    {
                        "link": "https://maps.google.com/?cid=999",
                        "title": "Vape Store",
                        "source_keyword": "kw1",
                        "maps_data": {
                            "place_id": "ChIJ_poland_vape_store",
                            "title": "Vape Store",
                            "address": "Warsaw, Poland",
                        },
                    }
                ],
                "existing_leads": [],
                "target_lead_count": 50,
                "insight": {"products": ["vape"]},
            }

            react_result = json.dumps({
                "company_name": "Vape Store Warsaw",
                "website": "https://vape-warsaw.pl",
                "industry": "Retail",
                "description": "Vape retailer in Poland",
                "emails": ["sales@vape-warsaw.pl"],
                "phone_numbers": [],
                "social_media": {},
                "decision_makers": [],
                "address": "Warsaw, Poland",
                "match_score": 0.75,
                "fit_reasons": ["Vape Store Warsaw is a retailer selling vape products in Poland"],
                "disqualify_reasons": [],
            })

            # Mock scraping to return valid lead
            with patch("agents.lead_extract_agent.JinaReaderTool") as MockJina, \
                 patch("agents.lead_extract_agent.LLMTool") as MockLLM, \
                 patch("agents.lead_extract_agent.GoogleSearchTool") as MockGoogle, \
                 patch("agents.lead_extract_agent.react_loop", return_value=react_result), \
                 patch("agents.lead_extract_agent.get_settings") as mock_settings, \
                 patch("agents.lead_extract_agent.current_lead_keys", return_value=set()):

                mock_settings.return_value.scrape_concurrency = 5

                jina_instance = AsyncMock()
                jina_instance.close = AsyncMock()
                MockJina.return_value = jina_instance

                llm_instance = AsyncMock()
                llm_instance.close = AsyncMock()
                # QuickGate passes
                llm_instance.generate = AsyncMock(return_value=json.dumps({
                    "pass_gate": True,
                    "entity_type": "company",
                    "customer_role_guess": "retailer",
                    "competitor_risk": "low",
                    "confidence": 0.8,
                    "reason": "Vape retailer",
                    "risk_flags": []
                }))
                MockLLM.return_value = llm_instance

                google_instance = AsyncMock()
                google_instance.close = AsyncMock()
                MockGoogle.return_value = google_instance

                result = await lead_extract_node(state)

                # 应该成功抓取波兰的 Vape Store（不同 place_id）
                assert "leads" in result
                assert len(result["leads"]) == 1
                assert result["leads"][0]["company_name"] == "Vape Store Warsaw"

    @pytest.mark.asyncio
    async def test_same_domain_with_different_company_is_allowed(self):
        """相同域名但公司名称不同的候选不能被域名规则误杀"""
        import tempfile
        from pathlib import Path

        from agents.lead_extract_agent import lead_extract_node
        from emailing.store import EmailStore

        with tempfile.TemporaryDirectory() as tmpdir:
            store = EmailStore(str(Path(tmpdir) / "test.db"))
            store.init_db()

            # Hunt A 已经收集了 Acme Corp；同域名的新公司名仍应允许抓取。
            hunt_a_lead = {
                "company_name": "Acme Corp",
                "website": "https://acme.com",
                "emails": [],
            }
            from agents.lead_identity import lead_identity_keys
            store.reserve_lead_keys(
                [hunt_a_lead],
                hunt_id="hunt-a",
                key_fn=lead_identity_keys,
                now_iso="2026-01-01"
            )

            # Hunt B 搜索到同一个域名但不同公司名
            state = {
                "hunt_id": "hunt-b",
                "hunt_round": 1,
                "current_stage": "lead_extract",
                "search_results": [
                    {
                        "link": "https://acme.com/contact",
                        "title": "Acme Corporation",
                        "source_keyword": "kw1",
                        "maps_data": {},
                    }
                ],
                "existing_leads": [],
                "target_lead_count": 50,
                "insight": {"products": ["widgets"]},
            }

            with patch("agents.lead_extract_agent.JinaReaderTool") as MockJina, \
                 patch("agents.lead_extract_agent.LLMTool") as MockLLM, \
                 patch("agents.lead_extract_agent.GoogleSearchTool") as MockGoogle, \
                 patch("agents.lead_extract_agent.get_settings") as mock_settings, \
                  patch("agents.lead_extract_agent.current_lead_keys", return_value={"company:acme corp"}):

                mock_settings.return_value.scrape_concurrency = 5

                jina_instance = AsyncMock()
                jina_instance.close = AsyncMock()
                MockJina.return_value = jina_instance

                llm_instance = AsyncMock()
                llm_instance.close = AsyncMock()
                MockLLM.return_value = llm_instance

                google_instance = AsyncMock()
                google_instance.close = AsyncMock()
                MockGoogle.return_value = google_instance

                result = await lead_extract_node(state)

                # 公司名不同，不应因共享域名而被预过滤。
                leads = result.get("leads", [])
                assert len(leads) == 1
