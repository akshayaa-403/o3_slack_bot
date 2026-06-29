import json
import os
import re
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
AUTO_CLAUDE_FALLBACK_ENABLED = os.environ.get("AUTO_CLAUDE_FALLBACK_ENABLED", "false").lower() == "true"
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
    "I could not resolve this automatically. Do you want me to create a Jira ticket? Reply yes to create it, or no to cancel."
)
CREATE_JIRA_TICKET_FUNCTION = os.environ.get("CREATE_JIRA_TICKET_FUNCTION")
ENABLE_ROVO_ENRICHMENT = os.environ.get("ENABLE_ROVO_ENRICHMENT", "false").lower() == "true"
ROVO_ENRICHMENT_FUNCTION = os.environ.get("ROVO_ENRICHMENT_FUNCTION")
JIRA_UNCLEAR_CONFIRMATION_REPLY = os.environ.get(
    "JIRA_UNCLEAR_CONFIRMATION_REPLY",
    "Please reply yes to create the Jira ticket, or no to cancel."
)
JIRA_CANCELLED_REPLY = os.environ.get(
    "JIRA_CANCELLED_REPLY",
    "Cancelled. I did not create a Jira ticket."
)
JIRA_CREATE_FAILED_REPLY = os.environ.get(
    "JIRA_CREATE_FAILED_REPLY",
    "I could not create the Jira ticket. Please try again later or contact support."
)
JIRA_CREATE_CONFIG_FAILED_REPLY = os.environ.get(
    "JIRA_CREATE_CONFIG_FAILED_REPLY",
    "Jira ticket creation is not configured correctly."
)
JIRA_CREATE_PERMISSION_FAILED_REPLY = os.environ.get(
    "JIRA_CREATE_PERMISSION_FAILED_REPLY",
    "I could not create the Jira ticket because Jira rejected the request. Please check Jira permissions or project settings."
)
JIRA_CREATE_TIMEOUT_REPLY = os.environ.get(
    "JIRA_CREATE_TIMEOUT_REPLY",
    "Jira did not respond in time. Please try again later."
)
JIRA_CREATE_IN_PROGRESS_REPLY = os.environ.get(
    "JIRA_CREATE_IN_PROGRESS_REPLY",
    "Jira ticket creation is already in progress. Please wait a moment."
)
JIRA_CREATE_STALE_REPLY = os.environ.get(
    "JIRA_CREATE_STALE_REPLY",
    "A Jira ticket creation was already started but did not finish cleanly. I will not create a second ticket automatically. Please check Jira or contact support."
)
JIRA_CREATING_STALE_SECONDS = int(os.environ.get("JIRA_CREATING_STALE_SECONDS", "300"))
JIRA_CREATED_DUPLICATE_WINDOW_SECONDS = int(os.environ.get("JIRA_CREATED_DUPLICATE_WINDOW_SECONDS", "600"))
EMPTY_USER_TEXT_REPLY = os.environ.get("EMPTY_USER_TEXT_REPLY", "Hi, how can I help?")
EMPTY_LEX_REPLY = os.environ.get(
    "EMPTY_LEX_REPLY",
    "I could not generate a response for that. Please try rephrasing your message."
)
LEX_ASSISTANCE_PROMPT_TEXT = os.environ.get(
    "LEX_ASSISTANCE_PROMPT_TEXT",
    "Was this helpful?"
)
LEX_ASSISTANCE_CLOSED_REPLY = os.environ.get(
    "LEX_ASSISTANCE_CLOSED_REPLY",
    "Okay, I will close this for now."
)
LEX_ASSISTANCE_DETAILS_PROMPT_TEXT = os.environ.get(
    "LEX_ASSISTANCE_DETAILS_PROMPT_TEXT",
    "What still failed? Please include the error message or the step where you are blocked."
)
CLAUDE_FINAL_ACTION_PROMPT_TEXT = os.environ.get(
    "CLAUDE_FINAL_ACTION_PROMPT_TEXT",
    "Would you like live agent support or a Jira ticket?"
)
CLAUDE_UNRESOLVED_REPLY = os.environ.get(
    "CLAUDE_UNRESOLVED_REPLY",
    "I could not resolve this automatically."
)
LIVE_AGENT_DEFERRED_REPLY = os.environ.get(
    "LIVE_AGENT_DEFERRED_REPLY",
    "I have marked this for live agent support. Live-agent handoff is not wired yet."
)

NEXT_ACTION_CREATE_JIRA_TICKET = "O3_CreateJiraTicket"
NEXT_ACTION_CLAUDE_ASSISTANCE = "O3_ClaudeFurtherAssistance"
NEXT_ACTION_FINAL_SUPPORT_OPTIONS = "O3_FinalSupportOptions"
NEXT_ACTION_LIVE_AGENT_SUPPORT = "O3_LiveAgentSupport"

ACTION_ID_ASSISTANCE_YES = "ivy_assistance_yes"
ACTION_ID_ASSISTANCE_NO = "ivy_assistance_no"
ACTION_ID_ASSISTANCE_SOLVED = "ivy_assistance_solved"
ACTION_ID_ASSISTANCE_NEED_MORE_HELP = "ivy_assistance_need_more_help"
ACTION_ID_ASSISTANCE_CREATE_JIRA_TICKET = "ivy_assistance_create_jira_ticket"
ACTION_ID_LIVE_AGENT_SUPPORT = "ivy_live_agent_support"
ACTION_ID_CREATE_JIRA_TICKET = "ivy_create_jira_ticket"

JIRA_CONFIRM_YES = {
    "yes",
    "y",
    "yeah",
    "yep",
    "confirm",
    "create",
    "create it",
    "please create",
    "ok",
    "okay",
    "sure"
}
JIRA_CONFIRM_NO = {
    "no",
    "n",
    "nope",
    "cancel",
    "stop",
    "do not create",
    "dont create",
    "don't create"
}

sessions_table = dynamodb.Table(DYNAMODB_TABLE)


def to_iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def ttl_epoch():
    return int(time.time()) + SESSION_TTL_SECONDS


def parse_iso_datetime(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)

    except ValueError:
        return None


def make_jira_request_id(session_id, event_id, intent_name, request_text):
    raw = "|".join([
        session_id or "",
        event_id or "",
        intent_name or "",
        request_text or ""
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def log_json(data):
    print(json.dumps(data, default=str))


def send_slack_message(channel, text, blocks=None):
    url = "https://slack.com/api/chat.postMessage"

    message = {
        "channel": channel,
        "text": text
    }

    if blocks:
        message["blocks"] = blocks

    data = json.dumps(message).encode("utf-8")

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


def slack_mrkdwn(text, limit=2900):
    value = (text or "").strip()

    if len(value) <= limit:
        return value

    return value[:limit - 3].rstrip() + "..."


def assistance_reply_text(lex_reply):
    return f"{(lex_reply or '').strip()}\n\n{LEX_ASSISTANCE_PROMPT_TEXT}"


def assistance_blocks(lex_reply):
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": slack_mrkdwn(lex_reply)
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": LEX_ASSISTANCE_PROMPT_TEXT
            }
        },
        {
            "type": "actions",
            "block_id": "ivy_assistance_actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Solved"
                    },
                    "action_id": ACTION_ID_ASSISTANCE_SOLVED,
                    "value": "solved"
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Need more help"
                    },
                    "action_id": ACTION_ID_ASSISTANCE_NEED_MORE_HELP,
                    "value": "need_more_help"
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Create Jira ticket"
                    },
                    "style": "primary",
                    "action_id": ACTION_ID_ASSISTANCE_CREATE_JIRA_TICKET,
                    "value": "create_jira_ticket"
                }
            ]
        }
    ]


def final_support_reply_text(reply):
    return f"{(reply or '').strip()}\n\n{CLAUDE_FINAL_ACTION_PROMPT_TEXT}"


def final_support_blocks(reply):
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": slack_mrkdwn(reply)
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": CLAUDE_FINAL_ACTION_PROMPT_TEXT
            }
        },
        {
            "type": "actions",
            "block_id": "ivy_final_support_actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Live agent support"
                    },
                    "action_id": ACTION_ID_LIVE_AGENT_SUPPORT,
                    "value": "live_agent_support"
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Create Jira ticket"
                    },
                    "style": "primary",
                    "action_id": ACTION_ID_CREATE_JIRA_TICKET,
                    "value": "create_jira_ticket"
                }
            ]
        }
    ]


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


