"""Live Slack demo of the tickets -> Lex-intents pipeline, with human approval.

FLOW (the approval flow you asked for):
    /generate-intents   ->  Agent 1 + Agent 2 run and PREPARE the intents
                            (clustered + labelled). NOTHING is written to Lex yet.
                            A review card is posted with Approve / Edit / Discard.
    click "Approve"     ->  the prepared intents are uploaded to Amazon Lex, then
                            a summary of what changed is posted back.
    click "Discard"     ->  the proposal is dropped; Lex is untouched.

Because button clicks are INBOUND events, this needs Socket Mode (an outbound
WebSocket) — a bot-token-only script cannot receive a button press. Socket Mode
needs its own Slack app (do NOT enable it on the production app; that disables
the Events API HTTP endpoint the live bot relies on). Setup steps are in
demo/README.md.

Only the ticket SOURCE is mocked (agents.connectors.MockTicketConnector). The
labelling is real Gemini and — when you flip DEMO_LEX_LOAD=real — the upload is
a REAL write to the Lex bot in DEMO_LEX_BOT_ID. Default is 'simulated' (logs
what would load, touches nothing) so a misclick can't change your bot.

--- Setup ------------------------------------------------------------------
Reads the repo-root .env (auto-loaded) / environment:
    SLACK_BOT_TOKEN=xoxb-...      required (bot in the demo app)
    SLACK_APP_TOKEN=xapp-...      required (app-level token, Socket Mode)
    GEMINI_API_KEY=...            optional; falls back to mock LLM if unset
    DEMO_LLM=gemini|mock          optional, default gemini
    DEMO_LEX_BOT_ID=...           Lex bot uploaded to on approve (default BOT_ID)
    DEMO_LEX_LOAD=simulated|real  optional, default simulated
    DEMO_LEX_BUILD=false|true     optional, default false (start a Lex build after load)

    pip install slack_bolt
    python demo/slack_intents_demo.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback

# Make the repo root importable when run as `python demo/slack_intents_demo.py`.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agents import orchestrate, review, triggers
from agents.connectors import MockTicketConnector
from agents.llm import GeminiLLMClient, MockLLMClient
from agents.llm import MistralLLMClient
from agents.review import (
    ACTION_ID_APPROVE,
    ACTION_ID_DISCARD,
    SlackReviewSurface,
    build_card,
)
from agents.store import InMemoryProposalStore
from agents.synthesize import write_intents_csv


def _load_dotenv() -> None:
    """Minimal .env loader (no python-dotenv dependency). Existing env wins."""
    path = os.path.join(REPO_ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# Canned Agent responses so DEMO_LLM=mock works offline. 4 tickets -> 4 extracts;
# the two Smartsheet tickets share "access smartsheet" -> 3 clusters -> 3 labels.
_MOCK_EXTRACT = [
    json.dumps({"request": "Reset ADAM privileged password", "resolution": "ADAM Self Service reset",
                "category": "Password & Auth", "phrasings": ["reset my ADAM password", "ADAM self service link errors"]}),
    json.dumps({"request": "GlobalProtect VPN disconnecting", "resolution": "Update client, switch gateway",
                "category": "VPN & Network", "phrasings": ["my vpn keeps dropping", "globalprotect disconnects"]}),
    json.dumps({"request": "Get access to Smartsheet", "resolution": "License via SSO",
                "category": "Software & Access", "phrasings": ["how do I get Smartsheet access", "smartsheet SSO no application found"]}),
    json.dumps({"request": "Get access to a Smartsheet license for planning", "resolution": "Provision license",
                "category": "Software & Access", "phrasings": ["I need a Smartsheet license", "smartsheet for project plans"]}),
]
_MOCK_LABEL = [
    json.dumps({"intent_name": "AdamPasswordReset", "category": "Password & Auth",
                "response": "Reset your privileged ADAM account via the ADAM Self Service portal.", "uses_lambda": False}),
    json.dumps({"intent_name": "VpnDisconnecting", "category": "VPN & Network",
                "response": "Update GlobalProtect to the latest build and switch to the regional gateway.", "uses_lambda": False}),
    json.dumps({"intent_name": "SmartsheetAccess", "category": "Software & Access",
                "response": "Raise a Smartsheet license request; access is granted via SSO.", "uses_lambda": True}),
]


def _build_llm():
    mode = os.environ.get("DEMO_LLM", "gemini").strip().lower()
    if mode == "mistral":
        if not os.environ.get("MIA_KEY"):
            print("  [!] MIA_KEY not set -> falling back to mock LLM.")
            return MockLLMClient(responses=_MOCK_EXTRACT + _MOCK_LABEL), "mock"
        return MistralLLMClient(), "mistral (real)"
    if mode == "mock" or not os.environ.get("GEMINI_API_KEY"):
        if mode != "mock":
            print("  [!] GEMINI_API_KEY not set -> falling back to mock LLM.")
        return MockLLMClient(responses=_MOCK_EXTRACT + _MOCK_LABEL), "mock"
    return GeminiLLMClient(), "gemini (real)"


def _simulated_loader(intents, load_options):
    """Stand-in for the Lex load: log what WOULD upload, touch nothing."""
    print(f"  [SIMULATED] would upload {len(intents)} intent(s) to Lex bot "
          f"{load_options.get('bot_id')!r} in {load_options.get('region')!r}:")
    for intent in intents:
        print(f"          - {intent.get('intent_name')} "
              f"({'action/Lambda' if intent.get('uses_lambda') else 'info'}), "
              f"{len(intent.get('utterances', []))} utterances")
    return {
        "ok": True, "bot_id": load_options.get("bot_id"), "intent_count": len(intents),
        "summary": {"created": len(intents), "updated": 0, "failures": [], "build": "(simulated)"},
    }


def _real_lex_loader(intents, load_options):
    """REAL upload: run the Lex-intent loader in-process against the live bot."""
    import lambda_o3_lex_intent_loader as lex_loader

    print(f"  [REAL] uploading {len(intents)} intent(s) to Lex bot "
          f"{load_options.get('bot_id')!r} …")
    return lex_loader.lambda_handler({
        "intents": intents,
        "bot_id": load_options["bot_id"],
        "region": load_options.get("region"),
        "locale_id": load_options.get("locale_id", "en_US"),
        "build": bool(load_options.get("build")),
        "wait_build": False,
    })


def main() -> None:
    _load_dotenv()

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    app_token = os.environ.get("SLACK_APP_TOKEN", "")
    if not bot_token.startswith("xoxb-"):
        print("SLACK_BOT_TOKEN (xoxb-...) is missing. Add it to the repo-root .env.")
        sys.exit(1)
    if not app_token.startswith("xapp-"):
        print("SLACK_APP_TOKEN (xapp-...) is missing. The approval flow needs Socket Mode.\n"
              "Create a demo Slack app, enable Socket Mode, and add its app-level token.\n"
              "See demo/README.md.")
        sys.exit(1)
    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError:
        print("Socket Mode needs slack_bolt.  Run:  pip install slack_bolt")
        sys.exit(1)

    llm, llm_label = _build_llm()
    lex_bot_id = os.environ.get("DEMO_LEX_BOT_ID") or os.environ.get("BOT_ID") or "DEMOBOT"
    region = os.environ.get("AWS_REGION", "ap-southeast-2")
    load_mode = os.environ.get("DEMO_LEX_LOAD", "simulated").strip().lower()
    build = os.environ.get("DEMO_LEX_BUILD", "false").strip().lower() == "true"
    loader_fn = _real_lex_loader if load_mode == "real" else _simulated_loader
    load_options = {"bot_id": lex_bot_id, "region": region, "locale_id": "en_US", "build": build}

    # Shared state: the slash command stores a proposal; the Approve click reads it.
    store = InMemoryProposalStore()
    app = App(token=bot_token)

    def _surface(channel):
        def post_message(channel_id, text, blocks=None, **kw):
            return app.client.chat_postMessage(channel=channel_id, text=text, blocks=blocks, **kw)["ts"]
        return SlackReviewSurface(post_message=post_message, channel=channel)

    @app.command("/generate-intents")
    def handle_generate(ack, command, logger):
        ack()
        channel_id = command["channel_id"]
        surface = _surface(channel_id)
        surface.post_summary(f":gear: Generating intents from resolved tickets… (LLM: {llm_label})")
        try:
            opts = triggers.parse_command_args((command.get("text") or "").strip())
            proposal = orchestrate.generate_proposal(
                MockTicketConnector(), llm, threshold=float(opts.get("threshold", 0.3)),
                limit=opts.get("limit"), store=store,
            )
            # No intents usually means the LLM extraction failed/returned empty for
            # every ticket (often a transient rate-limit). Say so instead of posting
            # an empty card, and point the user at a retry.
            if not proposal.get("intents"):
                stats = proposal.get("stats", {})
                surface.post_summary(
                    f":warning: No intents produced from {stats.get('tickets', 0)} ticket(s) "
                    f"— {stats.get('failed', 0)} failed, {stats.get('skipped', 0)} skipped "
                    f"in Agent 1 (LLM: {llm_label}). Often a transient LLM error — "
                    "run `/generate-intents` again."
                )
                return
            # Post the review card — Approve / Edit / Discard. Nothing loaded yet.
            logger.info(f"[generate] proposal_id={proposal.get('proposal_id')!r}, stored. store now has: {list(store._items.keys())}")
            surface.post(build_card(proposal))
        except Exception as error:  # noqa: BLE001
            logger.error("generate failed: %s", error)
            traceback.print_exc()
            surface.post_summary(f":x: Generation failed: `{error}`")

    def _proposal_id(body) -> str:
        value = (body.get("actions") or [{}])[0].get("value") or "{}"
        try:
            return json.loads(value).get("proposal_id", "")
        except ValueError:
            return ""

    @app.action(ACTION_ID_APPROVE)
    def handle_approve(ack, body, logger):
        ack()
        channel_id = (body.get("channel") or {}).get("id")
        surface = _surface(channel_id)
        proposal_id = _proposal_id(body)
        surface.post_summary(f":outbox_tray: Approved — uploading to Lex ({load_mode})…")
        try:
            result = triggers.handle_review_action(
                review.APPROVE, proposal_id, store,
                load_options=load_options, loader_fn=loader_fn,
            )
            surface.post_summary(result["summary"])
        except Exception as error:  # noqa: BLE001
            logger.error("approve failed: %s", error)
            traceback.print_exc()
            surface.post_summary(f":x: Upload failed: `{error}`")

    @app.action(ACTION_ID_DISCARD)
    def handle_discard(ack, body, logger):
        ack()
        channel_id = (body.get("channel") or {}).get("id")
        surface = _surface(channel_id)
        proposal_id = _proposal_id(body)
        logger.info(f"[discard] proposal_id={proposal_id!r}, store has: {list(store._items.keys())}")
        try:
            # Grab the intents BEFORE discarding (discard only flips status, keeps data).
            proposal = store.get(proposal_id)
            triggers.handle_review_action(review.DISCARD, proposal_id, store)
            intents = (proposal or {}).get("intents") or []
            if not intents:
                surface.post_summary(":wastebasket: Denied — nothing written to Lex. "
                                     "(No intents to export.)")
                return
            # Denied: don't touch Lex. Hand the user a CSV of the proposed intents
            # instead, so they can review/edit and load it later if they change their mind.
            csv_path = os.path.join(tempfile.gettempdir(), f"proposed_intents_{proposal_id}.csv")
            write_intents_csv(intents, csv_path)
            surface.post_summary(f":wastebasket: Denied — not loaded to Lex. "
                                 f"Exporting the {len(intents)} proposed intent(s) as a CSV…")
            app.client.files_upload_v2(
                channel=channel_id,
                file=csv_path,
                filename=f"proposed_intents_{proposal_id}.csv",
                title="Proposed intents (denied — not loaded to Lex)",
                initial_comment=(
                    f":page_facing_up: {len(intents)} proposed intent(s), not loaded. "
                    "Review/edit, then load when ready:\n"
                    "`python scripts/load_intents_to_lex.py --file proposed_intents_<id>.csv --apply`"
                ),
            )
        except Exception as error:  # noqa: BLE001
            logger.error("discard failed: %s", error)
            traceback.print_exc()
            surface.post_summary(f":x: Deny/export failed: `{error}`")

    print("=" * 70)
    print("  Slack intents demo LIVE (Socket Mode) — approval flow")
    print(f"  LLM: {llm_label}   |   Lex bot: {lex_bot_id}   |   upload: {load_mode.upper()}"
          f"{' + build' if build else ''}")
    if load_mode == "real":
        print(f"  *** REAL uploads to Lex bot {lex_bot_id} are ENABLED. Approving WILL modify it. ***")
    print("  In Slack:  /generate-intents   then click Approve.   Ctrl+C to stop.")
    print("=" * 70)
    SocketModeHandler(app, app_token).start()


if __name__ == "__main__":
    main()
