import json
import os
import time
import hashlib
import boto3
import urllib.request
from datetime import datetime, timezone, timedelta
from botocore.exceptions import ClientError

AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

lex = boto3.client("lexv2-runtime", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
scheduler = boto3.client("scheduler", region_name=AWS_REGION)
lambda_client = boto3.client("lambda", region_name=AWS_REGION)

BOT_ID = os.environ["BOT_ID"]
BOT_ALIAS_ID = os.environ["BOT_ALIAS_ID"]
LOCALE_ID = os.environ.get("LOCALE_ID", "en_US")

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "o3_slack_sessions")
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "86400"))
INACTIVITY_TIMEOUT_SECONDS = int(os.environ.get("INACTIVITY_TIMEOUT_SECONDS", "900"))
TIMEOUT_SCHEDULING_ENABLED = os.environ.get("TIMEOUT_SCHEDULING_ENABLED", "true").lower() == "true"
TIMEOUT_HANDLER_ARN = os.environ.get("TIMEOUT_HANDLER_ARN")
SCHEDULER_ROLE_ARN = os.environ.get("SCHEDULER_ROLE_ARN")
SCHEDULER_GROUP_NAME = os.environ.get("SCHEDULER_GROUP_NAME", "default")
SCHEDULER_NAME_PREFIX = os.environ.get("SCHEDULER_NAME_PREFIX", "o3-slack-timeout")
ENABLE_CLAUDE_FALLBACK = os.environ.get("ENABLE_CLAUDE_FALLBACK", "false").lower() == "true"
CLAUDE_FALLBACK_FUNCTION = os.environ.get("CLAUDE_FALLBACK_FUNCTION")
CLAUDE_FALLBACK_INTENTS = {
    intent_name.strip()
    for intent_name in os.environ.get(
        "CLAUDE_FALLBACK_INTENTS",
        "FallbackIntent,AMAZON.FallbackIntent,FallbackToLLM"
    ).split(",")
    if intent_name.strip()
}
CLAUDE_FAILURE_REPLY = os.environ.get(
    "CLAUDE_FAILURE_REPLY",
    "I could not resolve this automatically. This should be moved to ticket creation once Jira is connected."
)
EMPTY_USER_TEXT_REPLY = os.environ.get("EMPTY_USER_TEXT_REPLY", "Hi, how can I help?")
EMPTY_LEX_REPLY = os.environ.get(
    "EMPTY_LEX_REPLY",
    "I could not generate a response for that. Please try rephrasing your message."
)

sessions_table = dynamodb.Table(DYNAMODB_TABLE)


def to_iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def ttl_epoch():
    return int(time.time()) + SESSION_TTL_SECONDS


def log_json(data):
    print(json.dumps(data, default=str))


def send_slack_message(channel, text):
    url = "https://slack.com/api/chat.postMessage"

    data = json.dumps({
        "channel": channel,
        "text": text
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}"
        },
        method="POST"
    )

    with urllib.request.urlopen(req, timeout=10) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise Exception(f"Slack API error: {result.get('error')}")

    return result


def simplify_slot(slot):
    if not slot:
        return None

    value = slot.get("value", {})
    return (
        value.get("interpretedValue")
        or value.get("originalValue")
        or value.get("resolvedValues", [None])[0]
    )


def simplify_slots(raw_slots):
    if not raw_slots:
        return {}

    return {
        slot_name: simplify_slot(slot_value)
        for slot_name, slot_value in raw_slots.items()
    }


def get_conversation_status(lex_state):
    if lex_state == "Fulfilled":
        return "closed"

    if lex_state == "Failed":
        return "failed"

    return "active"


def timeout_schedule_name(session_id, phase):
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    suffix = f"-{phase}"
    max_prefix_length = 64 - len(digest) - len(suffix) - 1
    prefix = SCHEDULER_NAME_PREFIX[:max_prefix_length]

    return f"{prefix}-{digest}{suffix}"


def timeout_token(session_id, event_id, activity_at):
    raw_token = f"{session_id}|{event_id or ''}|{activity_at}"
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()[:32]


def scheduler_at_expression(due_at):
    utc_due_at = due_at.astimezone(timezone.utc).replace(microsecond=0)
    return f"at({utc_due_at.strftime('%Y-%m-%dT%H:%M:%S')})"


def upsert_schedule(name, due_at, payload):
    request = {
        "GroupName": SCHEDULER_GROUP_NAME,
        "ScheduleExpression": scheduler_at_expression(due_at),
        "ScheduleExpressionTimezone": "UTC",
        "FlexibleTimeWindow": {
            "Mode": "OFF"
        },
        "Target": {
            "Arn": TIMEOUT_HANDLER_ARN,
            "RoleArn": SCHEDULER_ROLE_ARN,
            "Input": json.dumps(payload)
        },
        "State": "ENABLED",
        "ActionAfterCompletion": "DELETE"
    }

    try:
        scheduler.update_schedule(Name=name, **request)
        return "updated"

    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    scheduler.create_schedule(Name=name, **request)
    return "created"