def invoke_create_jira_ticket(payload):
    if not CREATE_JIRA_TICKET_FUNCTION:
        return {
            "ok": False,
            "error": "missing_create_jira_ticket_function",
            "error_code": "missing_create_jira_ticket_function"
        }

    try:
        response = lambda_client.invoke(
            FunctionName=CREATE_JIRA_TICKET_FUNCTION,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8")
        )

    except ClientError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "jira_lambda_invoke_failed"
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "jira_lambda_invoke_failed"
        }

    raw_payload = response.get("Payload").read().decode("utf-8")

    if response.get("FunctionError"):
        return {
            "ok": False,
            "error": raw_payload or response.get("FunctionError"),
            "error_code": "jira_lambda_function_error"
        }

    if not raw_payload:
        return {
            "ok": False,
            "error": "empty_create_jira_response",
            "error_code": "invalid_create_jira_response"
        }

    try:
        return json.loads(raw_payload)

    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": "invalid_create_jira_response",
            "error_code": "invalid_create_jira_response",
            "raw_response": raw_payload
        }


def invoke_rovo_enrichment(payload):
    if not ENABLE_ROVO_ENRICHMENT:
        return {
            "ok": True,
            "skipped": True,
            "reason": "disabled"
        }

    if not ROVO_ENRICHMENT_FUNCTION:
        return {
            "ok": False,
            "error": "missing_rovo_enrichment_function",
            "error_code": "missing_rovo_enrichment_function"
        }

    try:
        response = lambda_client.invoke(
            FunctionName=ROVO_ENRICHMENT_FUNCTION,
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8")
        )

    except ClientError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "rovo_lambda_invoke_failed"
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "rovo_lambda_invoke_failed"
        }

    status_code = response.get("StatusCode")
    if status_code and 200 <= int(status_code) < 300:
        return {
            "ok": True,
            "status_code": status_code
        }

    return {
        "ok": False,
        "error": f"Unexpected Rovo Lambda invoke status: {status_code}",
        "error_code": "rovo_lambda_invoke_rejected",
        "status_code": status_code
    }


def get_session_item(session_id):
    response = sessions_table.get_item(
        Key={
            "session_id": session_id
        }
    )
    return response.get("Item") or {}


def has_pending_jira_confirmation(session_item):
    return (
        session_item.get("next_action") == NEXT_ACTION_CREATE_JIRA_TICKET
        and session_item.get("jira_status") == "pending_confirmation"
    )


def has_pending_assistance_confirmation(session_item):
    return (
        session_item.get("next_action") == NEXT_ACTION_CLAUDE_ASSISTANCE
        and session_item.get("assistance_status") == "pending_confirmation"
    )


def has_pending_assistance_details(session_item):
    return (
        session_item.get("next_action") == NEXT_ACTION_CLAUDE_ASSISTANCE
        and session_item.get("assistance_status") == "awaiting_details"
    )


def has_pending_final_support_options(session_item):
    return (
        session_item.get("next_action") == NEXT_ACTION_FINAL_SUPPORT_OPTIONS
        and session_item.get("support_options_status") == "pending"
    )


def jira_status_is_recent(session_item, timestamp_field, max_age_seconds):
    started_at = parse_iso_datetime(session_item.get(timestamp_field))

    if not started_at:
        return False

    age = datetime.now(timezone.utc) - started_at
    return age.total_seconds() <= max_age_seconds


def has_jira_confirmation_state(session_item, text):
    if has_pending_jira_confirmation(session_item):
        return True

    jira_status = session_item.get("jira_status")
    decision = classify_jira_confirmation(text)

    if (
        session_item.get("next_action") == "O3_CreateJiraTicket"
        and jira_status == "creating"
    ):
        return True

    return (
        jira_status == "created"
        and decision == "yes"
        and bool(session_item.get("jira_ticket_key") or session_item.get("last_jira_ticket_key"))
        and jira_status_is_recent(
            session_item,
            "jira_created_at",
            JIRA_CREATED_DUPLICATE_WINDOW_SECONDS
        )
    )


def normalize_confirmation_text(text):
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    normalized = normalized.strip(" .,!?:;\"'")
    return normalized


def classify_jira_confirmation(text):
    normalized = normalize_confirmation_text(text)

    if normalized in JIRA_CONFIRM_YES:
        return "yes"

    if normalized in JIRA_CONFIRM_NO:
        return "no"

    return "unclear"


def ensure_jira_confirmation_prompt(reply):
    prompt = (reply or "").strip()

    if (
        re.search(r"\byes\b", prompt, flags=re.IGNORECASE)
        and re.search(r"\bno\b", prompt, flags=re.IGNORECASE)
    ):
        return prompt

    if not prompt:
        return "Do you want me to create a Jira ticket? Reply yes to create it, or no to cancel."

    return f"{prompt} Reply yes to create a Jira ticket, or no to cancel."


def jira_ticket_reply(ticket_key, ticket_url):
    if ticket_key and ticket_url:
        return f"Created Jira ticket {ticket_key}: {ticket_url}"

    if ticket_key:
        return f"Created Jira ticket {ticket_key}."

    return "Created the Jira ticket."


def existing_jira_ticket_reply(ticket_key, ticket_url):
    if ticket_key and ticket_url:
        return f"Jira ticket already created {ticket_key}: {ticket_url}"

    if ticket_key:
        return f"Jira ticket already created {ticket_key}."

    return "Jira ticket already created."


def jira_failure_reply(jira_result):
    error_code = jira_result.get("error_code")
    status = jira_result.get("status")

    if error_code in {"jira_configuration_error", "missing_create_jira_ticket_function"}:
        return JIRA_CREATE_CONFIG_FAILED_REPLY

    if error_code in {"jira_network_error", "jira_timeout"}:
        return JIRA_CREATE_TIMEOUT_REPLY

    if error_code in {"jira_auth_or_permission_error", "jira_project_or_permission_error"}:
        return JIRA_CREATE_PERMISSION_FAILED_REPLY

    if status in {401, 403, 404}:
        return JIRA_CREATE_PERMISSION_FAILED_REPLY

    return JIRA_CREATE_FAILED_REPLY


def jira_creation_is_stale(session_item):
    create_started_at = parse_iso_datetime(session_item.get("jira_create_started_at"))

    if not create_started_at:
        return True

    age = datetime.now(timezone.utc) - create_started_at
    return age.total_seconds() > JIRA_CREATING_STALE_SECONDS


def acquire_jira_creation_lock(session_id, jira_request_id, event_id, now_iso):
    try:
        sessions_table.update_item(
            Key={
                "session_id": session_id
            },
            UpdateExpression="""
                SET
                    jira_status = :creating,
                    jira_request_id = :jira_request_id,
                    jira_confirm_event_id = :event_id,
                    jira_confirmed_at = :now,
                    jira_create_started_at = :now,
                    updated_at = :now
                REMOVE
                    jira_error,
                    jira_error_code,
                    jira_error_status
            """,
            ConditionExpression="""
                next_action = :next_action
                AND jira_status = :pending_confirmation
            """,
            ExpressionAttributeValues={
                ":creating": "creating",
                ":jira_request_id": jira_request_id,
                ":event_id": event_id or "unknown-event",
                ":now": now_iso,
                ":next_action": "O3_CreateJiraTicket",
                ":pending_confirmation": "pending_confirmation"
            }
        )

        log_json({
            "level": "INFO",
            "message": "jira_creation_lock_acquired",
            "session_id": session_id,
            "jira_request_id": jira_request_id
        })
        return True

    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log_json({
                "level": "INFO",
                "message": "jira_creation_lock_conflict",
                "session_id": session_id,
                "jira_request_id": jira_request_id
            })
            return False

        raise


def existing_jira_state_result(session_item, base_result):
    ticket_key = session_item.get("jira_ticket_key") or session_item.get("last_jira_ticket_key")
    ticket_url = session_item.get("jira_ticket_url") or session_item.get("last_jira_ticket_url")
    jira_status = session_item.get("jira_status")
    preserved_metadata = {
        "jira_request_id": session_item.get("jira_request_id") or base_result.get("jira_request_id"),
        "jira_requested_at": session_item.get("jira_requested_at") or base_result.get("jira_requested_at"),
        "jira_confirmed_at": session_item.get("jira_confirmed_at"),
        "jira_create_started_at": session_item.get("jira_create_started_at"),
        "jira_created_at": session_item.get("jira_created_at"),
        "last_jira_ticket_key": session_item.get("last_jira_ticket_key"),
        "last_jira_ticket_url": session_item.get("last_jira_ticket_url"),
        "last_jira_created_at": session_item.get("last_jira_created_at"),
    }

    if jira_status == "created":
        return {
            **base_result,
            **preserved_metadata,
            "lex_state": "Fulfilled",
            "response_source": "jira",
            "next_action": None,
            "jira_status": "created",
            "jira_ticket_key": ticket_key,
            "jira_ticket_url": ticket_url,
            "last_jira_ticket_key": ticket_key,
            "last_jira_ticket_url": ticket_url,
            "last_jira_created_at": session_item.get("jira_created_at") or session_item.get("last_jira_created_at"),
            "reply": existing_jira_ticket_reply(ticket_key, ticket_url),
        }

    if jira_status == "creating" and jira_creation_is_stale(session_item):
        return {
            **base_result,
            **preserved_metadata,
            "lex_state": "Failed",
            "response_source": "jira",
            "next_action": None,
            "jira_status": "create_failed",
            "jira_error": "stale_jira_creation",
            "jira_error_code": "stale_jira_creation",
            "reply": JIRA_CREATE_STALE_REPLY,
        }

    if jira_status == "creating":
        return {
            **base_result,
            **preserved_metadata,
            "lex_state": "InProgress",
            "response_source": "jira",
            "jira_status": "creating",
            "reply": JIRA_CREATE_IN_PROGRESS_REPLY,
        }

    return {
        **base_result,
        "reply": JIRA_UNCLEAR_CONFIRMATION_REPLY,
    }


