# Cloud Interview Questions and Answers for Project IVY

Use this guide to interview a candidate who has not seen Project IVY. The goal is to test whether they can contribute to a serverless AWS support bot that integrates Slack, SQS, Lambda, DynamoDB, EventBridge Scheduler, Lex, Bedrock, Rekognition, S3, OpenSearch, Jira, and external APIs.

## How To Score

- Strong hire: explains tradeoffs, failure modes, IAM boundaries, observability, retries, and cost.
- Hireable: knows AWS serverless basics and can reason through integrations with some prompting.
- Risky: knows service names but cannot explain operational behavior, security, or debugging.
- No hire: hand-wavy answers, unsafe IAM, no retry/idempotency thinking, weak production debugging.

## Core Cloud And Serverless

### 1. What are the main benefits and risks of using AWS Lambda for a Slack bot backend?

Expected answer:
- Benefits: no server management, scales on demand, low idle cost, integrates well with SQS/EventBridge/API Gateway.
- Risks: cold starts, timeout limits, concurrency spikes, dependency packaging, async retry behavior, external API latency, and observability complexity.
- A good design keeps Slack acknowledgement fast and moves slow work to SQS/Lambda workers.

Red flags:
- Says Lambda has no scaling limits.
- Ignores timeouts and retries.

### 2. Why would a Slack event handler put messages onto SQS instead of processing everything immediately?

Expected answer:
- Slack requires fast acknowledgement.
- SQS decouples ingestion from processing.
- SQS absorbs bursts and supports retries/dead-letter queues.
- Worker failures do not cause Slack retry storms if the handler already accepted the event.

### 3. What is idempotency, and why is it important for Slack events?

Expected answer:
- Idempotency means processing the same event multiple times has the same result as processing it once.
- Slack may retry events.
- Lambda/SQS can deliver duplicates.
- Store a deduplication key such as Slack `event_id` or a generated action id in DynamoDB with TTL.

### 4. What is the difference between synchronous and asynchronous Lambda invocation?

Expected answer:
- Synchronous waits for the response and returns payload/errors to caller.
- Asynchronous queues the event and returns after acceptance.
- Use synchronous when immediate decision is needed, such as Jira create result.
- Use asynchronous for background work, such as summarization or enrichment.

### 5. How would you choose Lambda timeout and memory for a worker calling external APIs?

Expected answer:
- Timeout must cover worst expected API latency plus retry overhead, but not be excessive.
- Memory affects CPU and network throughput.
- Start with realistic timeout, instrument duration, then tune.
- External calls need their own request timeouts shorter than Lambda timeout.

## SQS, Retries, And Failure Handling

### 6. How should an SQS Lambda worker handle partial failures in a batch?

Expected answer:
- Return `batchItemFailures` for only failed message ids.
- Successfully processed messages should not be retried.
- Poison messages should eventually go to a DLQ.

### 7. What should go into a dead-letter queue strategy?

Expected answer:
- Configure max receive count.
- Store failed events in DLQ for inspection/replay.
- Add alarms on DLQ depth.
- Include enough event context in logs to debug.

### 8. How do you prevent duplicate Jira tickets when users click a button twice?

Expected answer:
- Use a DynamoDB conditional update as a lock.
- Transition `jira_status=pending_confirmation` to `creating`.
- Only the first click succeeds.
- Store final ticket key and return existing ticket on duplicate/retry.

## DynamoDB And State

### 9. What makes DynamoDB a good fit for Slack session state?

Expected answer:
- Low-latency key-value access.
- Conditional writes for locks.
- TTL cleanup.
- Scales without managing servers.
- Natural key can be `channel:user:thread_ts`.

### 10. When would you use a conditional expression in DynamoDB?

Expected answer:
- For optimistic concurrency.
- To avoid stale timeout events closing a resumed session.
- To prevent duplicate ticket creation.
- To ensure a state transition happens only from the expected current state.

### 11. Why use TTL on session and deduplication tables?

Expected answer:
- Old sessions and dedup keys are temporary.
- TTL controls storage growth and cost.
- Dedup keys only need to live long enough to cover retry windows.

### 12. What are possible drawbacks of storing large transcripts directly in DynamoDB?

Expected answer:
- Item size limit is 400 KB.
- Costs can increase.
- Updates get heavier.
- Better pattern: store compact metadata in DynamoDB and raw audit/transcript in S3.

## EventBridge Scheduler And Timeouts

### 13. How would you implement a 15-minute inactivity timeout?

Expected answer:
- On each user activity, write a new `timeout_token` and `timeout_due_at`.
- Create/update an EventBridge Scheduler one-time schedule.
- Scheduled Lambda validates token before prompting/closing.
- Stale schedules should be ignored.

