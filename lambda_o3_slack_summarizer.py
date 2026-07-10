import json
import os
import time
import hashlib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

import boto3
from botocore.exceptions import ClientError

AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

# Shared AWS clients used by the Lambda invocation.
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
s3_client = boto3.client("s3", region_name=AWS_REGION)
bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
scheduler = boto3.client("scheduler", region_name=AWS_REGION)
lambda_client = boto3.client("lambda", region_name=AWS_REGION)

# Runtime configuration. Most values can be changed from Lambda environment
# variables without redeploying code.
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "o3_slack_sessions")
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "86400"))
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_API_TIMEOUT_SECONDS = int(os.environ.get("SLACK_API_TIMEOUT_SECONDS", "10"))
SUMMARY_HISTORY_LOOKBACK_SECONDS = int(os.environ.get("SUMMARY_HISTORY_LOOKBACK_SECONDS", "7200"))
SUMMARY_HISTORY_LIMIT = int(os.environ.get("SUMMARY_HISTORY_LIMIT", "100"))
SUMMARY_WEBHOOK_URL = os.environ.get("SUMMARY_WEBHOOK_URL") or os.environ.get("POWER_AUTOMATE_URL")
SUMMARY_WEBHOOK_TIMEOUT_SECONDS = int(os.environ.get("SUMMARY_WEBHOOK_TIMEOUT_SECONDS", "15"))
AUDIT_S3_BUCKET = os.environ.get("AUDIT_S3_BUCKET")
AUDIT_S3_PREFIX = os.environ.get("AUDIT_S3_PREFIX", "slack-audit")
CREATE_JIRA_TICKET_FUNCTION = os.environ.get("CREATE_JIRA_TICKET_FUNCTION")
CREATE_SUMMARY_JIRA_TICKET = os.environ.get("CREATE_SUMMARY_JIRA_TICKET", "true").lower() == "true"
TIMEOUT_SCHEDULING_ENABLED = os.environ.get("TIMEOUT_SCHEDULING_ENABLED", "true").lower() == "true"
SCHEDULER_GROUP_NAME = os.environ.get("SCHEDULER_GROUP_NAME", "default")
SCHEDULER_NAME_PREFIX = os.environ.get("SCHEDULER_NAME_PREFIX", "o3-slack-timeout")
SEND_CLOSE_NOTIFICATION = os.environ.get("SEND_CLOSE_NOTIFICATION", "true").lower() == "true"
SEND_FEEDBACK_PROMPT = os.environ.get("SEND_FEEDBACK_PROMPT", "true").lower() == "true"
ENABLE_AI_SUMMARY = os.environ.get("ENABLE_AI_SUMMARY", "true").lower() == "true"
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-2-lite-v1:0")
AI_SUMMARY_MAX_TOKENS = int(os.environ.get("AI_SUMMARY_MAX_TOKENS", "220"))
AI_SUMMARY_TEMPERATURE = float(os.environ.get("AI_SUMMARY_TEMPERATURE", "0.1"))
CLOSE_NOTIFICATION_TEXT = os.environ.get(
    "CLOSE_NOTIFICATION_TEXT",
    "This session has been closed."
)
FEEDBACK_PROMPT_TEXT = os.environ.get(
    "FEEDBACK_PROMPT_TEXT",
    "How was your experience with IVY Assist today?"
)
SUMMARY_SAVE_FAILED_TEXT = os.environ.get(
    "SUMMARY_SAVE_FAILED_TEXT",
    "I could not complete the summary/save step. This session has not been fully closed."
)

SESSION_STATE_SUMMARIZING = "SUMMARIZING"
SESSION_STATE_CLOSED = "CLOSED"
SESSION_STATE_FAILED = "FAILED"

sessions_table = dynamodb.Table(DYNAMODB_TABLE)

# Bot/system messages that should not appear in the final audit transcript.
EXCLUDED_MESSAGE_MARKERS = (
    "I haven't heard from you",
    "Summarizing conversation and closing thread",
    "No response received",
    "Are you still there? I will close this conversation if I do not hear back soon.",
)

PLACEHOLDER_TEXTS = {
    "Response",
    "Processing your request",
}


# Basic formatting helpers used across logging, timestamps, TTLs, and nullable
# text values.
def log_json(data):
    print(json.dumps(data, default=str))


def to_iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)

    except ValueError:
        return None


def ttl_epoch():
    return int(time.time()) + SESSION_TTL_SECONDS


def timeout_schedule_name(session_id, phase):
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    suffix = f"-{phase}"
    max_prefix_length = 64 - len(digest) - len(suffix) - 1
    prefix = SCHEDULER_NAME_PREFIX[:max_prefix_length]
    return f"{prefix}-{digest}{suffix}"


def delete_timeout_schedule(session_id, phase):
    if not TIMEOUT_SCHEDULING_ENABLED or not session_id:
        return

    name = timeout_schedule_name(session_id, phase)

    try:
        scheduler.delete_schedule(Name=name, GroupName=SCHEDULER_GROUP_NAME)
        log_json({
            "level": "INFO",
            "message": "summary_timeout_schedule_deleted",
            "session_id": session_id,
            "schedule_name": name,
        })
    except ClientError as error:
        if error.response["Error"]["Code"] == "ResourceNotFoundException":
            return

        log_json({
            "level": "ERROR",
            "message": "summary_timeout_schedule_delete_failed",
            "session_id": session_id,
            "schedule_name": name,
            "error": str(error),
        })


