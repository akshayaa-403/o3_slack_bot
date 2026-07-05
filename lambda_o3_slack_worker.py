import json
import os
import re
import time
import hashlib
import base64
import boto3
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError

AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

lex = boto3.client("lexv2-runtime", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
s3_client = boto3.client("s3", region_name=AWS_REGION)
scheduler = boto3.client("scheduler", region_name=AWS_REGION)
lambda_client = boto3.client("lambda", region_name=AWS_REGION)
bedrock_runtime = boto3.client("bedrock-runtime", region_name=AWS_REGION)
bedrock_agent_runtime = boto3.client("bedrock-agent-runtime", region_name=AWS_REGION)

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
LIVE_AGENT_FUNCTION = os.environ.get("LIVE_AGENT_FUNCTION")
LIVE_AGENT_WEBHOOK_URL = os.environ.get("LIVE_AGENT_WEBHOOK_URL") or os.environ.get("AUTOMATION_WEBHOOK_URL")
LIVE_AGENT_CONFIG_TABLE = os.environ.get("LIVE_AGENT_CONFIG_TABLE") or os.environ.get("CONFIG_TABLE")
LIVE_AGENT_CONFIG_INTENT = os.environ.get("LIVE_AGENT_CONFIG_INTENT", "LiveAgent")
LIVE_AGENT_WEBHOOK_TIMEOUT_SECONDS = int(os.environ.get("LIVE_AGENT_WEBHOOK_TIMEOUT_SECONDS", "10"))
ENABLE_REQUEST_AI_SUMMARY = os.environ.get("ENABLE_REQUEST_AI_SUMMARY", "true").lower() == "true"
REQUEST_SUMMARY_MODEL_ID = os.environ.get("REQUEST_SUMMARY_MODEL_ID", "amazon.nova-2-lite-v1:0")
REQUEST_SUMMARY_MAX_TOKENS = int(os.environ.get("REQUEST_SUMMARY_MAX_TOKENS", "180"))
REQUEST_SUMMARY_TEMPERATURE = float(os.environ.get("REQUEST_SUMMARY_TEMPERATURE", "0.1"))
SUMMARIZER_FUNCTION_NAME = os.environ.get("SUMMARIZER_FUNCTION_NAME")
SUMMARIZER_INVOKE_TIMEOUT_SECONDS = int(os.environ.get("SUMMARIZER_INVOKE_TIMEOUT_SECONDS", "25"))
IMAGE_REK_FUNCTION = os.environ.get("IMAGE_REK_FUNCTION")
IMAGE_ANALYSIS_UNAVAILABLE_REPLY = os.environ.get(
    "IMAGE_ANALYSIS_UNAVAILABLE_REPLY",
    "I received the image, but image analysis is not configured yet."
)
SCREENSHOT_MATCH_ENABLED = os.environ.get("SCREENSHOT_MATCH_ENABLED", "true").lower() == "true"
SCREENSHOT_ISSUE_TABLE = os.environ.get("SCREENSHOT_ISSUE_TABLE", "o3_screenshot_issue_kb")
SCREENSHOT_VECTOR_ENDPOINT = os.environ.get("SCREENSHOT_VECTOR_ENDPOINT", "").rstrip("/")
SCREENSHOT_VECTOR_BACKEND = os.environ.get("SCREENSHOT_VECTOR_BACKEND", "opensearch").lower()
SCREENSHOT_VECTOR_INDEX = os.environ.get("SCREENSHOT_VECTOR_INDEX", "o3-screenshot-issues")
SCREENSHOT_VECTOR_FIELD = os.environ.get("SCREENSHOT_VECTOR_FIELD", "image_vector")
SCREENSHOT_OPENSEARCH_SERVICE = os.environ.get("SCREENSHOT_OPENSEARCH_SERVICE", "aoss")
SCREENSHOT_EMBEDDING_MODEL_ID = os.environ.get("SCREENSHOT_EMBEDDING_MODEL_ID", "amazon.titan-embed-image-v1")
SCREENSHOT_MATCH_THRESHOLD = float(os.environ.get("SCREENSHOT_MATCH_THRESHOLD", "0.80"))
SCREENSHOT_VECTOR_K = int(os.environ.get("SCREENSHOT_VECTOR_K", "1"))
SCREENSHOT_EMBEDDING_MAX_BYTES = int(os.environ.get("SCREENSHOT_EMBEDDING_MAX_BYTES", "5000000"))
BEDROCK_KNOWLEDGE_BASE_ID = os.environ.get("BEDROCK_KNOWLEDGE_BASE_ID")
BEDROCK_KB_MODEL_ARN = os.environ.get("BEDROCK_KB_MODEL_ARN")
BEDROCK_KB_NUMBER_OF_RESULTS = int(os.environ.get("BEDROCK_KB_NUMBER_OF_RESULTS", "5"))
BEDROCK_KB_NO_ANSWER_MARKERS = [
    marker.strip().lower()
    for marker in os.environ.get(
        "BEDROCK_KB_NO_ANSWER_MARKERS",
        "no relevant information,no kb article found,i don't know,i do not know"
    ).split(",")
    if marker.strip()
]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_TIMEOUT_SECONDS = int(os.environ.get("GEMINI_TIMEOUT_SECONDS", "20"))
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
    "I have sent this to live agent support. Someone from the support team will follow up."
)
LIVE_AGENT_FAILED_REPLY = os.environ.get(
    "LIVE_AGENT_FAILED_REPLY",
    "I could not send this to live agent support. Please try again later or create a Jira ticket."
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
ACTION_ID_CLOSE_AND_SUMMARIZE = "ivy_close_and_summarize"

SESSION_STATE_OPEN = "OPEN"
SESSION_STATE_COLLECTING_DETAILS = "COLLECTING_DETAILS"
SESSION_STATE_WAITING_FOR_USER = "WAITING_FOR_USER"
SESSION_STATE_SUMMARIZING = "SUMMARIZING"
SESSION_STATE_CLOSED = "CLOSED"
SESSION_STATE_FAILED = "FAILED"

GREETING_ONLY_TEXTS = {
    "hi",
    "hello",
    "hey",
    "good morning",
    "good afternoon",
    "good evening",
    "test",
}

CLOSE_SUMMARY_RESPONSE_SOURCES = {
    "lex",
    "router",
    "claude",
    "image",
    "image_screenshot_match_lex",
    "image_bedrock_kb",
    "image_gemini",
}

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
screenshot_issue_table = dynamodb.Table(SCREENSHOT_ISSUE_TABLE) if SCREENSHOT_ISSUE_TABLE else None
live_agent_config_table = dynamodb.Table(LIVE_AGENT_CONFIG_TABLE) if LIVE_AGENT_CONFIG_TABLE else None


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


def value_is_false(value):
    return str(value).strip().lower() in {"false", "0", "no", "disabled"}


def send_slack_message(channel, text, blocks=None, thread_ts=None):
    url = "https://slack.com/api/chat.postMessage"

    message = {
        "channel": channel,
        "text": text
    }

    if thread_ts:
        message["thread_ts"] = thread_ts

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


def slack_api(method, params=None, payload=None, http_method=None):
    params = params or {}
    url = f"https://slack.com/api/{method}"
    data = None
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        http_method = http_method or "POST"
    else:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        http_method = http_method or "GET"

    req = urllib.request.Request(url, data=data, headers=headers, method=http_method)

    with urllib.request.urlopen(req, timeout=10) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise Exception(f"Slack API {method} failed: {result.get('error')}")

    return result


def fetch_conversation_metadata(channel, fallback_type=None):
    metadata = {
        "conversation_type": fallback_type,
        "is_dm": fallback_type == "im",
        "is_mpim": fallback_type == "mpim",
        "is_channel": fallback_type == "channel",
        "is_private": fallback_type == "group",
    }

    if not channel:
        return metadata

    try:
        response = slack_api("conversations.info", {"channel": channel})
        info = response.get("channel") or {}
        conversation_type = (
            "im" if info.get("is_im")
            else "mpim" if info.get("is_mpim")
            else "group" if info.get("is_group") or info.get("is_private")
            else "channel" if info.get("is_channel")
            else fallback_type
        )
        metadata.update({
            "conversation_type": conversation_type,
            "conversation_name": info.get("name") or info.get("user"),
            "is_dm": bool(info.get("is_im")),
            "is_mpim": bool(info.get("is_mpim")),
            "is_channel": bool(info.get("is_channel")),
            "is_private": bool(info.get("is_group") or info.get("is_private")),
        })

    except Exception as e:
        log_json({
            "level": "WARN",
            "message": "conversation_metadata_lookup_failed",
            "channel": channel,
            "error": str(e),
        })

    return metadata


def is_dm_like_conversation(metadata):
    return metadata.get("is_dm") or metadata.get("is_mpim") or metadata.get("conversation_type") in {"im", "mpim"}


def slack_mrkdwn(text, limit=2900):
    value = (text or "").strip()

    if len(value) <= limit:
        return value

    return value[:limit - 3].rstrip() + "..."


def compact_json(data):
    return json.dumps(data or {}, default=str, ensure_ascii=True, sort_keys=True)


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
                        "text": "Close & summarize"
                    },
                    "action_id": ACTION_ID_CLOSE_AND_SUMMARIZE,
                    "value": "close_and_summarize"
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "I need more help"
                    },
                    "action_id": ACTION_ID_ASSISTANCE_NEED_MORE_HELP,
                    "value": "need_more_help"
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
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Close & summarize"
                    },
                    "action_id": ACTION_ID_CLOSE_AND_SUMMARIZE,
                    "value": "close_and_summarize"
                }
            ]
        }
    ]


