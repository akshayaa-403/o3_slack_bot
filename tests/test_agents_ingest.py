"""Tests for Agent 1 (ingestion) + the LLM interface. No network, no API keys."""

import json

import pytest

from agents.connectors import MockTicketConnector, normalize_ticket
from agents.ingest import build_extraction_prompt, extract_interaction, run_ingestion
from agents.llm import LLMError, MockLLMClient, extract_json


# --- LLM JSON extraction -----------------------------------------------------
def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_embedded_in_prose():
    assert extract_json('Sure, here you go:\n{"a": 1}\nHope that helps!') == {"a": 1}


def test_extract_json_none_raises():
    with pytest.raises(LLMError):
        extract_json("there is no json here")


def test_mock_llm_returns_scripted_in_order():
    llm = MockLLMClient(responses=["one", "two"])
    assert llm.complete("x") == "one"
    assert llm.complete("y") == "two"
    assert len(llm.calls) == 2


# --- extraction --------------------------------------------------------------
def test_extract_interaction_basic():
    ticket = normalize_ticket({"id": "T1", "summary": "VPN drops", "category": "VPN"})
    llm = MockLLMClient(responses=[json.dumps({
        "request": "vpn keeps dropping",
        "resolution": "update the client",
        "category": "VPN & Network",
        "phrasings": ["vpn disconnects every few minutes"],
    })])
    interaction = extract_interaction(ticket, llm)
    assert interaction["request"] == "vpn keeps dropping"
    assert interaction["category_hint"] == "VPN & Network"
    # the ticket summary is kept as a real user phrasing
    assert "VPN drops" in interaction["phrasings"]
    assert "vpn disconnects every few minutes" in interaction["phrasings"]


def test_extract_interaction_empty_request_returns_none():
    ticket = normalize_ticket({"id": "T2", "summary": "noise"})
    llm = MockLLMClient(responses=[json.dumps({"request": "", "phrasings": []})])
    assert extract_interaction(ticket, llm) is None


def test_build_extraction_prompt_contains_ticket():
    ticket = normalize_ticket({"id": "T9", "summary": "Need Jira access", "description": "pls"})
    prompt = build_extraction_prompt(ticket)
    assert "Need Jira access" in prompt
    assert "T9" in prompt


# --- run_ingestion -----------------------------------------------------------
def test_run_ingestion_counts_and_tags_source():
    connector = MockTicketConnector()  # 4 sample tickets
    responses = [
        json.dumps({"request": f"req {i}", "resolution": "x", "category": "c",
                    "phrasings": [f"phrasing {i}"]})
        for i in range(4)
    ]
    corpus = run_ingestion(connector, MockLLMClient(responses=responses))
    assert corpus["count"] == 4
    assert corpus["source"] == "mock"
    assert all(item["source"] == "mock" for item in corpus["interactions"])


def test_run_ingestion_collects_failures_without_crashing():
    connector = MockTicketConnector(tickets=[{"id": "T1", "summary": "s"}])
    corpus = run_ingestion(connector, MockLLMClient(responses=["not json at all"]))
    assert corpus["count"] == 0
    assert len(corpus["failed"]) == 1
    assert corpus["failed"][0]["source_id"] == "T1"


def test_run_ingestion_skips_empty_request():
    connector = MockTicketConnector(tickets=[{"id": "T1", "summary": "s"}])
    corpus = run_ingestion(connector, MockLLMClient(responses=[json.dumps({"request": ""})]))
    assert corpus["count"] == 0
    assert corpus["skipped"] == 1


def test_mock_connector_respects_limit():
    assert len(MockTicketConnector().fetch_tickets(limit=2)) == 2