def text_or_empty(value):
    if value is None:
        return ""

    return str(value).strip()


def slack_api(method, params=None, payload=None, http_method=None):
    # Small Slack Web API wrapper. It supports query-string GET calls and JSON
    # POST calls, then raises when Slack returns ok=false.
    params = params or {}
    url = f"https://slack.com/api/{method}"

    data = None
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    }

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        http_method = http_method or "POST"
    else:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        http_method = http_method or "GET"

    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=http_method,
    )

    with urllib.request.urlopen(request, timeout=SLACK_API_TIMEOUT_SECONDS) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise ValueError(f"Slack API {method} failed: {result.get('error')}")

    return result


def get_session(session_id):
    # Load the tracked Slack session from DynamoDB before creating the summary.
    if not session_id:
        raise ValueError("Missing session_id")

    response = sessions_table.get_item(Key={"session_id": session_id})
    return response.get("Item")


def mark_summary_started(session_id, timeout_token, started_at):
    # Mark the summary as in-progress. When a timeout token is supplied, it must
    # still match the stored token so stale timeout events cannot close a newer
    # session.
    expression_values = {
        ":started": "started",
        ":summarizing": "summarizing",
        ":session_state": SESSION_STATE_SUMMARIZING,
        ":active": "active",
        ":failed": "failed",
        ":started_at": to_iso(started_at),
        ":ttl": ttl_epoch(),
    }
    condition = """
        attribute_exists(session_id)
        AND (
            attribute_not_exists(summary_status)
            OR summary_status = :summarizing
            OR summary_status = :failed
        )
        AND (
            attribute_not_exists(conversation_status)
            OR conversation_status = :active
            OR conversation_status = :summarizing
            OR conversation_status = :closed
        )
    """

    if timeout_token:
        condition += " AND timeout_token = :timeout_token"
        expression_values[":timeout_token"] = timeout_token

    expression_values[":closed"] = "closed"

    sessions_table.update_item(
        Key={"session_id": session_id},
        UpdateExpression="""
            SET
                summary_status = :started,
                conversation_status = :summarizing,
                session_state = :session_state,
                timeout_status = :summarizing,
                summary_started_at = :started_at,
                updated_at = :started_at,
                #ttl = :ttl
            REMOVE summary_error, summary_error_code
        """,
        ConditionExpression=condition,
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues=expression_values,
    )


def mark_summary_completed(
    session_id,
    completed_at,
    summary,
    audit_s3_key=None,
    webhook_sent=False,
    webhook_error=None,
    audit_error=None,
    summary_jira_result=None,
):
    # Store the final summary and delivery/audit status back on the session row.
    update_expression = """
        SET
            summary_status = :completed,
            conversation_status = :closed,
            session_state = :session_state,
            timeout_status = :closed,
            summary_completed_at = :completed_at,
            conversation_summary = :summary,
            summary_webhook_sent = :webhook_sent,
            updated_at = :completed_at,
            #ttl = :ttl
    """
    expression_values = {
        ":completed": "completed",
        ":closed": "closed",
        ":session_state": SESSION_STATE_CLOSED,
        ":completed_at": to_iso(completed_at),
        ":summary": summary,
        ":webhook_sent": webhook_sent,
        ":ttl": ttl_epoch(),
    }

    if audit_s3_key:
        update_expression += ", summary_audit_s3_key = :audit_s3_key"
        expression_values[":audit_s3_key"] = audit_s3_key

    if webhook_error:
        update_expression += ", summary_webhook_error = :webhook_error"
        expression_values[":webhook_error"] = text_or_empty(webhook_error)[:1000]

    if audit_error:
        update_expression += ", summary_audit_error = :audit_error"
        expression_values[":audit_error"] = text_or_empty(audit_error)[:1000]

    if summary_jira_result:
        update_expression += """
            ,
            summary_jira_status = :summary_jira_status,
            summary_jira_created_at = :summary_jira_created_at
        """
        expression_values[":summary_jira_status"] = (
            "created" if summary_jira_result.get("ok") else "create_failed"
        )
        expression_values[":summary_jira_created_at"] = to_iso(completed_at)

        if summary_jira_result.get("ticket_key"):
            update_expression += """
                ,
                summary_jira_ticket_key = :summary_jira_ticket_key,
                summary_jira_ticket_url = :summary_jira_ticket_url
            """
            expression_values[":summary_jira_ticket_key"] = summary_jira_result.get("ticket_key")
            expression_values[":summary_jira_ticket_url"] = summary_jira_result.get("ticket_url") or ""

        if summary_jira_result.get("error"):
            update_expression += ", summary_jira_error = :summary_jira_error"
            expression_values[":summary_jira_error"] = text_or_empty(summary_jira_result.get("error"))[:1000]

        if summary_jira_result.get("error_code"):
            update_expression += ", summary_jira_error_code = :summary_jira_error_code"
            expression_values[":summary_jira_error_code"] = text_or_empty(summary_jira_result.get("error_code"))[:200]

    sessions_table.update_item(
        Key={"session_id": session_id},
        UpdateExpression=update_expression,
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues=expression_values,
    )