def close_summary_actions_block():
    return {
        "type": "actions",
        "block_id": "ivy_close_summary_actions",
        "elements": [
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "Close & summarize"
                },
                "action_id": ACTION_ID_CLOSE_AND_SUMMARIZE,
                "value": "close_and_summarize"
            },
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "I need more help"
                },
                "action_id": ACTION_ID_ASSISTANCE_NEED_MORE_HELP,
                "value": "need_more_help"
            }
        ]
    }


def add_close_summary_actions(blocks, reply=None):
    value = list(blocks or [])

    if not value and reply:
        value.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": slack_mrkdwn(reply)
            }
        })

    if any(block.get("block_id") == "ivy_close_summary_actions" for block in value):
        return value

    value.append(close_summary_actions_block())
    return value


def normalized_user_text(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def has_meaningful_user_issue(text):
    value = normalized_user_text(text)
    if not value or value in GREETING_ONLY_TEXTS:
        return False

    return len(value) >= 8 or any(char in value for char in "?!.") or len(value.split()) >= 3


def has_meaningful_bot_answer(reply):
    value = normalized_user_text(reply)
    if not value:
        return False

    return len(value) >= 20 or len(value.split()) >= 5


def should_offer_close_summary(
    response_source,
    jira_status,
    assistance_status,
    support_options_status,
    session_item,
    user_text,
    bot_reply,
):
    if session_item.get("summary_status") in {"started", "completed"}:
        return False

    if session_item.get("conversation_status") in {"closed", "failed"}:
        return False

    return (
        response_source in CLOSE_SUMMARY_RESPONSE_SOURCES
        and not jira_status
        and assistance_status not in {"pending_confirmation", "awaiting_details"}
        and support_options_status not in {"pending", "creating_jira"}
        and has_meaningful_user_issue(user_text)
        and has_meaningful_bot_answer(bot_reply)
    )


def transcript_entry(sender, text, ts=None):
    clean_text = (text or "").strip()
    if not clean_text:
        return None

    return {
        "sender": sender,
        "text": clean_text,
        "ts": ts,
        "recorded_at": to_iso(datetime.now(timezone.utc)),
    }


def build_transcript_append(user_text, bot_text, user_ts, bot_ts=None):
    entries = []
    user_entry = transcript_entry("User", user_text, user_ts) if has_meaningful_user_issue(user_text) else None
    bot_entry = transcript_entry("Bot", bot_text, bot_ts) if has_meaningful_bot_answer(bot_text) else None

    if user_entry:
        entries.append(user_entry)
    if bot_entry:
        entries.append(bot_entry)

    return entries


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


def invoke_summarizer(payload):
    if not SUMMARIZER_FUNCTION_NAME:
        return {
            "ok": False,
            "error": "missing_summarizer_function",
            "error_code": "missing_summarizer_function"
        }

    try:
        response = lambda_client.invoke(
            FunctionName=SUMMARIZER_FUNCTION_NAME,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8")
        )

    except ClientError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "summarizer_lambda_invoke_failed"
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "summarizer_lambda_invoke_failed"
        }

    response_payload = {}
    if response.get("Payload"):
        response_payload = json.loads(response["Payload"].read().decode("utf-8") or "{}")

    if response.get("FunctionError"):
        return {
            "ok": False,
            "error": response_payload.get("error") or response.get("FunctionError"),
            "error_code": response_payload.get("error_code") or "summarizer_lambda_error",
            "status_code": response.get("StatusCode"),
        }

    status_code = response.get("StatusCode")
    if status_code and 200 <= int(status_code) < 300 and response_payload.get("ok"):
        return {**response_payload, "status_code": status_code}

    return {
        "ok": False,
        "error": response_payload.get("error") or f"Unexpected summarizer Lambda invoke status: {status_code}",
        "error_code": response_payload.get("error_code") or "summarizer_lambda_invoke_rejected",
        "status_code": status_code
    }


def invoke_image_analysis(payload):
    if not IMAGE_REK_FUNCTION:
        return {
            "ok": False,
            "error": "missing_image_rek_function",
            "error_code": "missing_image_rek_function"
        }

    try:
        response = lambda_client.invoke(
            FunctionName=IMAGE_REK_FUNCTION,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8")
        )

    except ClientError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "image_lambda_invoke_failed"
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "image_lambda_invoke_failed"
        }

    raw_payload = response.get("Payload").read().decode("utf-8")

    if response.get("FunctionError"):
        return {
            "ok": False,
            "error": raw_payload or response.get("FunctionError"),
            "error_code": "image_lambda_function_error"
        }

    if not raw_payload:
        return {
            "ok": False,
            "error": "empty_image_analysis_response",
            "error_code": "invalid_image_analysis_response"
        }

    try:
        return json.loads(raw_payload)

    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": "invalid_image_analysis_response",
            "error_code": "invalid_image_analysis_response",
            "raw_response": raw_payload
        }


def get_s3_object_bytes(s3_object):
    if not s3_object:
        return None

    bucket = s3_object.get("bucket")
    key = s3_object.get("key")
    if not bucket or not key:
        return None

    response = s3_client.get_object(Bucket=bucket, Key=key)
    return response["Body"].read()


def create_image_embedding_from_bytes(image_bytes):
    if not image_bytes:
        return {
            "ok": False,
            "error": "missing_image_bytes",
            "error_code": "missing_image_bytes"
        }

    if len(image_bytes) > SCREENSHOT_EMBEDDING_MAX_BYTES:
        return {
            "ok": False,
            "error": "image_too_large_for_embedding",
            "error_code": "image_too_large_for_embedding"
        }

    request_body = {
        "inputImage": base64.b64encode(image_bytes).decode("utf-8")
    }

    try:
        response = bedrock_runtime.invoke_model(
            modelId=SCREENSHOT_EMBEDDING_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(request_body).encode("utf-8")
        )
        response_body = json.loads(response["body"].read().decode("utf-8"))

    except ClientError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "embedding_request_failed"
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "embedding_request_failed"
        }

    embedding = response_body.get("embedding")
    if not embedding:
        return {
            "ok": False,
            "error": "empty_embedding_response",
            "error_code": "empty_embedding_response",
            "raw_response": response_body
        }

    return {
        "ok": True,
        "embedding": embedding,
        "model_id": SCREENSHOT_EMBEDDING_MODEL_ID
    }


def create_screenshot_embedding(image_result):
    try:
        image_bytes = get_s3_object_bytes(image_result.get("s3_object"))
        return create_image_embedding_from_bytes(image_bytes)

    except ClientError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "image_s3_read_failed"
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "image_s3_read_failed"
        }