### 14. Why include a timeout token instead of trusting the scheduled event?

Expected answer:
- A user may reply after the schedule was created.
- Old scheduled events may still fire.
- Token validates that the event belongs to the latest active session state.

### 15. What should happen when a user replies after a timeout prompt but before close?

Expected answer:
- Worker writes a new timeout token and due time.
- Old close schedule becomes stale.
- Timeout handler ignores old close because token or state does not match.

## IAM And Security

### 16. What is least privilege IAM in this project?

Expected answer:
- Each Lambda role gets only required actions and resources.
- Worker can invoke specific Lambdas, not all Lambdas.
- Jira Lambda can read only the Jira secret.
- Image Lambda can access only required S3 bucket and Rekognition actions.
- Summarizer can read/update session table, write audit prefix, and invoke Bedrock model if needed.

### 17. Where should Slack bot tokens, Jira API tokens, and Gemini keys be stored?

Expected answer:
- Prefer AWS Secrets Manager or SSM Parameter Store with encryption.
- Avoid hardcoding secrets or committing them.
- Lambda env vars can reference secret names, but secret values should not be in code.

### 18. How do you verify Slack request signatures?

Expected answer:
- Use Slack signing secret.
- Recreate base string from version, timestamp, raw body.
- HMAC SHA256 and constant-time compare.
- Reject old timestamps to prevent replay attacks.

### 19. What logs should never contain sensitive data?

Expected answer:
- Slack tokens, Jira tokens, API keys, user private data beyond necessary metadata, raw secret values.
- Logs can include event ids, session ids, status codes, sanitized error codes, and request ids.

## Slack Integration

### 20. Why should a bot ignore its own Slack messages?

Expected answer:
- To avoid loops.
- Slack bot messages can trigger event callbacks.
- Handler should ignore `bot_id` or bot message subtype.

### 21. How do Slack threads improve support-bot UX?

Expected answer:
- Each issue stays grouped under a top-level user message.
- Follow-ups and bot replies do not pollute the main DM.
- Summary can use `conversations.replies(channel, thread_ts)` for that issue only.

### 22. Can a Slack bot clear all chat history in a DM or group DM?

Expected answer:
- No. Slack API does not let a bot delete arbitrary user messages.
- A bot can delete messages it posted if token/scopes permit.
- User messages/files remain unless deleted by the user or workspace retention policies.

### 23. What should happen if Slack `chat.postMessage` fails?

Expected answer:
- Log the Slack error.
- Decide whether to retry based on failure type.
- Do not lose session state silently.
- For async processing, failed messages may retry through SQS if exception is raised.

## AI, Lex, And Bedrock

### 24. When should Lex be used versus Bedrock/Claude?

Expected answer:
- Lex is good for known intents, FAQ-like flows, and structured routing.
- Bedrock/Claude is useful for fallback, summarization, and natural language reasoning.
- Use guardrails: do not let LLM claim a Jira ticket was created unless the Jira API succeeded.

### 25. What should an AI summarizer do when Bedrock fails?

Expected answer:
- Do not fail the whole close/session flow.
- Store deterministic fallback summary.
- Record `ai_summary_error`.
- Keep audit data available in S3/DynamoDB.

### 26. How would you prevent hallucinated support actions in an LLM response?

Expected answer:
- Provide explicit system instructions.
- Include only trusted context.
- Enforce post-processing or source-of-truth checks.
- Separate "suggested next action" from "completed action."
- Never let the model invent ticket keys or claim API actions succeeded.

### 27. What is a safe prompt for summarizing a support conversation?

Expected answer:
- Ask for 1-3 concise sentences.
- Require only facts from transcript.
- Include session metadata such as Jira ticket key if present.
- Explicitly prohibit invented facts or implementation details.

## Image And Search Flow

### 28. How can AWS Rekognition help in screenshot-based support?

Expected answer:
- Detect text in screenshots.
- Detect labels/visual context.
- Convert image information into a text query for Lex/KB/LLM fallback.

### 29. Why might vector search be useful for screenshots?

Expected answer:
- Screenshots with similar UI/error states can be matched to known issues.
- Embeddings can retrieve similar image records even when OCR text differs.
- A confidence threshold prevents weak matches from being treated as truth.

### 30. How should the system behave if image analysis fails?

Expected answer:
- Return a safe user-facing fallback.
- Store error status/code in session.
- Do not crash the worker or leave the user without a response.
- Log enough metadata for debugging.

## Jira And External APIs

### 31. What should the Jira creation Lambda validate before sending a request?