def mark_summary_failed(session_id, failed_at, error, error_code):
    # Best-effort failure marker. If this write also fails, log it and preserve
    # the original error path.
    try:
        sessions_table.update_item(
            Key={"session_id": session_id},
            UpdateExpression="""
                SET
                    summary_status = :failed,
                    conversation_status = :failed_status,
                    session_state = :session_state,
                    timeout_status = :failed_status,
                    summary_failed_at = :failed_at,
                    summary_error = :error,
                    summary_error_code = :error_code,
                    updated_at = :failed_at,
                    #ttl = :ttl
            """,
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":failed": "failed",
                ":failed_status": "failed",
                ":session_state": SESSION_STATE_FAILED,
                ":failed_at": to_iso(failed_at),
                ":error": text_or_empty(error)[:1000],
                ":error_code": error_code,
                ":ttl": ttl_epoch(),
            },
        )
    except Exception as update_error:
        log_json({
            "level": "ERROR",
            "message": "summary_failure_mark_failed",
            "session_id": session_id,
            "error": str(update_error),
        })


def extract_rich_text(elements):
    # Slack rich_text blocks are nested; walk the structure and collect visible
    # text nodes.
    parts = []

    for element in elements or []:
        if element.get("type") == "text":
            parts.append(element.get("text", ""))
        if isinstance(element.get("elements"), list):
            parts.append(extract_rich_text(element["elements"]))

    return "".join(parts)


def extract_block_text(blocks):
    # Pull useful text out of Slack Block Kit messages, including sections,
    # context blocks, fields, and rich_text content.
    block_texts = []

    for block in blocks or []:
        block_type = block.get("type")

        if block_type == "section":
            text = (block.get("text") or {}).get("text")
            if text:
                block_texts.append(text)

            for field in block.get("fields") or []:
                if field.get("text"):
                    block_texts.append(field["text"])

        elif block_type == "context":
            for element in block.get("elements") or []:
                if element.get("text"):
                    block_texts.append(element["text"])

        elif block_type == "rich_text":
            rich_text = extract_rich_text(block.get("elements") or [])
            if rich_text:
                block_texts.append(rich_text)

    return "\n".join(text for text in block_texts if text).strip()