def build_jira_payload(session_item, body, session_id, text, raw_text):
    intent_name = (
        session_item.get("jira_intent_name")
        or session_item.get("lex_intent")
        or "UNKNOWN"
    )
    request_text = (
        session_item.get("jira_request_text")
        or session_item.get("last_user_text")
        or raw_text
        or text
    )
    request_raw_text = session_item.get("last_raw_user_text") or request_text

    return {
        "jira_request_id": session_item.get("jira_request_id"),
        "jira_requested_at": session_item.get("jira_requested_at"),
        "jira_confirmed_at": session_item.get("jira_confirmed_at"),
        "event_id": body.get("event_id"),
        "session_id": session_id,
        "channel": body.get("channel"),
        "user": body.get("user"),
        "text": request_text,
        "raw_text": request_raw_text,
        "confirmation_text": text,
        "lex": {
            "intent": intent_name,
            "state": session_item.get("lex_state"),
            "slots": session_item.get("lex_slots", {})
        },
        "slack": {
            "channel": body.get("channel"),
            "user": body.get("user"),
            "event_ts": body.get("ts"),
        }
    }


def split_rovo_support_text(text):
    value = (text or "").strip()
    marker = "\n\nUser follow-up:\n"

    if value.startswith("Original question:\n") and marker in value:
        original, followup = value[len("Original question:\n"):].split(marker, 1)
        return original.strip(), followup.strip()

    return value, ""


def build_rovo_payload(
    body,
    session_id,
    lex_intent,
    lex_state,
    lex_slots,
    jira_request_id,
    jira_requested_at,
    jira_created_at,
    jira_ticket_key,
    jira_ticket_url,
    jira_request_text,
    raw_text,
    support_original_text_value=None,
    support_raw_text_value=None,
    support_lex_reply=None,
    support_claude_reply=None,
    support_claude_error=None,
):
    support_original, user_followup = split_rovo_support_text(support_original_text_value)
    support_raw, raw_followup = split_rovo_support_text(support_raw_text_value)
    original_request = support_original or jira_request_text or raw_text or body.get("text") or ""
    original_raw_request = support_raw or raw_text or original_request
    request_text = jira_request_text or original_request
    followup_text = user_followup or raw_followup

    return {
        "jira_request_id": jira_request_id,
        "jira_requested_at": jira_requested_at,
        "jira_created_at": jira_created_at,
        "ticket_key": jira_ticket_key,
        "ticket_url": jira_ticket_url,
        "session_id": session_id,
        "channel": body.get("channel"),
        "user": body.get("user"),
        "text": request_text,
        "raw_text": raw_text or original_raw_request or request_text,
        "request": {
            "original_text": original_request,
            "raw_text": original_raw_request,
            "user_followup": followup_text,
            "jira_request_text": jira_request_text or request_text,
        },
        "answers": {
            "lex_answer_shown": support_lex_reply,
            "claude_answer": support_claude_reply,
            "claude_error": support_claude_error,
        },
        "lex": {
            "intent": lex_intent,
            "state": lex_state,
            "slots": lex_slots or {}
        },
        "slack": {
            "channel": body.get("channel"),
            "user": body.get("user"),
            "event_ts": body.get("ts")
        }
    }


def mark_rovo_invoke_failed(session_id, error, error_code):
    failed_at = to_iso(datetime.now(timezone.utc))

    try:
        sessions_table.update_item(
            Key={
                "session_id": session_id
            },
            UpdateExpression="""
                SET
                    rovo_status = :failed,
                    rovo_error = :error,
                    rovo_error_code = :error_code,
                    rovo_enriched_at = :failed_at,
                    updated_at = :failed_at
            """,
            ExpressionAttributeValues={
                ":failed": "failed",
                ":error": error,
                ":error_code": error_code,
                ":failed_at": failed_at
            }
        )

    except Exception as e:
        log_json({
            "level": "ERROR",
            "message": "rovo_invoke_failure_update_failed",
            "session_id": session_id,
            "error": str(e),
            "original_error": error,
            "original_error_code": error_code
        })


def store_rovo_slack_message_target(session_id, slack_ts, slack_text):
    if not (session_id and slack_ts):
        return

    try:
        sessions_table.update_item(
            Key={
                "session_id": session_id
            },
            UpdateExpression="""
                SET
                    rovo_slack_message_ts = :slack_ts,
                    rovo_slack_original_text = :slack_text,
                    updated_at = :updated_at
            """,
            ExpressionAttributeValues={
                ":slack_ts": slack_ts,
                ":slack_text": slack_text or "",
                ":updated_at": to_iso(datetime.now(timezone.utc)),
            }
        )

    except Exception as e:
        log_json({
            "level": "ERROR",
            "message": "rovo_slack_target_update_failed",
            "session_id": session_id,
            "error": str(e),
        })


def base_interactive_result(session_item):
    return {
        "lex_intent": session_item.get("lex_intent") or "INTERACTIVE_ACTION",
        "lex_state": "InProgress",
        "lex_slots": session_item.get("lex_slots", {}),
        "response_source": "interactive_action",
        "next_action": None,
        "reply": "That action is no longer active. Please send a new message.",
        "blocks": None,
        "jira_status": None,
        "jira_intent_name": None,
        "jira_request_text": None,
        "jira_request_id": None,
        "jira_requested_at": None,
        "jira_confirmed_at": None,
        "jira_create_started_at": None,
        "jira_created_at": None,
        "jira_ticket_key": None,
        "jira_ticket_url": None,
        "jira_error": None,
        "jira_error_code": None,
        "jira_error_status": None,
        "last_jira_ticket_key": None,
        "last_jira_ticket_url": None,
        "last_jira_created_at": None,
        "rovo_status": None,
        "rovo_requested_at": None,
        "rovo_enriched_at": None,
        "rovo_error": None,
        "rovo_error_code": None,
        "rovo_should_invoke": False,
        "claude_fallback_attempted": False,
        "claude_fallback_error": None,
        "claude_model_id": None,
        "assistance_status": None,
        "assistance_original_text": None,
        "assistance_raw_text": None,
        "assistance_lex_intent": None,
        "assistance_lex_state": None,
        "assistance_lex_slots": None,
        "assistance_lex_reply": None,
        "assistance_requested_at": None,
        "assistance_closed_at": None,
        "assistance_resolved_at": None,
        "support_options_status": None,
        "support_original_text": None,
        "support_raw_text": None,
        "support_lex_intent": None,
        "support_lex_state": None,
        "support_lex_slots": None,
        "support_lex_reply": None,
        "support_claude_reply": None,
        "support_claude_error": None,
        "support_requested_at": None,
        "support_resolved_at": None,
        "live_agent_status": None,
        "live_agent_requested_at": None,
    }


def build_assistance_claude_payload(session_item, body, session_id, followup_text=None, followup_raw_text=None):
    original_text = (
        session_item.get("assistance_original_text")
        or session_item.get("last_user_text")
        or ""
    )
    raw_text = (
        session_item.get("assistance_raw_text")
        or session_item.get("last_raw_user_text")
        or original_text
    )
    lex_reply = (
        session_item.get("assistance_lex_reply")
        or session_item.get("last_bot_reply")
        or ""
    )
    user_followup = (followup_text or session_item.get("assistance_followup_text") or original_text).strip()
    raw_followup = followup_raw_text or session_item.get("assistance_followup_raw_text") or user_followup

    return {
        "event_id": body.get("event_id"),
        "session_id": session_id,
        "channel": body.get("channel"),
        "channel_type": body.get("channel_type"),
        "routing_reason": "lex_assistance_details",
        "user": body.get("user"),
        "text": user_followup,
        "raw_text": raw_followup,
        "lex": {
            "intent": session_item.get("assistance_lex_intent") or session_item.get("lex_intent"),
            "state": session_item.get("assistance_lex_state") or session_item.get("lex_state"),
            "slots": session_item.get("assistance_lex_slots") or session_item.get("lex_slots", {}),
            "reply": slack_mrkdwn(lex_reply, 900)
        },
        "assistance": {
            "original_question": original_text,
            "user_followup": user_followup,
            "lex_answer_summary": slack_mrkdwn(lex_reply, 900)
        },
        "session": {
            "conversation_status": "active",
            "trigger": "lex_assistance_details"
        }
    }


