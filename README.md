# Project IVY — Slack IT-Support Bot

IVY is a Slack-based IT support assistant built on AWS Lambda. It handles user
requests from Slack (text and image uploads), resolves them through Amazon Lex,
a Bedrock Knowledge Base, and LLM fallbacks (Claude / Gemini), and can create
Jira tickets, page on-call engineers, and hand off to live agents.

> This repository is the source of truth for the Lambda functions and supporting
> tooling. The living design/status document is
> [`docs/PROJECT_IVY_STATUS.md`](docs/PROJECT_IVY_STATUS.md) — read it for the
> full architecture, environment variables, and change log.

## Repository layout

| Path | What it is |
|---|---|
| `lambda_o3_slack_handler.py` | Slack event entry point (enqueues work). |
| `lambda_o3_slack_worker.py` | Core worker — routing, Lex, image flow, fallbacks, Jira, live agent. |
| `lambda_o3_router.py` | Intent → downstream Lambda router. |
| `lambda_o3_claude_fallback.py` | Claude LLM fallback. |
| `lambda_o3_create_jira_ticket.py` | Jira ticket creation. |
| `lambda_o3_image_rek.py` | Image analysis (Amazon Rekognition path). |
| `lambda_o3_live_agent.py`, `live_agent_capacity.py` | Live-agent handoff + capacity. |
| `lambda_o3_rovo_enrichment.py` | Atlassian Rovo enrichment. |
| `lambda_o3_slack_summarizer.py` | Conversation summarization. |
| `lambda_o3_jsm_oncall_user.py` | JSM/Opsgenie on-call resolution. |
| `lambda_o3_screenshot_indexer.py` | Screenshot vector indexing. |
| `lambda_o3_slack_timeout_handler.py` | Session inactivity timeouts. |
| `load_lex_intents_from_excel.py` | Loads Lex intents from `Intents_20_List.xlsx`. |
| `ocr/` | Pluggable OCR engines + benchmark + PaddleOCR Lambda (see below). |
| `docs/` | Design and status documentation. |
| `tests/` | Test suite. |
| `fixtures/` | Sample images and events for local testing. |

## Image analysis: OCR → Gemini

Slack image uploads are analyzed by a separate Lambda referenced via the worker's
`IMAGE_REK_FUNCTION` env var. The current image flow is:

```
Slack image → worker → IMAGE_REK_FUNCTION Lambda → extracted text → Gemini → reply
```

The `ocr/` package provides pluggable OCR engines (`easyocr`, `paddleocr`,
`textract`, `mistral-ocr`) behind a common interface, a benchmark harness, and a
container-image Lambda handler (`ocr/aws_lambda_handler.py`) that can serve as
`IMAGE_REK_FUNCTION` in place of the Rekognition Lambda. PaddleOCR is the
current default engine.

- Benchmarking: see [`ocr/`](ocr/) — `python -m ocr.benchmark run` / `report`.
- Deploying the PaddleOCR Lambda: see [`ocr/DEPLOY.md`](ocr/DEPLOY.md).

## Development

Requires Python 3.11.

```bash
python -m venv venv
venv\Scripts\activate            # PowerShell: venv\Scripts\Activate.ps1
pip install -r requirements.txt  # root: boto3 + openpyxl for the Lambda code
```

The OCR engines have their own, heavier dependencies:

```bash
pip install -r ocr/requirements.txt
```

Note that `paddleocr` and the cloud OCR SDKs have conflicting dependencies on
some platforms — install them in separate virtual environments if needed (see
`ocr/DEPLOY.md`).

### Configuration

All configuration is via environment variables (the code reads them with
`os.environ`). Copy `.env.example` to `.env` and fill in the values you need;
the full list of every variable each Lambda reads is documented in
[`docs/PROJECT_IVY_STATUS.md`](docs/PROJECT_IVY_STATUS.md). **Never commit real
secrets** — `.env` files are gitignored.

### Tests

```bash
pip install pytest
pytest
```

## Deployment

Each `lambda_o3_*.py` file is deployed as its own AWS Lambda function (see the
name mapping in `docs/PROJECT_IVY_STATUS.md`). The PaddleOCR image-analysis
Lambda ships as a container image — full build/push/wire-up steps are in
[`ocr/DEPLOY.md`](ocr/DEPLOY.md).
