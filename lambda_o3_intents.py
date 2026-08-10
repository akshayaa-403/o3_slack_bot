"""Lambda: the tickets -> Lex-intents pipeline, driven from Slack (production).

This is the production home of what demo/slack_intents_demo.py ran locally over
Socket Mode. The Slack handler (lambda_o3_slack_handler) routes three things here
by async-invoking this function; each posts its own message(s) back to Slack:

    action=generate  <- /generate-intents slash command
        Run Agent 1 (ingest) + Agent 2 (cluster + label), save the proposal to
        DynamoDB, and post a review card with Approve / Deny buttons. Nothing is
        written to Lex yet.

    action=approve   <- the "Approve & load" button (agent2_intents_approve)
        Load the saved proposal's intents into Amazon Lex, then post a summary.

    action=discard   <- the "Deny" button (agent2_intents_discard)
        Mark the proposal discarded; Lex is untouched. Post a summary.

Because the slash command and the button click are separate invocations, the
proposal is persisted in DynamoDB (DynamoProposalStore), not in memory.

Event shape (sent by the handler):
    {"action": "generate", "channel": "C..|D..", "user": "U..", "text": "<flags>"}
    {"action": "approve",  "channel": "...", "user": "...", "proposal_id": "..."}
    {"action": "discard",  "channel": "...", "user": "...", "proposal_id": "..."}

Env:
    SLACK_BOT_TOKEN     required (to post back to Slack)
    INTENTS_TABLE       required (DynamoDB proposal store; PK proposal_id, TTL ttl)
    MIA_KEY             Mistral key for Agent 2 labels (falls back to mock if unset)
    MISTRAL_MODEL       optional (default mistral-large-latest)
    LEX_BOT_ID / BOT_ID Lex bot loaded on approve
    AWS_REGION          Lex/DynamoDB region (Lambda sets this automatically)
    INTENTS_LEX_LOAD    'real' (default) or 'simulated' (log only, touch nothing)
    INTENTS_LEX_BUILD   'false' (default) or 'true' (start a Lex build after load)
    INTENTS_SOURCE      'mock' (default) — ticket source connector
"""

from __future__ import annotations

import json
import os
import contextvars
import urllib.parse
import urllib.request

from agents import orchestrate, review, triggers
from agents.connectors import MockTicketConnector
from agents.llm import MistralLLMClient, MockLLMClient
from agents.review import build_card, render_slack_blocks
from agents.store import DynamoProposalStore
from agents.synthesize import write_intents_csv

AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
INTENTS_TABLE = os.environ.get("INTENTS_TABLE", "")
LEX_BOT_ID = os.environ.get("LEX_BOT_ID") or os.environ.get("BOT_ID") or ""
LEX_LOCALE_ID = os.environ.get("LOCALE_ID", "en_US")
INTENTS_LEX_LOAD = os.environ.get("INTENTS_LEX_LOAD", "real").strip().lower()
INTENTS_LEX_BUILD = os.environ.get("INTENTS_LEX_BUILD", "false").strip().lower() == "true"
INTENTS_SOURCE = os.environ.get("INTENTS_SOURCE", "mock").strip().lower()


# --- Per-tenant Slack token --------------------------------------------------
# Accepts `slack_bot_token` on the invoke payload so this can post as the right
# workspace once the caller supplies one.
#
# The handler does not send one yet, and that is deliberate rather than an
# oversight: this pipeline is an internal admin tool (/generate-intents and its
# review-card buttons), not per-customer functionality, so it runs in InnovyQ's
# own workspace on the shared token. Wiring tenant resolution into the handler
# purely for this would add a DynamoDB read and a KMS decrypt to the 3-second
# Slack ack path for no current benefit. The hook exists so that when intents
# genuinely go per-customer, only the caller needs changing.
_slack_token_ctx = contextvars.ContextVar("slack_token", default=None)


def current_slack_token():
    return _slack_token_ctx.get() or SLACK_BOT_TOKEN


def log_json(data):
    print(json.dumps(data, default=str))


def slack_post(channel, text, blocks=None):
    """Post a message to Slack via chat.postMessage (stdlib only)."""
    if not current_slack_token():
        raise ValueError("Missing SLACK_BOT_TOKEN")
    payload = {"channel": channel, "text": text}
    if blocks:
        payload["blocks"] = blocks
    request = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {current_slack_token()}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise ValueError(f"chat.postMessage failed: {result.get('error')}")
    return result.get("ts")