def support_original_text(session_item):
    return (
        session_item.get("support_original_text")
        or session_item.get("assistance_original_text")
        or session_item.get("jira_request_text")
        or session_item.get("last_user_text")
        or ""
    )


def support_raw_text(session_item):
    return (
        session_item.get("support_raw_text")
        or session_item.get("assistance_raw_text")
        or session_item.get("last_raw_user_text")
        or support_original_text(session_item)
    )


def build_support_jira_request_text(session_item):
    parts = []
    original_text = support_original_text(session_item)
    lex_reply = session_item.get("support_lex_reply") or session_item.get("assistance_lex_reply")
    claude_reply = session_item.get("support_claude_reply")
    claude_error = session_item.get("support_claude_error")

    if original_text:
        parts.append(f"User request:\n{original_text}")

    if lex_reply:
        parts.append(f"Lex answer already shown:\n{lex_reply}")

    if claude_reply:
        parts.append(f"Claude follow-up answer:\n{claude_reply}")
    elif claude_error:
        parts.append(f"Claude follow-up result:\nUnable to resolve automatically ({claude_error}).")

    return "\n\n".join(parts) or original_text or "User requested support from IVY."


def build_final_support_result(session_item, claude_result, now_iso):
    result = base_interactive_result(session_item)
    original_text = (
        session_item.get("assistance_original_text")
        or session_item.get("support_original_text")
        or session_item.get("last_user_text")
        or ""
    )
    raw_text = (
        session_item.get("assistance_raw_text")
        or session_item.get("support_raw_text")
        or session_item.get("last_raw_user_text")
        or original_text
    )
    followup_text = session_item.get("assistance_followup_text")
    followup_raw_text = session_item.get("assistance_followup_raw_text") or followup_text
    lex_reply = (
        session_item.get("assistance_lex_reply")
        or session_item.get("support_lex_reply")
        or session_item.get("last_bot_reply")
        or ""
    )
    claude_reply = (claude_result.get("reply") or "").strip()
    claude_ok = bool(claude_result.get("ok") and claude_reply)
    final_answer = claude_reply if claude_ok else CLAUDE_UNRESOLVED_REPLY
    support_text = original_text
    support_raw = raw_text

    if followup_text:
        support_text = "\n\n".join([
            f"Original question:\n{original_text}",
            f"User follow-up:\n{followup_text}"
        ])
        support_raw = "\n\n".join([
            f"Original question:\n{raw_text}",
            f"User follow-up:\n{followup_raw_text}"
        ])

    result.update({
        "lex_intent": session_item.get("assistance_lex_intent") or session_item.get("lex_intent") or "ClaudeAssistance",
        "lex_state": "Fulfilled" if claude_ok else "Failed",
        "lex_slots": session_item.get("assistance_lex_slots") or session_item.get("lex_slots", {}),
        "response_source": "claude" if claude_ok else "claude_failed",
        "next_action": NEXT_ACTION_FINAL_SUPPORT_OPTIONS,
        "reply": final_support_reply_text(final_answer),
        "blocks": final_support_blocks(final_answer),
        "claude_fallback_attempted": True,
        "claude_fallback_error": None if claude_ok else claude_result.get("error", "unknown_claude_error"),
        "claude_model_id": claude_result.get("model_id"),
        "assistance_resolved_at": now_iso,
        "support_options_status": "pending",
        "support_original_text": support_text,
        "support_raw_text": support_raw,
        "support_lex_intent": session_item.get("assistance_lex_intent") or session_item.get("lex_intent"),
        "support_lex_state": session_item.get("assistance_lex_state") or session_item.get("lex_state"),
        "support_lex_slots": session_item.get("assistance_lex_slots") or session_item.get("lex_slots", {}),
        "support_lex_reply": lex_reply,
        "support_claude_reply": claude_reply if claude_ok else None,
        "support_claude_error": None if claude_ok else claude_result.get("error", "unknown_claude_error"),
        "support_requested_at": now_iso,
    })
    return result


def acquire_support_jira_creation_lock(session_id, jira_request_id, event_id, now_iso):
    try:
        sessions_table.update_item(
            Key={
                "session_id": session_id
            },
            UpdateExpression="""
                SET
                    next_action = :jira_next_action,
                    support_options_status = :creating_jira,
                    jira_status = :creating,
                    jira_request_id = :jira_request_id,
                    jira_confirm_event_id = :event_id,
                    jira_confirmed_at = :now,
                    jira_create_started_at = :now,
                    updated_at = :now
                REMOVE
                    jira_error,
                    jira_error_code,
                    jira_error_status
            """,
            ConditionExpression="""
                (
                    next_action = :support_next_action
                    AND support_options_status = :support_pending
                )
                OR (
                    next_action = :assistance_next_action
                    AND assistance_status IN (:assistance_pending_confirmation, :awaiting_details)
                )
            """,
            ExpressionAttributeValues={
                ":jira_next_action": NEXT_ACTION_CREATE_JIRA_TICKET,
                ":support_next_action": NEXT_ACTION_FINAL_SUPPORT_OPTIONS,
                ":assistance_next_action": NEXT_ACTION_CLAUDE_ASSISTANCE,
                ":creating_jira": "creating_jira",
                ":creating": "creating",
                ":jira_request_id": jira_request_id,
                ":event_id": event_id or "unknown-event",
                ":now": now_iso,
                ":support_pending": "pending",
                ":assistance_pending_confirmation": "pending_confirmation",
                ":awaiting_details": "awaiting_details"
            }
        )

        log_json({
            "level": "INFO",
            "message": "support_jira_creation_lock_acquired",
            "session_id": session_id,
            "jira_request_id": jira_request_id
        })
        return True

    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log_json({
                "level": "INFO",
                "message": "support_jira_creation_lock_conflict",
                "session_id": session_id,
                "jira_request_id": jira_request_id
            })
            return False

        raise


def handle_support_create_jira(session_item, body, session_id, now_iso):
    result = base_interactive_result(session_item)
    intent_name = (
        session_item.get("support_lex_intent")
        or session_item.get("assistance_lex_intent")
        or session_item.get("lex_intent")
        or "ClaudeAssistance"
    )
    request_text = build_support_jira_request_text(session_item)
    jira_request_id = (
        session_item.get("jira_request_id")
        or make_jira_request_id(
            session_id,
            session_item.get("last_event_id") or body.get("event_id"),
            intent_name,
            request_text
        )
    )
    jira_requested_at = (
        session_item.get("jira_requested_at")
        or session_item.get("support_requested_at")
        or now_iso
    )

    result.update({
        "lex_intent": intent_name,
        "lex_state": "InProgress",
        "lex_slots": session_item.get("support_lex_slots") or session_item.get("lex_slots", {}),
        "response_source": "jira",
        "next_action": NEXT_ACTION_CREATE_JIRA_TICKET,
        "jira_status": "creating",
        "jira_intent_name": intent_name,
        "jira_request_text": request_text,
        "jira_request_id": jira_request_id,
        "jira_requested_at": jira_requested_at,
        "jira_confirmed_at": now_iso,
        "jira_create_started_at": now_iso,
        "support_options_status": "creating_jira",
        "support_original_text": session_item.get("support_original_text"),
        "support_raw_text": session_item.get("support_raw_text"),
        "support_lex_intent": session_item.get("support_lex_intent"),
        "support_lex_state": session_item.get("support_lex_state"),
        "support_lex_slots": session_item.get("support_lex_slots"),
        "support_lex_reply": session_item.get("support_lex_reply"),
        "support_claude_reply": session_item.get("support_claude_reply"),
        "support_claude_error": session_item.get("support_claude_error"),
        "support_requested_at": session_item.get("support_requested_at") or jira_requested_at,
    })

    if session_item.get("jira_status") in {"creating", "created"}:
        return existing_jira_state_result(session_item, result)

    if not (
        has_pending_final_support_options(session_item)
        or has_pending_assistance_confirmation(session_item)
        or has_pending_assistance_details(session_item)
    ):
        result.update({
            "lex_state": "Ignored",
            "response_source": "interactive_stale",
            "next_action": None,
            "jira_status": None,
            "reply": "That Jira ticket action is no longer active. Please send a new message."
        })
        return result

    if not acquire_support_jira_creation_lock(
        session_id,
        jira_request_id,
        body.get("event_id"),
        now_iso
    ):
        latest_session = get_session_item(session_id)
        if latest_session.get("jira_status") in {"creating", "created"}:
            return existing_jira_state_result(latest_session, result)

        result.update({
            "lex_state": "Ignored",
            "response_source": "interactive_stale",
            "next_action": None,
            "jira_status": None,
            "reply": "That Jira ticket action is no longer active. Please send a new message."
        })
        return result

    locked_session = {
        **session_item,
        "jira_request_id": jira_request_id,
        "jira_requested_at": jira_requested_at,
        "jira_confirmed_at": now_iso,
        "jira_create_started_at": now_iso,
        "jira_intent_name": intent_name,
        "jira_request_text": request_text,
        "last_raw_user_text": support_raw_text(session_item)
    }
    jira_result = invoke_create_jira_ticket(
        build_jira_payload(
            locked_session,
            body,
            session_id,
            body.get("action_value") or body.get("text") or "create_jira_ticket",
            support_raw_text(session_item)
        )
    )

    if jira_result.get("ok"):
        ticket_key = jira_result.get("ticket_key")
        ticket_url = jira_result.get("ticket_url")
        jira_created_at = to_iso(datetime.now(timezone.utc))
        result.update({
            "lex_state": "Fulfilled",
            "next_action": None,
            "jira_status": "created",
            "jira_created_at": jira_created_at,
            "jira_ticket_key": ticket_key,
            "jira_ticket_url": ticket_url,
            "last_jira_ticket_key": ticket_key,
            "last_jira_ticket_url": ticket_url,
            "last_jira_created_at": jira_created_at,
            "rovo_status": "pending" if ENABLE_ROVO_ENRICHMENT else None,
            "rovo_should_invoke": ENABLE_ROVO_ENRICHMENT,
            "support_options_status": "jira_created",
            "support_resolved_at": jira_created_at,
            "reply": jira_ticket_reply(ticket_key, ticket_url),
        })
        return result

    result.update({
        "lex_state": "Failed",
        "next_action": None,
        "jira_status": "create_failed",
        "jira_error": jira_result.get("error", "unknown_jira_error"),
        "jira_error_code": jira_result.get("error_code", "jira_create_failed"),
        "jira_error_status": jira_result.get("status"),
        "support_options_status": "jira_create_failed",
        "support_resolved_at": now_iso,
        "reply": jira_failure_reply(jira_result),
    })
    return result