def clean_slack_text(text):
    # Slack encodes a few HTML entities in message text.
    text = text_or_empty(text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return text


def message_sender(message, session_user=None):
    # Classify each message as User or Bot for the transcript.
    if message.get("bot_id") or message.get("subtype") == "bot_message":
        return "Bot"

    if session_user and message.get("user") == session_user:
        return "User"

    return "User" if message.get("user") else "Bot"


def clean_message(message, session_user=None):
    # Convert a raw Slack message into the compact transcript shape used by
    # summaries and audit logs. Empty, placeholder, and timeout/system messages
    # are removed.
    message_text = clean_slack_text(message.get("text"))
    block_text = clean_slack_text(extract_block_text(message.get("blocks")))

    if message_text in PLACEHOLDER_TEXTS:
        message_text = block_text or message_text
    elif block_text and block_text not in message_text:
        message_text = f"{message_text}\n{block_text}".strip()

    if not message_text and isinstance(message.get("attachments"), list):
        message_text = "\n".join(
            clean_slack_text(attachment.get("fallback"))
            for attachment in message["attachments"]
            if attachment.get("fallback")
        ).strip()

    if any(marker in message_text for marker in EXCLUDED_MESSAGE_MARKERS):
        return None

    if not message_text:
        return None

    return {
        "sender": message_sender(message, session_user),
        "text": message_text,
        "ts": message.get("ts"),
        "user": message.get("user"),
        "bot_id": message.get("bot_id"),
    }


def fetch_thread_history(channel, thread_ts):
    # Prefer thread replies when the session was handled in a Slack thread.
    history = slack_api(
        "conversations.replies",
        {
            "channel": channel,
            "ts": thread_ts,
            "limit": SUMMARY_HISTORY_LIMIT,
        },
    )
    return history, history.get("messages") or []


def fetch_channel_history(channel, session_item, closed_at):
    # Fall back to channel history around the session window when no thread
    # timestamp exists.
    session_start = parse_iso(session_item.get("created_at"))
    oldest_dt = session_start or (closed_at - timedelta(seconds=SUMMARY_HISTORY_LOOKBACK_SECONDS))
    latest_dt = closed_at + timedelta(seconds=60)

    history = slack_api(
        "conversations.history",
        {
            "channel": channel,
            "oldest": f"{oldest_dt.timestamp():.6f}",
            "latest": f"{latest_dt.timestamp():.6f}",
            "inclusive": "true",
            "limit": SUMMARY_HISTORY_LIMIT,
        },
    )
    messages = list(reversed(history.get("messages") or []))
    return history, messages


def fetch_history(session_item, closed_at):
    # Choose the right Slack history API based on whether the session has a
    # thread timestamp.
    channel = session_item.get("channel")
    if not channel:
        raise ValueError("Session is missing Slack channel")

    thread_ts = (
        session_item.get("thread_ts")
        or session_item.get("slack_thread_ts")
        or session_item.get("last_thread_ts")
    )

    if thread_ts:
        return fetch_thread_history(channel, thread_ts)

    return fetch_channel_history(channel, session_item, closed_at)


def clean_history(messages, session_user):
    # Clean every Slack message and drop anything that should not be audited.
    cleaned = []

    for message in messages:
        cleaned_message = clean_message(message, session_user)
        if cleaned_message:
            cleaned.append(cleaned_message)

    return cleaned


def clean_stored_history(session_item):
    cleaned = []

    for message in session_item.get("session_messages") or []:
        text = clean_slack_text(message.get("text"))
        if not text or any(marker in text for marker in EXCLUDED_MESSAGE_MARKERS):
            continue

        sender = message.get("sender")
        if sender not in {"User", "Bot"}:
            sender = "Bot" if message.get("bot_id") else "User"

        cleaned.append({
            "sender": sender,
            "text": text,
            "ts": message.get("ts"),
            "user": message.get("user"),
            "bot_id": message.get("bot_id"),
        })

    return cleaned


def should_use_stored_history(session_item):
    conversation_type = session_item.get("conversation_type")
    metadata = session_item.get("conversation_metadata") or {}

    return (
        bool(session_item.get("session_messages"))
        and (
            conversation_type in {"im", "mpim"}
            or metadata.get("is_dm")
            or metadata.get("is_mpim")
        )
    )


def get_user_email(user_id):
    # Look up the Slack user's email for downstream reporting. Email lookup
    # failures are non-fatal because the transcript can still be summarized.
    if not user_id:
        return None

    try:
        response = slack_api("users.info", {"user": user_id})
        return ((response.get("user") or {}).get("profile") or {}).get("email")

    except Exception as error:
        log_json({
            "level": "WARN",
            "message": "summary_user_email_lookup_failed",
            "user": user_id,
            "error": str(error),
        })
        return None


def build_summary(cleaned_history, session_item):
    # Build a structured, deterministic summary from session metadata and the
    # cleaned transcript. This is still useful even if AI summarization fails.
    user_messages = [m["text"] for m in cleaned_history if m["sender"] == "User"]
    bot_messages = [m["text"] for m in cleaned_history if m["sender"] == "Bot"]
    original_request = user_messages[0] if user_messages else session_item.get("last_user_text") or ""
    last_user_message = user_messages[-1] if user_messages else ""
    last_bot_message = bot_messages[-1] if bot_messages else session_item.get("last_bot_reply") or ""

    return {
        "type": "slack_session_summary",
        "session_id": session_item.get("session_id"),
        "channel": session_item.get("channel"),
        "user": session_item.get("user"),
        "conversation_status": session_item.get("conversation_status"),
        "timeout_status": session_item.get("timeout_status"),
        "response_source": session_item.get("response_source"),
        "lex_intent": session_item.get("lex_intent"),
        "lex_state": session_item.get("lex_state"),
        "jira_status": session_item.get("jira_status"),
        "jira_ticket_key": session_item.get("jira_ticket_key") or session_item.get("last_jira_ticket_key"),
        "message_count": len(cleaned_history),
        "user_message_count": len(user_messages),
        "bot_message_count": len(bot_messages),
        "original_request": original_request,
        "last_user_message": last_user_message,
        "last_bot_message": last_bot_message,
    }


def compact_json(data):
    # Produce stable one-line JSON for prompt metadata.
    return json.dumps(data or {}, default=str, ensure_ascii=True, sort_keys=True)


def transcript_for_ai(cleaned_history, limit=12000):
    # Keep the newest transcript text if the conversation is too long for the
    # prompt budget.
    lines = [
        f"{message['sender']}: {message['text']}"
        for message in cleaned_history
    ]
    transcript = "\n".join(lines).strip()

    if len(transcript) <= limit:
        return transcript

    return transcript[-limit:].lstrip()


def extract_claude_text(response_body):
    # Anthropic responses return content blocks; collect the text blocks only.
    parts = []

    for item in response_body.get("content", []):
        if item.get("type") == "text":
            text = text_or_empty(item.get("text"))
            if text:
                parts.append(text)

    return "\n".join(parts).strip()


def extract_nova_text(response_body):
    # Amazon Nova responses return text under output.message.content blocks.
    parts = []

    content = (
        ((response_body.get("output") or {}).get("message") or {}).get("content")
        or []
    )
    for item in content:
        text = text_or_empty(item.get("text"))
        if text:
            parts.append(text)

    return "\n".join(parts).strip()


def is_anthropic_model(model_id):
    return str(model_id or "").startswith("anthropic.")


def build_bedrock_summary_body(prompt):
    if is_anthropic_model(BEDROCK_MODEL_ID):
        return {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": AI_SUMMARY_MAX_TOKENS,
            "temperature": AI_SUMMARY_TEMPERATURE,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt,
                        }
                    ],
                }
            ],
        }

    return {
        "messages": [
            {
                "role": "user",
                "content": [{"text": prompt}],
            }
        ],
        "inferenceConfig": {
            "maxTokens": AI_SUMMARY_MAX_TOKENS,
            "temperature": AI_SUMMARY_TEMPERATURE,
        },
    }