def signed_opensearch_request(method, path, payload=None):
    if not SCREENSHOT_VECTOR_ENDPOINT:
        return {
            "ok": False,
            "error": "missing_screenshot_vector_endpoint",
            "error_code": "missing_screenshot_vector_endpoint"
        }

    body = json.dumps(payload or {}).encode("utf-8")
    payload_hash = hashlib.sha256(body).hexdigest()
    url = f"{SCREENSHOT_VECTOR_ENDPOINT}{path}"
    request = AWSRequest(
        method=method,
        url=url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Host": urllib.parse.urlparse(SCREENSHOT_VECTOR_ENDPOINT).netloc,
            "X-Amz-Content-Sha256": payload_hash
        }
    )
    SigV4Auth(
        boto3.Session().get_credentials(),
        SCREENSHOT_OPENSEARCH_SERVICE,
        AWS_REGION
    ).add_auth(request)

    prepared = request.prepare()
    urllib_request = urllib.request.Request(
        url,
        data=body,
        headers=dict(prepared.headers.items()),
        method=method
    )

    try:
        with urllib.request.urlopen(urllib_request, timeout=10) as response:
            response_body = response.read().decode("utf-8")

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "opensearch_request_failed"
        }

    if not response_body:
        return {
            "ok": True,
            "response": {}
        }

    try:
        return {
            "ok": True,
            "response": json.loads(response_body)
        }

    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": "invalid_opensearch_response",
            "error_code": "invalid_opensearch_response",
            "raw_response": response_body
        }


def search_screenshot_vector_index(embedding):
    payload = {
        "size": SCREENSHOT_VECTOR_K,
        "query": {
            "knn": {
                SCREENSHOT_VECTOR_FIELD: {
                    "vector": embedding,
                    "k": SCREENSHOT_VECTOR_K
                }
            }
        }
    }
    path = f"/{urllib.parse.quote(SCREENSHOT_VECTOR_INDEX, safe='')}/_search"
    result = signed_opensearch_request("POST", path, payload)
    if not result.get("ok"):
        return {
            **result,
            "backend": "opensearch",
            "opensearch_status": "failed",
            "vector_index": SCREENSHOT_VECTOR_INDEX,
            "vector_endpoint": SCREENSHOT_VECTOR_ENDPOINT,
        }

    hits = ((result.get("response") or {}).get("hits") or {}).get("hits") or []
    if not hits:
        return {
            "ok": True,
            "matched": False,
            "reason": "no_vector_hits",
            "backend": "opensearch",
            "opensearch_status": "ok",
            "vector_index": SCREENSHOT_VECTOR_INDEX,
            "vector_endpoint": SCREENSHOT_VECTOR_ENDPOINT,
            "hit_count": 0,
        }

    top_hit = hits[0]
    source = top_hit.get("_source") or {}
    return {
        "ok": True,
        "matched": True,
        "backend": "opensearch",
        "opensearch_status": "ok",
        "vector_index": SCREENSHOT_VECTOR_INDEX,
        "vector_endpoint": SCREENSHOT_VECTOR_ENDPOINT,
        "hit_count": len(hits),
        "issue_id": source.get("issue_id") or top_hit.get("_id"),
        "score": float(top_hit.get("_score") or 0),
        "vector_id": top_hit.get("_id"),
        "source": source
    }


def cosine_similarity(left, right):
    dot_product = 0.0
    left_norm = 0.0
    right_norm = 0.0

    for left_value, right_value in zip(left, right):
        left_float = float(left_value)
        right_float = float(right_value)
        dot_product += left_float * right_float
        left_norm += left_float * left_float
        right_norm += right_float * right_float

    if left_norm == 0 or right_norm == 0:
        return 0.0

    return dot_product / ((left_norm ** 0.5) * (right_norm ** 0.5))


def search_screenshot_dynamodb_embeddings(embedding):
    if not screenshot_issue_table:
        return {
            "ok": False,
            "error": "missing_screenshot_issue_table",
            "error_code": "missing_screenshot_issue_table"
        }

    try:
        response = screenshot_issue_table.scan()
        items = response.get("Items") or []

        while response.get("LastEvaluatedKey"):
            response = screenshot_issue_table.scan(
                ExclusiveStartKey=response["LastEvaluatedKey"]
            )
            items.extend(response.get("Items") or [])

    except ClientError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "screenshot_issue_scan_failed"
        }

    best_item = None
    best_score = 0.0
    for item in items:
        if item.get("enabled") is False or value_is_false(item.get("enabled")):
            continue

        item_embedding = item.get("image_embedding")
        if not item_embedding:
            continue

        score = cosine_similarity(embedding, item_embedding)
        if score > best_score:
            best_item = item
            best_score = score

    if not best_item:
        return {
            "ok": True,
            "matched": False,
            "reason": "no_dynamodb_embedding_hits"
        }

    return {
        "ok": True,
        "matched": True,
        "issue_id": best_item.get("issue_id"),
        "score": best_score,
        "vector_id": best_item.get("issue_id"),
        "source": best_item,
        "issue": best_item,
    }


def get_screenshot_issue(issue_id):
    if not screenshot_issue_table:
        return {
            "ok": False,
            "error": "missing_screenshot_issue_table",
            "error_code": "missing_screenshot_issue_table"
        }

    try:
        response = screenshot_issue_table.get_item(
            Key={
                "issue_id": issue_id
            }
        )

    except ClientError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "screenshot_issue_lookup_failed"
        }

    item = response.get("Item")
    if not item:
        return {
            "ok": False,
            "error": "screenshot_issue_not_found",
            "error_code": "screenshot_issue_not_found"
        }

    if item.get("enabled") is False or value_is_false(item.get("enabled")):
        return {
            "ok": False,
            "error": "screenshot_issue_disabled",
            "error_code": "screenshot_issue_disabled"
        }

    return {
        "ok": True,
        "item": item
    }


def find_matching_screenshot_issue(image_result):
    if not SCREENSHOT_MATCH_ENABLED:
        return {
            "ok": True,
            "matched": False,
            "reason": "screenshot_match_disabled"
        }

    if SCREENSHOT_VECTOR_BACKEND == "opensearch" and not SCREENSHOT_VECTOR_ENDPOINT:
        return {
            "ok": True,
            "matched": False,
            "reason": "missing_screenshot_vector_endpoint"
        }

    embedding_result = create_screenshot_embedding(image_result)
    if not embedding_result.get("ok"):
        return {
            "ok": False,
            "matched": False,
            "reason": embedding_result.get("error_code", "embedding_failed"),
            "error": embedding_result.get("error")
        }

    if SCREENSHOT_VECTOR_BACKEND == "dynamodb":
        search_result = search_screenshot_dynamodb_embeddings(embedding_result["embedding"])
    else:
        search_result = search_screenshot_vector_index(embedding_result["embedding"])

    log_json({
        "level": "INFO" if search_result.get("ok") else "ERROR",
        "message": "screenshot_vector_search_completed",
        "vector_backend": SCREENSHOT_VECTOR_BACKEND,
        "opensearch_status": search_result.get("opensearch_status"),
        "vector_index": search_result.get("vector_index") or SCREENSHOT_VECTOR_INDEX,
        "matched": search_result.get("matched", False),
        "issue_id": search_result.get("issue_id"),
        "score": search_result.get("score"),
        "hit_count": search_result.get("hit_count"),
        "reason": search_result.get("reason") or search_result.get("error_code"),
    })

    if not search_result.get("ok"):
        return {
            "ok": False,
            "matched": False,
            "reason": search_result.get("error_code", "vector_search_failed"),
            "error": search_result.get("error")
        }

    if not search_result.get("matched"):
        return search_result

    score = search_result.get("score", 0)
    if score < SCREENSHOT_MATCH_THRESHOLD:
        return {
            **search_result,
            "matched": False,
            "reason": "below_threshold",
            "threshold": SCREENSHOT_MATCH_THRESHOLD
        }

    issue = search_result.get("issue")
    if not issue:
        issue_result = get_screenshot_issue(search_result.get("issue_id"))
        if not issue_result.get("ok"):
            return {
                **search_result,
                "matched": False,
                "reason": issue_result.get("error_code", "issue_lookup_failed"),
                "error": issue_result.get("error"),
                "threshold": SCREENSHOT_MATCH_THRESHOLD
            }

        issue = issue_result["item"]

    expected_lex_intent = (issue.get("expected_lex_intent") or "").strip()
    if not expected_lex_intent:
        return {
            **search_result,
            "matched": False,
            "reason": "missing_expected_lex_intent",
            "threshold": SCREENSHOT_MATCH_THRESHOLD
        }

    lex_query = (issue.get("lex_query") or "").strip()
    if not lex_query:
        return {
            **search_result,
            "matched": False,
            "reason": "missing_lex_query",
            "threshold": SCREENSHOT_MATCH_THRESHOLD
        }

    return {
        **search_result,
        "matched": True,
        "threshold": SCREENSHOT_MATCH_THRESHOLD,
        "issue": issue,
        "lex_query": lex_query,
        "expected_lex_intent": expected_lex_intent
    }


