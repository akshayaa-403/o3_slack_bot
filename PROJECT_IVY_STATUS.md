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
- `lambda_o3_slack_timeout_handler.py` -> `O3_slack_timeout_handler`

## Current Implementation

### Slack Event Handler

File: `lambda_o3_slack_handler.py`

Responsibilities:

- Receives Slack Events API payloads through API Gateway or Lambda URL.
- Handles Slack `url_verification`.
- Optionally verifies Slack request signatures.
- Deduplicates accepted Slack DM events in DynamoDB using `event_id`.
- Ignores bot messages and unsupported message subtypes.
- Supports DMs only.
- Ignores public channels, private channels, MPIMs, and `app_mention` events for this phase.
- Sends valid Slack DM messages to SQS.

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
- Stores `last_activity_at`, `timeout_due_at`, `timeout_token`, and timeout status fields in the session record.
- Refreshes a per-session EventBridge Scheduler prompt schedule after active user messages.
- Resets stale timeout prompt/close fields when a user resumes an active session.
- Sends Lex response back to Slack through `chat.postMessage`.
- Handles empty user text with a friendly fallback.
- Handles empty Lex replies with a friendly fallback.
- Supports SQS partial batch failure response through `batchItemFailures`.

Current gap:

- Worker is still a thin implementation of the diagram's `O3_slack_node_handler`.
- It does not yet implement image handling, live agent handoff, Jira/Rovo, or LLM fallback.

### Slack Timeout Handler

File: `lambda_o3_slack_timeout_handler.py`

Responsibilities:

- Handles EventBridge Scheduler `prompt` and `close` actions.
- Validates scheduled events against the session's current `timeout_token`.
- Ignores stale schedules when a user has already replied or a conversation is no longer active.
- Sends the inactivity timeout prompt as a normal Slack DM message.
- Creates the close schedule after the timeout prompt is sent.
- Closes sessions that remain inactive through the grace window.
- Invokes an optional summarizer Lambda asynchronously when configured.

## Environment Variables

### Handler Lambda

Required:

- `SQS_QUEUE_URL`

Optional:

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
- `INACTIVITY_TIMEOUT_SECONDS`, default `900`
- `TIMEOUT_SCHEDULING_ENABLED`, default `true`
- `TIMEOUT_HANDLER_ARN`, required for timeout scheduling
- `SCHEDULER_ROLE_ARN`, required for timeout scheduling
- `SCHEDULER_GROUP_NAME`, default `default`
- `SCHEDULER_NAME_PREFIX`, default `o3-slack-timeout`
- `EMPTY_USER_TEXT_REPLY`, default `Hi, how can I help?`
- `EMPTY_LEX_REPLY`, default `I could not generate a response for that. Please try rephrasing your message.`

### Timeout Handler Lambda

Required:

- `SLACK_BOT_TOKEN`
- `SCHEDULER_ROLE_ARN`, required for prompt-to-close scheduling

Optional:

- `AWS_REGION`, default `ap-southeast-2`
- `DYNAMODB_TABLE`, default `o3_slack_sessions`
- `SESSION_TTL_SECONDS`, default `86400`
- `TIMEOUT_CLOSE_GRACE_SECONDS`, default `300`
- `TIMEOUT_PROMPT_TEXT`, default `Are you still there? I will close this conversation if I do not hear back soon.`
- `TIMEOUT_HANDLER_ARN`, defaults to the invoked Lambda ARN when available
- `SCHEDULER_GROUP_NAME`, default `default`
- `SCHEDULER_NAME_PREFIX`, default `o3-slack-timeout`
- `SUMMARIZER_FUNCTION_NAME`, optional Lambda name or ARN for async summarizer invocation

### Timeout Environment Notes

`TIMEOUT_HANDLER_ARN` is the Lambda ARN that EventBridge Scheduler should invoke later.

- On the worker Lambda, it points to `lambda_o3_slack_timeout_handler.py` so the worker can schedule the first inactivity prompt.
- On the timeout handler Lambda, it usually points to the timeout handler itself so the prompt action can schedule the later close action.
- The timeout handler can fall back to the invoked Lambda ARN from the runtime context, but setting `TIMEOUT_HANDLER_ARN` explicitly is clearer and supports aliases or pinned versions.

`SCHEDULER_ROLE_ARN` is not the worker Lambda execution role. It is the EventBridge Scheduler execution role.