def extract_bedrock_summary_text(response_body):
    if is_anthropic_model(BEDROCK_MODEL_ID):
        return extract_claude_text(response_body)

    return extract_nova_text(response_body)


def bedrock_stop_reason(response_body):
    return (
        response_body.get("stop_reason")
        or response_body.get("stopReason")
    )


def build_ai_summary_prompt(cleaned_history, summary, session_item, closed_at, close_reason):
    # Construct the prompt that asks Bedrock/Claude for a concise internal audit
    # summary with strict grounding rules.
    close_reason_instruction = (
        "If close_reason is inactivity_timeout, state that the session closed after inactivity. "
        "If close_reason is manual_close_summary, state that the user closed the session manually."
    )

    return "\n".join([
        "Summarize this closed IVY Slack support session for an internal audit log.",
        "",
        "Requirements:",
        "- Write 1 to 3 concise sentences.",
        "- State what the user requested or reported.",
        "- State what IVY did or answered, including Jira ticket key if present.",
        f"- {close_reason_instruction}",
        "- Do not invent facts, ticket keys, user names, or resolution details.",
        "- Do not mention implementation details like DynamoDB, Lambda, Slack API, or tokens.",
        "",
        "Session metadata:",
        compact_json({
            "session_id": session_item.get("session_id"),
            "closed_at": to_iso(closed_at),
            "close_reason": close_reason,
            "conversation_status": summary.get("conversation_status"),
            "timeout_status": summary.get("timeout_status"),
            "response_source": summary.get("response_source"),
            "lex_intent": summary.get("lex_intent"),
            "lex_state": summary.get("lex_state"),
            "jira_status": summary.get("jira_status"),
            "jira_ticket_key": summary.get("jira_ticket_key"),
        }),
        "",
        "Cleaned transcript:",
        transcript_for_ai(cleaned_history),
        "",
        "Return only the final summary text."
    ])


def generate_ai_summary(cleaned_history, summary, session_item, closed_at, close_reason):
    # Optionally ask Bedrock for a short natural-language summary of the cleaned
    # conversation.
    if not ENABLE_AI_SUMMARY:
        return None

    if not cleaned_history:
        return None

    prompt = build_ai_summary_prompt(
        cleaned_history,
        summary,
        session_item,
        closed_at,
        close_reason,
    )
    body = build_bedrock_summary_body(prompt)

    response = bedrock.invoke_model(
        modelId=BEDROCK_MODEL_ID,
        body=json.dumps(body).encode("utf-8"),
        contentType="application/json",
        accept="application/json",
    )

    response_body = json.loads(response["body"].read().decode("utf-8"))
    ai_summary = extract_bedrock_summary_text(response_body)

    if not ai_summary:
        raise ValueError("Bedrock returned an empty AI summary")

    return {
        "text": ai_summary,
        "model_id": BEDROCK_MODEL_ID,
        "stop_reason": bedrock_stop_reason(response_body),
        "usage": response_body.get("usage", {}),
    }


def post_summary_webhook(payload):
    # Send the completed audit payload to Power Automate or another configured
    # webhook endpoint.
    if not SUMMARY_WEBHOOK_URL:
        return False, None

    request = urllib.request.Request(
        SUMMARY_WEBHOOK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=SUMMARY_WEBHOOK_TIMEOUT_SECONDS) as response:
        if response.status < 200 or response.status >= 300:
            raise ValueError(f"Summary webhook returned HTTP {response.status}")

        response_text = response.read().decode("utf-8").strip()

    if not response_text:
        return True, None

    try:
        return True, json.loads(response_text)
    except ValueError:
        return True, {"summaryText": response_text}


def summary_ticket_request_id(session_id, closed_at):
    source = f"{session_id}:{to_iso(closed_at)}"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]
    return f"summary-{digest}"


def summary_text_for_jira(summary):
    return (
        text_or_empty(summary.get("ai_summary"))
        or text_or_empty(summary.get("last_user_message"))
        or text_or_empty(summary.get("original_request"))
        or "Closed IVY Slack support session summary."
    )


def build_summary_jira_payload(session_item, session_id, payload, summary, closed_at):
    conversation_text = "\n".join(payload.get("conversation") or [])
    summary_text = summary_text_for_jira(summary)
    original_request = text_or_empty(summary.get("original_request")) or summary_text

    return {
        "jira_request_id": summary_ticket_request_id(session_id, closed_at),
        "jira_requested_at": to_iso(closed_at),
        "jira_confirmed_at": to_iso(closed_at),
        "event_id": f"summary:{session_id}:{to_iso(closed_at)}",
        "session_id": session_id,
        "session_root_ts": session_item.get("session_root_ts") or session_item.get("thread_ts"),
        "channel": session_item.get("channel"),
        "user": session_item.get("user"),
        "text": original_request,
        "raw_text": original_request,
        "conversation_summary": summary_text,
        "conversation_text": conversation_text,
        "confirmation_text": "summary_close",
        "lex": {
            "intent": "SessionSummary",
            "state": "Closed",
            "slots": {},
        },
        "slack": {
            "channel": session_item.get("channel"),
            "user": session_item.get("user"),
            "event_ts": session_item.get("last_ts"),
            "thread_ts": session_item.get("thread_ts"),
        },
        "summary": summary,
        "summary_payload_type": payload.get("type"),
        "summary_close_reason": payload.get("reason"),
        "live_agent": {
            "status": session_item.get("live_agent_status"),
            "ticket_key": session_item.get("live_agent_ticket_key") or session_item.get("last_live_agent_ticket_key"),
            "ticket_url": session_item.get("live_agent_ticket_url") or session_item.get("last_live_agent_ticket_url"),
        },
    }


