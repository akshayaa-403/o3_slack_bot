"""Lambda: upload generated intents into Amazon Lex V2.

This is the SINK at the end of the tickets->intents pipeline. Agent 2 produces
intents in memory; the worker hands them off to THIS Lambda (async invoke), which
converts them to the loader's shape and pushes them into a Lex bot locale by
reusing load_lex_intents_from_excel.apply_intents. Returns a summary the caller
relays back to the user who ran /generate-intents.

Event shape (what the worker sends):
    {
      "bot_id": "ABCD1234",            # required
      "intents": [                     # Agent 2 output (agents.synthesize format)
        {"intent_name": "...", "category": "...", "uses_lambda": false,
         "response": "...", "utterances": ["...", "..."]},
        ...
      ],
      "region": "ap-southeast-2",      # optional (defaults below)
      "locale_id": "en_US",
      "lambda_mode": "none" | "marked",
      "dedupe": true, "prune_empty": true, "build": true, "wait_build": false
    }

Kept dependency-light: only load_lex_intents_from_excel (which lazy-imports boto3)
and stdlib. No LLM here — labeling already happened in Agent 2.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Dict, List

import load_lex_intents_from_excel as loader

DEFAULT_REGION = "ap-southeast-2"
DEFAULT_LOCALE = "en_US"


def to_loader_intents(agent_intents: List[Dict]) -> List[Dict]:
    """Convert Agent 2 intents into the dict shape apply_intents expects.

    Reuses the loader's own name-sanitizing/uniqueness helpers so names match
    exactly what the spreadsheet path would produce. Utterances containing slot
    braces are dropped (Lex rejects them as plain sample utterances).
    """
    used_names: set = set()
    loader_intents: List[Dict] = []
    for intent in agent_intents:
        original = (intent.get("intent_name") or "SupportRequest").strip()
        lex_name = loader.make_unique_name(loader.to_lex_intent_name(original), used_names)
        utterances = [
            u for u in (intent.get("utterances") or [])
            if u and "{" not in u and "}" not in u
        ]
        loader_intents.append({
            "original_name": original,
            "intent_name": lex_name,
            "category": intent.get("category", "") or "",
            "uses_lambda": bool(intent.get("uses_lambda")),
            "response": intent.get("response", "") or "",
            "utterances": utterances,
            "skipped_slot_utterances": [],
        })
    return loader_intents


def lambda_handler(event, context=None):
    """Load intents into Lex and return a summary of what changed."""
    if not event.get("bot_id"):
        return {"ok": False, "error": "missing_bot_id"}

    loader_intents = to_loader_intents(event.get("intents") or [])
    if event.get("dedupe", True):
        loader.dedupe_cross_intent_utterances(loader_intents)

    args = SimpleNamespace(
        region=event.get("region", DEFAULT_REGION),
        bot_id=event["bot_id"],
        bot_version=event.get("bot_version", "DRAFT"),
        locale_id=event.get("locale_id", DEFAULT_LOCALE),
        lambda_mode=event.get("lambda_mode", "none"),
        prune_empty=event.get("prune_empty", True),
        build=event.get("build", True),
        # Default async: don't block the Lambda waiting for the Lex build.
        wait_build=event.get("wait_build", False),
    )

    try:
        summary = loader.apply_intents(args, loader_intents)
    except Exception as error:  # noqa: BLE001 - report failure back to the caller
        return {"ok": False, "bot_id": args.bot_id, "error": str(error)}

    return {
        "ok": True,
        "bot_id": args.bot_id,
        "intent_count": len(loader_intents),
        "summary": summary,
    }