def refresh_timeout_schedule(session_id, timeout_state):
    if not TIMEOUT_SCHEDULING_ENABLED:
        log_json({
            "level": "INFO",
            "message": "timeout_schedule_skipped",
            "session_id": session_id,
            "reason": "disabled"
        })
        return False

    if not TIMEOUT_HANDLER_ARN or not SCHEDULER_ROLE_ARN:
        log_json({
            "level": "WARN",
            "message": "timeout_schedule_skipped",
            "session_id": session_id,
            "reason": "missing_timeout_scheduler_env",
            "has_timeout_handler_arn": bool(TIMEOUT_HANDLER_ARN),
            "has_scheduler_role_arn": bool(SCHEDULER_ROLE_ARN)
        })
        return False

    payload = {
        "action": "prompt",
        "session_id": session_id,
        "timeout_token": timeout_state["timeout_token"],
        "timeout_due_at": timeout_state["timeout_due_at"]
    }

    try:
        action = upsert_schedule(
            timeout_state["timeout_schedule_name"],
            timeout_state["timeout_due_at_dt"],
            payload
        )

        log_json({
            "level": "INFO",
            "message": "timeout_schedule_refreshed",
            "session_id": session_id,
            "schedule_name": timeout_state["timeout_schedule_name"],
            "timeout_due_at": timeout_state["timeout_due_at"],
            "scheduler_action": action
        })
        return True

    except Exception as e:
        log_json({
            "level": "ERROR",
            "message": "timeout_schedule_refresh_failed",
            "session_id": session_id,
            "schedule_name": timeout_state["timeout_schedule_name"],
            "error": str(e)
        })
        return False


def delete_timeout_schedule(session_id, phase):
    if not TIMEOUT_SCHEDULING_ENABLED:
        return

    name = timeout_schedule_name(session_id, phase)

    try:
        scheduler.delete_schedule(Name=name, GroupName=SCHEDULER_GROUP_NAME)

        log_json({
            "level": "INFO",
            "message": "timeout_schedule_deleted",
            "session_id": session_id,
            "schedule_name": name
        })

    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return

        log_json({
            "level": "ERROR",
            "message": "timeout_schedule_delete_failed",
            "session_id": session_id,
            "schedule_name": name,
            "error": str(e)
        })


def should_use_claude_fallback(text, lex_intent, lex_state, lex_reply_empty):
    if not ENABLE_CLAUDE_FALLBACK or not text:
        return False

    return (
        lex_state == "Failed"
        or lex_reply_empty
        or lex_intent in CLAUDE_FALLBACK_INTENTS
    )


def invoke_claude_fallback(payload):
    if not CLAUDE_FALLBACK_FUNCTION:
        return {
            "ok": False,
            "error": "missing_claude_fallback_function"
        }

    response = lambda_client.invoke(
        FunctionName=CLAUDE_FALLBACK_FUNCTION,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8")
    )

    raw_payload = response.get("Payload").read().decode("utf-8")

    if response.get("FunctionError"):
        return {
            "ok": False,
            "error": raw_payload or response.get("FunctionError")
        }

    if not raw_payload:
        return {
            "ok": False,
            "error": "empty_claude_lambda_response"
        }

    try:
        return json.loads(raw_payload)

    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": "invalid_claude_lambda_response",
            "raw_response": raw_payload
        }


def get_lex_reply(messages):
    replies = []

    for message in messages or []:
        content = (message.get("content") or "").strip()

        if content:
            replies.append(content)

    if replies:
        return "\n".join(replies), False

    log_json({
        "level": "WARN",
        "message": "empty_lex_reply"
    })

    return EMPTY_LEX_REPLY, True


