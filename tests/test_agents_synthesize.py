"""Tests for Agent 2 (synthesis) + its contract with the Lex loader. No network."""

import json

import pytest

import load_lex_intents_from_excel as loader
from agents.llm import MockLLMClient
from agents.synthesize import (
    cluster_interactions,
    jaccard,
    label_cluster,
    run_synthesis,
    write_intent_workbook,
)


# --- clustering --------------------------------------------------------------
def test_jaccard():
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert jaccard({"a"}, {"b"}) == 0.0
    assert jaccard(set(), {"a"}) == 0.0


def test_cluster_merges_similar_within_category():
    corpus = {"interactions": [
        {"request": "get access to smartsheet", "category_hint": "Software",
         "phrasings": ["a"], "resolution": "", "source_id": "1"},
        {"request": "get access to a smartsheet license", "category_hint": "Software",
         "phrasings": ["b"], "resolution": "", "source_id": "2"},
        {"request": "reset my password", "category_hint": "Auth",
         "phrasings": ["c"], "resolution": "", "source_id": "3"},
    ]}
    clusters = cluster_interactions(corpus, threshold=0.3)
    assert len(clusters) == 2                       # smartsheet x2 merge, password alone
    assert sorted(len(c["interactions"]) for c in clusters) == [1, 2]


def test_cluster_separates_by_category():
    corpus = {"interactions": [
        {"request": "access to smartsheet", "category_hint": "A",
         "phrasings": [], "resolution": "", "source_id": "1"},
        {"request": "access to smartsheet", "category_hint": "B",
         "phrasings": [], "resolution": "", "source_id": "2"},
    ]}
    assert len(cluster_interactions(corpus)) == 2   # same text, different category


# --- labelling ---------------------------------------------------------------
def test_label_cluster_uses_llm_and_grounds_utterances():
    cluster = {"category": "Software & Access", "interactions": [
        {"request": "get access to smartsheet", "resolution": "grant via sso",
         "phrasings": ["how do I get smartsheet?", "smartsheet access"], "source_id": "1"},
        {"request": "smartsheet license", "resolution": "provision license",
         "phrasings": ["need smartsheet license"], "source_id": "2"},
    ]}
    llm = MockLLMClient(responses=[json.dumps({
        "intent_name": "SmartsheetAccess", "category": "Software & Access",
        "response": "Raise a request; granted via SSO.", "uses_lambda": True,
    })])
    intent = label_cluster(cluster, llm)
    assert intent["intent_name"] == "SmartsheetAccess"
    assert intent["uses_lambda"] is True
    # utterances are the REAL phrasings, not invented by the LLM
    assert "how do I get smartsheet?" in intent["utterances"]
    assert "need smartsheet license" in intent["utterances"]
    assert intent["source_ids"] == ["1", "2"]


def test_label_cluster_falls_back_when_llm_output_unusable():
    cluster = {"category": "VPN", "interactions": [
        {"request": "vpn keeps dropping", "resolution": "update client",
         "phrasings": ["vpn drops"], "source_id": "9"},
    ]}
    intent = label_cluster(cluster, MockLLMClient(responses=["not json at all"]))
    assert intent["intent_name"]                       # deterministic fallback name
    assert intent["response"] == "update client"       # fallback from resolutions
    assert "vpn drops" in intent["utterances"]


# --- the contract with the loader -------------------------------------------
def test_synthesis_xlsx_round_trips_through_loader(tmp_path):
    corpus = {"interactions": [
        {"request": "get access to smartsheet", "resolution": "grant via sso",
         "category_hint": "Software & Access",
         "phrasings": ["how do I get access to smartsheet?"], "source_id": "1"},
        {"request": "reset my adam password", "resolution": "use self service",
         "category_hint": "Password & Auth",
         "phrasings": ["reset adam password"], "source_id": "2"},
    ]}
    labels = [
        json.dumps({"intent_name": "SmartsheetAccess", "category": "Software & Access",
                    "response": "Raise a request; granted via SSO.", "uses_lambda": True}),
        json.dumps({"intent_name": "AdamPasswordReset", "category": "Password & Auth",
                    "response": "Use ADAM Self Service.", "uses_lambda": False}),
    ]
    xlsx = str(tmp_path / "draft.xlsx")
    result = run_synthesis(corpus, MockLLMClient(responses=labels), output_xlsx=xlsx)
    assert result["intent_count"] == 2

    # The real loader consumes Agent 2's output unchanged.
    intents = loader.read_intents(xlsx, "All Intents")
    by_name = {i["intent_name"]: i for i in intents}
    assert "SmartsheetAccess" in by_name and "AdamPasswordReset" in by_name
    smartsheet = by_name["SmartsheetAccess"]
    assert smartsheet["uses_lambda"] is True
    assert smartsheet["utterances"]                    # utterances survived the round-trip
    assert "SSO" in smartsheet["response"]


def test_auto_load_requires_bot_id(tmp_path):
    corpus = {"interactions": [
        {"request": "x", "resolution": "", "category_hint": "C",
         "phrasings": ["x"], "source_id": "1"},
    ]}
    with pytest.raises(ValueError):
        run_synthesis(
            corpus, MockLLMClient(responses=["{}"]),
            output_xlsx=str(tmp_path / "d.xlsx"), auto_load=True,
        )