def build_image_payload(body, session_id, text, raw_text, image_files):
    return {
        "event_id": body.get("event_id"),
        "session_id": session_id,
        "channel": body.get("channel"),
        "channel_type": body.get("channel_type"),
        "routing_reason": body.get("routing_reason"),
        "user": body.get("user"),
        "text": text,
        "raw_text": raw_text,
        "files": image_files,
        "slack": {
            "channel": body.get("channel"),
            "user": body.get("user"),
            "event_ts": body.get("ts"),
            "thread_ts": body.get("thread_ts"),
        }
    }


def image_reply_from_result(result):
    if not result.get("ok"):
        if result.get("error_code") == "missing_image_rek_function":
            return IMAGE_ANALYSIS_UNAVAILABLE_REPLY

        return "I could not analyze that image. Please try again or describe the error in text."

    for key in ("reply", "message", "summary"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return "I analyzed the image, but there was no readable result."


def image_issue_text(text, image_result):
    parts = []

    if text:
        parts.append(f"User caption: {text}")

    detected_text = []
    for item in image_result.get("detected_text") or []:
        value = item.get("text") if isinstance(item, dict) else item
        if value:
            detected_text.append(str(value).strip())

    if detected_text:
        parts.append("Text visible in image: " + " | ".join(detected_text[:12]))

    labels = []
    for item in image_result.get("labels") or []:
        value = item.get("name") if isinstance(item, dict) else item
        if value:
            labels.append(str(value).strip())

    if labels:
        parts.append("Image labels: " + ", ".join(labels[:8]))

    summary = (image_result.get("summary") or "").strip()
    if summary and not parts:
        parts.append(summary)

    return "\n".join(parts).strip()


def lex_is_resolved(lex_intent, lex_state, lex_reply_empty):
    return (
        lex_state not in {"Failed", "Ignored"}
        and not lex_reply_empty
        and lex_intent not in CLAUDE_FALLBACK_INTENTS
    )


def bedrock_kb_answer_is_useful(answer):
    value = (answer or "").strip()
    if not value:
        return False

    value_lower = value.lower()
    return not any(marker in value_lower for marker in BEDROCK_KB_NO_ANSWER_MARKERS)


def invoke_bedrock_knowledge_base(query):
    if not BEDROCK_KNOWLEDGE_BASE_ID or not BEDROCK_KB_MODEL_ARN:
        return {
            "ok": False,
            "error": "missing_bedrock_kb_configuration",
            "error_code": "missing_bedrock_kb_configuration"
        }

    try:
        response = bedrock_agent_runtime.retrieve_and_generate(
            input={
                "text": query
            },
            retrieveAndGenerateConfiguration={
                "type": "KNOWLEDGE_BASE",
                "knowledgeBaseConfiguration": {
                    "knowledgeBaseId": BEDROCK_KNOWLEDGE_BASE_ID,
                    "modelArn": BEDROCK_KB_MODEL_ARN,
                    "retrievalConfiguration": {
                        "vectorSearchConfiguration": {
                            "numberOfResults": BEDROCK_KB_NUMBER_OF_RESULTS
                        }
                    }
                }
            }
        )

    except ClientError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "bedrock_kb_request_failed"
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "bedrock_kb_request_failed"
        }

    answer = ((response.get("output") or {}).get("text") or "").strip()
    if not bedrock_kb_answer_is_useful(answer):
        return {
            "ok": False,
            "error": "bedrock_kb_no_useful_answer",
            "error_code": "bedrock_kb_no_useful_answer",
            "raw_answer": answer,
            "citations": response.get("citations") or []
        }

    return {
        "ok": True,
        "reply": answer,
        "summary": answer,
        "source": "bedrock_knowledge_base",
        "citations": response.get("citations") or [],
        "session_id": response.get("sessionId")
    }


_gemini_api_key_cache = None


def get_gemini_api_key():
    global _gemini_api_key_cache

    if _gemini_api_key_cache:
        return _gemini_api_key_cache

    if GEMINI_API_KEY:
        _gemini_api_key_cache = GEMINI_API_KEY
        return _gemini_api_key_cache

    return ""


def extract_gemini_text(response_body):
    parts = []

    for candidate in response_body.get("candidates") or []:
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            text = (part.get("text") or "").strip()
            if text:
                parts.append(text)

    return "\n".join(parts).strip()


