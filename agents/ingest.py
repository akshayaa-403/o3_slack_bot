"""Agent 1 — ingest & normalize.

Deterministic orchestration: pull tickets from a connector (API work), then use
the LLM only to distill each messy ticket thread into a clean interaction:

    {request, resolution, category_hint, phrasings, source_id, source}

The output "interaction corpus" is what Agent 2 clusters into intents. Running
it is pure fan-out over tickets; the LLM is one call per ticket. Per-ticket
failures are collected, not fatal, so one bad ticket never sinks the run.

    from agents.ingest import run_ingestion
    from agents.connectors import MockTicketConnector
    from agents.llm import MockLLMClient
    corpus = run_ingestion(MockTicketConnector(), llm)
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from agents.llm import LLMClient, LLMError

EXTRACTION_SYSTEM = (
    "You are an IT support analyst. You read a single support ticket and distill "
    "it into a reusable knowledge item. Be faithful to the ticket; never invent "
    "resolutions that aren't supported by it."
)

EXTRACTION_INSTRUCTIONS = """From the ticket below, return ONLY a JSON object:
{
  "request": "one clear sentence stating what the user needed (their problem/ask)",
  "resolution": "the answer or steps that resolved it, or \\"\\" if none is present",
  "category": "a short category label for this request",
  "phrasings": ["the actual ways a user might ask this, drawn from the ticket text"]
}
Rules:
- "phrasings" are real user-style questions/requests (2-6 of them), not restatements of the resolution.
- If the ticket has no usable request, set "request" to "".
- Output JSON only, no prose.

TICKET:
"""


def build_extraction_prompt(ticket: Dict) -> str:
    parts = [
        f"id: {ticket.get('id', '')}",
        f"category: {ticket.get('category', '')}",
        f"summary: {ticket.get('summary', '')}",
        f"description: {ticket.get('description', '')}",
    ]
    comments = ticket.get("comments") or []
    if comments:
        parts.append("comments:")
        parts.extend(f"  - {c}" for c in comments)
    if ticket.get("resolution"):
        parts.append(f"resolution field: {ticket['resolution']}")
    return EXTRACTION_INSTRUCTIONS + "\n".join(parts)


def _clean_phrasings(value) -> List[str]:
    if not isinstance(value, list):
        return []
    seen = set()
    out = []
    for item in value:
        text = str(item).strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def extract_interaction(ticket: Dict, llm: LLMClient) -> Optional[Dict]:
    """Distill one ticket into an interaction, or None if unusable."""
    data = llm.complete_json(
        build_extraction_prompt(ticket), system=EXTRACTION_SYSTEM, temperature=0.0
    )
    if not isinstance(data, dict):
        raise LLMError("extraction did not return a JSON object")

    request = str(data.get("request") or "").strip()
    if not request:
        return None

    phrasings = _clean_phrasings(data.get("phrasings"))
    # Always keep the ticket summary as a phrasing — it's a real user wording.
    summary = (ticket.get("summary") or "").strip()
    if summary and summary.lower() not in {p.lower() for p in phrasings}:
        phrasings.insert(0, summary)

    return {
        "source_id": ticket.get("id", ""),
        "request": request,
        "resolution": str(data.get("resolution") or "").strip(),
        "category_hint": str(data.get("category") or ticket.get("category") or "").strip(),
        "phrasings": phrasings,
    }


def run_ingestion(
    connector,
    llm: LLMClient,
    *,
    limit: Optional[int] = None,
) -> Dict:
    """Fetch tickets and distill them into an interaction corpus.

    Returns {source, count, skipped, failed, interactions}. Never raises for a
    single bad ticket — those land in `failed`.
    """
    tickets = connector.fetch_tickets(limit=limit)
    interactions: List[Dict] = []
    skipped = 0
    failed: List[Dict] = []

    for ticket in tickets:
        try:
            interaction = extract_interaction(ticket, llm)
        except (LLMError, Exception) as error:  # noqa: BLE001 - best effort per ticket
            failed.append({"source_id": ticket.get("id", ""), "error": str(error)})
            continue
        if interaction is None:
            skipped += 1
            continue
        interaction["source"] = getattr(connector, "name", "unknown")
        interactions.append(interaction)

    return {
        "source": getattr(connector, "name", "unknown"),
        "count": len(interactions),
        "skipped": skipped,
        "failed": failed,
        "interactions": interactions,
    }


# --- runnable demo (mock connector + scripted mock LLM, no network) ----------
def _demo() -> None:
    from agents.connectors import MockTicketConnector
    from agents.llm import MockLLMClient

    # One scripted extraction per sample ticket, in order.
    scripted = [
        json.dumps({
            "request": "Reset my privileged ADAM account password",
            "resolution": "Use ADAM Self Service > Privileged Account > Reset; clear cookies and retry on the corporate network if it errors.",
            "category": "Password & Auth",
            "phrasings": ["how do I reset my ADAM password?", "ADAM self service reset link errors"],
        }),
        json.dumps({
            "request": "GlobalProtect VPN keeps disconnecting",
            "resolution": "Update GlobalProtect to the latest build and switch to the regional gateway.",
            "category": "VPN & Network",
            "phrasings": ["my VPN keeps dropping", "GlobalProtect disconnects every few minutes"],
        }),
        json.dumps({
            "request": "Get access to Smartsheet",
            "resolution": "Raise a Smartsheet license request; access is granted via SSO.",
            "category": "Software & Access",
            "phrasings": ["how do I get access to Smartsheet?", "Smartsheet SSO no application found"],
        }),
        json.dumps({
            "request": "Get a Smartsheet license for planning",
            "resolution": "Smartsheet license provisioned.",
            "category": "Software & Access",
            "phrasings": ["I need a Smartsheet license", "requesting Smartsheet for project plans"],
        }),
    ]
    llm = MockLLMClient(responses=scripted)
    corpus = run_ingestion(MockTicketConnector(), llm)

    print("=" * 70)
    print("  Agent 1 — ingestion demo (mock connector + mock LLM, no network)")
    print("=" * 70)
    print(f"source={corpus['source']}  extracted={corpus['count']}  "
          f"skipped={corpus['skipped']}  failed={len(corpus['failed'])}")
    for item in corpus["interactions"]:
        print(f"\n[{item['source_id']}] ({item['category_hint']})")
        print(f"  request:   {item['request']}")
        print(f"  resolution:{item['resolution'][:80]}")
        print(f"  phrasings: {item['phrasings']}")
    print("=" * 70)


if __name__ == "__main__":
    _demo()
