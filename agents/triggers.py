"""Triggers — the automation edge. Thin adapters that turn a platform event
into one orchestrator call, then send the user a short summary.

    handle_generate_command  <- Slack (or Teams, ...) slash command, on-demand
    handle_ticket_event      <- ticket-system webhook (e.g. Jira issue resolved)
    handle_review_action     <- optional review mode: Approve / Edit / Discard click

Default (automated) path per the agreed flow:
    command -> Agent 1 -> Agent 2 (Gemini labels) -> loader Lambda -> Lex -> summary

Adding Microsoft Teams, a cron schedule, or a ServiceNow webhook means a new
function here (or a new connector/surface) — the orchestrator underneath is
untouched. Everything is injected, so this whole module is testable with mocks.
"""

from __future__ import annotations

import shlex
from typing import Callable, Dict, Optional

from agents import orchestrate, review
from agents.llm import GeminiLLMClient, LLMClient
from agents.review import ReviewSurface
from agents.store import ProposalStore

# Ticket events that should kick off a generation run (extend per platform).
GENERATE_ON_EVENTS = {"issue_resolved", "issue_generic", "jira:issue_updated"}


def parse_command_args(text: str) -> Dict:
    """Parse `/generate-intents --threshold 0.3 --limit 50` into a dict.

    Unknown/malformed flags are ignored rather than fatal — a support user
    fat-fingering a flag should still get a run with defaults.
    """
    opts: Dict[str, object] = {}
    try:
        tokens = shlex.split(text or "")
    except ValueError:
        tokens = (text or "").split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("--threshold", "--limit"):
            if index + 1 < len(tokens):
                raw = tokens[index + 1]
                try:
                    opts[token[2:]] = float(raw) if token == "--threshold" else int(raw)
                except ValueError:
                    pass
                index += 2
                continue
        index += 1
    return opts


def _run_and_summarize(
    *,
    connector,
    llm: Optional[LLMClient],
    review_surface: ReviewSurface,
    store: Optional[ProposalStore],
    load_options: Dict,
    loader_fn: Optional[Callable[..., Dict]] = None,
    threshold: float = 0.3,
    limit: Optional[int] = None,
) -> Dict:
    # Default to the live provider (Gemini) unless a client is injected (tests).
    llm = llm or GeminiLLMClient()
    result = orchestrate.run_and_load(
        connector, llm, load_options=load_options, threshold=threshold,
        limit=limit, loader_fn=loader_fn, store=store,
    )
    result["post_ref"] = review_surface.post_summary(result["summary"])
    return result


def handle_generate_command(
    text: str,
    *,
    connector,
    review_surface: ReviewSurface,
    load_options: Dict,
    llm: Optional[LLMClient] = None,
    store: Optional[ProposalStore] = None,
    loader_fn: Optional[Callable[..., Dict]] = None,
) -> Dict:
    """Slack/Teams slash command -> run the full pipeline -> summarize to the user."""
    opts = parse_command_args(text)
    return _run_and_summarize(
        connector=connector, llm=llm, review_surface=review_surface, store=store,
        load_options=load_options, loader_fn=loader_fn,
        threshold=float(opts.get("threshold", 0.3)), limit=opts.get("limit"),
    )


def handle_ticket_event(
    event: Dict,
    *,
    connector,
    review_surface: ReviewSurface,
    load_options: Dict,
    llm: Optional[LLMClient] = None,
    store: Optional[ProposalStore] = None,
    loader_fn: Optional[Callable[..., Dict]] = None,
    limit: Optional[int] = None,
) -> Optional[Dict]:
    """Ticket-system webhook -> run the pipeline if the event qualifies.

    Returns None (no-op) for events we don't act on, so the webhook endpoint can
    ack everything cheaply and only do work when it matters.
    """
    event_name = str(event.get("event_type") or event.get("issue_event_type_name") or "").strip()
    if event_name not in GENERATE_ON_EVENTS:
        return None
    return _run_and_summarize(
        connector=connector, llm=llm, review_surface=review_surface, store=store,
        load_options=load_options, loader_fn=loader_fn, limit=limit,
    )