def slack_upload_csv(channel, filename, content_bytes, title, comment):
    """Upload a CSV to a Slack channel via the external-upload flow (stdlib only).
    Needs the files:write scope on the app."""
    query = urllib.parse.urlencode({"filename": filename, "length": len(content_bytes)})
    req1 = urllib.request.Request(
        "https://slack.com/api/files.getUploadURLExternal?" + query,
        headers={"Authorization": f"Bearer {current_slack_token()}"}, method="GET")
    with urllib.request.urlopen(req1, timeout=10) as resp:
        d1 = json.loads(resp.read().decode("utf-8"))
    if not d1.get("ok"):
        raise ValueError(f"getUploadURLExternal failed: {d1.get('error')}")
    upload_url, file_id = d1["upload_url"], d1["file_id"]

    boundary = "----ivycsvboundary9f2b1c"
    pre = (f"--{boundary}\r\n"
           f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
           "Content-Type: text/csv\r\n\r\n").encode("utf-8")
    body = pre + content_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req2 = urllib.request.Request(
        upload_url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
    urllib.request.urlopen(req2, timeout=20).read()

    payload = {"files": [{"id": file_id, "title": title}], "channel_id": channel}
    if comment:
        payload["initial_comment"] = comment
    req3 = urllib.request.Request(
        "https://slack.com/api/files.completeUploadExternal",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {current_slack_token()}", "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req3, timeout=10) as resp:
        d3 = json.loads(resp.read().decode("utf-8"))
    if not d3.get("ok"):
        raise ValueError(f"completeUploadExternal failed: {d3.get('error')}")
    return True


def _build_llm():
    """Mistral when MIA_KEY is set; a mock otherwise so a run never hard-fails."""
    if os.environ.get("MIA_KEY"):
        return MistralLLMClient(), "mistral"
    log_json({"level": "WARN", "message": "intents_no_mia_key", "detail": "MIA_KEY unset -> mock LLM"})
    # Canned Agent-1 extracts (4 tickets) THEN Agent-2 labels (3 clusters) so the
    # mock path produces a real proposal, not an empty one.
    mock_extracts = [
        json.dumps({"request": "Reset ADAM privileged password", "resolution": "ADAM Self Service reset",
                    "category": "Password & Auth", "phrasings": ["reset my ADAM password", "ADAM self service link errors"]}),
        json.dumps({"request": "GlobalProtect VPN disconnecting", "resolution": "Update client, switch gateway",
                    "category": "VPN & Network", "phrasings": ["my vpn keeps dropping", "globalprotect disconnects"]}),
        json.dumps({"request": "Get access to Smartsheet", "resolution": "License via SSO",
                    "category": "Software & Access", "phrasings": ["how do I get Smartsheet access", "smartsheet SSO no application found"]}),
        json.dumps({"request": "Get a Smartsheet license for planning", "resolution": "Provision license",
                    "category": "Software & Access", "phrasings": ["I need a Smartsheet license", "smartsheet for project plans"]}),
    ]
    mock_labels = [
        json.dumps({"intent_name": "AdamPasswordReset", "category": "Password & Auth",
                    "response": "Reset your ADAM account via the ADAM Self Service portal.", "uses_lambda": False}),
        json.dumps({"intent_name": "VpnDisconnecting", "category": "VPN & Network",
                    "response": "Update GlobalProtect and switch to the regional gateway.", "uses_lambda": False}),
        json.dumps({"intent_name": "SmartsheetAccess", "category": "Software & Access",
                    "response": "Raise a Smartsheet license request; access is granted via SSO.", "uses_lambda": True}),
    ]
    return MockLLMClient(responses=mock_extracts + mock_labels), "mock"


def _build_connector():
    # Only mock is wired for now (Jira issue creation is blocked on this tenant).
    # A JiraConnector drops in here later without touching the rest.
    return MockTicketConnector()


def _store():
    if not INTENTS_TABLE:
        raise ValueError("INTENTS_TABLE is not set")
    return DynamoProposalStore(INTENTS_TABLE, region=AWS_REGION)


def _load_options():
    return {"bot_id": LEX_BOT_ID, "region": AWS_REGION,
            "locale_id": LEX_LOCALE_ID, "build": INTENTS_LEX_BUILD}


def _simulated_loader(intents, load_options):
    log_json({"level": "INFO", "message": "intents_simulated_load",
              "bot_id": load_options.get("bot_id"), "intent_count": len(intents)})
    return {"ok": True, "bot_id": load_options.get("bot_id"), "intent_count": len(intents),
            "summary": {"created": len(intents), "updated": 0, "failures": [], "build": "(simulated)"}}


def _real_lex_loader(intents, load_options):
    import lambda_o3_lex_intent_loader as lex_loader

    return lex_loader.lambda_handler({
        "intents": intents,
        "bot_id": load_options["bot_id"],
        "region": load_options.get("region"),
        "locale_id": load_options.get("locale_id", "en_US"),
        "build": bool(load_options.get("build")),
        "wait_build": False,
    })


def _loader_fn():
    return _real_lex_loader if INTENTS_LEX_LOAD == "real" else _simulated_loader


def handle_generate(channel, text):
    llm, llm_label = _build_llm()
    slack_post(channel, f":gear: Generating intents from resolved tickets… (LLM: {llm_label})")
    opts = triggers.parse_command_args((text or "").strip())
    proposal = orchestrate.generate_proposal(
        _build_connector(), llm, threshold=float(opts.get("threshold", 0.3)),
        limit=opts.get("limit"), store=_store(),
    )
    if not proposal.get("intents"):
        stats = proposal.get("stats", {})
        slack_post(channel, f":warning: No intents produced from {stats.get('tickets', 0)} ticket(s) "
                            f"— {stats.get('failed', 0)} failed, {stats.get('skipped', 0)} skipped. "
                            "Often a transient LLM error — run `/generate-intents` again.")
        return {"ok": True, "intents": 0, "proposal_id": proposal.get("proposal_id")}
    card = build_card(proposal)
    slack_post(channel, card["title"], blocks=render_slack_blocks(card))
    log_json({"level": "INFO", "message": "intents_proposal_posted",
              "proposal_id": proposal["proposal_id"], "intent_count": len(proposal["intents"])})
    return {"ok": True, "intents": len(proposal["intents"]), "proposal_id": proposal["proposal_id"]}


def handle_approve(channel, proposal_id):
    slack_post(channel, f":outbox_tray: Approved — uploading to Lex ({INTENTS_LEX_LOAD})…")
    result = triggers.handle_review_action(
        review.APPROVE, proposal_id, _store(),
        load_options=_load_options(), loader_fn=_loader_fn(),
    )
    slack_post(channel, result["summary"])
    return {"ok": True, "proposal_id": proposal_id}


def handle_discard(channel, proposal_id):
    store = _store()
    proposal = store.get(proposal_id)
    triggers.handle_review_action(review.DISCARD, proposal_id, store)
    intents = (proposal or {}).get("intents") or []
    if not intents:
        slack_post(channel, ":wastebasket: Denied — nothing written to Lex.")
        return {"ok": True, "proposal_id": proposal_id, "intents": 0}
    # Export the proposed intents as a CSV the user can review/edit and load later.
    try:
        path = os.path.join("/tmp", f"proposed_intents_{proposal_id}.csv")
        write_intents_csv(intents, path)
        with open(path, "rb") as handle:
            content = handle.read()
        slack_upload_csv(
            channel,
            f"proposed_intents_{proposal_id}.csv",
            content,
            "Proposed intents (denied — not loaded to Lex)",
            f":wastebasket: Denied — not loaded to Lex. Here are the {len(intents)} proposed "
            "intent(s) as a CSV — review/edit, then load later with "
            "`python scripts/load_intents_to_lex.py --file <this-file>.csv --apply`.",
        )
        return {"ok": True, "proposal_id": proposal_id, "intents": len(intents), "csv": True}
    except Exception as error:  # noqa: BLE001 - fall back to a text summary if upload fails
        log_json({"level": "WARN", "message": "intents_csv_upload_failed", "error": str(error)})
        names = ", ".join(i.get("intent_name", "?") for i in intents)
        slack_post(channel, f":wastebasket: Denied — not loaded to Lex. The {len(intents)} proposed "
                            f"intent(s) were: {names}. (CSV export needs the `files:write` scope on "
                            "the Slack app.) Run `/generate-intents` again to redo.")
        return {"ok": True, "proposal_id": proposal_id, "intents": len(intents), "csv": False}


def lambda_handler(event, context=None):
    action = (event or {}).get("action")
    channel = (event or {}).get("channel")
    proposal_id = (event or {}).get("proposal_id")
    # Set unconditionally, including to None: Lambda reuses warm containers,
    # so a conditional set would let this run inherit the previous caller's
    # token. None simply falls back to the env var.
    _slack_token_ctx.set((event or {}).get("slack_bot_token"))
    try:
        if not channel:
            raise ValueError("missing channel")
        if action == "generate":
            return handle_generate(channel, event.get("text"))
        if action == "approve":
            return handle_approve(channel, proposal_id)
        if action == "discard":
            return handle_discard(channel, proposal_id)
        raise ValueError(f"unknown action: {action!r}")
    except Exception as error:  # noqa: BLE001 - report back to Slack, don't crash silently
        log_json({"level": "ERROR", "message": "intents_action_failed",
                  "action": action, "proposal_id": proposal_id, "error": str(error)})
        if channel:
            try:
                slack_post(channel, f":x: Intents `{action}` failed: `{error}`")
            except Exception:  # noqa: BLE001
                pass
        return {"ok": False, "action": action, "error": str(error)}