- EventBridge Scheduler assumes this role when it invokes the timeout handler Lambda.
- The role trust policy must allow `scheduler.amazonaws.com` to call `sts:AssumeRole`.
- The role permission policy must allow `lambda:InvokeFunction` on the timeout handler Lambda ARN.
- The worker Lambda execution role and timeout handler Lambda execution role also need `scheduler:CreateSchedule`, `scheduler:UpdateSchedule`, `scheduler:DeleteSchedule`, and `iam:PassRole` for this scheduler role.

Timeout schedule chain:

- User message -> worker schedules `action=prompt`.
- Prompt timer fires -> timeout handler sends the Slack timeout prompt.
- Timeout handler schedules itself with `action=close`.
- Close timer fires -> timeout handler closes the session if the `timeout_token` is still current.

## Architecture Alignment

Implemented from diagram:

- `Slack -> API Gateway -> O3_slack_queue`
- `O3_slack_queue -> O3_EventDedup2`
- `O3_slack_queue -> O3_slack_events`
- `O3_slack_events -> O3_slack_node_handler`
- `O3_slack_node_handler -> LEX`
- `O3_slack_node_handler -> O3_slack_sessions`
- `O3_slack_node_handler -> Slack reply`
- `O3_slack_node_handler -> O3_Slack_Scheduler_Role`
- `O3_Slack_Scheduler_Role -> O3_slack_timeout_handler`
- `O3_slack_timeout_handler -> Slack timeout prompt`
- `O3_slack_timeout_handler -> O3_slack_sessions`
- `O3_slack_timeout_handler -> O3_slack_summarizer` optional async hook

Not implemented yet:

- Actual `O3_slack_summarizer` implementation
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

- Python syntax parsing passed for handler, worker, and timeout handler after recent edits.
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
- Public channel with bot mention: should ignore.
- Private channel message: should ignore.
- `app_mention`: should ignore.
- Empty DM text: should send empty-user-text fallback.
- Duplicate Slack `event_id`: should return `duplicate ignored`.

Manual SQS worker test:

- Invalid record body should return the failed `messageId` in `batchItemFailures`.

Manual timeout-flow test:

- Temporarily set `INACTIVITY_TIMEOUT_SECONDS=60` and `TIMEOUT_CLOSE_GRACE_SECONDS=60` for a faster test.
- DM the bot and confirm the session row has `timeout_status=scheduled`, `timeout_due_at`, `timeout_token`, and `timeout_schedule_name`.
- Confirm the EventBridge Scheduler prompt schedule exists for the session.
- Wait for the prompt schedule to fire and confirm Slack receives the timeout prompt.
- Confirm the session row moves to `timeout_status=prompted` and gets `timeout_close_due_at`.
- Reply before the close schedule fires and confirm the worker writes a new `timeout_token`; the old close schedule should be ignored as stale.
- Repeat without replying and confirm the close action sets `conversation_status=closed` and `timeout_status=closed`.
- If `SUMMARIZER_FUNCTION_NAME` is configured, confirm the timeout handler invokes it asynchronously after closing.

## Next Work

Planned next phase: deploy and verify timeout flow in AWS, then begin Lex/router fulfillment.

Timeout deployment checks:

- Create or confirm the EventBridge Scheduler execution role can invoke `lambda_o3_slack_timeout_handler.py`.
- Set `TIMEOUT_HANDLER_ARN` and `SCHEDULER_ROLE_ARN` on the worker Lambda.
- Set `SLACK_BOT_TOKEN` and `SCHEDULER_ROLE_ARN` on the timeout handler Lambda.
- Run the manual timeout-flow test above with short timeout values before restoring 15-minute and 5-minute defaults.

After timeout verification:

- Create `lambda_o3_router.py`.
- Dispatch by Lex intent name.
- Stub CreateJiraTicket, ImageRek, LiveAgent, and Escalation handlers.

## Change Log

### 2026-06-21

- Created `PROJECT_IVY_STATUS.md` as the living project documentation file.
- Documented current handler and worker responsibilities.
- Documented current architecture alignment against the PDF.
- Documented timeout-flow plan as the next implementation phase.
- Changed Slack routing scope to strict DM-only; public/private channels, MPIMs, and app mentions are deferred.
- Added timeout scheduling fields and EventBridge Scheduler refresh to `lambda_o3_slack_worker.py`.
- Added `lambda_o3_slack_timeout_handler.py` for timeout prompt and close actions.
- Added optional summarizer invocation hook after inactivity close.
- Documented why `TIMEOUT_HANDLER_ARN` and `SCHEDULER_ROLE_ARN` are required in both timeout-related Lambdas.

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