def invoke_create_jira_ticket(payload):
    if not CREATE_SUMMARY_JIRA_TICKET:
        return {
            "ok": False,
            "skipped": True,
            "reason": "disabled",
        }

    if not CREATE_JIRA_TICKET_FUNCTION:
        return {
            "ok": False,
            "skipped": True,
            "reason": "missing_create_jira_ticket_function",
            "error": "missing_create_jira_ticket_function",
            "error_code": "missing_create_jira_ticket_function",
        }

    response = lambda_client.invoke(
        FunctionName=CREATE_JIRA_TICKET_FUNCTION,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    raw_payload = response.get("Payload").read().decode("utf-8") if response.get("Payload") else ""

    if response.get("FunctionError"):
        return {
            "ok": False,
            "error": raw_payload or response.get("FunctionError"),
            "error_code": "jira_lambda_function_error",
        }

    if not raw_payload:
        return {
            "ok": False,
            "error": "empty_create_jira_response",
            "error_code": "invalid_create_jira_response",
        }

    try:
        parsed = json.loads(raw_payload)
    except ValueError:
        return {
            "ok": False,
            "error": "invalid_create_jira_response",
            "error_code": "invalid_create_jira_response",
            "raw_response": raw_payload,
        }

    return parsed if isinstance(parsed, dict) else {
        "ok": False,
        "error": "invalid_create_jira_response",
        "error_code": "invalid_create_jira_response",
    }


def put_audit_log(payload, closed_at):
    # Persist the full audit payload to S3 when an audit bucket is configured.
    if not AUDIT_S3_BUCKET:
        return None

    safe_session_id = "".join(
        char if char.isalnum() or char in "-_" else "_"
        for char in text_or_empty(payload.get("session_id"))[:120]
    ) or "unknown-session"
    date_key = to_iso(closed_at).split("T", 1)[0]
    key = f"{AUDIT_S3_PREFIX.rstrip('/')}/{date_key}/{safe_session_id}.json"

    s3_client.put_object(
        Bucket=AUDIT_S3_BUCKET,
        Key=key,
        Body=json.dumps(payload, indent=2, default=str).encode("utf-8"),
        ContentType="application/json",
    )

    return key


def structured_summary_text(webhook_response, summary):
    if isinstance(webhook_response, dict):
        for key in ("summaryText", "summary_text", "summary", "message"):
            value = webhook_response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        issue = webhook_response.get("issue")
        resolution = webhook_response.get("resolution")
        next_step = webhook_response.get("nextStep") or webhook_response.get("next_step")
        parts = []
        if issue:
            parts.append(f"* Issue: {issue}")
        if resolution:
            parts.append(f"* Resolution: {resolution}")
        if next_step:
            parts.append(f"* Next step: {next_step}")
        if parts:
            return "\n".join(parts)

    return summary.get("ai_summary")


def summary_jira_status_text(summary_jira_result):
    if summary_jira_result is None:
        if CREATE_SUMMARY_JIRA_TICKET:
            return "Jira follow-up ticket: not attempted."
        return "Jira follow-up ticket: disabled."

    if summary_jira_result.get("ok"):
        ticket_key = text_or_empty(summary_jira_result.get("ticket_key"))
        ticket_url = text_or_empty(summary_jira_result.get("ticket_url"))
        if ticket_key and ticket_url:
            return f"Jira follow-up ticket created: {ticket_key} {ticket_url}"
        if ticket_key:
            return f"Jira follow-up ticket created: {ticket_key}"
        return "Jira follow-up ticket created."

    if summary_jira_result.get("skipped"):
        reason = text_or_empty(summary_jira_result.get("reason") or summary_jira_result.get("error_code"))
        return f"Jira follow-up ticket skipped: {reason or 'not configured'}."

    error = text_or_empty(
        summary_jira_result.get("error")
        or summary_jira_result.get("error_code")
        or "unknown error"
    )
    return f"Jira follow-up ticket creation failed: {error}"


def final_close_message(webhook_response, summary, summary_jira_result=None):
    summary_text = structured_summary_text(webhook_response, summary)
    jira_status = summary_jira_status_text(summary_jira_result)

    if summary_text:
        return "\n".join([
            "This session is now closed.",
            "",
            "Summary:",
            summary_text,
            "",
            jira_status,
            "",
            "Saved successfully for follow-up.",
        ])

    return "\n".join([
        "This session is now closed.",
        "",
        jira_status,
        "",
        "Summary saved successfully for follow-up.",
    ])


def send_close_notification(channel, text):
    # Send the final close message after audit persistence has succeeded.
    if not SEND_CLOSE_NOTIFICATION or not channel:
        return

    slack_api(
        "chat.postMessage",
        payload={
            "channel": channel,
            "text": text or CLOSE_NOTIFICATION_TEXT,
        },
    )


def send_feedback_prompt(channel):
    # Optional Slack feedback prompt with 1-5 star buttons.
    if not SEND_FEEDBACK_PROMPT or not channel:
        return

    slack_api(
        "chat.postMessage",
        payload={
            "channel": channel,
            "text": FEEDBACK_PROMPT_TEXT,
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": FEEDBACK_PROMPT_TEXT,
                    },
                },
                {
                    "type": "actions",
                    "block_id": "feedback_stars",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": f"{rating} star" if rating == 1 else f"{rating} stars", "emoji": True},
                            "action_id": f"feedback_{rating}",
                            "value": str(rating),
                        }
                        for rating in (1, 2, 3, 4, 5)
                    ],
                },
            ],
        },
    )