def handle_interactive_action(session_item, body, session_id):
    action_id = body.get("action_id")
    now_iso = to_iso(datetime.now(timezone.utc))
    result = base_interactive_result(session_item)

    if action_id in {ACTION_ID_ASSISTANCE_NO, ACTION_ID_ASSISTANCE_SOLVED}:
        if not (
            has_pending_assistance_confirmation(session_item)
            or has_pending_assistance_details(session_item)
        ):
            return result

        result.update({
            "lex_state": "Fulfilled",
            "response_source": "assistance_closed",
            "reply": LEX_ASSISTANCE_CLOSED_REPLY,
            "assistance_status": "closed",
            "assistance_closed_at": now_iso,
        })
        return result

    if action_id in {ACTION_ID_ASSISTANCE_YES, ACTION_ID_ASSISTANCE_NEED_MORE_HELP}:
        if not has_pending_assistance_confirmation(session_item):
            return result

        result.update({
            "lex_state": "InProgress",
            "response_source": "assistance_details_requested",
            "next_action": NEXT_ACTION_CLAUDE_ASSISTANCE,
            "reply": LEX_ASSISTANCE_DETAILS_PROMPT_TEXT,
            "assistance_status": "awaiting_details",
            "assistance_original_text": session_item.get("assistance_original_text") or session_item.get("last_user_text"),
            "assistance_raw_text": session_item.get("assistance_raw_text") or session_item.get("last_raw_user_text"),
            "assistance_lex_intent": session_item.get("assistance_lex_intent") or session_item.get("lex_intent"),
            "assistance_lex_state": session_item.get("assistance_lex_state") or session_item.get("lex_state"),
            "assistance_lex_slots": session_item.get("assistance_lex_slots") or session_item.get("lex_slots", {}),
            "assistance_lex_reply": session_item.get("assistance_lex_reply") or session_item.get("last_bot_reply"),
            "assistance_requested_at": session_item.get("assistance_requested_at") or now_iso,
        })
        return result

    if action_id == ACTION_ID_ASSISTANCE_CREATE_JIRA_TICKET:
        if not (
            has_pending_assistance_confirmation(session_item)
            or has_pending_assistance_details(session_item)
        ):
            return result

        support_session = {
            **session_item,
            "support_original_text": session_item.get("assistance_original_text") or session_item.get("last_user_text"),
            "support_raw_text": session_item.get("assistance_raw_text") or session_item.get("last_raw_user_text"),
            "support_lex_intent": session_item.get("assistance_lex_intent") or session_item.get("lex_intent"),
            "support_lex_state": session_item.get("assistance_lex_state") or session_item.get("lex_state"),
            "support_lex_slots": session_item.get("assistance_lex_slots") or session_item.get("lex_slots", {}),
            "support_lex_reply": session_item.get("assistance_lex_reply") or session_item.get("last_bot_reply"),
            "support_requested_at": session_item.get("assistance_requested_at") or now_iso,
        }
        return handle_support_create_jira(support_session, body, session_id, now_iso)

    if action_id == ACTION_ID_LIVE_AGENT_SUPPORT:
        if not has_pending_final_support_options(session_item):
            return result

        result.update({
            "lex_state": "Fulfilled",
            "response_source": "live_agent",
            "next_action": NEXT_ACTION_LIVE_AGENT_SUPPORT,
            "reply": LIVE_AGENT_DEFERRED_REPLY,
            "support_options_status": "live_agent_deferred",
            "support_resolved_at": now_iso,
            "live_agent_status": "deferred",
            "live_agent_requested_at": now_iso,
        })
        return result

    if action_id == ACTION_ID_CREATE_JIRA_TICKET:
        return handle_support_create_jira(session_item, body, session_id, now_iso)

    result.update({
        "response_source": "interactive_unknown",
        "reply": "I could not recognize that action. Please send a new message."
    })
    return result


