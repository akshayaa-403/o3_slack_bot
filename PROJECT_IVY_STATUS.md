# Project IVY Status

Last updated: 2026-06-21

This is the living implementation document for Project IVY. Update it whenever code, AWS wiring, architecture decisions, environment variables, or test behavior changes.

## Maintenance Rule

Every implementation change should update one or more of these sections:

- Current Implementation
- Environment Variables
- Architecture Alignment
- Testing Notes
- Change Log
- Next Work

## Architecture Source

Primary architecture reference:

- `Ivy Bot - AWS flow diagram_Page 1.pdf`

Extracted architecture artifacts generated from the PDF:

- `E:\CodeDetector\beetle.sandbox.code\tmp\pdfs\ivy_stack_read\architecture_model.json`
- `E:\CodeDetector\beetle.sandbox.code\tmp\pdfs\ivy_stack_read\architecture_model.py`
- `E:\CodeDetector\beetle.sandbox.code\tmp\pdfs\ivy_stack_read\architecture_graph_networkx.json`

The diagram maps the current code approximately as:

- `lambda_o3_slack_handler.py` -> `O3_slack_queue`
- `lambda_o3_slack_worker.py` -> early version of `O3_slack_node_handler`

## Current Implementation

### Slack Event Handler

File: `lambda_o3_slack_handler.py`

Responsibilities:

- Receives Slack Events API payloads through API Gateway or Lambda URL.
- Handles Slack `url_verification`.
- Optionally verifies Slack request signatures.
- Deduplicates Slack events in DynamoDB using `event_id`.
- Ignores bot messages and unsupported message subtypes.
- Supports:
  - DMs
  - `app_mention`
  - channel messages only when the bot is mentioned
- Cleans bot mention text before sending to Lex.
- Sends valid Slack messages to SQS.

Current testing mode:

- Slack signature verification is disabled unless `VERIFY_SLACK_SIGNATURE=true`.
- `SLACK_SIGNING_SECRET` is optional while verification is disabled.

### Slack Worker / Node Handler

File: `lambda_o3_slack_worker.py`

Responsibilities:

- Consumes SQS records.
- Calls AWS Lex V2 `recognize_text`.
- Simplifies Lex slots.
- Stores session state in DynamoDB table `o3_slack_sessions` by default.
- Sends Lex response back to Slack through `chat.postMessage`.
- Handles empty user text with a friendly fallback.
- Handles empty Lex replies with a friendly fallback.
- Supports SQS partial batch failure response through `batchItemFailures`.

Current gap:

- Worker is still a thin implementation of the diagram's `O3_slack_node_handler`.
- It does not yet implement timeout scheduling, summarizer, image handling, live agent handoff, Jira/Rovo, or LLM fallback.

## Environment Variables

### Handler Lambda

Required:

- `SQS_QUEUE_URL`

Optional:

- `SLACK_BOT_USER_ID`
- `DEDUP_TABLE`, default `O3_EventDedup2`
- `DEDUP_TTL_SECONDS`, default `172800`
- `VERIFY_SLACK_SIGNATURE`, default `false`
- `SLACK_SIGNING_SECRET`, required only when `VERIFY_SLACK_SIGNATURE=true`
- `SLACK_SIGNATURE_TOLERANCE_SECONDS`, default `300`

### Worker Lambda

Required:

- `BOT_ID`
- `BOT_ALIAS_ID`
- `SLACK_BOT_TOKEN`

Optional:

- `AWS_REGION`, default `ap-southeast-2`
- `LOCALE_ID`, default `en_US`
- `DYNAMODB_TABLE`, default `o3_slack_sessions`
- `SESSION_TTL_SECONDS`, default `86400`
- `EMPTY_USER_TEXT_REPLY`, default `Hi, how can I help?`
- `EMPTY_LEX_REPLY`, default `I could not generate a response for that. Please try rephrasing your message.`

## Architecture Alignment

Implemented from diagram:

- `Slack -> API Gateway -> O3_slack_queue`
- `O3_slack_queue -> O3_EventDedup2`
- `O3_slack_queue -> O3_slack_events`
- `O3_slack_events -> O3_slack_node_handler`
- `O3_slack_node_handler -> LEX`
- `O3_slack_node_handler -> O3_slack_sessions`
- `O3_slack_node_handler -> Slack reply`

Not implemented yet:

- `O3_slack_node_handler -> O3_Slack_Scheduler_Role`
- `O3_Slack_Scheduler_Role -> O3_slack_timeout_handler`
- `O3_slack_timeout_handler -> O3_slack_summarizer`
- `LEX -> O3_lambda_router`
- `LEX -> O3_Escalation`
- `LEX -> Claude`
- `O3_lambda_router -> O3_CreateJiraTicket`
- `O3_lambda_router -> O3_Image_rek`
- `O3_lambda_router -> O3_live_agent`
- `O3_CreateJiraTicket -> Jira + Rovo`
- live-agent/on-call tables and locks

## Testing Notes

Current known tests/checks:

- Python syntax parsing passed for handler and worker after recent edits.
- PDF architecture extraction was run with:
  - PyMuPDF
  - pdfplumber
  - pdfminer.six
  - opencv-python
  - networkx
  - shapely

Manual Slack test scenarios:

- DM bot with text: should process.
- Public channel without bot mention: should ignore.
- Public channel with bot mention: should process.
- Bare bot mention: should send empty-user-text fallback.
- Duplicate Slack `event_id`: should return `duplicate ignored`.

Manual SQS worker test:

- Invalid record body should return the failed `messageId` in `batchItemFailures`.

## Next Work

Planned next phase: timeout flow.

Decision summary:

- Inactivity timeout: 15 minutes.
- Timeout prompt: post in Slack thread.
- Close grace window after prompt: 5 minutes.
- AWS region: use Lambda `AWS_REGION`.
- Summarizer: optional hook only for now.

Implementation outline:

- Add thread/session continuity to worker.
- Add EventBridge Scheduler schedule refresh after each user message.
- Create `lambda_o3_slack_timeout_handler.py`.
- Timeout handler should ignore stale schedules, prompt once, then close stale sessions.
- Optional summarizer invocation should run only when configured.

## Change Log

### 2026-06-21

- Created `PROJECT_IVY_STATUS.md` as the living project documentation file.
- Documented current handler and worker responsibilities.
- Documented current architecture alignment against the PDF.
- Documented timeout-flow plan as the next implementation phase.

### 2026-06-20

- Added optional Slack request signature verification gate.
- Disabled signature enforcement by default for open testing with `VERIFY_SLACK_SIGNATURE=false`.
- Added Slack event routing polish:
  - DMs
  - `app_mention`
  - bot-mentioned channel messages
  - ignore non-mentioned public channel messages
  - ignore unsupported Slack subtypes
- Added cleaned Slack text and raw Slack text to SQS payload.
- Added worker handling for empty user text and empty Lex replies.
- Added SQS partial batch failure response.
- Extracted architecture diagram into code/data artifacts using the advanced PDF stack.

