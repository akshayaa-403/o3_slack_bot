"""Agent 2 — synthesize intents & load.

Deterministic orchestration again: cluster the interaction corpus by similarity
(no LLM), then use the LLM only to *label* each cluster into an intent — a name,
a merged response, and a static-vs-lambda call. Sample utterances come from the
REAL ticket phrasings (grounded, not invented), so the matchable part of each
intent never hallucinates.

Output is the exact spreadsheet the Lex loader already consumes, so:
  - review mode (default): write the xlsx; a human checks it, then runs
    load_lex_intents_from_excel.py on it.
  - auto-load mode: also invoke that loader programmatically.

    from agents.synthesize import run_synthesis
    result = run_synthesis(corpus, llm, output_xlsx="draft.xlsx")          # review
    result = run_synthesis(corpus, llm, output_xlsx="draft.xlsx",          # auto
                           auto_load=True, load_options={"bot_id": "..."})

Clustering here is a lightweight token-overlap (Jaccard) within each category —
good enough to group obvious duplicates. The similarity function is pluggable,
so real embeddings drop in later without touching the rest.
"""

from __future__ import annotations

import csv
import re
from typing import Callable, Dict, List, Optional

from agents.llm import LLMClient

STOPWORDS = {
    "the", "a", "an", "to", "my", "i", "is", "are", "for", "of", "in", "on",
    "and", "how", "do", "can", "get", "need", "please", "help", "with", "me",
    "am", "unable", "not", "cannot", "want", "would", "like",
}

MAX_UTTERANCES_PER_INTENT = 40

LABEL_SYSTEM = (
    "You are an IT support taxonomy expert. You turn a group of similar support "
    "requests into a single bot intent. Base the response only on the provided "
    "resolutions; never invent steps. Write the response as direct, present-tense "
    "instructions to the end user (second person, imperative) — e.g. 'To fix this, "
    "update X and switch Y.' Never phrase it as a past-tense note of what an agent "
    "did (not 'Updated the client…' but 'Update the client…')."
)

LABEL_INSTRUCTIONS = """Given the similar requests and their resolutions below, return ONLY JSON:
{
  "intent_name": "ShortPascalCaseName",
  "category": "a short category label",
  "response": "a clear answer written as direct instructions TO THE USER (second person, imperative present tense, e.g. 'To reset your password, go to...'); convert resolution notes into user-facing steps and never copy past-tense 'we did X' phrasing (\\"\\" if none)",
  "uses_lambda": true|false
}
Set "uses_lambda" true only if resolving this needs the bot to DO something
(provision access, create a ticket, run an action). Set it false for a purely
informational answer. Output JSON only.

REQUESTS:
{requests}

RESOLUTIONS:
{resolutions}
"""


# --- clustering (deterministic) ---------------------------------------------
def _tokens(text: str) -> set:
    return {
        word
        for word in re.findall(r"[a-z0-9]+", (text or "").lower())
        if word not in STOPWORDS and len(word) > 1
    }


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cluster_interactions(
    corpus,
    *,
    threshold: float = 0.3,
    similarity_fn: Optional[Callable[[set, set], float]] = None,
) -> List[Dict]:
    """Group interactions into clusters of the same underlying request.

    Greedy: within each category, an interaction joins the most similar existing
    cluster if similarity >= threshold, else starts a new one. Deterministic for
    a given input order. `similarity_fn` defaults to Jaccard over tokens; swap in
    an embedding cosine later.
    """
    similarity_fn = similarity_fn or jaccard
    interactions = corpus["interactions"] if isinstance(corpus, dict) else corpus

    by_category: Dict[str, List[Dict]] = {}
    for item in interactions:
        by_category.setdefault(item.get("category_hint") or "Uncategorized", []).append(item)

    clusters: List[Dict] = []
    for category, items in by_category.items():
        local: List[Dict] = []
        for item in items:
            tokens = _tokens(item["request"])
            best = None
            best_sim = 0.0
            for candidate in local:
                sim = similarity_fn(tokens, candidate["tokens"])
                if sim > best_sim:
                    best_sim, best = sim, candidate
            if best is not None and best_sim >= threshold:
                best["interactions"].append(item)
                best["tokens"] |= tokens
            else:
                local.append({"category": category, "tokens": set(tokens), "interactions": [item]})
        clusters.extend(local)
    return clusters


