"""Orchestrator — the platform-agnostic pipeline every trigger calls.

Primary (automated) flow, matching the agreed design:

    command -> Agent 1 (ingest tickets) -> Agent 2 (cluster + label with the LLM)
            -> hand off to the loader Lambda -> Lex -> summary back to the user

    run_and_load:      the whole automated path in one call, returns a summary.
    generate_proposal: just the tickets->intents half (used by run_and_load, and
                       by the optional human-review path).
    approve/discard:   the optional review path (Slack card) — same loader seam.

The connector (source), llm, store, and loader are all injected. Swapping
Slack->Teams, Jira->ServiceNow, or Gemini->another model touches the edges
(triggers.py / connectors.py / review.py / llm.py), never this file. The loader
is a seam too: in production it invokes the Lex-loader Lambda; tests pass a fake.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from typing import Callable, Dict, Optional

from agents.ingest import run_ingestion
from agents.llm import LLMClient
from agents.store import ProposalStore
from agents.synthesize import run_synthesis

# Proposal status lifecycle.
PENDING = "pending"
LOADED = "loaded"
DISCARDED = "discarded"

# Env var naming the deployed Lex-loader Lambda (lambda_o3_lex_intent_loader).
LOADER_LAMBDA_ENV = "LEX_INTENT_LOADER_FUNCTION"


def _new_proposal_id() -> str:
    return uuid.uuid4().hex[:12]


# --- loader seam -------------------------------------------------------------
def invoke_loader_lambda(intents, load_options: Dict) -> Dict:
    """Default production loader: async-invoke the Lex-loader Lambda.

    The worker (running Agents 1+2) hands the intents off to a separate Lambda
    so loading + the Lex build don't block the pipeline. Requires boto3 and the
    LEX_INTENT_LOADER_FUNCTION env var; tests inject a fake instead of calling this.
    """
    import boto3  # local import: only needed in the deployed worker

    function_name = load_options.get("loader_function") or os.environ.get(LOADER_LAMBDA_ENV)
    if not function_name:
        raise ValueError(f"loader function not configured ({LOADER_LAMBDA_ENV})")

    payload = {"intents": intents, **{k: v for k, v in load_options.items()
                                      if k != "loader_function"}}
    client = boto3.client("lambda", region_name=load_options.get("region"))
    response = client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    body = response.get("Payload")
    return json.loads(body.read().decode("utf-8")) if body else {"ok": True}


def load_intents(
    intents,
    *,
    load_options: Optional[Dict] = None,
    loader_fn: Optional[Callable[..., Dict]] = None,
) -> Dict:
    """Hand intents to the loader (Lambda in prod, fake in tests). Needs bot_id."""
    load_options = dict(load_options or {})
    if not load_options.get("bot_id"):
        raise ValueError("loading requires load_options['bot_id']")
    loader_fn = loader_fn or invoke_loader_lambda
    return loader_fn(intents, load_options)


# --- Agent 1 + Agent 2 -------------------------------------------------------
def generate_proposal(
    connector,
    llm: LLMClient,
    *,
    threshold: float = 0.3,
    limit: Optional[int] = None,
    output_dir: Optional[str] = None,
    proposal_id: Optional[str] = None,
    store: Optional[ProposalStore] = None,
) -> Dict:
    """Run Agent 1 + Agent 2 into a PENDING proposal (does NOT touch Lex).

    Writes a review xlsx only when `output_dir` is given (optional audit / power-
    user artifact); the automated path loads from the in-memory intents instead,
    so it needs no spreadsheet library at runtime.
    """
    proposal_id = proposal_id or _new_proposal_id()
    xlsx_path = os.path.join(output_dir, f"intents_{proposal_id}.xlsx") if output_dir else None

    corpus = run_ingestion(connector, llm, limit=limit)
    synth = run_synthesis(corpus, llm, threshold=threshold, output_xlsx=xlsx_path)

    proposal = {
        "proposal_id": proposal_id,
        "source": corpus.get("source", "unknown"),
        "status": PENDING,
        "stats": {
            "tickets": corpus.get("count", 0) + corpus.get("skipped", 0) + len(corpus.get("failed", [])),
            "interactions": corpus.get("count", 0),
            "skipped": corpus.get("skipped", 0),
            "failed": len(corpus.get("failed", [])),
            "clusters": synth.get("cluster_count", 0),
            "intents": synth.get("intent_count", 0),
        },
        "intents": synth.get("intents", []),
        "xlsx_path": xlsx_path,
    }
    if store is not None:
        store.put(proposal)
    return proposal


# --- automated end-to-end path ----------------------------------------------
def run_and_load(
    connector,
    llm: LLMClient,
    *,
    load_options: Dict,
    threshold: float = 0.3,
    limit: Optional[int] = None,
    loader_fn: Optional[Callable[..., Dict]] = None,
    store: Optional[ProposalStore] = None,
    proposal_id: Optional[str] = None,
) -> Dict:
    """The full automated pipeline: tickets -> intents -> Lex, in one call.

    Returns {proposal, load, summary} where `summary` is the human-facing line
    the trigger sends back to whoever ran the command.
    """
    proposal = generate_proposal(
        connector, llm, threshold=threshold, limit=limit,
        proposal_id=proposal_id, store=store,
    )
    load_result = load_intents(
        proposal["intents"], load_options=load_options, loader_fn=loader_fn
    )
    if store is not None:
        store.set_status(proposal["proposal_id"], LOADED)
    proposal["status"] = LOADED
    return {
        "proposal": proposal,
        "load": load_result,
        "summary": summarize(proposal, load_result),
    }


def summarize(proposal: Dict, load_result: Dict) -> str:
    """One-line, human-facing summary of a completed run."""
    stats = proposal.get("stats", {})
    inner = (load_result or {}).get("summary") or {}
    parts = [
        f"Generated {stats.get('intents', 0)} intent(s) from "
        f"{stats.get('tickets', 0)} {proposal.get('source', 'ticket')} ticket(s)."
    ]
    if not (load_result or {}).get("ok", True):
        parts.append(f"Load FAILED: {load_result.get('error', 'unknown error')}")
        return " ".join(parts)
    if inner:
        parts.append(
            f"Loaded to Lex: {inner.get('created', 0)} created, "
            f"{inner.get('updated', 0)} updated."
        )
        failures = inner.get("failures") or []
        if failures:
            parts.append(f"{len(failures)} intent(s) failed.")
        if inner.get("build"):
            parts.append(f"Build: {inner['build']}.")
    else:
        parts.append("Loaded to Lex.")
    return " ".join(parts)


# --- optional human-review path (Slack card) --------------------------------
def approve_proposal(
    proposal_id: str,
    store: ProposalStore,
    *,
    load_options: Optional[Dict] = None,
    loader_fn: Optional[Callable[..., Dict]] = None,
) -> Dict:
    """Load an approved proposal's intents into Lex (via the same loader seam)."""
    proposal = store.get(proposal_id)
    if proposal is None:
        raise ValueError(f"unknown proposal_id: {proposal_id}")

    load_result = load_intents(
        proposal["intents"], load_options=load_options, loader_fn=loader_fn
    )
    store.set_status(proposal_id, LOADED)
    return {"proposal_id": proposal_id, "status": LOADED, "load": load_result,
            "summary": summarize(proposal, load_result)}


def discard_proposal(proposal_id: str, store: ProposalStore) -> Dict:
    """Mark a proposal discarded; nothing is loaded."""
    if store.get(proposal_id) is None:
        raise ValueError(f"unknown proposal_id: {proposal_id}")
    store.set_status(proposal_id, DISCARDED)
    return {"proposal_id": proposal_id, "status": DISCARDED}
