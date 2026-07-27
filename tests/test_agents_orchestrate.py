"""Tests for the orchestration layer: store, review surface, orchestrator,
triggers, Gemini client, loader converter. Full command->agents->Lex->summary
flow, all mocked. No network, no AWS, no API keys.
"""

import json

import pytest

from agents import review, triggers
from agents.connectors import MockTicketConnector
from agents.llm import GeminiLLMClient, LLMError, MockLLMClient
from agents.orchestrate import (
    approve_proposal,
    discard_proposal,
    generate_proposal,
    run_and_load,
    summarize,
)
from agents.review import (
    ACTION_ID_APPROVE,
    ACTION_ID_DISCARD,
    ACTION_ID_EDIT,
    MockReviewSurface,
    SlackReviewSurface,
    build_card,
    render_slack_blocks,
    slack_action_to_intent_action,
)
from agents.store import InMemoryProposalStore

BOT = {"bot_id": "BOT123", "region": "ap-southeast-2"}


def _extract_scripts():
    return [
        json.dumps({"request": "Reset ADAM password", "resolution": "Use self service",
                    "category": "Password & Auth", "phrasings": ["reset my ADAM password"]}),
        json.dumps({"request": "VPN keeps dropping", "resolution": "Update client",
                    "category": "VPN & Network", "phrasings": ["my vpn drops"]}),
        json.dumps({"request": "Access to Smartsheet", "resolution": "Grant via SSO",
                    "category": "Software & Access", "phrasings": ["how do I get Smartsheet access?"]}),
        json.dumps({"request": "Access to a Smartsheet license", "resolution": "Provision license",
                    "category": "Software & Access", "phrasings": ["I need a Smartsheet license"]}),
    ]


def _label_scripts():
    return [
        json.dumps({"intent_name": "AdamPasswordReset", "category": "Password & Auth",
                    "response": "Use ADAM Self Service.", "uses_lambda": False}),
        json.dumps({"intent_name": "VpnDisconnecting", "category": "VPN & Network",
                    "response": "Update GlobalProtect.", "uses_lambda": False}),
        json.dumps({"intent_name": "SmartsheetAccess", "category": "Software & Access",
                    "response": "Raise a Smartsheet request; access via SSO.", "uses_lambda": True}),
    ]


def _llm():
    # Agent 1 calls once per ticket (4), Agent 2 once per cluster (3): FIFO queue.
    return MockLLMClient(responses=_extract_scripts() + _label_scripts())


def _recording_loader():
    """A fake loader Lambda that records what it was handed and reports success."""
    seen = {}

    def loader_fn(intents, load_options):
        seen["intents"] = intents
        seen["load_options"] = load_options
        return {"ok": True, "bot_id": load_options["bot_id"], "intent_count": len(intents),
                "summary": {"created": len(intents), "updated": 0, "failures": [], "build": "Building"}}

    return loader_fn, seen


# --- store -------------------------------------------------------------------
def test_store_put_get_and_status():
    store = InMemoryProposalStore()
    store.put({"proposal_id": "p1", "status": "pending"})
    assert store.get("p1")["status"] == "pending"
    store.set_status("p1", "loaded")
    assert store.get("p1")["status"] == "loaded"
    assert store.get("missing") is None


# --- orchestrator: generate --------------------------------------------------
def test_generate_proposal_pending_with_intents_and_stats():
    store = InMemoryProposalStore()
    proposal = generate_proposal(MockTicketConnector(), _llm(), proposal_id="p1", store=store)
    assert proposal["status"] == "pending"
    assert proposal["stats"]["tickets"] == 4
    assert proposal["stats"]["intents"] == 3        # Smartsheet's two tickets merge
    assert proposal["stats"]["clusters"] == 3
    assert proposal["xlsx_path"] is None            # no sheet unless output_dir given
    assert len(proposal["intents"]) == 3
    assert store.get("p1") is proposal


