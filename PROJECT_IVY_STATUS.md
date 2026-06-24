# Project IVY Status

Last updated: 2026-06-24

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
- `lambda_o3_claude_fallback.py` -> `Claude`
- `lambda_o3_router.py` -> `O3_lambda_router`
- `lambda_o3_create_jira_ticket.py` -> `O3_CreateJiraTicket`

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
- Reads Lex fulfillment `sessionAttributes` from the router and stores action metadata in the session record.
- Handles pending Jira ticket confirmations before calling Lex.
- Invokes the CreateJiraTicket Lambda only after the user confirms ticket creation.
- Invokes Claude fallback through Lambda when Lex fails, returns a fallback intent, or has no useful reply.
- Stores `response_source`, `claude_fallback_attempted`, and optional Claude/Jira-deferred metadata in the session record.
- Handles empty user text with a friendly fallback.
- Handles empty Lex replies with a friendly fallback.
- Supports SQS partial batch failure response through `batchItemFailures`.

Current gap:

- Worker is still a thin implementation of the diagram's `O3_slack_node_handler`.
- It does not yet implement image handling, live agent handoff, Rovo enrichment, or escalation.

### Lex Intent Import

File: `load_lex_intents_from_excel.py`

Responsibilities:

- Reads `Intents_20_List.xlsx` and loads each row group as a separate Lex V2 intent.
- Cleans static FAQ responses into readable Lex message groups.
- Sanitizes Excel intent names that are not Lex-safe.
- Skips slot-placeholder utterances such as `{DirectoryTask}` for the FAQ import path.
- Supports `--lambda-mode marked` to enable Lex fulfillment on rows marked `Uses Lambda = Yes`.

Current behavior:

- The initial 21 IVY support intents have been loaded and tested.
- The 7 imported intents marked `Uses Lambda = Yes` are intended to route to `lambda_o3_router.py`.

### Claude Fallback

File: `lambda_o3_claude_fallback.py`

Responsibilities:

- Receives current-session fallback context from the Slack worker.
- Calls Anthropic Claude through Amazon Bedrock `bedrock-runtime.invoke_model`.
- Uses the Bedrock Claude Messages API request shape.
- Returns a normalized reply to the worker when Claude succeeds.
- Returns a structured failure response when Bedrock or Claude fails.

Current behavior:

- Claude runs only when Lex fails, returns a configured fallback intent, or has no useful reply.
- If Claude succeeds, the Slack reply comes from Claude and the session stores `response_source=claude`.
- If Claude fails, no Jira API is called yet; the session stores `next_action=O3_CreateJiraTicket` and `jira_status=deferred`.

### Router / Jira Confirmation

File: `lambda_o3_router.py`

Responsibilities:

- Receives Lex fulfillment events for imported action-style intents.
- Routes the real imported `Uses Lambda = Yes` intent names to the Jira confirmation flow.
- Returns a Lex `Close` response with `response_source=router`, `next_action=O3_CreateJiraTicket`, and `jira_status=pending_confirmation`.
- Stores `jira_intent_name` and `jira_request_text` in Lex session attributes for the worker.
- Does not call Jira directly; the worker performs confirmation and invokes the CreateJiraTicket Lambda.

Current behavior:

- The router handles `AWSaccount`, `AWSRelatedQueries`, `AccessforCamtasia`, `AccesstoOpsgenie`, `AccessToPCQ`, `AccessToIkbInnovyQCom`, and `AccessToUemGpcloudserviceCom` as Jira ticket actions that require user confirmation.
- Claude fallback remains separate and only runs when Lex fails, falls back, or returns no useful reply.

### Jira Ticket Creation

File: `lambda_o3_create_jira_ticket.py`

Responsibilities:

- Reads Jira Cloud credentials from AWS Secrets Manager.
- Creates Jira issues through Jira Cloud REST API v3.
- Builds Jira issue descriptions using Atlassian Document Format.
- Returns normalized `ticket_key` and `ticket_url` to the worker.

Current behavior:

- Worker asks the user to reply yes/no after a routed action intent.
- `yes` invokes the CreateJiraTicket Lambda and stores `jira_status=created`, `jira_ticket_key`, and `jira_ticket_url`.
- `no` stores `jira_status=cancelled` without calling Jira.
- Unclear replies keep `jira_status=pending_confirmation` and ask for yes/no again.

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
- `ENABLE_CLAUDE_FALLBACK`, default `false`
- `CLAUDE_FALLBACK_FUNCTION`, required when `ENABLE_CLAUDE_FALLBACK=true`
- `CLAUDE_FALLBACK_INTENTS`, default `FallbackIntent,AMAZON.FallbackIntent,FallbackToLLM`
- `CLAUDE_FAILURE_REPLY`, default `I could not resolve this automatically. This should be moved to ticket creation once Jira is connected.`
- `CREATE_JIRA_TICKET_FUNCTION`, required for confirmed Jira ticket creation
- `JIRA_UNCLEAR_CONFIRMATION_REPLY`, default `Please reply yes to create the Jira ticket, or no to cancel.`
- `JIRA_CANCELLED_REPLY`, default `Cancelled. I did not create a Jira ticket.`
- `JIRA_CREATE_FAILED_REPLY`, default `I could not create the Jira ticket. Please try again later or contact support.`
- `EMPTY_USER_TEXT_REPLY`, default `Hi, how can I help?`
- `EMPTY_LEX_REPLY`, default `I could not generate a response for that. Please try rephrasing your message.`

IAM:

- Worker Lambda execution role needs `dynamodb:GetItem` and `dynamodb:UpdateItem` on `O3_slack_sessions`.
- Worker Lambda execution role needs `lambda:InvokeFunction` on the CreateJiraTicket Lambda.

### Router Lambda

Optional:

- `AWS_REGION`, default `ap-southeast-2`
- `DEFAULT_ROUTER_REPLY`, default `I understood the request, but that action is not wired yet.`
- `JIRA_CONFIRMATION_REPLY`, default confirmation prompt for imported action intents.
- `CREATE_JIRA_TICKET_FUNCTION`, `IMAGE_REK_FUNCTION`, `LIVE_AGENT_FUNCTION`, `ESCALATION_FUNCTION`, and `LLM_FALLBACK_FUNCTION` are reserved for later phases.

### CreateJiraTicket Lambda

Required:

- `JIRA_SECRET_ID`
- `JIRA_PROJECT_KEY`

Optional:

- `AWS_REGION`, default `ap-southeast-2`
- `JIRA_ISSUE_TYPE_NAME`, default `Task`
- `JIRA_LABELS`, default `project-ivy,o3-slack`
- `JIRA_TIMEOUT_SECONDS`, default `15`

Jira secret JSON:

```json
{
  "site_url": "https://your-domain.atlassian.net",
  "email": "jira-service-account@example.com",
  "api_token": "atlassian-api-token"
}
```

IAM:

- CreateJiraTicket Lambda execution role needs `secretsmanager:GetSecretValue` for `JIRA_SECRET_ID`.

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

### Claude Fallback Lambda

Optional:

- `AWS_REGION`, default `ap-southeast-2`
- `BEDROCK_MODEL_ID`, default `anthropic.claude-haiku-4-5-20251001-v1:0`
- `CLAUDE_MAX_TOKENS`, default `500`
- `CLAUDE_TEMPERATURE`, default `0.2`
- `CLAUDE_SYSTEM_PROMPT`, default concise IVY support-assistant instructions

IAM:

- Claude fallback Lambda execution role needs `bedrock:InvokeModel` for the configured Bedrock model.
- Worker Lambda execution role needs `lambda:InvokeFunction` on the Claude fallback Lambda.
- Bedrock model access must be enabled in the same AWS region used by `AWS_REGION`.

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
- `LEX -> O3_lambda_router`
- `O3_lambda_router -> O3_CreateJiraTicket` through worker confirmation
- `O3_CreateJiraTicket -> Jira`
- `LEX -> Claude`
- `Claude -> O3_CreateJiraTicket` deferred marker only

Not implemented yet:

- Actual `O3_slack_summarizer` implementation
- `LEX -> O3_Escalation`
- `O3_lambda_router -> O3_Image_rek`
- `O3_lambda_router -> O3_live_agent`
- `O3_CreateJiraTicket -> Rovo`
- live-agent/on-call tables and locks

## Testing Notes

Current known tests/checks:

- Python syntax parsing passed for handler, worker, timeout handler, and Claude fallback after recent edits.
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