def invoke_gemini_fallback(query, image_result, kb_error=None):
    api_key = get_gemini_api_key()
    if not api_key:
        return {
            "ok": False,
            "error": "missing_gemini_api_key",
            "error_code": "missing_gemini_api_key"
        }

    prompt = "\n".join([
        "You are IVY, a concise IT support assistant.",
        "Use the user's screenshot context to suggest the most likely resolution.",
        "If the screenshot text is ambiguous, ask one clear clarifying question.",
        "Do not claim that a Jira ticket was created.",
        "",
        "Screenshot-derived issue:",
        query,
        "",
        "Image analysis JSON:",
        json.dumps({
            "summary": image_result.get("summary"),
            "detected_text": image_result.get("detected_text"),
            "labels": image_result.get("labels"),
            "bedrock_kb_error": kb_error,
        }, default=str, ensure_ascii=True),
    ])

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{urllib.parse.quote(GEMINI_MODEL, safe='')}:generateContent"
    )
    data = json.dumps({
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ]
    }).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST"
    )

    try:
        started_at = time.time()
        with urllib.request.urlopen(request, timeout=GEMINI_TIMEOUT_SECONDS) as response:
            response_body = json.loads(response.read().decode("utf-8"))

    except Exception as e:
        error_body = None
        if hasattr(e, "read"):
            try:
                error_body = e.read().decode("utf-8", errors="replace")[:1000]
            except Exception:
                error_body = None

        log_json({
            "level": "ERROR",
            "message": "gemini_request_failed",
            "model_id": GEMINI_MODEL,
            "timeout_seconds": GEMINI_TIMEOUT_SECONDS,
            "latency_seconds": round(time.time() - started_at, 2) if "started_at" in locals() else None,
            "error": str(e),
            "error_body": error_body,
        })
        return {
            "ok": False,
            "error": str(e),
            "error_code": "gemini_request_failed",
            "error_body": error_body,
        }

    reply = extract_gemini_text(response_body)
    if not reply:
        log_json({
            "level": "ERROR",
            "message": "gemini_empty_reply",
            "model_id": GEMINI_MODEL,
            "latency_seconds": round(time.time() - started_at, 2),
            "candidate_count": len(response_body.get("candidates") or []),
        })
        return {
            "ok": False,
            "error": "empty_gemini_reply",
            "error_code": "empty_gemini_reply",
            "raw_response": response_body
        }

    log_json({
        "level": "INFO",
        "message": "gemini_request_completed",
        "model_id": GEMINI_MODEL,
        "latency_seconds": round(time.time() - started_at, 2),
        "candidate_count": len(response_body.get("candidates") or []),
        "reply_length": len(reply),
    })

    return {
        "ok": True,
        "reply": reply,
        "summary": reply,
        "source": "gemini",
        "model_id": GEMINI_MODEL
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
    summary_result = build_request_conversation_summary({
        **session_item,
        "session_id": session_id,
    })

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
        "conversation_summary": summary_result.get("text"),
        "conversation_summary_model_id": summary_result.get("model_id"),
        "conversation_summary_usage": summary_result.get("usage", {}),
        "conversation_summary_error": summary_result.get("error"),
        "conversation_summary_fallback_used": summary_result.get("fallback_used", False),
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
            "thread_ts": body.get("thread_ts"),
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
            "event_ts": body.get("ts"),
            "thread_ts": body.get("thread_ts"),
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


def mark_session_summarizing(session_id, timeout_token_value, started_at):
    remove_attributes = [
        "next_action",
        "jira_status",
        "jira_intent_name",
        "jira_request_text",
        "jira_request_id",
        "jira_requested_at",
        "jira_confirmed_at",
        "jira_create_started_at",
        "jira_error",
        "jira_error_code",
        "jira_error_status",
        "assistance_status",
        "assistance_original_text",
        "assistance_raw_text",
        "assistance_lex_intent",
        "assistance_lex_state",
        "assistance_lex_slots",
        "assistance_lex_reply",
        "assistance_requested_at",
        "assistance_followup_text",
        "assistance_followup_raw_text",
        "assistance_followup_at",
        "support_options_status",
        "support_original_text",
        "support_raw_text",
        "support_lex_intent",
        "support_lex_state",
        "support_lex_slots",
        "support_lex_reply",
        "support_claude_reply",
        "support_claude_error",
        "support_requested_at",
        "live_agent_status",
        "live_agent_requested_at",
        "live_agent_error",
        "live_agent_error_code",
        "timeout_due_at",
        "timeout_schedule_name",
        "timeout_prompt_started_at",
        "timeout_prompted_at",
        "timeout_close_due_at",
    ]
    now_iso = to_iso(started_at)

    expression_values = {
        ":summarizing": "summarizing",
        ":session_state": SESSION_STATE_SUMMARIZING,
        ":manual_reason": "button_close_summary",
        ":now": now_iso,
        ":ttl": ttl_epoch(),
        ":active": "active",
    }
    condition = "conversation_status = :active"

    if timeout_token_value:
        condition += " AND timeout_token = :timeout_token"
        expression_values[":timeout_token"] = timeout_token_value

    sessions_table.update_item(
        Key={"session_id": session_id},
        UpdateExpression=f"""
            SET
                conversation_status = :summarizing,
                session_state = :session_state,
                timeout_status = :summarizing,
                summary_status = :summarizing,
                summary_started_at = :now,
                manual_close_reason = :manual_reason,
                updated_at = :now,
                #ttl = :ttl
            REMOVE {", ".join(remove_attributes)}
        """,
        ConditionExpression=condition,
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues=expression_values,
    )


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
        "live_agent_error": None,
        "live_agent_error_code": None,
        "manual_close_summary": False,
        "manual_close_timeout_token": None,
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


def compact_text(value, limit=1200):
    text = re.sub(r"\s+", " ", (value or "").strip())

    if len(text) <= limit:
        return text

    return text[:limit - 3].rstrip() + "..."


def request_summary_transcript(session_item, limit=40):
    messages = session_item.get("session_messages") or []
    transcript = []

    if isinstance(messages, list):
        for message in messages[-limit:]:
            if not isinstance(message, dict):
                continue

            sender = compact_text(message.get("sender") or "Unknown", 80)
            text = compact_text(message.get("text"), 1200)
            if text:
                transcript.append({
                    "sender": sender,
                    "text": text,
                    "ts": message.get("ts"),
                    "recorded_at": message.get("recorded_at")
                })

    if transcript:
        return transcript

    fallback_entries = [
        ("User", support_original_text(session_item)),
        ("IVY Lex", session_item.get("support_lex_reply") or session_item.get("assistance_lex_reply")),
        ("IVY Claude", session_item.get("support_claude_reply")),
        (
            "IVY Claude",
            f"Unable to resolve automatically ({session_item.get('support_claude_error')})."
            if session_item.get("support_claude_error")
            else None
        ),
        ("User", session_item.get("last_user_text")),
        ("IVY", session_item.get("last_bot_reply")),
    ]

    seen = set()
    for sender, text in fallback_entries:
        clean_text = compact_text(text, 1200)
        if not clean_text:
            continue

        key = (sender, clean_text)
        if key in seen:
            continue

        seen.add(key)
        transcript.append({
            "sender": sender,
            "text": clean_text,
            "ts": None,
            "recorded_at": None
        })

    return transcript


def transcript_for_request_summary(transcript):
    lines = []

    for message in transcript or []:
        sender = compact_text(message.get("sender") or "Unknown", 80)
        text = compact_text(message.get("text"), 1200)
        if sender and text:
            lines.append(f"{sender}: {text}")

    return "\n".join(lines)


def deterministic_request_summary(session_item, transcript):
    parts = []
    original_text = compact_text(support_original_text(session_item), 800)
    lex_reply = compact_text(session_item.get("support_lex_reply") or session_item.get("assistance_lex_reply"), 800)
    claude_reply = compact_text(session_item.get("support_claude_reply"), 800)
    claude_error = compact_text(session_item.get("support_claude_error"), 300)
    last_bot_reply = compact_text(session_item.get("last_bot_reply"), 800)

    if original_text:
        parts.append(f"User request: {original_text}")

    if lex_reply:
        parts.append(f"Lex answer shown: {lex_reply}")

    if claude_reply:
        parts.append(f"Claude answer shown: {claude_reply}")
    elif claude_error:
        parts.append(f"Claude fallback result: unable to resolve automatically ({claude_error}).")

    if not lex_reply and not claude_reply and last_bot_reply:
        parts.append(f"Last IVY reply: {last_bot_reply}")

    if not parts and transcript:
        first_message = transcript[0]
        parts.append(f"Conversation: {compact_text(first_message.get('text'), 1000)}")

    return "\n".join(parts) or "User requested support from IVY."


def request_summary_prompt(session_item, transcript, fallback_summary):
    return "\n".join([
        "Summarize this IVY Slack support conversation for a human IT support agent.",
        "Use only the supplied transcript and metadata.",
        "Include the user's issue, what IVY already answered or tried, and what still needs agent attention.",
        "Do not invent ticket keys, user names, troubleshooting steps, or resolution details.",
        "Return 2-4 concise sentences only.",
        "",
        "Session metadata:",
        compact_json({
            "session_id": session_item.get("session_id"),
            "lex_intent": session_item.get("support_lex_intent") or session_item.get("lex_intent"),
            "lex_state": session_item.get("support_lex_state") or session_item.get("lex_state"),
            "response_source": session_item.get("response_source"),
            "jira_status": session_item.get("jira_status"),
        }),
        "",
        "Deterministic context:",
        fallback_summary,
        "",
        "Transcript:",
        transcript_for_request_summary(transcript),
    ])


def extract_converse_text(response):
    parts = []
    message = ((response or {}).get("output") or {}).get("message") or {}

    for item in message.get("content", []):
        text = (item.get("text") or "").strip()
        if text:
            parts.append(text)

    return "\n".join(parts).strip()


def build_request_conversation_summary(session_item):
    transcript = request_summary_transcript(session_item)
    fallback_summary = deterministic_request_summary(session_item, transcript)

    if not ENABLE_REQUEST_AI_SUMMARY:
        return {
            "text": fallback_summary,
            "model_id": None,
            "usage": {},
            "error": None,
            "fallback_used": True,
            "transcript": transcript,
        }

    try:
        response = bedrock_runtime.converse(
            modelId=REQUEST_SUMMARY_MODEL_ID,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "text": request_summary_prompt(session_item, transcript, fallback_summary)
                        }
                    ]
                }
            ],
            inferenceConfig={
                "maxTokens": REQUEST_SUMMARY_MAX_TOKENS,
                "temperature": REQUEST_SUMMARY_TEMPERATURE,
            },
        )
        summary_text = extract_converse_text(response)

        if not summary_text:
            raise ValueError("Bedrock returned an empty request summary")

        return {
            "text": summary_text,
            "model_id": REQUEST_SUMMARY_MODEL_ID,
            "usage": response.get("usage", {}),
            "error": None,
            "fallback_used": False,
            "transcript": transcript,
        }

    except Exception as e:
        log_json({
            "level": "WARN",
            "message": "request_conversation_summary_failed",
            "model_id": REQUEST_SUMMARY_MODEL_ID,
            "error": str(e),
        })
        return {
            "text": fallback_summary,
            "model_id": REQUEST_SUMMARY_MODEL_ID,
            "usage": {},
            "error": str(e),
            "fallback_used": True,
            "transcript": transcript,
        }


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