def test_generate_proposal_writes_optional_sheet(tmp_path):
    proposal = generate_proposal(
        MockTicketConnector(), _llm(), proposal_id="p1", output_dir=str(tmp_path)
    )
    assert (tmp_path / "intents_p1.xlsx").exists()


# --- the automated end-to-end path ------------------------------------------
def test_run_and_load_generates_loads_and_summarizes():
    loader_fn, seen = _recording_loader()
    store = InMemoryProposalStore()
    result = run_and_load(
        MockTicketConnector(), _llm(), load_options=BOT, loader_fn=loader_fn, store=store,
    )
    assert result["proposal"]["status"] == "loaded"
    assert len(seen["intents"]) == 3                # Agent 2's intents were handed off
    assert seen["load_options"]["bot_id"] == "BOT123"
    assert "Loaded to Lex" in result["summary"]
    assert store.get(result["proposal"]["proposal_id"])["status"] == "loaded"


def test_run_and_load_requires_bot_id():
    with pytest.raises(ValueError):
        run_and_load(MockTicketConnector(), _llm(), load_options={}, loader_fn=lambda *a: {})


def test_summarize_reports_load_failure():
    proposal = {"source": "jira", "stats": {"intents": 2, "tickets": 5}}
    text = summarize(proposal, {"ok": False, "error": "AccessDenied"})
    assert "FAILED" in text and "AccessDenied" in text


# --- triggers: slash command (auto) -----------------------------------------
def test_handle_generate_command_auto_loads_and_summarizes():
    loader_fn, seen = _recording_loader()
    surface = MockReviewSurface()
    result = triggers.handle_generate_command(
        "--threshold 0.3", connector=MockTicketConnector(), llm=_llm(),
        review_surface=surface, store=InMemoryProposalStore(),
        load_options=BOT, loader_fn=loader_fn,
    )
    assert result["proposal"]["status"] == "loaded"
    assert len(seen["intents"]) == 3
    assert len(surface.summaries) == 1              # user got exactly one summary
    assert "Loaded to Lex" in surface.summaries[0]


def test_parse_command_args():
    assert triggers.parse_command_args("--threshold 0.25 --limit 50") == {"threshold": 0.25, "limit": 50}
    assert triggers.parse_command_args("") == {}
    assert triggers.parse_command_args("--threshold notanumber") == {}   # bad flag ignored


# --- triggers: webhook (auto) -----------------------------------------------
def test_handle_ticket_event_ignores_irrelevant_events():
    loader_fn, _ = _recording_loader()
    out = triggers.handle_ticket_event(
        {"event_type": "issue_commented"}, connector=MockTicketConnector(),
        llm=_llm(), review_surface=MockReviewSurface(),
        load_options=BOT, loader_fn=loader_fn,
    )
    assert out is None


def test_handle_ticket_event_runs_on_resolved():
    loader_fn, seen = _recording_loader()
    surface = MockReviewSurface()
    out = triggers.handle_ticket_event(
        {"event_type": "issue_resolved"}, connector=MockTicketConnector(),
        llm=_llm(), review_surface=surface, load_options=BOT, loader_fn=loader_fn,
    )
    assert out is not None
    assert len(seen["intents"]) == 3 and len(surface.summaries) == 1


# --- review card + Slack rendering (optional review mode) -------------------
def test_build_card_and_slack_blocks():
    proposal = {
        "proposal_id": "p9", "source": "jira",
        "stats": {"tickets": 4, "interactions": 4, "clusters": 3},
        "intents": [
            {"intent_name": "SmartsheetAccess", "category": "Software & Access",
             "uses_lambda": True, "utterances": ["a", "b"], "response": "Raise a request."},
        ],
    }
    card = build_card(proposal)
    assert card["rows"][0]["kind"] == "action"       # uses_lambda -> action
    blocks = render_slack_blocks(card)
    action_block = [b for b in blocks if b["type"] == "actions"][0]
    action_ids = {e["action_id"] for e in action_block["elements"]}
    assert action_ids == {ACTION_ID_APPROVE, ACTION_ID_EDIT, ACTION_ID_DISCARD}
    for element in action_block["elements"]:
        assert json.loads(element["value"])["proposal_id"] == "p9"