def handle_review_action(
    action: str,
    proposal_id: str,
    store: ProposalStore,
    *,
    load_options: Optional[Dict] = None,
    loader_fn: Optional[Callable[..., Dict]] = None,
) -> Dict:
    """Optional review mode: dispatch an Approve / Edit / Discard click."""
    if action == review.APPROVE:
        return orchestrate.approve_proposal(
            proposal_id, store, load_options=load_options, loader_fn=loader_fn
        )
    if action == review.DISCARD:
        return orchestrate.discard_proposal(proposal_id, store)
    if action == review.EDIT:
        proposal = store.get(proposal_id)
        if proposal is None:
            raise ValueError(f"unknown proposal_id: {proposal_id}")
        # Edit = hand the draft sheet to a power user; they load it after tweaking.
        return {"proposal_id": proposal_id, "action": review.EDIT,
                "xlsx_path": proposal.get("xlsx_path")}
    raise ValueError(f"unknown review action: {action}")


# --- runnable demo: command -> agents -> loader -> Lex -> summary (mocks) ----
def _demo() -> None:
    import json
    import sys

    try:  # summary text is plain ASCII, but be safe on cp1252 consoles
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    from agents.connectors import MockTicketConnector
    from agents.llm import MockLLMClient
    from agents.review import SlackReviewSurface
    from agents.store import InMemoryProposalStore

    extract = [
        json.dumps({"request": "Reset ADAM password", "resolution": "Use self service",
                    "category": "Password & Auth", "phrasings": ["reset my ADAM password"]}),
        json.dumps({"request": "VPN keeps dropping", "resolution": "Update GlobalProtect",
                    "category": "VPN & Network", "phrasings": ["my vpn drops"]}),
        json.dumps({"request": "Access to Smartsheet", "resolution": "Grant via SSO",
                    "category": "Software & Access", "phrasings": ["how do I get Smartsheet access?"]}),
        json.dumps({"request": "Access to a Smartsheet license", "resolution": "Provision license",
                    "category": "Software & Access", "phrasings": ["I need a Smartsheet license"]}),
    ]
    label = [
        json.dumps({"intent_name": "AdamPasswordReset", "category": "Password & Auth",
                    "response": "Use ADAM Self Service.", "uses_lambda": False}),
        json.dumps({"intent_name": "VpnDisconnecting", "category": "VPN & Network",
                    "response": "Update GlobalProtect and switch gateway.", "uses_lambda": False}),
        json.dumps({"intent_name": "SmartsheetAccess", "category": "Software & Access",
                    "response": "Raise a Smartsheet request; access via SSO.", "uses_lambda": True}),
    ]
    llm = MockLLMClient(responses=extract + label)  # stands in for the live GeminiLLMClient

    # Fake loader Lambda: pretend Lex accepted everything.
    def fake_loader(intents, load_options):
        return {"ok": True, "bot_id": load_options["bot_id"], "intent_count": len(intents),
                "summary": {"created": len(intents), "updated": 0, "failures": [], "build": "Building"}}

    captured = {}
    surface = SlackReviewSurface(
        post_message=lambda channel, text, **kw: captured.update(channel=channel, text=text) or "ts-1",
        channel="#it-support-intents",
    )

    print("=" * 70)
    print("  Automated pipeline demo: /generate-intents -> agents -> Lex -> summary")
    print("=" * 70)
    print("\n[1] user runs:  /generate-intents --threshold 0.3")

    result = handle_generate_command(
        "--threshold 0.3", connector=MockTicketConnector(), llm=llm,
        review_surface=surface, store=InMemoryProposalStore(),
        load_options={"bot_id": "DEMOBOT", "region": "ap-southeast-2"},
        loader_fn=fake_loader,
    )
    stats = result["proposal"]["stats"]
    print(f"    Agent 1: {stats['tickets']} tickets -> {stats['interactions']} interactions")
    print(f"    Agent 2: {stats['clusters']} clusters -> {stats['intents']} intents "
          f"(Gemini labels them)")
    print(f"    handed off to loader Lambda -> Lex bot DEMOBOT (status: {result['proposal']['status']})")
    print(f"\n[2] summary posted back to {captured['channel']}:")
    print(f"    \"{captured['text']}\"")
    print("=" * 70)


if __name__ == "__main__":
    _demo()