def notify_summary_failed(channel):
    if not channel:
        return

    slack_api(
        "chat.postMessage",
        payload={
            "channel": channel,
            "text": SUMMARY_SAVE_FAILED_TEXT,
        },
    )


def lambda_handler(event, context):
    # Main Lambda entry point. It loads the session, fetches Slack history,
    # builds summaries, delivers audit outputs, updates DynamoDB, and returns a
    # compact status object to the caller.
    session_id = event.get("session_id")
    timeout_token = event.get("timeout_token")
    started_at = datetime.now(timezone.utc).replace(microsecond=0)
    closed_at = parse_iso(event.get("closed_at")) or started_at

    log_json({
        "level": "INFO",
        "message": "summary_received",
        "session_id": session_id,
        "reason": event.get("reason"),
    })

    try:
        # Ignore events for missing sessions rather than failing the Lambda.
        session_item = get_session(session_id)
        if not session_item:
            log_json({
                "level": "INFO",
                "message": "summary_ignored",
                "session_id": session_id,
                "reason": "session_not_found",
            })
            return {
                "ok": False,
                "ignored": True,
                "reason": "session_not_found",
                "session_id": session_id,
            }

        mark_summary_started(session_id, timeout_token, started_at)

        # Fetch, clean, and summarize the Slack conversation. DM and group-DM
        # sessions use the transcript stored during active conversation so they
        # do not depend on Slack thread replies.
        if should_use_stored_history(session_item):
            raw_history = {
                "source": "dynamodb_session_messages",
                "message_count": len(session_item.get("session_messages") or []),
            }
            cleaned_history = clean_stored_history(session_item)
        else:
            raw_history, messages = fetch_history(session_item, closed_at)
            cleaned_history = clean_history(messages, session_item.get("user"))

        user_email = get_user_email(session_item.get("user"))
        summary = build_summary(cleaned_history, {**session_item, "session_id": session_id})
        ai_summary_error = None

        # AI summary is optional; failures are captured in the payload but do not
        # stop webhook/S3 audit delivery.
        try:
            ai_summary = generate_ai_summary(
                cleaned_history,
                summary,
                {**session_item, "session_id": session_id},
                closed_at,
                event.get("reason"),
            )
            if ai_summary:
                summary["ai_summary"] = ai_summary["text"]
                summary["ai_summary_model_id"] = ai_summary["model_id"]
                summary["ai_summary_stop_reason"] = ai_summary.get("stop_reason")
                summary["ai_summary_usage"] = ai_summary.get("usage", {})
        except Exception as error:
            ai_summary_error = str(error)
            summary["ai_summary_error"] = ai_summary_error[:1000]
            log_json({
                "level": "ERROR",
                "message": "summary_ai_generation_failed",
                "session_id": session_id,
                "model_id": BEDROCK_MODEL_ID,
                "error": ai_summary_error,
            })

        # Payload contains both human-readable conversation lines and structured
        # message objects for downstream consumers.
        payload = {
            "type": "log",
            "session_id": session_id,
            "timeout_token": timeout_token,
            "closed_at": to_iso(closed_at),
            "reason": event.get("reason"),
            "channel": session_item.get("channel"),
            "channelInfo": {
                "conversationType": session_item.get("conversation_type") or event.get("conversation_type"),
                "conversationMetadata": session_item.get("conversation_metadata") or {},
            },
            "user": session_item.get("user"),
            "userEmail": user_email,
            "threadTs": session_item.get("thread_ts"),
            "sessionState": session_item.get("session_state"),
            "conversation": [
                f"{message['sender']}: {message['text']}"
                for message in cleaned_history
            ],
            "cleanConversation": cleaned_history,
            "summary": summary,
            "aiSummary": summary.get("ai_summary"),
            "aiSummaryError": ai_summary_error,
            "rawSlackHistory": raw_history,
        }

        # Webhook delivery is best-effort and recorded on the DynamoDB session.
        webhook_sent = False
        webhook_response = None
        webhook_error = None
        try:
            webhook_sent, webhook_response = post_summary_webhook(payload)
            if webhook_response is not None:
                payload["summaryWebhookResponse"] = webhook_response
        except Exception as error:
            webhook_error = str(error)
            log_json({
                "level": "ERROR",
                "message": "summary_webhook_failed",
                "session_id": session_id,
                "error": webhook_error,
            })

        # S3 audit persistence is also best-effort; failures do not block session
        # completion.
        audit_s3_key = None
        audit_error = None
        try:
            audit_s3_key = put_audit_log(payload, closed_at)
        except Exception as error:
            audit_error = str(error)
            log_json({
                "level": "ERROR",
                "message": "summary_audit_s3_failed",
                "session_id": session_id,
                "error": audit_error,
            })

        summary_jira_result = None
        if CREATE_SUMMARY_JIRA_TICKET and not session_item.get("summary_jira_ticket_key"):
            try:
                summary_jira_payload = build_summary_jira_payload(
                    session_item,
                    session_id,
                    payload,
                    summary,
                    closed_at,
                )
                summary_jira_result = invoke_create_jira_ticket(summary_jira_payload)
                payload["summaryJiraResult"] = summary_jira_result
                log_json({
                    "level": "INFO" if summary_jira_result.get("ok") else "ERROR",
                    "message": "summary_jira_create_completed",
                    "session_id": session_id,
                    "ok": summary_jira_result.get("ok"),
                    "ticket_key": summary_jira_result.get("ticket_key"),
                    "error_code": summary_jira_result.get("error_code"),
                    "skipped": summary_jira_result.get("skipped", False),
                })
            except Exception as error:
                summary_jira_result = {
                    "ok": False,
                    "error": str(error),
                    "error_code": "summary_jira_create_failed",
                }
                payload["summaryJiraResult"] = summary_jira_result
                log_json({
                    "level": "ERROR",
                    "message": "summary_jira_create_failed",
                    "session_id": session_id,
                    "error": str(error),
                })

        if webhook_error or audit_error:
            failure_error = webhook_error or audit_error
            mark_summary_failed(
                session_id,
                datetime.now(timezone.utc).replace(microsecond=0),
                failure_error,
                "summary_save_failed",
            )
            try:
                notify_summary_failed(session_item.get("channel"))
            except Exception as error:
                log_json({
                    "level": "ERROR",
                    "message": "summary_failure_notification_failed",
                    "session_id": session_id,
                    "error": str(error),
                })
            return {
                "ok": False,
                "session_id": session_id,
                "error": "Summary save step failed.",
                "error_code": "summary_save_failed",
                "webhook_sent": webhook_sent,
                "webhook_error": webhook_error,
                "audit_s3_key": audit_s3_key,
                "audit_error": audit_error,
            }

        # Record the completed summary and all delivery outcomes.
        completed_at = datetime.now(timezone.utc).replace(microsecond=0)
        final_message = final_close_message(webhook_response, summary, summary_jira_result)
        mark_summary_completed(
            session_id,
            completed_at,
            summary,
            audit_s3_key=audit_s3_key,
            webhook_sent=webhook_sent,
            webhook_error=webhook_error,
            audit_error=audit_error,
            summary_jira_result=summary_jira_result,
        )
        delete_timeout_schedule(session_id, "prompt")
        delete_timeout_schedule(session_id, "close")

        try:
            send_close_notification(session_item.get("channel"), final_message)
            send_feedback_prompt(session_item.get("channel"))
        except Exception as error:
            log_json({
                "level": "ERROR",
                "message": "summary_optional_slack_message_failed",
                "session_id": session_id,
                "error": str(error),
            })

        result = {
            "ok": True,
            "session_id": session_id,
            "message_count": len(cleaned_history),
            "audit_s3_key": audit_s3_key,
            "webhook_sent": webhook_sent,
            "ai_summary_generated": bool(summary.get("ai_summary")),
            "summary_jira_ticket_key": (summary_jira_result or {}).get("ticket_key"),
            "summary_jira_ok": (summary_jira_result or {}).get("ok"),
        }

        log_json({
            "level": "INFO",
            "message": "summary_completed",
            **result,
        })
        return result

    except ClientError as error:
        # Conditional failures mean another event already handled the session or
        # the timeout token was stale.
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log_json({
                "level": "INFO",
                "message": "summary_ignored",
                "session_id": session_id,
                "reason": "stale_or_missing_session",
            })
            return {
                "ok": False,
                "ignored": True,
                "reason": "stale_or_missing_session",
                "session_id": session_id,
            }

        mark_summary_failed(session_id, datetime.now(timezone.utc).replace(microsecond=0), error, "aws_client_error")
        raise

    except (urllib.error.URLError, TimeoutError) as error:
        mark_summary_failed(session_id, datetime.now(timezone.utc).replace(microsecond=0), error, "network_error")
        log_json({
            "level": "ERROR",
            "message": "summary_network_error",
            "session_id": session_id,
            "error": str(error),
        })
        return {
            "ok": False,
            "error": "Network error while summarizing Slack session.",
            "error_code": "network_error",
        }

    except Exception as error:
        mark_summary_failed(session_id, datetime.now(timezone.utc).replace(microsecond=0), error, "summary_failed")
        log_json({
            "level": "ERROR",
            "message": "summary_failed",
            "session_id": session_id,
            "error": str(error),
        })
        return {
            "ok": False,
            "error": str(error),
            "error_code": "summary_failed",
        }