def get_live_agent_config():
    if not live_agent_config_table:
        return None

    try:
        response = live_agent_config_table.get_item(
            Key={"intentName": LIVE_AGENT_CONFIG_INTENT}
        )
        return response.get("Item")

    except ClientError as e:
        log_json({
            "level": "WARN",
            "message": "live_agent_config_lookup_failed",
            "intent_name": LIVE_AGENT_CONFIG_INTENT,
            "error": str(e),
        })
        return None


def parse_config_parameters(config):
    parameters = (config or {}).get("parameters")
    if isinstance(parameters, dict):
        return parameters

    if isinstance(parameters, str) and parameters.strip():
        try:
            parsed = json.loads(parameters)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            log_json({
                "level": "WARN",
                "message": "live_agent_config_parameters_invalid",
                "intent_name": LIVE_AGENT_CONFIG_INTENT,
            })

    return {}


def compact_session_messages(session_item, limit=40):
    messages = session_item.get("session_messages") or []
    return messages[-limit:]


def build_live_agent_payload(session_item, body, session_id, now_iso):
    config = get_live_agent_config()
    config_parameters = parse_config_parameters(config)
    original_text = support_original_text(session_item) or session_item.get("last_user_text") or body.get("text") or ""
    raw_text = support_raw_text(session_item) or session_item.get("last_raw_user_text") or original_text
    summary_result = build_request_conversation_summary({
        **session_item,
        "session_id": session_id,
    })

    payload = {
        "type": "live_agent_handoff",
        "source": "slack",
        "session_id": session_id,
        "intent_name": LIVE_AGENT_CONFIG_INTENT,
        "configIntent": LIVE_AGENT_CONFIG_INTENT,
        "requested_at": now_iso,
        "title": (config or {}).get("title") or "Live agent support request",
        "description": original_text,
        "raw_text": raw_text,
        "conversation_summary": summary_result.get("text"),
        "conversation_summary_model_id": summary_result.get("model_id"),
        "conversation_summary_usage": summary_result.get("usage", {}),
        "conversation_summary_error": summary_result.get("error"),
        "conversation_summary_fallback_used": summary_result.get("fallback_used", False),
        "requestType": (config or {}).get("requestType"),
        "branching": (config or {}).get("branching"),
        "assignment": {
            "assignee": config_parameters.get("assignee"),
            "projectParams": config_parameters.get("projectParams") or config_parameters.get("assignee"),
        },
        "businessNotification": {
            "description": (config or {}).get("description"),
            "additionsDetails": [],
            "slackMessage": [],
        },
        "slack": {
            "channelId": body.get("channel") or session_item.get("channel"),
            "threadTs": session_item.get("thread_ts") or body.get("thread_ts"),
            "userId": body.get("user") or session_item.get("user"),
            "eventTs": body.get("ts"),
            "channelType": body.get("channel_type") or session_item.get("channel_type"),
            "conversationType": session_item.get("conversation_type"),
            "conversationMetadata": session_item.get("conversation_metadata") or {},
        },
        "user": body.get("user") or session_item.get("user"),
        "email": session_item.get("email") or session_item.get("user_email"),
        "atlassianAccountId": session_item.get("atlassianAccountId") or session_item.get("atlassian_account_id"),
        "conversation": compact_session_messages(session_item),
        "context": {
            "last_bot_reply": session_item.get("last_bot_reply"),
            "support_lex_reply": session_item.get("support_lex_reply"),
            "support_claude_reply": session_item.get("support_claude_reply"),
            "support_claude_error": session_item.get("support_claude_error"),
            "lex_intent": session_item.get("lex_intent"),
            "lex_state": session_item.get("lex_state"),
            "response_source": session_item.get("response_source"),
            "conversation_summary": summary_result.get("text"),
            "conversation_summary_model_id": summary_result.get("model_id"),
            "conversation_summary_fallback_used": summary_result.get("fallback_used", False),
        },
        "config": config,
    }

    return payload


def invoke_live_agent_webhook(payload):
    if not LIVE_AGENT_WEBHOOK_URL:
        return {
            "ok": False,
            "error": "missing_live_agent_webhook_url",
            "error_code": "missing_live_agent_webhook_url",
        }

    request = urllib.request.Request(
        LIVE_AGENT_WEBHOOK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=LIVE_AGENT_WEBHOOK_TIMEOUT_SECONDS) as response:
            response_text = response.read().decode("utf-8")
            if response.status < 200 or response.status >= 300:
                return {
                    "ok": False,
                    "error": f"Live agent webhook returned HTTP {response.status}",
                    "error_code": "live_agent_webhook_failed",
                    "status": response.status,
                }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "live_agent_webhook_failed",
        }

    parsed_response = None
    if response_text.strip():
        try:
            parsed_response = json.loads(response_text)
        except ValueError:
            parsed_response = {"message": response_text.strip()}

    return {
        "ok": True,
        "status": response.status,
        "response": parsed_response,
    }


def invoke_live_agent_function(payload):
    if not LIVE_AGENT_FUNCTION:
        return {
            "ok": False,
            "error": "missing_live_agent_function",
            "error_code": "missing_live_agent_function",
        }

    try:
        response = lambda_client.invoke(
            FunctionName=LIVE_AGENT_FUNCTION,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8")
        )

    except ClientError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "live_agent_lambda_invoke_failed",
        }

    raw_payload = response.get("Payload").read().decode("utf-8") if response.get("Payload") else ""
    parsed_payload = {}
    if raw_payload:
        try:
            parsed_payload = json.loads(raw_payload)
        except ValueError:
            parsed_payload = {"message": raw_payload}

    if response.get("FunctionError"):
        return {
            "ok": False,
            "error": parsed_payload.get("error") or response.get("FunctionError"),
            "error_code": parsed_payload.get("error_code") or "live_agent_lambda_error",
            "response": parsed_payload,
        }

    return {
        "ok": parsed_payload.get("ok", True),
        "status_code": response.get("StatusCode"),
        "response": parsed_payload,
        "error": parsed_payload.get("error"),
        "error_code": parsed_payload.get("error_code"),
    }


def invoke_live_agent_handoff(payload):
    if LIVE_AGENT_FUNCTION:
        result = invoke_live_agent_function(payload)
        result["target"] = "lambda"
        return result

    result = invoke_live_agent_webhook(payload)
    result["target"] = "webhook"
    return result