def process_record(record):
    body = json.loads(record["body"])

    event_id = body.get("event_id")
    text = (body.get("text") or "").strip()
    raw_text = body.get("raw_text", text)
    user = body.get("user", "unknown-user")
    channel = body.get("channel")
    ts = body.get("ts")
    event_type = body.get("event_type")
    channel_type = body.get("channel_type")
    routing_reason = body.get("routing_reason")

    session_id = f"{channel}:{user}"
    lex_session_id = session_id

    log_json({
        "level": "INFO",
        "message": "worker_processing_started",
        "event_id": event_id,
        "channel": channel,
        "event_type": event_type,
        "channel_type": channel_type,
        "routing_reason": routing_reason,
        "user": user,
        "text": text
    })

    if text:
        response = lex.recognize_text(
            botId=BOT_ID,
            botAliasId=BOT_ALIAS_ID,
            localeId=LOCALE_ID,
            sessionId=lex_session_id,
            text=text
        )

        session_state = response.get("sessionState", {})
        intent = session_state.get("intent", {})
        lex_session_attributes = session_state.get("sessionAttributes", {}) or {}

        lex_intent = intent.get("name", "UNKNOWN")
        lex_state = intent.get("state", "UNKNOWN")
        lex_slots = simplify_slots(intent.get("slots", {}))
        lex_reply, lex_reply_empty = get_lex_reply(response.get("messages", []))
    else:
        lex_intent = "EMPTY_MESSAGE"
        lex_state = "Ignored"
        lex_slots = {}
        lex_session_attributes = {}
        lex_reply = EMPTY_USER_TEXT_REPLY
        lex_reply_empty = False

    response_source = "lex"
    claude_fallback_attempted = False
    claude_fallback_error = None
    claude_model_id = None
    next_action = None
    jira_status = None

    if lex_session_attributes.get("response_source") == "router":
        response_source = "router"
        next_action = lex_session_attributes.get("next_action") or None
        jira_status = lex_session_attributes.get("jira_status") or None

        log_json({
            "level": "INFO",
            "message": "worker_router_metadata_received",
            "event_id": event_id,
            "session_id": session_id,
            "lex_intent": lex_intent,
            "next_action": next_action,
            "jira_status": jira_status
        })

    if response_source != "router" and should_use_claude_fallback(text, lex_intent, lex_state, lex_reply_empty):
        claude_fallback_attempted = True
        claude_payload = {
            "event_id": event_id,
            "session_id": session_id,
            "channel": channel,
            "channel_type": channel_type,
            "routing_reason": routing_reason,
            "user": user,
            "text": text,
            "raw_text": raw_text,
            "lex": {
                "intent": lex_intent,
                "state": lex_state,
                "slots": lex_slots,
                "reply": lex_reply
            },
            "session": {
                "conversation_status": get_conversation_status(lex_state)
            }
        }
        claude_result = invoke_claude_fallback(claude_payload)
        claude_model_id = claude_result.get("model_id")

        if claude_result.get("ok") and (claude_result.get("reply") or "").strip():
            lex_reply = claude_result["reply"].strip()
            response_source = "claude"

            log_json({
                "level": "INFO",
                "message": "claude_fallback_used",
                "event_id": event_id,
                "session_id": session_id,
                "lex_intent": lex_intent,
                "lex_state": lex_state,
                "model_id": claude_model_id
            })

        else:
            response_source = "claude_failed"
            claude_fallback_error = claude_result.get("error", "unknown_claude_error")
            next_action = "O3_CreateJiraTicket"
            jira_status = "deferred"
            lex_reply = CLAUDE_FAILURE_REPLY

            log_json({
                "level": "WARN",
                "message": "claude_fallback_failed_defer_jira",
                "event_id": event_id,
                "session_id": session_id,
                "lex_intent": lex_intent,
                "lex_state": lex_state,
                "error": claude_fallback_error
            })

    conversation_status = get_conversation_status(lex_state)
    if response_source in {"claude", "claude_failed"}:
        conversation_status = "active"

    activity_at_dt = datetime.now(timezone.utc).replace(microsecond=0)
    updated_at = to_iso(activity_at_dt)

    timeout_state = None
    if conversation_status == "active":
        timeout_due_at_dt = activity_at_dt + timedelta(seconds=INACTIVITY_TIMEOUT_SECONDS)
        timeout_due_at = to_iso(timeout_due_at_dt)
        timeout_state = {
            "timeout_due_at_dt": timeout_due_at_dt,
            "timeout_due_at": timeout_due_at,
            "timeout_token": timeout_token(session_id, event_id, updated_at),
            "timeout_schedule_name": timeout_schedule_name(session_id, "prompt")
        }

    update_expression = """
        SET
            #channel = :channel,
            #user = :user,
            last_event_id = :event_id,
            last_user_text = :last_user_text,
            last_raw_user_text = :last_raw_user_text,
            last_bot_reply = :last_bot_reply,
            response_source = :response_source,
            claude_fallback_attempted = :claude_fallback_attempted,
            last_ts = :last_ts,
            last_activity_at = :last_activity_at,
            event_type = :event_type,
            channel_type = :channel_type,
            routing_reason = :routing_reason,
            lex_session_id = :lex_session_id,
            lex_intent = :lex_intent,
            lex_state = :lex_state,
            lex_slots = :lex_slots,
            conversation_status = :conversation_status,
            timeout_status = :timeout_status,
            created_at = if_not_exists(created_at, :created_at),
            updated_at = :updated_at,
            #ttl = :ttl
    """

    expression_attribute_values = {
        ":channel": channel,
        ":user": user,
        ":event_id": event_id,
        ":last_user_text": text,
        ":last_raw_user_text": raw_text,
        ":last_bot_reply": lex_reply,
        ":response_source": response_source,
        ":claude_fallback_attempted": claude_fallback_attempted,
        ":last_ts": ts,
        ":last_activity_at": updated_at,
        ":event_type": event_type,
        ":channel_type": channel_type,
        ":routing_reason": routing_reason,
        ":lex_session_id": lex_session_id,
        ":lex_intent": lex_intent,
        ":lex_state": lex_state,
        ":lex_slots": lex_slots,
        ":conversation_status": conversation_status,
        ":timeout_status": "scheduled" if timeout_state else "inactive",
        ":created_at": updated_at,
        ":updated_at": updated_at,
        ":ttl": ttl_epoch(),
        ":one": 1
    }

    remove_attributes = []

    if timeout_state:
        update_expression += """
            ,
            timeout_due_at = :timeout_due_at,
            timeout_token = :timeout_token,
            timeout_schedule_name = :timeout_schedule_name
        """
        expression_attribute_values.update({
            ":timeout_due_at": timeout_state["timeout_due_at"],
            ":timeout_token": timeout_state["timeout_token"],
            ":timeout_schedule_name": timeout_state["timeout_schedule_name"]
        })
        remove_attributes.extend([
            "timeout_prompt_started_at",
            "timeout_prompted_at",
            "timeout_close_due_at",
            "timeout_closed_at"
        ])
    else:
        remove_attributes.extend([
            "timeout_due_at",
            "timeout_token",
            "timeout_schedule_name",
            "timeout_prompt_started_at",
            "timeout_prompted_at",
            "timeout_close_due_at",
            "timeout_closed_at"
        ])

    if next_action:
        update_expression += """
            ,
            next_action = :next_action,
            jira_status = :jira_status
        """
        expression_attribute_values.update({
            ":next_action": next_action,
            ":jira_status": jira_status
        })
    else:
        remove_attributes.extend([
            "next_action",
            "jira_status"
        ])

    if claude_fallback_error:
        update_expression += """
            ,
            claude_fallback_error = :claude_fallback_error
        """
        expression_attribute_values[":claude_fallback_error"] = claude_fallback_error
    else:
        remove_attributes.append("claude_fallback_error")

    if claude_model_id:
        update_expression += """
            ,
            claude_model_id = :claude_model_id
        """
        expression_attribute_values[":claude_model_id"] = claude_model_id
    else:
        remove_attributes.append("claude_model_id")

    if remove_attributes:
        update_expression += " REMOVE " + ", ".join(remove_attributes)

    update_expression += """
        ADD
            message_count :one
    """

    sessions_table.update_item(
        Key={
            "session_id": session_id
        },
        UpdateExpression=update_expression,
        ExpressionAttributeNames={
            "#channel": "channel",
            "#user": "user",
            "#ttl": "ttl"
        },
        ExpressionAttributeValues=expression_attribute_values
    )

    if timeout_state:
        refresh_timeout_schedule(session_id, timeout_state)
    else:
        delete_timeout_schedule(session_id, "prompt")
        delete_timeout_schedule(session_id, "close")

    slack_response = send_slack_message(channel, lex_reply)

    log_json({
        "level": "INFO",
        "message": "worker_processing_completed",
        "event_id": event_id,
        "channel": channel,
        "event_type": event_type,
        "channel_type": channel_type,
        "routing_reason": routing_reason,
        "user": user,
        "text": text,
        "lex_intent": lex_intent,
        "lex_state": lex_state,
        "lex_slots": lex_slots,
        "response_source": response_source,
        "claude_fallback_attempted": claude_fallback_attempted,
        "next_action": next_action,
        "jira_status": jira_status,
        "timeout_status": "scheduled" if timeout_state else "inactive",
        "timeout_due_at": timeout_state["timeout_due_at"] if timeout_state else None,
        "reply_sent": True,
        "slack_ts": slack_response.get("ts")
    })


def lambda_handler(event, context):
    batch_item_failures = []

    for record in event["Records"]:
        try:
            process_record(record)

        except Exception as e:
            message_id = record.get("messageId")

            log_json({
                "level": "ERROR",
                "message": "worker_processing_failed",
                "message_id": message_id,
                "error": str(e),
                "record_body": record.get("body")
            })

            if message_id:
                batch_item_failures.append({
                    "itemIdentifier": message_id
                })

    return {
        "batchItemFailures": batch_item_failures
    }