# --- labelling (LLM, with deterministic fallback) ---------------------------
def _collect_utterances(interactions: List[Dict]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in interactions:
        for phrasing in item.get("phrasings", []):
            text = re.sub(r"\s+", " ", str(phrasing)).strip()
            key = text.lower()
            if text and key not in seen:
                seen.add(key)
                out.append(text)
    return out[:MAX_UTTERANCES_PER_INTENT]


def _pascal(text: str) -> str:
    parts = re.findall(r"[0-9A-Za-z]+", text or "")
    return "".join(p[:1].upper() + p[1:] for p in parts)


def _fallback_name(category: str, requests: List[str]) -> str:
    counts: Dict[str, int] = {}
    for request in requests:
        for token in _tokens(request):
            counts[token] = counts.get(token, 0) + 1
    top = sorted(counts, key=lambda t: (-counts[t], t))[:2]
    name = _pascal(category) + _pascal(" ".join(top))
    return name or "SupportRequest"


def _fallback_response(resolutions: List[str]) -> str:
    seen = set()
    merged = []
    for resolution in resolutions:
        text = (resolution or "").strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            merged.append(text)
    return "\n\n".join(merged)


def build_label_prompt(category: str, requests: List[str], resolutions: List[str]) -> str:
    # NB: use .replace(), not .format() -- the template contains literal { } from
    # the JSON schema example, which str.format() would try to interpret as fields.
    requests_block = "\n".join(f"- {r}" for r in requests)
    resolutions_block = "\n".join(f"- {r}" for r in resolutions) or "- (none provided)"
    return (
        LABEL_INSTRUCTIONS
        .replace("{requests}", requests_block)
        .replace("{resolutions}", resolutions_block)
    )


def label_cluster(cluster: Dict, llm: LLMClient) -> Dict:
    """Turn one cluster into an intent. LLM names + writes; utterances stay real."""
    interactions = cluster["interactions"]
    requests = [i["request"] for i in interactions]
    resolutions = [i["resolution"] for i in interactions if i.get("resolution")]
    utterances = _collect_utterances(interactions)

    name = response = ""
    category = cluster.get("category", "")
    uses_lambda = False
    try:
        data = llm.complete_json(
            build_label_prompt(category, requests, resolutions),
            system=LABEL_SYSTEM,
            temperature=0.1,
        )
        if isinstance(data, dict):
            name = str(data.get("intent_name") or "").strip()
            response = str(data.get("response") or "").strip()
            category = str(data.get("category") or category).strip()
            uses_lambda = bool(data.get("uses_lambda"))
    except Exception:  # noqa: BLE001 - labelling is best-effort; fall back deterministically
        pass

    return {
        "intent_name": name or _fallback_name(category, requests),
        "category": category,
        "uses_lambda": uses_lambda,
        "response": response or _fallback_response(resolutions),
        "utterances": utterances,
        "source_ids": [i.get("source_id", "") for i in interactions],
    }


# --- output: the loader's spreadsheet / CSV ----------------------------------
INTENT_SHEET_HEADER = ["#", "Intent Name", "Category", "Utterance", "Total Utts", "Uses Lambda", "Response / Answer"]


def _intent_rows(intents: List[Dict]) -> List[List]:
    """Row layout load_lex_intents_from_excel.read_intents() consumes, shared by
    both the xlsx and CSV writers so the two formats never drift apart."""
    rows = [list(INTENT_SHEET_HEADER)]
    for index, intent in enumerate(intents, start=1):
        utterances = intent["utterances"] or [""]
        first, rest = utterances[0], utterances[1:]
        rows.append([
            index,
            intent["intent_name"],
            intent.get("category", ""),
            first,
            len(intent["utterances"]),
            "Yes" if intent.get("uses_lambda") else "No",
            intent.get("response", ""),
        ])
        for utterance in rest:
            rows.append(["", "", "", utterance, "", "", ""])
    return rows


def write_intent_workbook(intents: List[Dict], path: str) -> str:
    """Write intents as the same xlsx shape load_lex_intents_from_excel consumes."""
    from openpyxl import Workbook

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "All Intents"
    worksheet.append([" All Intents (generated by Agent 2)"])
    for row in _intent_rows(intents):
        worksheet.append(row)
    workbook.save(path)
    return path


def write_intents_csv(intents: List[Dict], path: str) -> str:
    """Write intents as a CSV that load_lex_intents_from_excel.read_intents()
    reads directly (same header/column names as the xlsx export)."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerows(_intent_rows(intents))
    return path


def _apply_via_loader(xlsx_path: str, *, bot_id: str, region: str = "ap-southeast-2",
                      locale_id: str = "en_US", sheet: str = "All Intents",
                      lambda_mode: str = "none", dedupe: bool = True,
                      prune_empty: bool = True, build: bool = True,
                      wait_build: bool = True) -> Dict:
    """Load a generated xlsx into Lex by reusing load_lex_intents_from_excel."""
    from types import SimpleNamespace

    import load_lex_intents_from_excel as loader

    intents = loader.read_intents(xlsx_path, sheet)
    if dedupe:
        loader.dedupe_cross_intent_utterances(intents)
    args = SimpleNamespace(
        region=region, bot_id=bot_id, bot_version="DRAFT", locale_id=locale_id,
        lambda_mode=lambda_mode, prune_empty=prune_empty, build=build, wait_build=wait_build,
    )
    loader.apply_intents(args, intents)
    return {"loaded_from": xlsx_path, "bot_id": bot_id}


def run_synthesis(
    corpus,
    llm: LLMClient,
    *,
    threshold: float = 0.3,
    output_xlsx: Optional[str] = None,
    auto_load: bool = False,
    load_options: Optional[Dict] = None,
) -> Dict:
    """Corpus -> clustered, labelled intents -> (optionally) an xlsx / a Lex load."""
    clusters = cluster_interactions(corpus, threshold=threshold)
    intents = [label_cluster(cluster, llm) for cluster in clusters]

    result = {
        "cluster_count": len(clusters),
        "intent_count": len(intents),
        "intents": intents,
    }
    if output_xlsx:
        write_intent_workbook(intents, output_xlsx)
        result["xlsx_path"] = output_xlsx
    if auto_load:
        if not output_xlsx:
            raise ValueError("auto_load requires output_xlsx (the sheet to load)")
        if not (load_options or {}).get("bot_id"):
            raise ValueError("auto_load requires load_options['bot_id']")
        result["load"] = _apply_via_loader(output_xlsx, **load_options)
    return result


# --- runnable end-to-end demo (Agent 1 -> Agent 2 -> xlsx), no network ------
def _demo() -> None:
    import json
    import os

    from agents.connectors import MockTicketConnector
    from agents.ingest import run_ingestion
    from agents.llm import MockLLMClient

    # Agent 1: scripted extractions (SUP-1004 shares "access" so it clusters with SUP-1003).
    ingest_scripts = [
        json.dumps({"request": "Reset my privileged ADAM account password",
                    "resolution": "Use ADAM Self Service > Privileged Account > Reset.",
                    "category": "Password & Auth", "phrasings": ["how do I reset my ADAM password?"]}),
        json.dumps({"request": "GlobalProtect VPN keeps disconnecting",
                    "resolution": "Update GlobalProtect and switch to the regional gateway.",
                    "category": "VPN & Network", "phrasings": ["my VPN keeps dropping"]}),
        json.dumps({"request": "Get access to Smartsheet",
                    "resolution": "Raise a Smartsheet license request; access via SSO.",
                    "category": "Software & Access", "phrasings": ["how do I get access to Smartsheet?"]}),
        json.dumps({"request": "Get access to a Smartsheet license for planning",
                    "resolution": "Provision Smartsheet license via SSO.",
                    "category": "Software & Access", "phrasings": ["I need a Smartsheet license"]}),
    ]
    corpus = run_ingestion(MockTicketConnector(), MockLLMClient(responses=ingest_scripts))

    # Agent 2: one label per cluster. 4 interactions -> 3 clusters (Smartsheet merges).
    label_scripts = [
        json.dumps({"intent_name": "AdamPasswordReset", "category": "Password & Auth",
                    "response": "Use ADAM Self Service > Privileged Account > Reset.", "uses_lambda": False}),
        json.dumps({"intent_name": "VpnDisconnecting", "category": "VPN & Network",
                    "response": "Update GlobalProtect and switch to the regional gateway.", "uses_lambda": False}),
        json.dumps({"intent_name": "SmartsheetAccess", "category": "Software & Access",
                    "response": "Raise a Smartsheet license request; access is granted via SSO.", "uses_lambda": True}),
    ]
    scratch = os.environ.get("TEMP", ".")
    xlsx_path = os.path.join(scratch, "agent2_intents_draft.xlsx")
    result = run_synthesis(corpus, MockLLMClient(responses=label_scripts), output_xlsx=xlsx_path)

    print("=" * 70)
    print("  Agent 2 — synthesis demo (Agent 1 -> Agent 2 -> loader xlsx)")
    print("=" * 70)
    print(f"interactions in: {corpus['count']}  ->  clusters: {result['cluster_count']}  "
          f"->  intents: {result['intent_count']}")
    for intent in result["intents"]:
        kind = "lambda" if intent["uses_lambda"] else "static"
        print(f"\n• {intent['intent_name']} [{intent['category']}] ({kind}) "
              f"from {intent['source_ids']}")
        print(f"    utterances: {intent['utterances']}")
        print(f"    response:   {intent['response'][:80]}")
    print(f"\nWrote draft sheet the Lex loader can consume: {result['xlsx_path']}")
    print("Review it, then run load_lex_intents_from_excel.py on it "
          "(or set auto_load=True with a bot_id).")
    print("=" * 70)


if __name__ == "__main__":
    _demo()
