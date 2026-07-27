# O3 Slack Bot — Intent Pipeline & Lex Integration

A Slack bot that converts resolved support tickets into Amazon Lex intents through an automated two-agent pipeline with human-in-the-loop approval.

## Overview

The O3 bot helps teams scale support by:
1. **Ingesting tickets** from Jira or mock sources
2. **Extracting & clustering** similar requests into candidate intents using an LLM
3. **Human approval** in Slack with Approve/Deny flow
4. **Loading intents** to Amazon Lex for automated handling

The pipeline uses **Mistral** for LLM inference and **Textract** for OCR on images in conversations.

---

## Architecture

### Core Pipeline (agents/)

```
Tickets (Jira/Mock)
    ↓
[Agent 1: Ingest] — Extract request/resolution from each ticket
    ↓
[Agent 2: Synthesize] — Cluster similar extracts + label with LLM
    ↓
Proposal (JSON: intents list + stats)
    ↓
[Slack Review Surface] — Post approval card
    ↓
Approve → [Loader Lambda] → Amazon Lex
Deny   → [CSV Export] → User downloads & can edit
```

### Files

- **agents/ingest.py** — Agent 1, runs ingestion pipeline
- **agents/synthesize.py** — Agent 2, clusters and labels intents
- **agents/orchestrate.py** — Ties agents together, handles proposal lifecycle
- **agents/review.py** — Slack card rendering & action mapping
- **agents/store.py** — In-memory/DynamoDB proposal store
- **agents/triggers.py** — Slash command & button handlers
- **agents/llm.py** — LLM client (Mistral, Gemini fallback, mock)
- **agents/connectors.py** — Ticket sources (Jira, mock)

### Integrations

- **lambda_o3_lex_intent_loader.py** — Creates/updates intents in Lex
- **load_lex_intents_from_excel.py** — Converts CSV/XLSX → Lex API payload
- **ocr/textract_engine.py** — Extract text from images via AWS Textract
- **ocr/aws_lambda_handler.py** — OCR Lambda entry point

---

## Setup

### Prerequisites

- Python 3.11+
- AWS credentials (for Lex, Textract, DynamoDB in production)
- Slack bot with OAuth scopes: `commands`, `chat:write`, `files:write`
- Mistral API key (or Gemini for fallback)

### Configuration

Edit `.env` (repository root, gitignored):

```bash
# Slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...          # Only for Socket Mode demo

# LLM
GEMINI_API_KEY=...                # Optional, fallback to mock
GEMINI_MODEL=gemini-flash-latest
MIA_KEY=...                       # Mistral API key

# Lex
BOT_ID=...
LOCALE_ID=en_US
AWS_REGION=ap-southeast-2

# Intent Pipeline Demo
DEMO_LLM=mistral                  # or "gemini", "mock"
DEMO_LEX_BOT_ID=...               # Lex bot to load intents into
DEMO_LEX_LOAD=real                # or "simulated"
DEMO_LEX_BUILD=false              # Start build after loading?

# Jira (optional, for real ticket source)
JIRA_BASE_URL=https://...
JIRA_EMAIL=...
JIRA_API_TOKEN=...
JIRA_PROJECT_KEY=...
```

---

## Running the Demo

### 1. Mock Pipeline (no credentials needed)

```bash
# Generate intents from 4 built-in mock tickets
# Uses mock LLM (no API calls)
export DEMO_LLM=mock
python demo/slack_intents_demo.py
```

Then in Slack: `/generate-intents` → see 3 proposed intents → click **Approve** or **Deny**.

### 2. With Mistral LLM

```bash
export DEMO_LLM=mistral
export DEMO_LEX_LOAD=simulated    # Don't actually write to Lex
python demo/slack_intents_demo.py
```

Ingests mock tickets, uses **real Mistral API** for clustering/labeling, posts to Slack.

### 3. With Real Jira Tickets

```bash
# First, seed sample tickets into Jira project
python scripts/seed_jira_sample_tickets.py

# Or use existing resolved tickets
python scripts/generate_intents_from_jira.py
```

Then run the demo as above — it will fetch real tickets instead of mocks.

### 4. Load to Real Lex

```bash
export DEMO_LEX_LOAD=real
python demo/slack_intents_demo.py
```

Now **Approve** in Slack will actually write intents to Amazon Lex in `BOT_ID`.

---

## Scripts

### Generate Intents

```bash
# From Jira (requires JIRA_* env vars)
python scripts/generate_intents_from_jira.py --limit 10

# Outputs: data/intents_from_jira_<timestamp>.csv
```

**Options:**
- `--limit N` — Stop after N tickets
- `--threshold 0.3` — Clustering similarity threshold (0–1)
- `--project KEY` — Jira project (default: JIRA_PROJECT_KEY)
- `--dry-run` — Use mock LLM instead of real API
- `--output <path>` — Custom CSV path

### Load Intents to Lex

```bash
# Dry run (shows what WOULD load, changes nothing)
python scripts/load_intents_to_lex.py --file data/intents_from_jira_*.csv

# Actually load
python scripts/load_intents_to_lex.py --file data/intents_from_jira_*.csv --apply

# Also trigger Lex build
python scripts/load_intents_to_lex.py --file ... --apply --build --wait-build
```

**Options:**
- `--dedupe-utterances` — Remove duplicates across intents
- `--prune-empty` — Delete intents with no utterances
- `--json-preview <path>` — Save Lex API payload for review

**Accepts:** CSV or XLSX (same column layout).

### Delete Demo Intents