Manual Claude fallback test:

- Enable Bedrock model access for the configured Claude model in the Lambda region.
- Deploy `lambda_o3_claude_fallback.py`.
- Set `ENABLE_CLAUDE_FALLBACK=true` and `CLAUDE_FALLBACK_FUNCTION` on the worker Lambda.
- Confirm worker role can invoke the Claude fallback Lambda.
- Confirm Claude fallback role can call `bedrock:InvokeModel`.
- Send a Slack DM that Lex cannot answer.
- Confirm worker logs show `claude_fallback_used` when Claude succeeds.
- Confirm Slack receives Claude's answer and DynamoDB stores `response_source=claude`.
- Temporarily break `CLAUDE_FALLBACK_FUNCTION` or Bedrock permission and confirm the session stores `next_action=O3_CreateJiraTicket`, `jira_status=deferred`, and `response_source=claude_failed`.

Manual router/Jira confirmation test:

- Deploy `lambda_o3_router.py` and attach it as the Lex fulfillment Lambda for the alias locale.
- Deploy `lambda_o3_create_jira_ticket.py`.
- Create the Jira secret in Secrets Manager.
- Set `CREATE_JIRA_TICKET_FUNCTION` on the worker Lambda.
- Re-run `load_lex_intents_from_excel.py` with `--lambda-mode marked`.
- Send a routed action phrase such as `I need access to PCQ`.
- Confirm router logs show `router_event_received` and `router_jira_confirmation_requested`.
- Confirm Slack asks for yes/no ticket creation confirmation.
- Confirm DynamoDB stores `response_source=router`, `next_action=O3_CreateJiraTicket`, and `jira_status=pending_confirmation`.
- Reply `no` and confirm `jira_status=cancelled` and no Jira ticket is created.
- Repeat and reply `yes`; confirm Jira ticket creation and `jira_status=created`, `jira_ticket_key`, and `jira_ticket_url`.
- Send a static FAQ phrase such as `reset adam password` and confirm it still stores `response_source=lex`.
- Send an unknown phrase and confirm Claude fallback still stores `response_source=claude`.

## Next Work

Planned next phase: deploy and verify real `O3_CreateJiraTicket`, then decide whether to add Rovo enrichment, image recognition, or live-agent handoff.

Jira deployment checks:

- Create Jira service account API token.
- Store Jira secret in Secrets Manager.
- Deploy `lambda_o3_create_jira_ticket.py`.
- Set CreateJiraTicket Lambda env vars.
- Add worker `CREATE_JIRA_TICKET_FUNCTION`.
- Add IAM permissions for worker invoke and CreateJiraTicket secret read.
- Run the manual router/Jira confirmation test above.
- Keep image recognition, live-agent handoff, and escalation as later phases.

## Change Log

### 2026-06-24

- Added `lambda_o3_create_jira_ticket.py` for Jira Cloud issue creation.
- Changed router action intents from deferred stubs to yes/no Jira confirmation prompts.
- Added worker-side Jira confirmation handling before Lex fallback.
- Added worker invocation of CreateJiraTicket Lambda after user confirmation.
- Added session metadata for `jira_status=created|cancelled|create_failed`, `jira_ticket_key`, `jira_ticket_url`, and `jira_error`.
- Documented Jira Secrets Manager configuration, IAM, and manual tests.

### 2026-06-23

- Loaded and tested the initial 21 IVY support intents from `Intents_20_List.xlsx`.
- Added Lex intent loader support for cleaned responses, Lex-safe intent names, skipped slot placeholders, and marked fulfillment hooks.
- Implemented router stubs for the imported `Uses Lambda = Yes` intents.
- Added worker support for router-returned Lex `sessionAttributes` so sessions store `response_source=router`, `next_action=O3_CreateJiraTicket`, and `jira_status=deferred`.
- Documented router-stub AWS wiring and manual tests.

### 2026-06-22

- Added `lambda_o3_claude_fallback.py` for Bedrock Claude fallback.
- Added worker-side Claude fallback invocation for Lex fallback, failed, or empty-reply cases.
- Added session metadata for `response_source`, Claude fallback attempts, and Jira-deferred failure handling.
- Documented Claude fallback environment variables, IAM, and manual AWS tests.

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
