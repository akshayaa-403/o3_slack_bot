"""Script 1 of 2: pull resolved Jira tickets and generate a Lex-intents CSV.

    Jira (real tickets) -> Agent 1 (ingest/distill) -> Agent 2 (cluster + label
    with Gemini) -> intents_<timestamp>.csv

The CSV this writes has the exact column shape
load_lex_intents_from_excel.read_intents() consumes, so the companion script
(scripts/load_intents_to_lex.py) can load it straight into Amazon Lex — nothing
in this script talks to AWS.

--- Setup --------------------------------------------------------------------
Reads the repo-root .env (auto-loaded) / environment:
    JIRA_BASE_URL=https://your-domain.atlassian.net     required (live mode)
    JIRA_EMAIL=you@example.com                          required (live mode)
    JIRA_API_TOKEN=...                                  required (live mode)
    JIRA_PROJECT_KEY=IVY                                optional, narrows the JQL
    GEMINI_API_KEY=...                                  required (live mode)
    GEMINI_MODEL=gemini-flash-latest                    optional

--- Run ------------------------------------------------------------------
    # real Jira + real Gemini
    python scripts/generate_intents_from_jira.py

    # narrow to your project, tune clustering, cap ticket count
    python scripts/generate_intents_from_jira.py --project IVY --threshold 0.3 --limit 200

    # exercise the whole script with no Jira/Gemini creds (canned data)
    python scripts/generate_intents_from_jira.py --dry-run

    # custom JQL / output path
    python scripts/generate_intents_from_jira.py \\
        --jql "project = IVY AND statusCategory = Done ORDER BY resolved DESC" \\
        --output data/intents_from_jira.csv
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agents.connectors import JiraConnector, MockTicketConnector
from agents.ingest import run_ingestion
from agents.llm import GeminiLLMClient, MockLLMClient
from agents.synthesize import run_synthesis, write_intents_csv

DEFAULT_JQL = "statusCategory = Done ORDER BY resolved DESC"

# Scripted mock LLM output for --dry-run (no network, no credentials needed) —
# proves the script's wiring end to end without touching Jira or Gemini.
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


def _load_dotenv() -> None:
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


def build_jql(args) -> str:
    if args.jql:
        return args.jql
    project = args.project or os.environ.get("JIRA_PROJECT_KEY", "")
    if project:
        return f"project = {project} AND {DEFAULT_JQL}"
    return DEFAULT_JQL


def build_connector(args):
    if args.dry_run:
        print("  [dry-run] using MockTicketConnector (no Jira credentials needed)")
        return MockTicketConnector()

    site_url = os.environ.get("JIRA_BASE_URL", "")
    email = os.environ.get("JIRA_EMAIL", "")
    token = os.environ.get("JIRA_API_TOKEN", "")
    missing = [name for name, value in
               (("JIRA_BASE_URL", site_url), ("JIRA_EMAIL", email), ("JIRA_API_TOKEN", token))
               if not value]
    if missing:
        print(f"Missing Jira credentials: {', '.join(missing)}. Set them in .env, "
              f"or pass --dry-run to test with sample tickets instead.")
        sys.exit(1)

    return JiraConnector(site_url, email, token, jql=build_jql(args))


def build_llm(args):
    if args.dry_run:
        return MockLLMClient(responses=_MOCK_EXTRACT + _MOCK_LABEL)
    if not os.environ.get("GEMINI_API_KEY"):
        print("Missing GEMINI_API_KEY. Set it in .env, or pass --dry-run to test without it.")
        sys.exit(1)
    return GeminiLLMClient()


def default_output_path() -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(REPO_ROOT, "data", f"intents_from_jira_{stamp}.csv")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate a Lex-intents CSV from resolved Jira tickets."
    )
    parser.add_argument("--jql", help="Custom JQL. Overrides --project and the default JQL.")
    parser.add_argument("--project", help="Jira project key to scope the default JQL to "
                                          "(falls back to JIRA_PROJECT_KEY).")
    parser.add_argument("--limit", type=int, help="Max tickets to pull.")
    parser.add_argument("--threshold", type=float, default=0.3,
                         help="Clustering similarity threshold (0-1). Lower = more merging.")
    parser.add_argument("--output", help="CSV path to write. Default: data/intents_from_jira_<timestamp>.csv")
    parser.add_argument("--dry-run", action="store_true",
                         help="Use built-in sample tickets and a scripted mock LLM — "
                              "no Jira or Gemini credentials required. For testing the script itself.")
    return parser.parse_args()


def main() -> None:
    _load_dotenv()
    args = parse_args()
    output_path = args.output or default_output_path()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    connector = build_connector(args)
    llm = build_llm(args)

    print("=" * 70)
    print(f"  Generating intents from {getattr(connector, 'name', 'ticket')} tickets")
    if isinstance(connector, JiraConnector):
        print(f"  JQL: {build_jql(args)}")
    print("=" * 70)

    print("Agent 1: fetching + distilling tickets...")
    corpus = run_ingestion(connector, llm, limit=args.limit)
    print(f"  {corpus['count']} interactions extracted, {corpus['skipped']} skipped, "
          f"{len(corpus['failed'])} failed")
    if corpus["failed"]:
        for failure in corpus["failed"][:10]:
            print(f"    FAILED {failure['source_id']}: {failure['error']}")

    if not corpus["interactions"]:
        print("\nNo usable interactions extracted — nothing to write.")
        sys.exit(1)

    print("Agent 2: clustering + labelling with the LLM...")
    result = run_synthesis(corpus, llm, threshold=args.threshold)
    print(f"  {result['cluster_count']} clusters -> {result['intent_count']} intents")

    write_intents_csv(result["intents"], output_path)

    print(f"\nWrote: {output_path}")
    for intent in result["intents"]:
        kind = "action/Lambda" if intent["uses_lambda"] else "info"
        print(f"  - {intent['intent_name']} [{intent['category']}] ({kind}), "
              f"{len(intent['utterances'])} utterances")
    print(f"\nNext: python scripts/load_intents_to_lex.py --file {output_path} --bot-id <BOT_ID> --apply")


if __name__ == "__main__":
    main()