def handle_jira_confirmation(session_item, body, session_id, text, raw_text):
    decision = classify_jira_confirmation(text)
    now_iso = to_iso(datetime.now(timezone.utc))
    intent_name = (
        session_item.get("jira_intent_name")
        or session_item.get("lex_intent")
        or "JIRA_CONFIRMATION"
    )
    request_text = (
        session_item.get("jira_request_text")
        or session_item.get("last_user_text")
        or raw_text
        or text
    )
    jira_request_id = (
        session_item.get("jira_request_id")
        or make_jira_request_id(
            session_id,
            session_item.get("last_event_id"),
            intent_name,
            request_text
        )
    )
    jira_requested_at = session_item.get("jira_requested_at") or session_item.get("updated_at") or now_iso

    base_result = {
        "lex_intent": intent_name,
        "lex_state": "InProgress",
        "lex_slots": session_item.get("lex_slots", {}),
        "response_source": "jira_confirmation",
        "next_action": "O3_CreateJiraTicket",
        "jira_status": "pending_confirmation",
        "jira_intent_name": intent_name,
        "jira_request_text": request_text,
        "jira_request_id": jira_request_id,
        "jira_requested_at": jira_requested_at,
        "jira_confirmed_at": None,
        "jira_create_started_at": None,
        "jira_created_at": None,
        "jira_ticket_key": None,
        "jira_ticket_url": None,
        "jira_error": None,
        "jira_error_code": None,
        "jira_error_status": None,
        "last_jira_ticket_key": None,
        "last_jira_ticket_url": None,
        "last_jira_created_at": None,
        "rovo_status": None,
        "rovo_requested_at": None,
        "rovo_enriched_at": None,
        "rovo_error": None,
        "rovo_error_code": None,
        "rovo_should_invoke": False,
    }

    if session_item.get("jira_status") in {"creating", "created"}:
        return existing_jira_state_result(session_item, base_result)

    if decision == "no":
        return {
            **base_result,
            "lex_state": "Fulfilled",
            "response_source": "jira",
            "next_action": None,
            "jira_status": "cancelled",
            "reply": JIRA_CANCELLED_REPLY,
        }

    if decision != "yes":
        return {
            **base_result,
            "reply": JIRA_UNCLEAR_CONFIRMATION_REPLY,
        }

    if not acquire_jira_creation_lock(
        session_id,
        jira_request_id,
        body.get("event_id"),
        now_iso
    ):
        return existing_jira_state_result(get_session_item(session_id), base_result)

    locked_session = {
        **session_item,
        "jira_request_id": jira_request_id,
        "jira_requested_at": jira_requested_at,
        "jira_confirmed_at": now_iso,
        "jira_create_started_at": now_iso
    }
    jira_result = invoke_create_jira_ticket(
        build_jira_payload(locked_session, body, session_id, text, raw_text)
    )

    if jira_result.get("ok"):
        ticket_key = jira_result.get("ticket_key")
        ticket_url = jira_result.get("ticket_url")
        jira_created_at = to_iso(datetime.now(timezone.utc))
        return {
            **base_result,
            "lex_state": "Fulfilled",
            "response_source": "jira",
            "next_action": None,
            "jira_status": "created",
            "jira_confirmed_at": now_iso,
            "jira_create_started_at": now_iso,
            "jira_created_at": jira_created_at,
            "jira_ticket_key": ticket_key,
            "jira_ticket_url": ticket_url,
            "last_jira_ticket_key": ticket_key,
            "last_jira_ticket_url": ticket_url,
            "last_jira_created_at": jira_created_at,
            "rovo_status": "pending" if ENABLE_ROVO_ENRICHMENT else None,
            "rovo_should_invoke": ENABLE_ROVO_ENRICHMENT,
            "reply": jira_ticket_reply(ticket_key, ticket_url),
        }

    return {
        **base_result,
        "lex_state": "Failed",
        "response_source": "jira",
        "next_action": None,
        "jira_status": "create_failed",
        "jira_confirmed_at": now_iso,
        "jira_create_started_at": now_iso,
        "jira_error": jira_result.get("error", "unknown_jira_error"),
        "jira_error_code": jira_result.get("error_code", "jira_create_failed"),
        "jira_error_status": jira_result.get("status"),
        "reply": jira_failure_reply(jira_result),
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
    action_id = body.get("action_id")
    action_value = body.get("action_value")
    is_interactive_action = event_type == "interactive_action"

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
        "text": text,
        "action_id": action_id
    })

    existing_session = get_session_item(session_id)
    response_source = "lex"
    claude_fallback_attempted = False
    claude_fallback_error = None
    claude_model_id = None
    next_action = None
    jira_status = None
    jira_intent_name = None
    jira_request_text = None
    jira_request_id = None
    jira_requested_at = None
    jira_confirmed_at = None
    jira_create_started_at = None
    jira_created_at = None
    jira_ticket_key = None
    jira_ticket_url = None
    jira_error = None
    jira_error_code = None
    jira_error_status = None
    last_jira_ticket_key = None
    last_jira_ticket_url = None
    last_jira_created_at = None
    rovo_status = None
    rovo_requested_at = None
    rovo_enriched_at = None
    rovo_error = None
    rovo_error_code = None
    rovo_should_invoke = False
    assistance_status = None
    assistance_original_text = None
    assistance_raw_text = None
    assistance_lex_intent = None
    assistance_lex_state = None
    assistance_lex_slots = None
    assistance_lex_reply = None
    assistance_requested_at = None
    assistance_closed_at = None
    assistance_resolved_at = None
    support_options_status = None
    support_original_text_value = None
    support_raw_text_value = None
    support_lex_intent = None
    support_lex_state = None
    support_lex_slots = None
    support_lex_reply = None
    support_claude_reply = None
    support_claude_error = None
    support_requested_at = None
    support_resolved_at = None
    live_agent_status = None
    live_agent_requested_at = None
    slack_blocks = None
    jira_confirmation_handled = False
    interactive_action_handled = False
    assistance_details_handled = False

    if is_interactive_action:
        interactive_action_handled = True
        interactive_result = handle_interactive_action(
            existing_session,
            body,
            session_id
        )

        lex_intent = interactive_result["lex_intent"]
        lex_state = interactive_result["lex_state"]
        lex_slots = interactive_result["lex_slots"]
        lex_reply = interactive_result["reply"]
        slack_blocks = interactive_result.get("blocks")
        lex_reply_empty = False
        lex_session_attributes = {}
        response_source = interactive_result["response_source"]
        claude_fallback_attempted = interactive_result.get("claude_fallback_attempted", False)
        claude_fallback_error = interactive_result.get("claude_fallback_error")
        claude_model_id = interactive_result.get("claude_model_id")
        next_action = interactive_result.get("next_action")
        jira_status = interactive_result.get("jira_status")
        jira_intent_name = interactive_result.get("jira_intent_name")
        jira_request_text = interactive_result.get("jira_request_text")
        jira_request_id = interactive_result.get("jira_request_id")
        jira_requested_at = interactive_result.get("jira_requested_at")
        jira_confirmed_at = interactive_result.get("jira_confirmed_at")
        jira_create_started_at = interactive_result.get("jira_create_started_at")
        jira_created_at = interactive_result.get("jira_created_at")
        jira_ticket_key = interactive_result.get("jira_ticket_key")
        jira_ticket_url = interactive_result.get("jira_ticket_url")
        jira_error = interactive_result.get("jira_error")
        jira_error_code = interactive_result.get("jira_error_code")
        jira_error_status = interactive_result.get("jira_error_status")
        last_jira_ticket_key = interactive_result.get("last_jira_ticket_key")
        last_jira_ticket_url = interactive_result.get("last_jira_ticket_url")
        last_jira_created_at = interactive_result.get("last_jira_created_at")
        rovo_status = interactive_result.get("rovo_status")
        rovo_requested_at = interactive_result.get("rovo_requested_at")
        rovo_enriched_at = interactive_result.get("rovo_enriched_at")
        rovo_error = interactive_result.get("rovo_error")
        rovo_error_code = interactive_result.get("rovo_error_code")
        rovo_should_invoke = interactive_result.get("rovo_should_invoke", False)
        assistance_status = interactive_result.get("assistance_status")
        assistance_original_text = interactive_result.get("assistance_original_text")
        assistance_raw_text = interactive_result.get("assistance_raw_text")
        assistance_lex_intent = interactive_result.get("assistance_lex_intent")
        assistance_lex_state = interactive_result.get("assistance_lex_state")
        assistance_lex_slots = interactive_result.get("assistance_lex_slots")
        assistance_lex_reply = interactive_result.get("assistance_lex_reply")
        assistance_requested_at = interactive_result.get("assistance_requested_at")
        assistance_closed_at = interactive_result.get("assistance_closed_at")
        assistance_resolved_at = interactive_result.get("assistance_resolved_at")
        support_options_status = interactive_result.get("support_options_status")
        support_original_text_value = interactive_result.get("support_original_text")
        support_raw_text_value = interactive_result.get("support_raw_text")
        support_lex_intent = interactive_result.get("support_lex_intent")
        support_lex_state = interactive_result.get("support_lex_state")
        support_lex_slots = interactive_result.get("support_lex_slots")
        support_lex_reply = interactive_result.get("support_lex_reply")
        support_claude_reply = interactive_result.get("support_claude_reply")
        support_claude_error = interactive_result.get("support_claude_error")
        support_requested_at = interactive_result.get("support_requested_at")
        support_resolved_at = interactive_result.get("support_resolved_at")
        live_agent_status = interactive_result.get("live_agent_status")
        live_agent_requested_at = interactive_result.get("live_agent_requested_at")

        log_json({
            "level": "INFO",
            "message": "interactive_action_handled",
            "event_id": event_id,
            "session_id": session_id,
            "action_id": action_id,
            "action_value": action_value,
            "response_source": response_source,
            "next_action": next_action,
            "support_options_status": support_options_status,
            "jira_status": jira_status
        })

    elif has_pending_assistance_details(existing_session) and text:
        assistance_details_handled = True
        claude_fallback_attempted = True

        details_session = {
            **existing_session,
            "assistance_followup_text": text,
            "assistance_followup_raw_text": raw_text,
            "assistance_followup_at": to_iso(datetime.now(timezone.utc)),
        }

        if ENABLE_CLAUDE_FALLBACK:
            claude_result = invoke_claude_fallback(
                build_assistance_claude_payload(
                    details_session,
                    body,
                    session_id,
                    text,
                    raw_text
                )
            )
        else:
            claude_result = {
                "ok": False,
                "error": "claude_fallback_disabled"
            }

        final_support_result = build_final_support_result(
            details_session,
            claude_result,
            to_iso(datetime.now(timezone.utc))
        )

        lex_intent = final_support_result["lex_intent"]
        lex_state = final_support_result["lex_state"]
        lex_slots = final_support_result["lex_slots"]
        lex_reply = final_support_result["reply"]
        slack_blocks = final_support_result.get("blocks")
        lex_reply_empty = False
        lex_session_attributes = {}
        response_source = final_support_result["response_source"]
        claude_fallback_error = final_support_result.get("claude_fallback_error")
        claude_model_id = final_support_result.get("claude_model_id")
        next_action = final_support_result.get("next_action")
        assistance_status = final_support_result.get("assistance_status")
        assistance_resolved_at = final_support_result.get("assistance_resolved_at")
        support_options_status = final_support_result.get("support_options_status")
        support_original_text_value = final_support_result.get("support_original_text")
        support_raw_text_value = final_support_result.get("support_raw_text")
        support_lex_intent = final_support_result.get("support_lex_intent")
        support_lex_state = final_support_result.get("support_lex_state")
        support_lex_slots = final_support_result.get("support_lex_slots")
        support_lex_reply = final_support_result.get("support_lex_reply")
        support_claude_reply = final_support_result.get("support_claude_reply")
        support_claude_error = final_support_result.get("support_claude_error")
        support_requested_at = final_support_result.get("support_requested_at")

        log_json({
            "level": "INFO" if response_source == "claude" else "WARN",
            "message": "assistance_details_claude_completed",
            "event_id": event_id,
            "session_id": session_id,
            "lex_intent": lex_intent,
            "response_source": response_source,
            "model_id": claude_model_id,
            "error": claude_fallback_error
        })

    elif has_jira_confirmation_state(existing_session, text):
        jira_confirmation_handled = True
        confirmation_result = handle_jira_confirmation(
            existing_session,
            body,
            session_id,
            text,
            raw_text
        )

        lex_intent = confirmation_result["lex_intent"]
        lex_state = confirmation_result["lex_state"]
        lex_slots = confirmation_result["lex_slots"]
        lex_reply = confirmation_result["reply"]
        lex_reply_empty = False
        lex_session_attributes = {}
        response_source = confirmation_result["response_source"]
        next_action = confirmation_result["next_action"]
        jira_status = confirmation_result["jira_status"]
        jira_intent_name = confirmation_result["jira_intent_name"]
        jira_request_text = confirmation_result["jira_request_text"]
        jira_request_id = confirmation_result["jira_request_id"]
        jira_requested_at = confirmation_result["jira_requested_at"]
        jira_confirmed_at = confirmation_result["jira_confirmed_at"]
        jira_create_started_at = confirmation_result["jira_create_started_at"]
        jira_created_at = confirmation_result["jira_created_at"]
        jira_ticket_key = confirmation_result["jira_ticket_key"]
        jira_ticket_url = confirmation_result["jira_ticket_url"]
        jira_error = confirmation_result["jira_error"]
        jira_error_code = confirmation_result["jira_error_code"]
        jira_error_status = confirmation_result["jira_error_status"]
        last_jira_ticket_key = confirmation_result["last_jira_ticket_key"]
        last_jira_ticket_url = confirmation_result["last_jira_ticket_url"]
        last_jira_created_at = confirmation_result["last_jira_created_at"]
        rovo_status = confirmation_result["rovo_status"]
        rovo_requested_at = confirmation_result["rovo_requested_at"]
        rovo_enriched_at = confirmation_result["rovo_enriched_at"]
        rovo_error = confirmation_result["rovo_error"]
        rovo_error_code = confirmation_result["rovo_error_code"]
        rovo_should_invoke = confirmation_result["rovo_should_invoke"]

        log_json({
            "level": "INFO",
            "message": "jira_confirmation_handled",
            "event_id": event_id,
            "session_id": session_id,
            "decision": classify_jira_confirmation(text),
            "jira_status": jira_status,
            "jira_request_id": jira_request_id,
            "jira_ticket_key": jira_ticket_key,
            "jira_error_code": jira_error_code
        })

    elif text:
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

    if lex_session_attributes.get("response_source") == "router":
        response_source = "router"
        next_action = lex_session_attributes.get("next_action") or None
        jira_status = lex_session_attributes.get("jira_status") or None
        jira_intent_name = lex_session_attributes.get("jira_intent_name") or lex_intent
        jira_request_text = lex_session_attributes.get("jira_request_text") or text

        log_json({
            "level": "INFO",
            "message": "worker_router_metadata_received",
            "event_id": event_id,
            "session_id": session_id,
            "lex_intent": lex_intent,
            "next_action": next_action,
            "jira_status": jira_status
        })

    if (
        AUTO_CLAUDE_FALLBACK_ENABLED
        and not interactive_action_handled
        and not assistance_details_handled
        and not jira_confirmation_handled
        and response_source != "router"
        and should_use_claude_fallback(text, lex_intent, lex_state, lex_reply_empty)
    ):
        claude_fallback_attempted = True
        original_lex_reply = lex_reply
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
        final_support_session = {
            **existing_session,
            "assistance_original_text": text,
            "assistance_raw_text": raw_text,
            "assistance_lex_intent": lex_intent,
            "assistance_lex_state": lex_state,
            "assistance_lex_slots": lex_slots,
            "assistance_lex_reply": original_lex_reply,
            "lex_intent": lex_intent,
            "lex_state": lex_state,
            "lex_slots": lex_slots,
            "last_user_text": text,
            "last_raw_user_text": raw_text,
        }
        final_support_result = build_final_support_result(
            final_support_session,
            claude_result,
            to_iso(datetime.now(timezone.utc))
        )

        lex_state = final_support_result["lex_state"]
        lex_reply = final_support_result["reply"]
        slack_blocks = final_support_result.get("blocks")
        response_source = final_support_result["response_source"]
        claude_fallback_error = final_support_result.get("claude_fallback_error")
        claude_model_id = final_support_result.get("claude_model_id")
        next_action = final_support_result.get("next_action")
        assistance_resolved_at = final_support_result.get("assistance_resolved_at")
        support_options_status = final_support_result.get("support_options_status")
        support_original_text_value = final_support_result.get("support_original_text")
        support_raw_text_value = final_support_result.get("support_raw_text")
        support_lex_intent = final_support_result.get("support_lex_intent")
        support_lex_state = final_support_result.get("support_lex_state")
        support_lex_slots = final_support_result.get("support_lex_slots")
        support_lex_reply = final_support_result.get("support_lex_reply")
        support_claude_reply = final_support_result.get("support_claude_reply")
        support_claude_error = final_support_result.get("support_claude_error")
        support_requested_at = final_support_result.get("support_requested_at")

        log_json({
            "level": "INFO" if response_source == "claude" else "WARN",
            "message": "claude_fallback_completed_with_final_options",
            "event_id": event_id,
            "session_id": session_id,
            "lex_intent": lex_intent,
            "lex_state": lex_state,
            "response_source": response_source,
            "model_id": claude_model_id,
            "error": claude_fallback_error
        })

    if (
        not interactive_action_handled
        and not assistance_details_handled
        and not jira_confirmation_handled
        and response_source == "lex"
        and text
        and lex_state != "Ignored"
        and not next_action
        and not jira_status
    ):
        original_lex_reply = lex_reply
        lex_reply = assistance_reply_text(original_lex_reply)
        slack_blocks = assistance_blocks(original_lex_reply)
        next_action = NEXT_ACTION_CLAUDE_ASSISTANCE
        assistance_status = "pending_confirmation"
        assistance_original_text = text
        assistance_raw_text = raw_text
        assistance_lex_intent = lex_intent
        assistance_lex_state = lex_state
        assistance_lex_slots = lex_slots
        assistance_lex_reply = original_lex_reply

        log_json({
            "level": "INFO",
            "message": "lex_assistance_prompt_added",
            "event_id": event_id,
            "session_id": session_id,
            "lex_intent": lex_intent,
            "lex_state": lex_state
        })

    conversation_status = get_conversation_status(lex_state)
    if response_source in {"claude", "claude_failed"}:
        conversation_status = "active"
    if jira_status in {"pending_confirmation", "creating"}:
        conversation_status = "active"
    if assistance_status in {"pending_confirmation", "awaiting_details"}:
        conversation_status = "active"
    if support_options_status in {"pending", "creating_jira"}:
        conversation_status = "active"

    activity_at_dt = datetime.now(timezone.utc).replace(microsecond=0)
    updated_at = to_iso(activity_at_dt)

    if jira_status == "pending_confirmation":
        jira_request_id = jira_request_id or make_jira_request_id(
            session_id,
            event_id,
            jira_intent_name or lex_intent,
            jira_request_text or text
        )
        jira_requested_at = jira_requested_at or updated_at

    if assistance_status in {"pending_confirmation", "awaiting_details"}:
        assistance_requested_at = assistance_requested_at or updated_at

    if support_options_status == "pending":
        support_requested_at = support_requested_at or updated_at

    if live_agent_status == "deferred":
        live_agent_requested_at = live_agent_requested_at or updated_at

    if rovo_should_invoke and rovo_status == "pending":
        rovo_requested_at = rovo_requested_at or updated_at

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
            next_action = :next_action
        """
        expression_attribute_values.update({
            ":next_action": next_action
        })
    else:
        remove_attributes.append("next_action")

    if jira_status:
        update_expression += """
            ,
            jira_status = :jira_status
        """
        expression_attribute_values[":jira_status"] = jira_status
    else:
        remove_attributes.append("jira_status")

    if jira_intent_name:
        update_expression += """
            ,
            jira_intent_name = :jira_intent_name
        """
        expression_attribute_values[":jira_intent_name"] = jira_intent_name
    else:
        remove_attributes.append("jira_intent_name")

    if jira_request_text:
        update_expression += """
            ,
            jira_request_text = :jira_request_text
        """
        expression_attribute_values[":jira_request_text"] = jira_request_text
    else:
        remove_attributes.append("jira_request_text")

    if jira_request_id:
        update_expression += """
            ,
            jira_request_id = :jira_request_id
        """
        expression_attribute_values[":jira_request_id"] = jira_request_id
    else:
        remove_attributes.append("jira_request_id")

    if jira_requested_at:
        update_expression += """
            ,
            jira_requested_at = :jira_requested_at
        """
        expression_attribute_values[":jira_requested_at"] = jira_requested_at
    else:
        remove_attributes.append("jira_requested_at")

    if jira_confirmed_at:
        update_expression += """
            ,
            jira_confirmed_at = :jira_confirmed_at
        """
        expression_attribute_values[":jira_confirmed_at"] = jira_confirmed_at
    else:
        remove_attributes.append("jira_confirmed_at")

    if jira_create_started_at:
        update_expression += """
            ,
            jira_create_started_at = :jira_create_started_at
        """
        expression_attribute_values[":jira_create_started_at"] = jira_create_started_at
    else:
        remove_attributes.append("jira_create_started_at")

    if jira_created_at:
        update_expression += """
            ,
            jira_created_at = :jira_created_at
        """
        expression_attribute_values[":jira_created_at"] = jira_created_at
    else:
        remove_attributes.append("jira_created_at")

    if jira_ticket_key:
        update_expression += """
            ,
            jira_ticket_key = :jira_ticket_key
        """
        expression_attribute_values[":jira_ticket_key"] = jira_ticket_key
    else:
        remove_attributes.append("jira_ticket_key")

    if jira_ticket_url:
        update_expression += """
            ,
            jira_ticket_url = :jira_ticket_url
        """
        expression_attribute_values[":jira_ticket_url"] = jira_ticket_url
    else:
        remove_attributes.append("jira_ticket_url")

    if jira_error:
        update_expression += """
            ,
            jira_error = :jira_error
        """
        expression_attribute_values[":jira_error"] = jira_error
    else:
        remove_attributes.append("jira_error")

    if jira_error_code:
        update_expression += """
            ,
            jira_error_code = :jira_error_code
        """
        expression_attribute_values[":jira_error_code"] = jira_error_code
    else:
        remove_attributes.append("jira_error_code")

    if jira_error_status:
        update_expression += """
            ,
            jira_error_status = :jira_error_status
        """
        expression_attribute_values[":jira_error_status"] = jira_error_status
    else:
        remove_attributes.append("jira_error_status")

    if last_jira_ticket_key:
        update_expression += """
            ,
            last_jira_ticket_key = :last_jira_ticket_key
        """
        expression_attribute_values[":last_jira_ticket_key"] = last_jira_ticket_key

    if last_jira_ticket_url:
        update_expression += """
            ,
            last_jira_ticket_url = :last_jira_ticket_url
        """
        expression_attribute_values[":last_jira_ticket_url"] = last_jira_ticket_url

    if last_jira_created_at:
        update_expression += """
            ,
            last_jira_created_at = :last_jira_created_at
        """
        expression_attribute_values[":last_jira_created_at"] = last_jira_created_at

    if rovo_status:
        update_expression += """
            ,
            rovo_status = :rovo_status
        """
        expression_attribute_values[":rovo_status"] = rovo_status

    if rovo_requested_at:
        update_expression += """
            ,
            rovo_requested_at = :rovo_requested_at
        """
        expression_attribute_values[":rovo_requested_at"] = rovo_requested_at

    if rovo_enriched_at:
        update_expression += """
            ,
            rovo_enriched_at = :rovo_enriched_at
        """
        expression_attribute_values[":rovo_enriched_at"] = rovo_enriched_at

    if rovo_error:
        update_expression += """
            ,
            rovo_error = :rovo_error
        """
        expression_attribute_values[":rovo_error"] = rovo_error

    if rovo_error_code:
        update_expression += """
            ,
            rovo_error_code = :rovo_error_code
        """
        expression_attribute_values[":rovo_error_code"] = rovo_error_code

    if rovo_status == "pending":
        remove_attributes.extend([
            "rovo_enriched_at",
            "rovo_error",
            "rovo_error_code",
            "rovo_summary",
            "rovo_comment_id"
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

    support_flow_attributes = {
        "assistance_status": assistance_status,
        "assistance_original_text": assistance_original_text,
        "assistance_raw_text": assistance_raw_text,
        "assistance_lex_intent": assistance_lex_intent,
        "assistance_lex_state": assistance_lex_state,
        "assistance_lex_slots": assistance_lex_slots,
        "assistance_lex_reply": assistance_lex_reply,
        "assistance_requested_at": assistance_requested_at,
        "assistance_closed_at": assistance_closed_at,
        "assistance_resolved_at": assistance_resolved_at,
        "support_options_status": support_options_status,
        "support_original_text": support_original_text_value,
        "support_raw_text": support_raw_text_value,
        "support_lex_intent": support_lex_intent,
        "support_lex_state": support_lex_state,
        "support_lex_slots": support_lex_slots,
        "support_lex_reply": support_lex_reply,
        "support_claude_reply": support_claude_reply,
        "support_claude_error": support_claude_error,
        "support_requested_at": support_requested_at,
        "support_resolved_at": support_resolved_at,
        "live_agent_status": live_agent_status,
        "live_agent_requested_at": live_agent_requested_at,
    }

    for attribute_name, attribute_value in support_flow_attributes.items():
        if attribute_value is not None:
            value_name = f":{attribute_name}"
            update_expression += f"""
            ,
            {attribute_name} = {value_name}
        """
            expression_attribute_values[value_name] = attribute_value
        else:
            remove_attributes.append(attribute_name)

    remove_attributes = list(dict.fromkeys(remove_attributes))

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

    if rovo_should_invoke:
        rovo_payload = build_rovo_payload(
            body,
            session_id,
            lex_intent,
            lex_state,
            lex_slots,
            jira_request_id,
            jira_requested_at,
            jira_created_at,
            jira_ticket_key,
            jira_ticket_url,
            jira_request_text,
            raw_text,
            support_original_text_value,
            support_raw_text_value,
            support_lex_reply,
            support_claude_reply,
            support_claude_error,
        )
        rovo_result = invoke_rovo_enrichment(rovo_payload)

        if rovo_result.get("ok"):
            log_json({
                "level": "INFO",
                "message": "rovo_enrichment_invoked",
                "event_id": event_id,
                "session_id": session_id,
                "jira_request_id": jira_request_id,
                "ticket_key": jira_ticket_key,
                "skipped": rovo_result.get("skipped", False)
            })

        else:
            mark_rovo_invoke_failed(
                session_id,
                rovo_result.get("error", "unknown_rovo_invoke_error"),
                rovo_result.get("error_code", "rovo_lambda_invoke_failed")
            )
            rovo_status = "failed"
            rovo_error = rovo_result.get("error", "unknown_rovo_invoke_error")
            rovo_error_code = rovo_result.get("error_code", "rovo_lambda_invoke_failed")

            log_json({
                "level": "ERROR",
                "message": "rovo_enrichment_invoke_failed",
                "event_id": event_id,
                "session_id": session_id,
                "jira_request_id": jira_request_id,
                "ticket_key": jira_ticket_key,
                "error": rovo_error,
                "error_code": rovo_error_code
            })

    if timeout_state:
        refresh_timeout_schedule(session_id, timeout_state)
    else:
        delete_timeout_schedule(session_id, "prompt")
        delete_timeout_schedule(session_id, "close")

    slack_response = send_slack_message(channel, lex_reply, slack_blocks)
    if rovo_should_invoke and jira_ticket_key:
        store_rovo_slack_message_target(
            session_id,
            slack_response.get("ts"),
            lex_reply,
        )

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
        "action_id": action_id,
        "lex_intent": lex_intent,
        "lex_state": lex_state,
        "lex_slots": lex_slots,
        "response_source": response_source,
        "claude_fallback_attempted": claude_fallback_attempted,
        "next_action": next_action,
        "assistance_status": assistance_status,
        "support_options_status": support_options_status,
        "live_agent_status": live_agent_status,
        "jira_status": jira_status,
        "jira_intent_name": jira_intent_name,
        "jira_request_id": jira_request_id,
        "jira_ticket_key": jira_ticket_key,
        "jira_ticket_url": jira_ticket_url,
        "jira_error": jira_error,
        "jira_error_code": jira_error_code,
        "rovo_status": rovo_status,
        "rovo_error": rovo_error,
        "rovo_error_code": rovo_error_code,
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