def live_agent_reply(result):
    response = result.get("response") if isinstance(result, dict) else None
    if isinstance(response, dict):
        for key in ("reply", "message", "text"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return LIVE_AGENT_DEFERRED_REPLY if result.get("ok") else LIVE_AGENT_FAILED_REPLY


def handle_interactive_action(session_item, body, session_id):
    action_id = body.get("action_id")
    now_iso = to_iso(datetime.now(timezone.utc))
    result = base_interactive_result(session_item)

    if action_id == ACTION_ID_CLOSE_AND_SUMMARIZE:
        if session_item.get("conversation_status") == "summarizing" or session_item.get("summary_status") == "started":
            result.update({
                "response_source": "manual_close_summary_duplicate",
                "reply": "This session is already being closed and summarized.",
            })
            return result

        if session_item.get("conversation_status") == "closed" or session_item.get("summary_status") == "completed":
            result.update({
                "response_source": "manual_close_summary_duplicate",
                "reply": "This session has already been closed.",
            })
            return result

        if session_item.get("conversation_status") in {"failed"}:
            result.update({
                "response_source": "manual_close_summary_failed",
                "reply": "This session is in a failed state. Please send a new message to start again.",
            })
            return result

        result.update({
            "lex_state": "Fulfilled",
            "response_source": "manual_close_summary",
            "next_action": None,
            "reply": "Closed this IVY session and started the summary.",
            "manual_close_summary": True,
            "manual_close_timeout_token": session_item.get("timeout_token"),
        })
        return result

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
        if not (
            has_pending_assistance_confirmation(session_item)
            or session_item.get("session_state") == SESSION_STATE_WAITING_FOR_USER
            or session_item.get("conversation_status") == "active"
        ):
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

        live_agent_result = invoke_live_agent_handoff(
            build_live_agent_payload(session_item, body, session_id, now_iso)
        )
        live_agent_ok = bool(live_agent_result.get("ok"))

        log_json({
            "level": "INFO" if live_agent_ok else "ERROR",
            "message": "live_agent_handoff_completed",
            "session_id": session_id,
            "target": live_agent_result.get("target"),
            "ok": live_agent_ok,
            "error_code": live_agent_result.get("error_code"),
        })

        result.update({
            "lex_state": "Fulfilled" if live_agent_ok else "Failed",
            "response_source": "live_agent",
            "next_action": None,
            "reply": live_agent_reply(live_agent_result),
            "support_options_status": "live_agent_requested" if live_agent_ok else "live_agent_failed",
            "support_resolved_at": now_iso if live_agent_ok else None,
            "live_agent_status": "requested" if live_agent_ok else "failed",
            "live_agent_requested_at": now_iso,
            "live_agent_error": None if live_agent_ok else live_agent_result.get("error"),
            "live_agent_error_code": None if live_agent_ok else live_agent_result.get("error_code"),
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
    image_files = body.get("files") or []
    has_image = bool(body.get("has_image") or image_files)
    user = body.get("user", "unknown-user")
    channel = body.get("channel")
    ts = body.get("ts")
    thread_ts = body.get("thread_ts")
    event_type = body.get("event_type")
    channel_type = body.get("channel_type")
    routing_reason = body.get("routing_reason")
    action_id = body.get("action_id")
    action_value = body.get("action_value")
    is_interactive_action = event_type == "interactive_action"

    conversation_metadata = fetch_conversation_metadata(channel, body.get("conversation_type") or channel_type)
    conversation_type = conversation_metadata.get("conversation_type") or channel_type
    dm_like_conversation = is_dm_like_conversation(conversation_metadata)
    session_thread_ts = None if dm_like_conversation else thread_ts
    session_id = f"{channel}:{user}:{session_thread_ts}" if session_thread_ts else f"{channel}:{user}"

    log_json({
        "level": "INFO",
        "message": "worker_processing_started",
        "event_id": event_id,
        "channel": channel,
        "event_type": event_type,
        "channel_type": channel_type,
        "routing_reason": routing_reason,
        "conversation_type": conversation_type,
        "dm_like_conversation": dm_like_conversation,
        "user": user,
        "text": text,
        "thread_ts": session_thread_ts,
        "image_file_count": len(image_files),
        "action_id": action_id
    })

    existing_session = get_session_item(session_id)
    reset_closed_session = (
        not is_interactive_action
        and not session_thread_ts
        and existing_session.get("conversation_status") in {"closed", "failed"}
    )
    if reset_closed_session:
        lex_session_id = f"{session_id}:{event_id or int(time.time())}"
        log_json({
            "level": "INFO",
            "message": "closed_session_reset_for_new_conversation",
            "event_id": event_id,
            "session_id": session_id,
            "previous_conversation_status": existing_session.get("conversation_status"),
            "lex_session_id": lex_session_id,
        })
        existing_session = {}
    else:
        lex_session_id = existing_session.get("lex_session_id") or session_id

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
    image_status = None
    image_requested_at = None
    image_analyzed_at = None
    image_error = None
    image_error_code = None
    image_summary = None
    image_resolution_source = None
    image_files_value = image_files if has_image else None
    image_match_issue_id = None
    image_match_score = None
    image_match_expected_lex_intent = None
    image_match_actual_lex_intent = None
    image_match_fallback_reason = None
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
    live_agent_error = None
    live_agent_error_code = None
    slack_blocks = None
    jira_confirmation_handled = False
    interactive_action_handled = False
    assistance_details_handled = False
    manual_close_summary = False
    manual_close_timeout_token = None

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
        live_agent_error = interactive_result.get("live_agent_error")
        live_agent_error_code = interactive_result.get("live_agent_error_code")
        manual_close_summary = interactive_result.get("manual_close_summary", False)
        manual_close_timeout_token = interactive_result.get("manual_close_timeout_token")

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

        if manual_close_summary:
            closed_at = datetime.now(timezone.utc).replace(microsecond=0)

            try:
                mark_session_summarizing(
                    session_id,
                    manual_close_timeout_token,
                    closed_at
                )

            except ClientError as e:
                if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    lex_reply = "That action is no longer active. Please send a new message."
                    send_slack_message(channel, lex_reply)
                    log_json({
                        "level": "INFO",
                        "message": "manual_close_summary_ignored",
                        "event_id": event_id,
                        "session_id": session_id,
                        "reason": "stale_or_closed_session"
                    })
                    return

                raise

            delete_timeout_schedule(session_id, "prompt")
            delete_timeout_schedule(session_id, "close")

            summarizer_result = invoke_summarizer({
                "session_id": session_id,
                "timeout_token": manual_close_timeout_token,
                "closed_at": to_iso(closed_at),
                "reason": "manual_close_summary",
                "conversation_type": conversation_type,
            })

            log_json({
                "level": "INFO" if summarizer_result.get("ok") else "ERROR",
                "message": "manual_close_summary_summarizer_invoked",
                "event_id": event_id,
                "session_id": session_id,
                "summarizer_function": SUMMARIZER_FUNCTION_NAME,
                "ok": summarizer_result.get("ok"),
                "error": summarizer_result.get("error"),
                "error_code": summarizer_result.get("error_code")
            })

            if not summarizer_result.get("ok"):
                send_slack_message(
                    channel,
                    "I could not complete the summary/save step. This session has not been fully closed."
                )

            log_json({
                "level": "INFO",
                "message": "manual_close_summary_completed",
                "event_id": event_id,
                "session_id": session_id,
                "summarizer_ok": summarizer_result.get("ok"),
                "error_code": summarizer_result.get("error_code")
            })
            return

    elif has_image and not is_interactive_action:
        image_requested_at = to_iso(datetime.now(timezone.utc))
        image_result = invoke_image_analysis(
            build_image_payload(body, session_id, text, raw_text, image_files)
        )
        image_analyzed_at = to_iso(datetime.now(timezone.utc))
        image_status = "analysis_completed" if image_result.get("ok") else "failed"
        image_error = image_result.get("error")
        image_error_code = image_result.get("error_code")
        image_summary = (
            image_result.get("summary")
            or image_result.get("message")
            or image_result.get("reply")
        )
        image_query = image_issue_text(text, image_result)

        if image_result.get("ok") and image_query:
            lex_session_attributes = {}
            match_result = find_matching_screenshot_issue(image_result)
            image_match_issue_id = match_result.get("issue_id")
            image_match_score = match_result.get("score")
            image_match_expected_lex_intent = match_result.get("expected_lex_intent")
            image_match_fallback_reason = match_result.get("reason")

            if match_result.get("matched"):
                response = lex.recognize_text(
                    botId=BOT_ID,
                    botAliasId=BOT_ALIAS_ID,
                    localeId=LOCALE_ID,
                    sessionId=lex_session_id,
                    text=match_result["lex_query"]
                )

                session_state = response.get("sessionState", {})
                intent = session_state.get("intent", {})
                lex_session_attributes = session_state.get("sessionAttributes", {}) or {}

                lex_intent = intent.get("name", "ImageVectorMatch")
                image_match_actual_lex_intent = lex_intent
                lex_state = intent.get("state", "UNKNOWN")
                lex_slots = simplify_slots(intent.get("slots", {}))
                lex_reply, lex_reply_empty = get_lex_reply(response.get("messages", []))

                expected_intent = match_result.get("expected_lex_intent")
                lex_intent_matches = not expected_intent or lex_intent == expected_intent

                if lex_intent_matches and lex_is_resolved(lex_intent, lex_state, lex_reply_empty):
                    image_status = "completed"
                    image_resolution_source = "screenshot_match_lex"
                    response_source = "image_screenshot_match_lex"
                    image_summary = match_result.get("lex_query") or image_summary

                else:
                    image_match_fallback_reason = "lex_intent_mismatch_or_unresolved"
                    log_json({
                        "level": "WARN",
                        "message": "screenshot_match_lex_rejected",
                        "event_id": event_id,
                        "session_id": session_id,
                        "issue_id": image_match_issue_id,
                        "match_score": image_match_score,
                        "expected_lex_intent": expected_intent,
                        "actual_lex_intent": lex_intent,
                        "lex_state": lex_state,
                        "lex_reply_empty": lex_reply_empty
                    })

                    gemini_result = invoke_gemini_fallback(
                        image_query,
                        image_result,
                        image_match_fallback_reason
                    )
                    if gemini_result.get("ok"):
                        lex_intent = "ImageLLMFallback"
                        lex_state = "Fulfilled"
                        lex_slots = {}
                        lex_reply = gemini_result["reply"]
                        lex_reply_empty = False
                        image_status = "completed"
                        image_resolution_source = "gemini"
                        response_source = "image_gemini"
                        image_summary = gemini_result.get("summary") or image_summary
                    else:
                        lex_intent = "ImageLLMFallback"
                        lex_state = "Failed"
                        lex_slots = {}
                        image_status = "failed"
                        image_resolution_source = "unresolved"
                        response_source = "image"
                        image_error = gemini_result.get("error")
                        image_error_code = gemini_result.get("error_code")
                        lex_reply = image_reply_from_result({
                            "ok": False,
                            "error": image_error,
                            "error_code": image_error_code,
                        })
                        lex_reply_empty = False

            else:
                gemini_result = invoke_gemini_fallback(
                    image_query,
                    image_result,
                    image_match_fallback_reason or "no_confident_screenshot_match"
                )
                if gemini_result.get("ok"):
                    lex_intent = "ImageLLMFallback"
                    lex_state = "Fulfilled"
                    lex_slots = {}
                    lex_session_attributes = {}
                    lex_reply = gemini_result["reply"]
                    lex_reply_empty = False
                    image_status = "completed"
                    image_resolution_source = "gemini"
                    response_source = "image_gemini"
                    image_summary = gemini_result.get("summary") or image_summary
                else:
                    lex_intent = "ImageLLMFallback"
                    lex_state = "Failed"
                    lex_slots = {}
                    lex_session_attributes = {}
                    image_status = "failed"
                    image_resolution_source = "unresolved"
                    response_source = "image"
                    image_error = gemini_result.get("error") or match_result.get("error")
                    image_error_code = gemini_result.get("error_code") or match_result.get("reason")
                    lex_reply = image_reply_from_result({
                        "ok": False,
                        "error": image_error,
                        "error_code": image_error_code,
                    })
                    lex_reply_empty = False

        else:
            lex_intent = "ImageRek"
            lex_state = "Failed"
            lex_slots = {}
            lex_session_attributes = {}
            lex_reply = image_reply_from_result(image_result)
            lex_reply_empty = False
            response_source = "image"
            image_resolution_source = "image_analysis"

        log_json({
            "level": "INFO" if image_status == "completed" else "WARN",
            "message": "image_flow_completed",
            "event_id": event_id,
            "session_id": session_id,
            "image_status": image_status,
            "image_resolution_source": image_resolution_source,
            "image_match_issue_id": image_match_issue_id,
            "image_match_score": image_match_score,
            "image_match_expected_lex_intent": image_match_expected_lex_intent,
            "image_match_actual_lex_intent": image_match_actual_lex_intent,
            "image_match_fallback_reason": image_match_fallback_reason,
            "image_file_count": len(image_files),
            "error_code": image_error_code
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
        and has_meaningful_user_issue(text)
        and has_meaningful_bot_answer(lex_reply)
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

    offer_close_summary = (
        not is_interactive_action
        and should_offer_close_summary(
            response_source,
            jira_status,
            assistance_status,
            support_options_status,
            existing_session,
            text,
            lex_reply,
        )
    )
    if offer_close_summary:
        conversation_status = "active"

    if conversation_status == "failed":
        session_state = SESSION_STATE_FAILED
    elif offer_close_summary:
        session_state = SESSION_STATE_WAITING_FOR_USER
    elif assistance_status == "pending_confirmation":
        session_state = SESSION_STATE_WAITING_FOR_USER
    elif assistance_status == "awaiting_details" or support_options_status in {"pending", "creating_jira"}:
        session_state = SESSION_STATE_COLLECTING_DETAILS
    elif conversation_status == "closed":
        session_state = SESSION_STATE_CLOSED
    else:
        session_state = SESSION_STATE_OPEN

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

    if live_agent_status == "requested":
        live_agent_requested_at = live_agent_requested_at or updated_at

    if rovo_should_invoke and rovo_status == "pending":
        rovo_requested_at = rovo_requested_at or updated_at

    transcript_append = build_transcript_append(text, lex_reply, ts)

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

    if offer_close_summary:
        slack_blocks = add_close_summary_actions(slack_blocks, lex_reply)

    created_at_expression = (
        "created_at = :created_at"
        if reset_closed_session
        else "created_at = if_not_exists(created_at, :created_at)"
    )

    update_expression = f"""
        SET
            #channel = :channel,
            #user = :user,
            last_event_id = :event_id,
            last_user_text = :last_user_text,
            last_raw_user_text = :last_raw_user_text,
            last_bot_reply = :last_bot_reply,
            thread_ts = :thread_ts,
            response_source = :response_source,
            claude_fallback_attempted = :claude_fallback_attempted,
            last_ts = :last_ts,
            last_activity_at = :last_activity_at,
            event_type = :event_type,
            channel_type = :channel_type,
            routing_reason = :routing_reason,
            conversation_type = :conversation_type,
            conversation_metadata = :conversation_metadata,
            lex_session_id = :lex_session_id,
            lex_intent = :lex_intent,
            lex_state = :lex_state,
            lex_slots = :lex_slots,
            conversation_status = :conversation_status,
            session_state = :session_state,
            timeout_status = :timeout_status,
            {created_at_expression},
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
        ":thread_ts": session_thread_ts,
        ":response_source": response_source,
        ":claude_fallback_attempted": claude_fallback_attempted,
        ":last_ts": ts,
        ":last_activity_at": updated_at,
        ":event_type": event_type,
        ":channel_type": channel_type,
        ":routing_reason": routing_reason,
        ":conversation_type": conversation_type,
        ":conversation_metadata": conversation_metadata,
        ":lex_session_id": lex_session_id,
        ":lex_intent": lex_intent,
        ":lex_state": lex_state,
        ":lex_slots": lex_slots,
        ":conversation_status": conversation_status,
        ":session_state": session_state,
        ":timeout_status": "scheduled" if timeout_state else "inactive",
        ":created_at": updated_at,
        ":updated_at": updated_at,
        ":ttl": ttl_epoch(),
        ":one": 1
    }

    remove_attributes = []
    if reset_closed_session:
        remove_attributes.extend([
            "manual_closed_at",
            "manual_close_reason",
            "summary_status",
            "summary_started_at",
            "summary_completed_at",
            "summary_failed_at",
            "summary_error",
            "summary_error_code",
            "conversation_summary",
            "summary_webhook_sent",
            "summary_webhook_error",
            "summary_audit_s3_key",
            "summary_audit_error",
        ])

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

    image_attributes = {
        "image_status": image_status,
        "image_requested_at": image_requested_at,
        "image_analyzed_at": image_analyzed_at,
        "image_error": image_error,
        "image_error_code": image_error_code,
        "image_summary": image_summary,
        "image_resolution_source": image_resolution_source,
        "image_files": image_files_value,
    }

    for attribute_name, attribute_value in image_attributes.items():
        if attribute_value is not None:
            value_name = f":{attribute_name}"
            update_expression += f"""
            ,
            {attribute_name} = {value_name}
        """
            expression_attribute_values[value_name] = attribute_value
        else:
            remove_attributes.append(attribute_name)

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
        "live_agent_error": live_agent_error,
        "live_agent_error_code": live_agent_error_code,
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

    if transcript_append:
        update_expression += """
            ,
            session_messages = list_append(if_not_exists(session_messages, :empty_list), :transcript_append)
        """
        expression_attribute_values[":empty_list"] = []
        expression_attribute_values[":transcript_append"] = transcript_append

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
        "live_agent_error_code": live_agent_error_code,
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
        "image_status": image_status,
        "image_resolution_source": image_resolution_source,
        "image_match_issue_id": image_match_issue_id,
        "image_match_score": image_match_score,
        "image_match_expected_lex_intent": image_match_expected_lex_intent,
        "image_match_actual_lex_intent": image_match_actual_lex_intent,
        "image_match_fallback_reason": image_match_fallback_reason,
        "image_error_code": image_error_code,
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