Expected answer:
- Required env vars and secret fields exist.
- Project key and issue type are configured.
- Payload fields are valid and length-limited.
- Labels are sanitized.

### 32. Why should Jira errors be mapped to user-safe messages?

Expected answer:
- Raw API errors can expose implementation details.
- User should know whether it is config, permission, network, rate limit, or temporary failure.
- Logs can keep detailed body/status for operators.

### 33. How should rate limits be handled for Slack, Jira, or AI APIs?

Expected answer:
- Respect HTTP 429 and retry-after when available.
- Use exponential backoff where appropriate.
- Avoid aggressive retries from multiple layers.
- Add alarms and metrics for rate-limit frequency.

## Observability And Operations

### 34. What structured logs would you add to this system?

Expected answer:
- `event_id`, `session_id`, `thread_ts`, `user`, `channel`, `response_source`, `lex_intent`, status fields, error codes, downstream latency.
- Avoid sensitive values.
- Use consistent `message` keys for searchable events.

### 35. What CloudWatch alarms would you configure?

Expected answer:
- Lambda errors/throttles/duration.
- SQS age of oldest message and DLQ depth.
- DynamoDB throttles.
- Scheduler invocation failures.
- External API failure/rate-limit spikes.
- Bedrock/Jira/Slack integration failures.

### 36. How would you debug "user clicked button but nothing happened"?

Expected answer:
- Check Slack interactivity endpoint logs.
- Confirm request signature and payload parse.
- Check dedup table for duplicate action.
- Check SQS message enqueue.
- Check worker logs by interactive action id/session id.
- Check DynamoDB conditional update conflicts.
- Check Slack `chat.postMessage` response.

### 37. How would you debug "timeout closed an active conversation"?

Expected answer:
- Inspect session `timeout_token`, `timeout_due_at`, and latest activity.
- Check whether worker refreshed schedule/token after user reply.
- Verify timeout handler validates token and state.
- Check clock/timezone handling.

## Architecture Scenario Questions

### 38. Design the high-level architecture for a Slack IT support bot on AWS.

Expected answer:
- Slack Events API to API Gateway/Lambda handler.
- Handler verifies signature, deduplicates, and enqueues SQS.
- Worker Lambda handles Lex/AI/Jira/image flows.
- DynamoDB stores sessions and locks.
- EventBridge Scheduler handles inactivity.
- S3 stores audit logs/images.
- Secrets Manager stores tokens.
- CloudWatch logs/alarms monitor operations.

### 39. A user sends three messages quickly. How do you keep session state correct?

Expected answer:
- Use per-session key and update order carefully.
- Use event timestamps if ordering matters.
- Consider SQS FIFO if strict ordering is required.
- Use conditional writes for critical transitions.
- Make responses idempotent.

### 40. How would you reduce cost without hurting reliability?

Expected answer:
- Keep Lambda memory/timeouts tuned.
- Use SQS batching with partial failure.
- Store raw large payloads in S3, not DynamoDB.
- Avoid unnecessary Bedrock calls; use deterministic routing first.
- Apply retention/TTL policies.

## Practical Exercise

Prompt for candidate:

"A user sends a Slack DM with a screenshot. The bot replies correctly, then 15 minutes later should summarize the thread and allow the user to close the session. Sketch the data flow and failure handling."

Strong answer should include:
- Slack handler receives message and sends SQS record with `thread_ts`.
- Worker downloads/analyzes image or calls image Lambda.
- Worker routes through Lex/KB/LLM fallback.
- Worker stores session in DynamoDB with `session_id=channel:user:thread_ts`.
- Worker schedules timeout with EventBridge Scheduler and token.
- Timeout handler validates token.
- Summarizer fetches thread transcript with `conversations.replies`.
- Bedrock generates summary; fallback deterministic summary if Bedrock fails.
- Summary/audit stored in DynamoDB/S3.
- Close button marks session closed.
- Stale schedules/actions are ignored.

## Candidate Red Flags

- Uses broad IAM such as `Action: *`, `Resource: *` without justification.
- Does not mention Slack retries, duplicate events, or fast acknowledgements.
- Does not understand DynamoDB conditional writes.
- Assumes Slack bot can delete all user messages.
- Lets LLM perform side effects directly without API confirmation.
- Ignores observability and DLQs.
- Does not design around external API failures.

## Suggested Interview Structure

- 10 minutes: architecture discussion.
- 15 minutes: AWS service questions.
- 15 minutes: failure-mode scenarios.
- 10 minutes: security/IAM/secrets.
- 10 minutes: practical design exercise.
- Optional 30 minutes: coding exercise to implement a small idempotent Lambda handler or DynamoDB conditional update.