def test_slack_action_mapping():
    assert slack_action_to_intent_action(ACTION_ID_APPROVE) == review.APPROVE
    assert slack_action_to_intent_action(ACTION_ID_DISCARD) == review.DISCARD
    assert slack_action_to_intent_action("something_else") is None


def test_slack_surface_posts_card_and_summary():
    calls = []
    surface = SlackReviewSurface(
        post_message=lambda channel, text, blocks=None, **kw: calls.append((channel, text, blocks)) or "ts1",
        channel="C123",
    )
    surface.post(build_card({"proposal_id": "p", "source": "mock", "stats": {}, "intents": []}))
    ref = surface.post_summary("all done")
    assert calls[0][0] == "C123"
    assert calls[1] == ("C123", "all done", None)    # summary is plain text, no blocks
    assert ref["ref"] == "ts1"


# --- optional review mode: approve / discard --------------------------------
def test_approve_loads_intents_via_injected_loader():
    loader_fn, seen = _recording_loader()
    store = InMemoryProposalStore()
    generate_proposal(MockTicketConnector(), _llm(), proposal_id="p1", store=store)
    result = triggers.handle_review_action(
        review.APPROVE, "p1", store, load_options=BOT, loader_fn=loader_fn,
    )
    assert result["status"] == "loaded"
    assert len(seen["intents"]) == 3                 # the proposal's own intents
    assert store.get("p1")["status"] == "loaded"


def test_approve_requires_bot_id():
    store = InMemoryProposalStore()
    generate_proposal(MockTicketConnector(), _llm(), proposal_id="p1", store=store)
    with pytest.raises(ValueError):
        approve_proposal("p1", store, load_options={}, loader_fn=lambda *a: {})


def test_approve_unknown_proposal_raises():
    with pytest.raises(ValueError):
        approve_proposal("nope", InMemoryProposalStore(), load_options=BOT, loader_fn=lambda *a: {})


def test_discard_marks_status():
    store = InMemoryProposalStore()
    generate_proposal(MockTicketConnector(), _llm(), proposal_id="p1", store=store)
    result = triggers.handle_review_action(review.DISCARD, "p1", store)
    assert result["status"] == "discarded"
    assert store.get("p1")["status"] == "discarded"


# --- Gemini client (no network — urlopen monkeypatched) ---------------------
def test_gemini_client_builds_json_request_and_parses(monkeypatch):
    import agents.llm as llm_mod

    captured = {}

    class FakeResp:
        def __init__(self, body):
            self._body = body

        def read(self):
            return json.dumps(self._body).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResp({"candidates": [{"content": {"parts": [{"text": '{"intent_name": "X"}'}]}}]})

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)
    client = GeminiLLMClient(api_key="secret", model="gemini-2.5-flash")
    data = client.complete_json("label these requests", system="be terse")

    assert data == {"intent_name": "X"}
    assert "gemini-2.5-flash:generateContent" in captured["url"]
    assert captured["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert captured["body"]["systemInstruction"]["parts"][0]["text"] == "be terse"


def test_gemini_client_requires_api_key():
    with pytest.raises(LLMError):
        GeminiLLMClient(api_key="").complete("hello")


# --- loader Lambda converter -------------------------------------------------
def test_loader_converts_agent_intents_and_drops_slot_utterances():
    import lambda_o3_lex_intent_loader as lex_loader

    out = lex_loader.to_loader_intents([
        {"intent_name": "Smartsheet Access", "category": "C", "uses_lambda": True,
         "response": "r", "utterances": ["get access", "smartsheet please", "give me {slot}"]},
    ])
    assert len(out) == 1
    assert out[0]["utterances"] == ["get access", "smartsheet please"]  # brace utterance dropped
    assert out[0]["original_name"] == "Smartsheet Access"
    assert out[0]["uses_lambda"] is True
    assert out[0]["intent_name"]                     # a Lex-safe name was generated