```bash
# Dry run
python scripts/delete_lex_intents.py

# Actually delete
python scripts/delete_lex_intents.py --apply

# Delete specific intents by name
python scripts/delete_lex_intents.py --names IntentA IntentB --apply
```

### Seed Sample Jira Tickets

```bash
# Dry run (shows what WOULD create)
python scripts/seed_jira_sample_tickets.py

# Create 4 sample resolved tickets
python scripts/seed_jira_sample_tickets.py --apply
```

---

## Project Structure

```
.
├── agents/
│   ├── ingest.py           # Agent 1: extract tickets
│   ├── synthesize.py       # Agent 2: cluster + label
│   ├── orchestrate.py      # Pipeline orchestration
│   ├── review.py           # Slack card + actions
│   ├── store.py            # Proposal persistence
│   ├── triggers.py         # Slash command handlers
│   ├── llm.py              # LLM clients (Mistral, Gemini, mock)
│   └── connectors.py       # Ticket sources
│
├── demo/
│   └── slack_intents_demo.py  # Socket Mode demo with approval flow
│
├── scripts/
│   ├── generate_intents_from_jira.py  # Jira → CSV
│   ├── load_intents_to_lex.py         # CSV → Lex
│   ├── delete_lex_intents.py          # Cleanup
│   ├── seed_jira_sample_tickets.py    # Create test tickets
│   └── test_jira_auth.py              # Verify Jira credentials
│
├── ocr/
│   ├── base.py             # OCR interface
│   ├── engines/
│   │   └── textract_engine.py  # AWS Textract implementation
│   ├── integration.py       # Image analysis entrypoint
│   ├── aws_lambda_handler.py   # Lambda handler
│   └── Dockerfile          # Container for Lambda deployment
│
├── data/
│   └── lex_full_export_preview.json  # Preview of intents in Lex
│
├── tests/
│   ├── test_agents_ingest.py
│   ├── test_agents_synthesize.py
│   └── test_agents_orchestrate.py
│
├── lambda_o3_lex_intent_loader.py  # Lex intent upload Lambda
├── load_lex_intents_from_excel.py  # CSV/XLSX → Lex payload converter
├── .env                     # Environment variables (gitignored)
└── README.md               # This file
```

---

## Testing

```bash
# Run unit tests
python -m pytest tests/

# Test with mock LLM (no API keys needed)
DEMO_LLM=mock pytest tests/

# Test Jira auth
python scripts/test_jira_auth.py
```

---

## Development

### Adding a New Ticket Source

1. Implement `TicketConnector` in `agents/connectors.py`
2. Return list of `{"summary": ..., "description": ...}`
3. Pass to `orchestrate.generate_proposal(connector=MyConnector())`

### Adding a New LLM Provider

1. Implement `LLMClient` in `agents/llm.py`
2. Implement `complete()` and `complete_json()`
3. Update `demo/slack_intents_demo.py` to detect and instantiate

### Modifying the Intent Card Layout

1. Edit `build_card()` in `agents/review.py` (platform-agnostic model)
2. Edit `render_slack_blocks()` for Slack-specific rendering

---

## Production Notes

- **Proposal Store**: Currently `InMemoryProposalStore` (demo only). For Lambda, use DynamoDB via `ProposalStore` interface.
- **OCR**: Textract is server-side; requires AWS credentials with `textract:DetectDocumentText` permission.
- **LLM**: Mistral is the primary provider. Gemini available as fallback (requires `GEMINI_API_KEY`).
- **Lex Bot**: Intents are created in the specified `BOT_ID`. Always test in a sandbox bot first.

---

## Troubleshooting

**"unknown proposal_id" error in Slack**
- Proposal was lost from store (restarted demo). Re-run `/generate-intents`.

**"No intents produced" message**
- LLM extraction failed (rate limit or empty response). Check logs, retry.

**Lex load fails with "permission denied"**
- AWS credentials missing or lack Lex permissions. Verify `BOT_ID` and role.

**Mistral API errors**
- Check `MIA_KEY` is valid and has quota. Fallback to `DEMO_LLM=mock` to test pipeline.

---

## Environment Variables Reference

| Variable | Purpose | Required |
|----------|---------|----------|
| `SLACK_BOT_TOKEN` | Slack bot auth | Yes |
| `SLACK_APP_TOKEN` | Socket Mode (demo only) | No (demo) |
| `MIA_KEY` | Mistral API key | If `DEMO_LLM=mistral` |
| `GEMINI_API_KEY` | Google Gemini API | If `DEMO_LLM=gemini` |
| `BOT_ID` | Amazon Lex bot ID | Yes (for Lex loading) |
| `LOCALE_ID` | Lex locale (default: en_US) | No |
| `AWS_REGION` | AWS region | Yes |
| `DEMO_LLM` | LLM for demo (mistral/gemini/mock) | No (default: mistral) |
| `DEMO_LEX_BOT_ID` | Lex bot for demo loading | No |
| `DEMO_LEX_LOAD` | real or simulated | No (default: simulated) |
| `DEMO_LEX_BUILD` | Start Lex build after load | No (default: false) |
| `JIRA_BASE_URL` | Jira Cloud URL | If using Jira |
| `JIRA_EMAIL` | Jira account email | If using Jira |
| `JIRA_API_TOKEN` | Jira API token | If using Jira |
| `JIRA_PROJECT_KEY` | Jira project key | If using Jira |
| `OCR_ENGINE` | OCR engine (textract only) | No (default: textract) |

---

## License

Part of Project IVY. See LICENSE.
