import json
import os
import base64
import hashlib
from datetime import datetime, timezone
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

import chat_locks
import live_agent_capacity as capacity_dispatcher
import lambda_o3_jsm_oncall_user as oncall_user_helper
from lambda_o3_jsm_oncall_user import get_current_oncall_user

AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
lambda_client = boto3.client("lambda", region_name=AWS_REGION)

CONFIG_TABLE = os.environ.get("CONFIG_TABLE") or os.environ.get("LIVE_AGENT_CONFIG_TABLE")
SESSION_TABLE = os.environ.get("DYNAMODB_TABLE") or os.environ.get("SESSION_TABLE") or "o3_slack_sessions"
LIVE_AGENT_CONFIG_INTENT = os.environ.get("LIVE_AGENT_CONFIG_INTENT", "LiveAgent")
LIVE_AGENT_DEFAULT_REQUEST_TYPE = os.environ.get("LIVE_AGENT_DEFAULT_REQUEST_TYPE", "live_agent")
LIVE_AGENT_DEFAULT_BRANCHING = os.environ.get("LIVE_AGENT_DEFAULT_BRANCHING", "live_agent")
LIVE_AGENT_WEBHOOK_URL = os.environ.get("LIVE_AGENT_WEBHOOK_URL") or os.environ.get("AUTOMATION_WEBHOOK_URL")
LIVE_AGENT_WEBHOOK_SECRET = os.environ.get("LIVE_AGENT_WEBHOOK_SECRET", "").strip()
LIVE_AGENT_CALLBACK_SECRET = os.environ.get("LIVE_AGENT_CALLBACK_SECRET", "").strip()
LIVE_AGENT_WEBHOOK_TIMEOUT_SECONDS = int(os.environ.get("LIVE_AGENT_WEBHOOK_TIMEOUT_SECONDS", "10"))
LIVE_AGENT_TICKET_BASE_URL = os.environ.get("LIVE_AGENT_TICKET_BASE_URL", "https://innovyq.atlassian.net").rstrip("/")
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "86400"))
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
ONCALL_USER_TABLE = os.environ.get("ONCALL_USER_TABLE", "O3_JSMOps_Oncall")
ONCALL_CACHE_KEY = os.environ.get("ONCALL_CACHE_KEY", "current")
ONCALL_USER_FUNCTION = os.environ.get("ONCALL_USER_FUNCTION") or os.environ.get("JSM_ONCALL_USER_FUNCTION")
AGENT_CHAT_LOCK_TABLE = os.environ.get("AGENT_CHAT_LOCK_TABLE", "O3_Lambda_Agent_Chat_Locks")
ENABLE_LIVE_AGENT_CAPACITY = os.environ.get("ENABLE_LIVE_AGENT_CAPACITY", "false").lower() == "true"
LIVE_AGENT_MAX_ACTIVE_CHATS = int(os.environ.get("LIVE_AGENT_MAX_ACTIVE_CHATS", "5"))
MAX_QUEUE_DRAIN_PER_INVOCATION = max(1, int(os.environ.get("MAX_QUEUE_DRAIN_PER_INVOCATION", "1")))
ENABLE_CUSTOM_CAPACITY_DISPATCHER = (
    os.environ.get("ENABLE_CUSTOM_CAPACITY_DISPATCHER", "false").lower() == "true"
)
LIVE_AGENT_BUSY_REPLY = os.environ.get(
    "LIVE_AGENT_BUSY_REPLY",
    "All live agents are busy right now. You are in the queue and support will pick this up as soon as someone is available."
)
LIVE_AGENT_SUPPORT_MODE = os.environ.get("LIVE_AGENT_SUPPORT_MODE", "").strip().lower()
LIVE_AGENT_SUPPORT_CHANNEL_ID = os.environ.get("LIVE_AGENT_SUPPORT_CHANNEL_ID", "").strip()
try:
    LIVE_AGENT_AGENT_SLACK_MAP = json.loads(os.environ.get("LIVE_AGENT_AGENT_SLACK_MAP", "{}") or "{}")
    if not isinstance(LIVE_AGENT_AGENT_SLACK_MAP, dict):
        LIVE_AGENT_AGENT_SLACK_MAP = {}
except ValueError:
    LIVE_AGENT_AGENT_SLACK_MAP = {}

ACTION_ID_LIVE_AGENT_RESOLVE = "ivy_live_agent_resolve"
ACTION_ID_LIVE_AGENT_CANCEL = "ivy_live_agent_cancel"
ACTION_ID_LIVE_AGENT_REASSIGN = "ivy_live_agent_reassign"
ACTION_ID_LIVE_AGENT_REPLY = "ivy_live_agent_reply"
ACTION_ID_FEEDBACK_RATING = "ivy_feedback_rating"
LIVE_AGENT_RESOLVE_STATUS_NAMES = [
    name.strip()
    for name in os.environ.get("LIVE_AGENT_RESOLVE_STATUS_NAMES", "Resolved").split(",")
    if name.strip()
]
LIVE_AGENT_CANCEL_STATUS_NAMES = [
    name.strip()
    for name in os.environ.get("LIVE_AGENT_CANCEL_STATUS_NAMES", "Cancelled,Canceled").split(",")
    if name.strip()
]
LIVE_AGENT_RESOLUTION_TRANSCRIPT_LIMIT = max(
    1,
    int(os.environ.get("LIVE_AGENT_RESOLUTION_TRANSCRIPT_LIMIT", "120")),
)
LIVE_AGENT_RESOLUTION_COMMENT_TEXT_LIMIT = max(
    1000,
    int(os.environ.get("LIVE_AGENT_RESOLUTION_COMMENT_TEXT_LIMIT", "30000")),
)
LIVE_AGENT_RESOLUTION_COMMENT_MARKER = "[IVY_INTERNAL_RESOLUTION_SUMMARY]"

SUCCESS_REPLY = os.environ.get(
    "LIVE_AGENT_SUCCESS_REPLY",
    "I have sent this to live agent support. Someone from the support team will follow up."
)
FAILURE_REPLY = os.environ.get(
    "LIVE_AGENT_FAILURE_REPLY",
    "I could not send this to live agent support. Please try again later or create a Jira ticket."
)

config_table = dynamodb.Table(CONFIG_TABLE) if CONFIG_TABLE else None
session_table = dynamodb.Table(SESSION_TABLE) if SESSION_TABLE else None
oncall_user_table = dynamodb.Table(ONCALL_USER_TABLE) if ONCALL_USER_TABLE else None
agent_chat_lock_table = dynamodb.Table(AGENT_CHAT_LOCK_TABLE) if AGENT_CHAT_LOCK_TABLE else None


def log_json(data):
    print(json.dumps(data, default=str))


def text_or_empty(value):
    if value is None:
        return ""

    return str(value).strip()


def parse_json_map(value):
    if isinstance(value, dict):
        return value

    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}

    return {}


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ttl_epoch():
    return int(time.time()) + SESSION_TTL_SECONDS


def api_response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(body, default=str),
    }


def parse_api_gateway_body(event):
    if not isinstance(event, dict) or "body" not in event:
        return None

    raw_body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8")

    if isinstance(raw_body, dict):
        return raw_body

    if not str(raw_body).strip():
        return {}

    try:
        parsed = json.loads(raw_body)
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


def header_value(event, header_name):
    headers = event.get("headers") if isinstance(event, dict) else {}
    headers = headers if isinstance(headers, dict) else {}
    multi_value_headers = event.get("multiValueHeaders") if isinstance(event, dict) else {}
    multi_value_headers = multi_value_headers if isinstance(multi_value_headers, dict) else {}
    target = header_name.lower()

    for key, value in headers.items():
        if str(key).lower() == target:
            return text_or_empty(value)

    for key, values in multi_value_headers.items():
        if str(key).lower() == target and isinstance(values, list) and values:
            return text_or_empty(values[0])

    return ""


def validate_callback_secret(event):
    if not LIVE_AGENT_CALLBACK_SECRET:
        return True

    return header_value(event, "X-IVY-Callback-Secret") == LIVE_AGENT_CALLBACK_SECRET


def nested_get(data, *keys):
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def first_text(*values):
    for value in values:
        text = text_or_empty(value)
        if text:
            return text
    return ""


def present_text(data, key):
    if isinstance(data, dict) and key in data and data.get(key) is not None:
        return text_or_empty(data.get(key))
    return None


def canonical_slack_context(session_id="", session_root_ts="", slack_channel="", slack_thread_ts="", slack_user=""):
    channel = text_or_empty(slack_channel)
    thread_ts = text_or_empty(slack_thread_ts)
    user = text_or_empty(slack_user)
    return {
        "session_id": text_or_empty(session_id),
        "session_root_ts": text_or_empty(session_root_ts),
        "slack_channel": channel,
        "slack_thread_ts": thread_ts,
        "slack_user": user,
        "slack": {
            "channelId": channel,
            "threadTs": thread_ts,
            "userId": user,
        },
    }


def normalize_ticket_key(callback):
    return first_text(
        callback.get("ticket_key"),
        callback.get("issue_key"),
        callback.get("key"),
        nested_get(callback, "issue", "key"),
        nested_get(callback, "createdIssue", "key"),
    )


def normalize_ticket_url(callback, ticket_key):
    ticket_url = first_text(
        callback.get("ticket_url"),
        callback.get("issue_url"),
        callback.get("url"),
        nested_get(callback, "issue", "url"),
        nested_get(callback, "issue", "self"),
        nested_get(callback, "createdIssue", "url"),
        nested_get(callback, "createdIssue", "self"),
    )

    if ticket_url:
        return ticket_url

    base_url = first_text(callback.get("baseUrl"), callback.get("base_url")) or LIVE_AGENT_TICKET_BASE_URL
    if ticket_key and base_url:
        return f"{base_url.rstrip('/')}/browse/{ticket_key}"

    return ""


def is_jsm_callback(event):
    callback = parse_api_gateway_body(event) if isinstance(event, dict) and "body" in event else event
    if not isinstance(callback, dict):
        return False

    event_type = text_or_empty(callback.get("event_type")).lower()

    return event_type in {
        "live_agent_ticket_created",
        "live_agent_status_changed",
        "status_changed",
        "live_agent_public_comment_added",
        "public_comment_added",
        "agent_comment_added",
    }


def is_unsupported_jsm_callback(event):
    callback = parse_api_gateway_body(event) if isinstance(event, dict) and "body" in event else event
    if not isinstance(callback, dict):
        return False

    return (
        text_or_empty(callback.get("source")).lower() == "jsm_automation"
        and text_or_empty(callback.get("event_type")).lower() not in {
            "live_agent_ticket_created",
            "live_agent_status_changed",
            "status_changed",
            "live_agent_public_comment_added",
            "public_comment_added",
            "agent_comment_added",
        }
    )


def normalize_callback(event):
    callback = parse_api_gateway_body(event) if isinstance(event, dict) and "body" in event else event
    callback = callback if isinstance(callback, dict) else {}
    ticket_key = normalize_ticket_key(callback)
    ticket_url = normalize_ticket_url(callback, ticket_key)
    slack = callback.get("slack") if isinstance(callback.get("slack"), dict) else {}
    issue = callback.get("issue") if isinstance(callback.get("issue"), dict) else {}
    assignee = callback.get("assignee") if isinstance(callback.get("assignee"), dict) else {}
    fields = nested_get(issue, "fields") or {}
    issue_assignee = fields.get("assignee") if isinstance(fields.get("assignee"), dict) else {}
    attrs = lex_session_attributes(callback)
    session_id = first_text(callback.get("session_id"), attrs.get("session_id"), callback.get("sessionId"))
    session_root_ts = first_text(
        callback.get("session_root_ts"),
        attrs.get("session_root_ts"),
        callback.get("sessionRootTs"),
        attrs.get("sessionRootTs"),
        attrs.get("slackThreadTs"),
    )
    slack_channel = first_text(
        callback.get("slack_channel"),
        attrs.get("slack_channel"),
        callback.get("slackChannel"),
        attrs.get("slackChannel"),
        callback.get("channel"),
        slack.get("channelId"),
        slack.get("channel"),
        attrs.get("slackChannelId"),
    )
    slack_thread_ts = present_text(callback, "slack_thread_ts")
    if slack_thread_ts is None:
        slack_thread_ts = present_text(attrs, "slack_thread_ts")
    if slack_thread_ts is None:
        slack_thread_ts = first_text(
            callback.get("slackThreadTs"),
            attrs.get("slackThreadTs"),
            callback.get("thread_ts"),
            callback.get("threadTs"),
            slack.get("threadTs"),
            slack.get("thread_ts"),
        )
    slack_user = first_text(
        callback.get("slack_user"),
        attrs.get("slack_user"),
        callback.get("slackUser"),
        attrs.get("slackUser"),
        callback.get("user"),
        slack.get("userId"),
        slack.get("user"),
        attrs.get("slackUserId"),
    )

    return {
        "event_type": callback_event_type(callback),
        **canonical_slack_context(session_id, session_root_ts, slack_channel, slack_thread_ts, slack_user),
        "ticket_key": ticket_key,
        "ticket_url": ticket_url,
        "ticket_status": first_text(
            callback.get("ticket_status"),
            callback.get("status"),
            callback.get("issue_status"),
            nested_get(callback, "issue", "status", "name"),
            nested_get(callback, "createdIssue", "status", "name"),
        ),
        "jira_project": first_text(
            callback.get("jira_project"),
            callback.get("project_key"),
            nested_get(callback, "project", "key"),
            nested_get(issue, "fields", "project", "key"),
            ticket_key.split("-", 1)[0] if "-" in ticket_key else "",
        ),
        "issue_type": first_text(
            callback.get("issue_type"),
            callback.get("issueType"),
            nested_get(callback, "issue", "fields", "issuetype", "name"),
            nested_get(issue, "fields", "issuetype", "name"),
        ),
        "portal_request_type": first_text(
            callback.get("portal_request_type"),
            callback.get("portalRequestType"),
            callback.get("request_type"),
            callback.get("requestType"),
        ),
        "assignee_account_id": first_text(
            callback.get("assignee_account_id"),
            callback.get("assigneeAccountId"),
            callback.get("agent_account_id"),
            callback.get("agentAccountId"),
            assignee.get("accountId"),
            issue_assignee.get("accountId"),
        ),
        "assignee_email": first_text(
            callback.get("assignee_email"),
            callback.get("assigneeEmail"),
            assignee.get("emailAddress"),
            issue_assignee.get("emailAddress"),
        ),
        "assignee_display_name": first_text(
            callback.get("assignee_display_name"),
            callback.get("assigneeDisplayName"),
            callback.get("assignee_name"),
            callback.get("assigneeName"),
            assignee.get("displayName"),
            issue_assignee.get("displayName"),
        ),
        "user_request": first_text(
            callback.get("user_request"),
            callback.get("userRequest"),
            callback.get("description"),
            callback.get("request_text"),
            callback.get("requestText"),
        ),
        "raw_callback": callback,
    }


def normalize_live_agent_status(status):
    value = text_or_empty(status).lower()
    normalized = value.replace("-", " ").replace("_", " ")

    if not normalized:
        return ""

    if any(marker in normalized for marker in ("waiting for customer", "customer", "pending customer")):
        return "waiting_for_customer"

    if "waiting for support" in normalized:
        return "waiting_for_support"

    if any(marker in normalized for marker in ("resolved", "done", "closed", "complete")):
        return "resolved"

    if any(marker in normalized for marker in ("cancelled", "canceled")):
        return "cancelled"

    if "failed" in normalized:
        return "failed"

    if any(marker in normalized for marker in ("in progress", "open", "to do", "triage")):
        return "in_progress"

    return normalized.replace(" ", "_")


def display_status(status, normalized_status=""):
    value = text_or_empty(status)
    if value:
        return value

    if normalized_status:
        return normalized_status.replace("_", " ").title()

    return "Unknown"


def live_agent_ticket_reply(ticket_key, ticket_url):
    if ticket_key and ticket_url:
        return f"Live agent support ticket created: {ticket_key} {ticket_url}"

    if ticket_key:
        return f"Live agent support ticket created: {ticket_key}"

    return SUCCESS_REPLY


def live_agent_assigned_reply(ticket_key, ticket_url, agent_name):
    agent_text = text_or_empty(agent_name) or "a live agent"
    if ticket_key:
        return f"✅ I have sent this to live agent support. {agent_text} has been assigned to {ticket_key}."

    return f"✅ I have sent this to live agent support. {agent_text} has been assigned."


def live_agent_busy_queue_reply(ticket_key):
    if ticket_key:
        return f"⌛ All live agents are currently busy. Your request {ticket_key} is in the queue. We'll notify you as soon as an agent is available."

    return "⌛ All live agents are currently busy. Your request is in the queue. We'll notify you as soon as an agent is available."


def live_agent_no_oncall_queue_reply(ticket_key):
    if ticket_key:
        return f"⏳ Your live-agent request {ticket_key} was created, but no on-call agent is currently available. You are in the queue."

    return "⏳ Your live-agent request was created, but no on-call agent is currently available. You are in the queue."


def callback_ticket(callback):
    return {
        "ticket_key": callback.get("ticket_key") or "",
        "ticket_url": callback.get("ticket_url") or "",
        "status": callback.get("ticket_status") or "",
    }


def callback_slack_context(callback):
    return {
        "session_id": callback.get("session_id") or "",
        "slack_channel": callback.get("slack_channel") or "",
        "slack_thread_ts": callback.get("slack_thread_ts") or "",
        "slack_user": callback.get("slack_user") or "",
    }


def live_agent_agent_from_oncall(oncall_user):
    return {
        "account_id": oncall_user.get("account_id") or "",
        "display_name": oncall_user.get("display_name") or "",
        "email": oncall_user.get("email") or "",
    }


def live_agent_promoted_reply(ticket_key, ticket_url):
    if ticket_key and ticket_url:
        return f"A live agent is now available for {ticket_key}: {ticket_url}"

    if ticket_key:
        return f"A live agent is now available for {ticket_key}."

    return "A live agent is now available for your request."


def live_agent_queue_assigned_reply(ticket_key, agent_name):
    agent_text = text_or_empty(agent_name) or "The on-call agent"
    if ticket_key:
        return f"✅ A live agent is now available. {agent_text} has been assigned to {ticket_key}."

    return f"✅ A live agent is now available. {agent_text} has been assigned to your request."


def live_agent_slack_console_enabled():
    return LIVE_AGENT_SUPPORT_MODE == "slack_console" and bool(LIVE_AGENT_SUPPORT_CHANNEL_ID)


def slack_user_mention(user_id):
    user_id = text_or_empty(user_id)
    return f"<@{user_id}>" if user_id else "Unassigned"


def slack_mrkdwn(value, limit=2900):
    text = text_or_empty(value)
    if len(text) <= limit:
        return text
    return text[:limit - 3].rstrip() + "..."


def live_agent_issue_title(callback):
    title = first_text(
        callback.get("user_request"),
        callback.get("description"),
        callback.get("conversation_summary"),
        callback.get("ticket_key"),
        "Live-agent request",
    )
    title = " ".join(title.split())
    return slack_mrkdwn(title, 140)


def live_agent_support_action_value(action, ticket_key):
    return json.dumps(
        {
            "action": action,
            "ticket_key": ticket_key or "",
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def resolve_agent_slack_user_id(callback, assignment=None):
    assignment = assignment or {}
    agent = assignment.get("agent") if isinstance(assignment.get("agent"), dict) else {}
    candidates = [
        callback.get("assigned_agent_slack_user_id"),
        callback.get("assignee_slack_user_id"),
        assignment.get("assigned_agent_slack_user_id"),
        agent.get("slack_user_id"),
    ]
    for value in candidates:
        value = text_or_empty(value)
        if value:
            return value

    lookup_keys = [
        callback.get("assignee_account_id"),
        callback.get("assignee_email"),
        assignment.get("assigned_jira_account_id"),
        assignment.get("assigned_agent_id"),
        assignment.get("assigned_agent_name"),
        agent.get("jira_account_id"),
        agent.get("agent_id"),
        agent.get("email"),
        agent.get("display_name"),
    ]
    for key in lookup_keys:
        value = LIVE_AGENT_AGENT_SLACK_MAP.get(text_or_empty(key))
        if text_or_empty(value):
            return text_or_empty(value)

    return ""


def live_agent_support_thread_text(callback, assignment, assigned_slack_user_id):
    ticket_key = callback.get("ticket_key") or "live-agent request"
    assignment_status = text_or_empty(assignment.get("assignment_status")) or "UNKNOWN"
    parts = [
        f"*{live_agent_issue_title(callback)}*",
        f"*Ticket:* {ticket_key}",
        f"*Status:* {assignment_status.title()}",
        f"*Assigned agent:* {slack_user_mention(assigned_slack_user_id)}",
    ]
    if callback.get("ticket_url"):
        parts.append(f"*JSM ticket:* {callback['ticket_url']}")
    if callback.get("slack_user"):
        parts.append(f"*Requester:* <@{callback['slack_user']}>")
    if callback.get("user_request"):
        parts.append(f"*User request:*\n{callback['user_request']}")
    elif callback.get("conversation_summary"):
        parts.append(f"*Summary:*\n{callback['conversation_summary']}")
    parts.append("Reply in this thread to message the requester. Use Reply to customer if thread events are unavailable.")
    return "\n\n".join(parts)


def live_agent_support_thread_blocks(callback, assignment, assigned_slack_user_id):
    ticket_key = callback.get("ticket_key") or ""
    assignment_status = text_or_empty(assignment.get("assignment_status")) or "UNKNOWN"
    request_text = first_text(callback.get("user_request"), callback.get("conversation_summary"))
    context_items = [
        f"*Ticket:*\n{ticket_key or '-'}",
        f"*Status:*\n{assignment_status.title()}",
        f"*Assigned:*\n{slack_user_mention(assigned_slack_user_id)}",
    ]
    if callback.get("slack_user"):
        context_items.append(f"*Requester:*\n<@{callback['slack_user']}>")

    elements = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Reply to customer"},
            "style": "primary",
            "action_id": ACTION_ID_LIVE_AGENT_REPLY,
            "value": live_agent_support_action_value("reply", ticket_key),
        },
    ]
    if callback.get("ticket_url"):
        elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "Open JSM ticket"},
            "url": callback["ticket_url"],
            "action_id": "ivy_live_agent_open_ticket",
        })
    elements.extend([
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Resolve"},
            "action_id": ACTION_ID_LIVE_AGENT_RESOLVE,
            "value": live_agent_support_action_value("resolve", ticket_key),
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Cancel"},
            "style": "danger",
            "action_id": ACTION_ID_LIVE_AGENT_CANCEL,
            "value": live_agent_support_action_value("cancel", ticket_key),
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Reassign"},
            "action_id": ACTION_ID_LIVE_AGENT_REASSIGN,
            "value": live_agent_support_action_value("reassign", ticket_key),
        },
    ])

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": live_agent_issue_title(callback)},
        },
        {
            "type": "section",
            "fields": [{"type": "mrkdwn", "text": item} for item in context_items],
        },
    ]
    if request_text:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*User request:*\n{slack_mrkdwn(request_text, 1800)}"},
        })
    blocks.extend([
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Reply in this thread to message the requester. Use Reply to customer if thread events are unavailable.",
                }
            ],
        },
        {
            "type": "actions",
            "block_id": "ivy_live_agent_support_actions",
            "elements": elements,
        },
    ])
    return blocks


def mark_live_agent_support_thread(pointer_session_id, target_session_id, support_result):
    if not (session_table and pointer_session_id and support_result.get("ts")):
        return

    now_iso = utc_now_iso()
    values = {
        ":support_channel": LIVE_AGENT_SUPPORT_CHANNEL_ID,
        ":support_thread_ts": support_result["ts"],
        ":assigned_slack_user": support_result.get("assigned_slack_user_id") or "",
        ":bridge_status": "active",
        ":now": now_iso,
        ":ttl": ttl_epoch(),
    }
    expression = """
        SET
            support_channel = :support_channel,
            support_thread_ts = :support_thread_ts,
            assigned_agent_slack_user_id = :assigned_slack_user,
            bridge_status = :bridge_status,
            support_thread_created_at = :now,
            updated_at = :now,
            #ttl = :ttl
    """
    names = {"#ttl": "ttl"}

    for session_id in [pointer_session_id, target_session_id]:
        if not session_id:
            continue
        session_table.update_item(
            Key={"session_id": session_id},
            UpdateExpression=expression,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )


def post_live_agent_support_thread(callback, assignment):
    if not live_agent_slack_console_enabled():
        return {"attempted": False, "reason": "slack_console_disabled"}

    assigned_slack_user_id = resolve_agent_slack_user_id(callback, assignment)
    text = live_agent_support_thread_text(callback, assignment, assigned_slack_user_id)
    blocks = live_agent_support_thread_blocks(callback, assignment, assigned_slack_user_id)
    result = post_slack_message(LIVE_AGENT_SUPPORT_CHANNEL_ID, text, blocks=blocks)
    return {
        "attempted": True,
        "ts": result.get("ts"),
        "assigned_slack_user_id": assigned_slack_user_id,
    }


def agent_key_from_user(user):
    if not isinstance(user, dict):
        return ""

    for field in ("account_id", "accountId", "email", "emailAddress", "display_name", "displayName"):
        value = text_or_empty(user.get(field))
        if value:
            return value.lower()

    return ""


def normalize_callback_agent(callback):
    agent = {
        "account_id": callback.get("assignee_account_id") or "",
        "email": callback.get("assignee_email") or "",
        "display_name": callback.get("assignee_display_name") or "",
        "source": "callback",
    }
    agent["agent_key"] = agent_key_from_user(agent)
    return agent if agent["agent_key"] else {}


def current_oncall_user():
    if not oncall_user_table:
        return {}

    try:
        response = oncall_user_table.get_item(Key={"oncall_key": ONCALL_CACHE_KEY})
        item = response.get("Item") or {}
        if item:
            return item
    except ClientError as error:
        log_json({
            "level": "WARN",
            "message": "oncall_user_lookup_get_failed",
            "error": str(error),
        })

    try:
        response = oncall_user_table.scan(Limit=25)
        items = response.get("Items") or []
    except ClientError as error:
        log_json({
            "level": "WARN",
            "message": "oncall_user_lookup_scan_failed",
            "error": str(error),
        })
        return {}

    for item in items:
        if text_or_empty(item.get("oncall_key")).lower() == ONCALL_CACHE_KEY.lower():
            return item

    for item in items:
        if item.get("is_current") is True or text_or_empty(item.get("status")).lower() in {"current", "active"}:
            return item

    return items[0] if items else {}


def resolve_live_agent_agent(callback):
    agent = normalize_callback_agent(callback)
    if agent:
        return agent

    item = current_oncall_user()
    if not item:
        return {}

    agent = {
        "account_id": first_text(item.get("account_id"), item.get("accountId")),
        "email": first_text(item.get("email"), item.get("emailAddress")),
        "display_name": first_text(item.get("display_name"), item.get("displayName")),
        "source": "oncall_cache",
    }
    agent["agent_key"] = agent_key_from_user(agent)
    return agent if agent["agent_key"] else {}


def normalize_oncall_helper_user(result):
    if not isinstance(result, dict):
        return {}

    if result.get("account_id") or result.get("display_name") or result.get("email"):
        user = result
    else:
        user = result.get("assignee")
    if not isinstance(user, dict):
        user = result.get("current_user")
    if not isinstance(user, dict):
        user = result.get("user")
    if not isinstance(user, dict):
        return {}

    normalized = {
        "assignee_account_id": first_text(user.get("account_id"), user.get("accountId")),
        "assignee_display_name": first_text(user.get("display_name"), user.get("displayName")),
        "assignee_email": first_text(user.get("email"), user.get("emailAddress")),
    }
    return normalized if any(normalized.values()) else {}


def invoke_oncall_user_helper(callback):
    if not ONCALL_USER_FUNCTION:
        return {}

    payload = {
        "source": "o3_live_agent",
        "event_type": "resolve_live_agent_assignee",
        "ticket_key": callback.get("ticket_key"),
        "ticket_url": callback.get("ticket_url"),
        "session_id": callback.get("session_id"),
        "slack_channel": callback.get("slack_channel"),
        "slack_thread_ts": callback.get("slack_thread_ts"),
        "slack_user": callback.get("slack_user"),
    }

    try:
        response = lambda_client.invoke(
            FunctionName=ONCALL_USER_FUNCTION,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8"),
        )
    except ClientError as error:
        log_json({
            "level": "WARN",
            "message": "oncall_user_helper_invoke_failed",
            "ticket_key": callback.get("ticket_key"),
            "session_id": callback.get("session_id"),
            "error": str(error),
        })
        return {}

    raw_payload = response.get("Payload").read().decode("utf-8") if response.get("Payload") else ""
    try:
        result = json.loads(raw_payload) if raw_payload else {}
    except ValueError:
        result = {"message": raw_payload}

    if response.get("FunctionError") or not result.get("ok"):
        log_json({
            "level": "WARN",
            "message": "oncall_user_helper_no_assignee",
            "ticket_key": callback.get("ticket_key"),
            "session_id": callback.get("session_id"),
            "function_error": response.get("FunctionError"),
            "error_code": result.get("error_code"),
            "error": result.get("error"),
        })
        return {}

    return normalize_oncall_helper_user(result)


def enrich_missing_assignee(callback):
    if callback.get("assignee_account_id"):
        return callback

    helper_user = invoke_oncall_user_helper(callback)
    if not helper_user:
        return callback

    enriched = {**callback}
    for field, value in helper_user.items():
        if value and not enriched.get(field):
            enriched[field] = value

    return enriched


def ticket_lock_id(ticket_key):
    return f"live_agent_chat_ticket:{ticket_key}"


def agent_slot_lock_id(agent_key, slot_number):
    slot_agent = hashlib.sha256(agent_key.encode("utf-8")).hexdigest()[:24]
    return f"live_agent_chat_slot:{slot_agent}:{slot_number}"


def get_chat_lock(ticket_key):
    if not (agent_chat_lock_table and ticket_key):
        return {}

    response = agent_chat_lock_table.get_item(Key={"lock_id": ticket_lock_id(ticket_key)})
    return response.get("Item") or {}


def scan_chat_locks(filter_expression):
    if not agent_chat_lock_table:
        return []

    items = []
    scan_kwargs = {"FilterExpression": filter_expression}
    while True:
        response = agent_chat_lock_table.scan(**scan_kwargs)
        items.extend(response.get("Items") or [])
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    return items


def active_chat_count(agent_key):
    return len(scan_chat_locks(
        Attr("record_type").eq("ticket_lock")
        & Attr("agent_key").eq(agent_key)
        & Attr("lock_status").eq("active")
    ))


def acquire_agent_slot(agent, callback, now_iso):
    if not agent_chat_lock_table:
        return None

    ttl = ttl_epoch()
    for slot_number in range(1, LIVE_AGENT_MAX_ACTIVE_CHATS + 1):
        lock_id = agent_slot_lock_id(agent["agent_key"], slot_number)
        try:
            agent_chat_lock_table.put_item(
                Item={
                    "lock_id": lock_id,
                    "record_type": "agent_slot",
                    "lock_status": "active",
                    "slot_number": slot_number,
                    "agent_key": agent["agent_key"],
                    "agent_account_id": agent.get("account_id") or "",
                    "agent_email": agent.get("email") or "",
                    "agent_display_name": agent.get("display_name") or "",
                    "ticket_key": callback["ticket_key"],
                    "session_id": callback.get("session_id") or "",
                    "slack_channel": callback.get("slack_channel") or "",
                    "slack_thread_ts": callback.get("slack_thread_ts") or "",
                    "slack_user": callback.get("slack_user") or "",
                    "created_at": now_iso,
                    "updated_at": now_iso,
                    "ttl": ttl,
                },
                ConditionExpression=(
                    Attr("lock_id").not_exists()
                    | Attr("lock_status").ne("active")
                ),
            )
            return {
                "lock_id": lock_id,
                "slot_number": slot_number,
            }
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                continue
            raise

    return None


def release_agent_slot(slot_lock_id, now_iso):
    if not (agent_chat_lock_table and slot_lock_id):
        return

    agent_chat_lock_table.update_item(
        Key={"lock_id": slot_lock_id},
        UpdateExpression="""
            SET
                lock_status = :released,
                released_at = :now,
                updated_at = :now,
                #ttl = :ttl
        """,
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={
            ":released": "released",
            ":now": now_iso,
            ":ttl": ttl_epoch(),
        },
    )


def create_ticket_chat_lock(callback, agent, lock_status, now_iso, slot=None):
    if not agent_chat_lock_table:
        return {}

    item = {
        "lock_id": ticket_lock_id(callback["ticket_key"]),
        "record_type": "ticket_lock",
        "lock_status": lock_status,
        "agent_key": agent["agent_key"],
        "agent_account_id": agent.get("account_id") or "",
        "agent_email": agent.get("email") or "",
        "agent_display_name": agent.get("display_name") or "",
        "agent_source": agent.get("source") or "",
        "ticket_key": callback["ticket_key"],
        "ticket_url": callback.get("ticket_url") or "",
        "session_id": callback.get("session_id") or "",
        "slack_channel": callback.get("slack_channel") or "",
        "slack_thread_ts": callback.get("slack_thread_ts") or "",
        "slack_user": callback.get("slack_user") or "",
        "created_at": now_iso,
        "updated_at": now_iso,
        "ttl": ttl_epoch(),
    }

    if lock_status == "waiting":
        item["queued_at"] = now_iso

    if slot:
        item["slot_lock_id"] = slot["lock_id"]
        item["slot_number"] = slot["slot_number"]
        item["activated_at"] = now_iso

    try:
        agent_chat_lock_table.put_item(
            Item=item,
            ConditionExpression=Attr("lock_id").not_exists(),
        )
        return {
            **item,
            "created": True,
        }
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return {
                **get_chat_lock(callback["ticket_key"]),
                "created": False,
            }
        raise


def update_live_agent_capacity_state(callback, pointer_session_id, capacity_status, agent, lock_item=None):
    if not session_table:
        return

    now_iso = utc_now_iso()
    lock_item = lock_item or {}
    values = {
        ":capacity_status": capacity_status,
        ":agent_key": agent.get("agent_key") or "",
        ":agent_account_id": agent.get("account_id") or "",
        ":agent_email": agent.get("email") or "",
        ":agent_display_name": agent.get("display_name") or "",
        ":lock_id": lock_item.get("lock_id") or ticket_lock_id(callback["ticket_key"]),
        ":slot_lock_id": lock_item.get("slot_lock_id") or "",
        ":now": now_iso,
        ":ttl": ttl_epoch(),
    }
    expression = """
        SET
            live_agent_capacity_status = :capacity_status,
            live_agent_agent_key = :agent_key,
            live_agent_agent_account_id = :agent_account_id,
            live_agent_agent_email = :agent_email,
            live_agent_agent_display_name = :agent_display_name,
            live_agent_chat_lock_id = :lock_id,
            live_agent_slot_lock_id = :slot_lock_id,
            live_agent_capacity_updated_at = :now,
            updated_at = :now,
            #ttl = :ttl
    """

    if capacity_status == "waiting":
        expression += """,
            live_agent_status = :queued_status,
            live_agent_queue_enqueued_at = if_not_exists(live_agent_queue_enqueued_at, :now)
        """
        values[":queued_status"] = "queued"

    if capacity_status == "active" and lock_item.get("queued_at"):
        expression += """,
            live_agent_queue_promoted_at = :now
        """

    target_keys = []
    if callback.get("session_id"):
        target_keys.append(callback["session_id"])
    if pointer_session_id:
        target_keys.append(pointer_session_id)

    for session_id in dict.fromkeys(target_keys):
        session_table.update_item(
            Key={"session_id": session_id},
            UpdateExpression=expression,
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues=values,
        )


def apply_chat_capacity(callback, pointer_session_id):
    if not agent_chat_lock_table or LIVE_AGENT_MAX_ACTIVE_CHATS < 1:
        return {
            "status": "disabled",
            "reason": "chat_lock_table_not_configured",
        }

    existing_lock = get_chat_lock(callback["ticket_key"])
    if existing_lock.get("lock_status") == "active":
        return {
            "status": "active",
            "duplicate": True,
            "agent_key": existing_lock.get("agent_key"),
            "active_chat_count": active_chat_count(existing_lock.get("agent_key")),
            "lock": existing_lock,
        }

    if existing_lock.get("lock_status") == "waiting":
        return {
            "status": "waiting",
            "duplicate": True,
            "agent_key": existing_lock.get("agent_key"),
            "active_chat_count": active_chat_count(existing_lock.get("agent_key")),
            "lock": existing_lock,
        }

    agent = resolve_live_agent_agent(callback)
    if not agent:
        return {
            "status": "disabled",
            "reason": "missing_agent",
        }

    now_iso = utc_now_iso()
    slot = acquire_agent_slot(agent, callback, now_iso)
    if slot:
        lock_item = create_ticket_chat_lock(callback, agent, "active", now_iso, slot=slot)
        if not lock_item.get("created"):
            release_agent_slot(slot["lock_id"], now_iso)
            return {
                "status": "active" if lock_item.get("lock_status") == "active" else lock_item.get("lock_status") or "duplicate",
                "duplicate": True,
                "agent_key": lock_item.get("agent_key"),
                "active_chat_count": active_chat_count(agent["agent_key"]),
                "lock": lock_item,
            }

        update_live_agent_capacity_state(callback, pointer_session_id, "active", agent, lock_item)
        return {
            "status": "active",
            "agent_key": agent["agent_key"],
            "active_chat_count": active_chat_count(agent["agent_key"]),
            "lock": lock_item,
        }

    lock_item = create_ticket_chat_lock(callback, agent, "waiting", now_iso)
    update_live_agent_capacity_state(callback, pointer_session_id, "waiting", agent, lock_item)
    return {
        "status": "waiting",
        "agent_key": agent["agent_key"],
        "active_chat_count": active_chat_count(agent["agent_key"]),
        "lock": lock_item,
    }


def release_chat_lock_for_ticket(ticket_key):
    if not agent_chat_lock_table:
        return {}

    lock_item = get_chat_lock(ticket_key)
    if lock_item.get("lock_status") != "active":
        return {
            "released": False,
            "reason": "not_active",
            "lock": lock_item,
        }

    now_iso = utc_now_iso()
    agent_chat_lock_table.update_item(
        Key={"lock_id": lock_item["lock_id"]},
        UpdateExpression="""
            SET
                lock_status = :released,
                released_at = :now,
                updated_at = :now,
                #ttl = :ttl
        """,
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={
            ":released": "released",
            ":now": now_iso,
            ":ttl": ttl_epoch(),
        },
    )
    release_agent_slot(lock_item.get("slot_lock_id"), now_iso)
    return {
        "released": True,
        "agent_key": lock_item.get("agent_key"),
        "lock": lock_item,
    }


def waiting_chat_locks(agent_key):
    items = scan_chat_locks(
        Attr("record_type").eq("ticket_lock")
        & Attr("agent_key").eq(agent_key)
        & Attr("lock_status").eq("waiting")
    )
    return sorted(items, key=lambda item: text_or_empty(item.get("queued_at")) or text_or_empty(item.get("created_at")))


def acquire_queue_promotion_notice(pointer_session_id):
    if not (session_table and pointer_session_id):
        return False

    now_iso = utc_now_iso()
    try:
        session_table.update_item(
            Key={"session_id": pointer_session_id},
            UpdateExpression="""
                SET
                    live_agent_queue_promotion_slack_status = :posting,
                    live_agent_queue_promotion_started_at = :now,
                    updated_at = :now,
                    #ttl = :ttl
            """,
            ConditionExpression=Attr("live_agent_queue_promotion_slack_status").not_exists(),
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":posting": "posting",
                ":now": now_iso,
                ":ttl": ttl_epoch(),
            },
        )
        return True
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def mark_queue_promotion_notice(pointer_session_id, status, result=None, error=None):
    if not (session_table and pointer_session_id):
        return

    now_iso = utc_now_iso()
    values = {
        ":status": status,
        ":now": now_iso,
        ":ttl": ttl_epoch(),
    }
    expression = """
        SET
            live_agent_queue_promotion_slack_status = :status,
            updated_at = :now,
            #ttl = :ttl
    """
    if result and result.get("ts"):
        expression += """,
            live_agent_queue_promotion_slack_ts = :slack_ts,
            live_agent_queue_promotion_sent_at = :now
        """
        values[":slack_ts"] = result["ts"]
    if error:
        expression += """,
            live_agent_queue_promotion_slack_error = :error
        """
        values[":error"] = str(error)

    session_table.update_item(
        Key={"session_id": pointer_session_id},
        UpdateExpression=expression,
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues=values,
    )


def promote_oldest_waiting_chat(agent_key):
    if not (agent_chat_lock_table and agent_key):
        return {
            "promoted": False,
            "reason": "missing_agent_or_table",
        }

    for waiting_lock in waiting_chat_locks(agent_key):
        callback = {
            "ticket_key": waiting_lock.get("ticket_key"),
            "ticket_url": waiting_lock.get("ticket_url"),
            "session_id": waiting_lock.get("session_id"),
            "slack_channel": waiting_lock.get("slack_channel"),
            "slack_thread_ts": waiting_lock.get("slack_thread_ts"),
            "slack_user": waiting_lock.get("slack_user"),
        }
        agent = {
            "agent_key": waiting_lock.get("agent_key") or "",
            "account_id": waiting_lock.get("agent_account_id") or "",
            "email": waiting_lock.get("agent_email") or "",
            "display_name": waiting_lock.get("agent_display_name") or "",
            "source": waiting_lock.get("agent_source") or "queue",
        }
        now_iso = utc_now_iso()
        slot = acquire_agent_slot(agent, callback, now_iso)
        if not slot:
            return {
                "promoted": False,
                "reason": "capacity_full",
            }

        try:
            agent_chat_lock_table.update_item(
                Key={"lock_id": waiting_lock["lock_id"]},
                UpdateExpression="""
                    SET
                        lock_status = :active,
                        slot_lock_id = :slot_lock_id,
                        slot_number = :slot_number,
                        activated_at = :now,
                        updated_at = :now,
                        #ttl = :ttl
                """,
                ConditionExpression=Attr("lock_status").eq("waiting"),
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":active": "active",
                    ":slot_lock_id": slot["lock_id"],
                    ":slot_number": slot["slot_number"],
                    ":now": now_iso,
                    ":ttl": ttl_epoch(),
                },
            )
        except ClientError as error:
            release_agent_slot(slot["lock_id"], now_iso)
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                continue
            raise

        promoted_lock = {
            **waiting_lock,
            "lock_status": "active",
            "slot_lock_id": slot["lock_id"],
            "slot_number": slot["slot_number"],
            "activated_at": now_iso,
        }
        pointer_session_id = f"live_agent_ticket:{waiting_lock['ticket_key']}"
        update_live_agent_capacity_state(callback, pointer_session_id, "active", agent, promoted_lock)

        slack_result = {"attempted": False}
        if waiting_lock.get("slack_channel") and acquire_queue_promotion_notice(pointer_session_id):
            try:
                slack_result = post_slack_message(
                    waiting_lock["slack_channel"],
                    live_agent_promoted_reply(waiting_lock.get("ticket_key"), waiting_lock.get("ticket_url")),
                    text_or_empty(waiting_lock.get("slack_thread_ts")),
                )
                mark_queue_promotion_notice(pointer_session_id, "sent", result=slack_result)
            except Exception as error:
                mark_queue_promotion_notice(pointer_session_id, "failed", error=error)
                log_json({
                    "level": "ERROR",
                    "message": "live_agent_queue_promotion_slack_failed",
                    "ticket_key": waiting_lock.get("ticket_key"),
                    "error": str(error),
                })
                slack_result = {
                    "attempted": True,
                    "ok": False,
                    "error": str(error),
                }

        return {
            "promoted": True,
            "ticket_key": waiting_lock.get("ticket_key"),
            "agent_key": agent_key,
            "slot_lock_id": slot["lock_id"],
            "slack": slack_result,
        }

    return {
        "promoted": False,
        "reason": "queue_empty",
    }


def update_live_agent_session(callback):
    if not session_table:
        return

    now_iso = utc_now_iso()
    expression_values = {
        ":live_agent_status": "ticket_created",
        ":support_options_status": "live_agent_requested",
        ":ticket_key": callback["ticket_key"],
        ":callback_at": now_iso,
        ":updated_at": now_iso,
        ":ttl": ttl_epoch(),
    }
    update_expression = """
        SET
            live_agent_status = :live_agent_status,
            support_options_status = :support_options_status,
            live_agent_ticket_key = :ticket_key,
            last_live_agent_ticket_key = :ticket_key,
            live_agent_requested_at = if_not_exists(live_agent_requested_at, :updated_at),
            live_agent_callback_at = :callback_at,
            live_agent_updated_at = :updated_at,
            updated_at = :updated_at,
            #ttl = :ttl
    """
    expression_attribute_names = {
        "#ttl": "ttl",
    }

    remove_attributes = []
    if callback.get("ticket_url"):
        update_expression += """,
            live_agent_ticket_url = :ticket_url,
            last_live_agent_ticket_url = :ticket_url
        """
        expression_values[":ticket_url"] = callback["ticket_url"]
    else:
        remove_attributes.extend(["live_agent_ticket_url", "last_live_agent_ticket_url"])

    if callback.get("ticket_status"):
        update_expression += """,
            live_agent_jira_status = :jira_status
        """
        expression_values[":jira_status"] = callback["ticket_status"]
    else:
        remove_attributes.append("live_agent_jira_status")

    optional_fields = {
        "live_agent_jira_project": callback.get("jira_project"),
        "live_agent_issue_type": callback.get("issue_type"),
        "live_agent_portal_request_type": callback.get("portal_request_type"),
        "live_agent_assignee_account_id": callback.get("assignee_account_id"),
        "live_agent_assignee_display_name": callback.get("assignee_display_name"),
        "live_agent_assignee_email": callback.get("assignee_email"),
        "live_agent_user_request": callback.get("user_request"),
    }

    for attribute_name, attribute_value in optional_fields.items():
        if attribute_value:
            value_name = f":{attribute_name}"
            update_expression += f""",
            {attribute_name} = {value_name}
        """
            expression_values[value_name] = attribute_value
        else:
            remove_attributes.append(attribute_name)

    if remove_attributes:
        update_expression += " REMOVE " + ", ".join(remove_attributes)

    session_table.update_item(
        Key={
            "session_id": callback["session_id"]
        },
        UpdateExpression=update_expression,
        ExpressionAttributeNames=expression_attribute_names,
        ExpressionAttributeValues=expression_values,
    )


def put_live_agent_ticket_pointer(callback):
    if not session_table:
        return None

    now_iso = utc_now_iso()
    pointer_session_id = f"live_agent_ticket:{callback['ticket_key']}"
    item = {
        "session_id": pointer_session_id,
        "pointer_type": "live_agent_ticket",
        "target_session_id": callback["session_id"],
        "ticket_key": callback["ticket_key"],
        "ticket_url": callback.get("ticket_url") or "",
        "ticket_status": callback.get("ticket_status") or "",
        "live_agent_status": "ticket_created",
        "slack_channel": callback.get("slack_channel") or "",
        "slack_thread_ts": callback.get("slack_thread_ts") or "",
        "slack_user": callback.get("slack_user") or "",
        "assignee_account_id": callback.get("assignee_account_id") or "",
        "assignee_display_name": callback.get("assignee_display_name") or "",
        "assignee_email": callback.get("assignee_email") or "",
        "user_request": callback.get("user_request") or "",
        "created_at": now_iso,
        "updated_at": now_iso,
        "ttl": ttl_epoch(),
    }

    session_table.update_item(
        Key={"session_id": pointer_session_id},
        UpdateExpression="""
            SET
                pointer_type = :pointer_type,
                target_session_id = :target_session_id,
                ticket_key = :ticket_key,
                ticket_url = :ticket_url,
                ticket_status = :ticket_status,
                live_agent_status = :live_agent_status,
                slack_channel = :slack_channel,
                slack_thread_ts = :slack_thread_ts,
                slack_user = :slack_user,
                assignee_account_id = :assignee_account_id,
                assignee_display_name = :assignee_display_name,
                assignee_email = :assignee_email,
                user_request = :user_request,
                created_at = if_not_exists(created_at, :created_at),
                updated_at = :updated_at,
                #ttl = :ttl
        """,
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={
            ":pointer_type": item["pointer_type"],
            ":target_session_id": item["target_session_id"],
            ":ticket_key": item["ticket_key"],
            ":ticket_url": item["ticket_url"],
            ":ticket_status": item["ticket_status"],
            ":live_agent_status": item["live_agent_status"],
            ":slack_channel": item["slack_channel"],
            ":slack_thread_ts": item["slack_thread_ts"],
            ":slack_user": item["slack_user"],
            ":assignee_account_id": item["assignee_account_id"],
            ":assignee_display_name": item["assignee_display_name"],
            ":assignee_email": item["assignee_email"],
            ":user_request": item["user_request"],
            ":created_at": item["created_at"],
            ":updated_at": item["updated_at"],
            ":ttl": item["ttl"],
        },
    )

    return pointer_session_id


def acquire_slack_confirmation(callback, pointer_session_id):
    if not (session_table and pointer_session_id and callback.get("slack_channel")):
        return False

    now_iso = utc_now_iso()
    try:
        session_table.update_item(
            Key={"session_id": pointer_session_id},
            UpdateExpression="""
                SET
                    live_agent_slack_confirmation_status = :posting,
                    live_agent_slack_confirmation_started_at = :now,
                    updated_at = :now,
                    #ttl = :ttl
            """,
            ConditionExpression=(
                Attr("live_agent_slack_confirmation_status").not_exists()
                | Attr("live_agent_slack_confirmation_status").eq("failed")
            ),
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":posting": "posting",
                ":now": now_iso,
                ":ttl": ttl_epoch(),
            },
        )
        return True
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def mark_slack_confirmation(pointer_session_id, status, result=None, error=None):
    if not (session_table and pointer_session_id):
        return

    now_iso = utc_now_iso()
    expression_values = {
        ":status": status,
        ":now": now_iso,
        ":ttl": ttl_epoch(),
    }
    update_expression = """
        SET
            live_agent_slack_confirmation_status = :status,
            updated_at = :now,
            #ttl = :ttl
    """

    if result and result.get("ts"):
        update_expression += """,
            live_agent_slack_confirmation_ts = :slack_ts,
            live_agent_slack_confirmation_sent_at = :now
        """
        expression_values[":slack_ts"] = result["ts"]

    if error:
        update_expression += """,
            live_agent_slack_confirmation_error = :error
        """
        expression_values[":error"] = str(error)

    session_table.update_item(
        Key={"session_id": pointer_session_id},
        UpdateExpression=update_expression,
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues=expression_values,
    )


def post_slack_message(channel, text, thread_ts="", blocks=None):
    if not (SLACK_BOT_TOKEN and channel):
        return {
            "attempted": False
        }

    message = {
        "channel": channel,
        "text": text,
    }

    if thread_ts:
        message["thread_ts"] = thread_ts
    if blocks:
        message["blocks"] = blocks

    request = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps(message).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError(f"Slack API error: {result.get('error')}")

    return {
        "attempted": True,
        "ts": result.get("ts"),
    }


def post_slack_ticket(callback):
    if not (SLACK_BOT_TOKEN and callback.get("slack_channel")):
        return {
            "attempted": False
        }

    return post_slack_message(
        callback["slack_channel"],
        live_agent_ticket_reply(callback.get("ticket_key"), callback.get("ticket_url")),
        callback.get("slack_thread_ts"),
    )


def callback_event_type(callback):
    return text_or_empty(callback.get("event_type")).lower()


def is_status_changed_callback(callback):
    return callback_event_type(callback) in {"live_agent_status_changed", "status_changed"}


def is_public_comment_callback(callback):
    return callback_event_type(callback) in {
        "live_agent_public_comment_added",
        "public_comment_added",
        "agent_comment_added",
    }


def normalize_status_callback(event):
    base = normalize_callback(event)
    callback = base["raw_callback"]
    status = first_text(
        callback.get("ticket_status"),
        callback.get("status"),
        callback.get("transition_to"),
        callback.get("transitionTo"),
        callback.get("to_status"),
        callback.get("toStatus"),
        nested_get(callback, "issue", "status", "name"),
        nested_get(callback, "createdIssue", "status", "name"),
    )
    normalized_status = normalize_live_agent_status(status)

    return {
        **base,
        "ticket_status": status,
        "live_agent_status": normalized_status,
        "transition_to": first_text(
            callback.get("transition_to"),
            callback.get("transitionTo"),
            callback.get("to_status"),
            callback.get("toStatus"),
        ),
        "updated_at": first_text(
            callback.get("updated_at"),
            callback.get("updatedAt"),
            nested_get(callback, "issue", "fields", "updated"),
            nested_get(callback, "createdIssue", "fields", "updated"),
        ),
    }


def boolish_true(value):
    if isinstance(value, bool):
        return value

    return text_or_empty(value).lower() in {"true", "1", "yes", "y"}


def normalize_public_comment_callback(event):
    base = normalize_callback(event)
    callback = base["raw_callback"]

    comment = callback.get("comment") if isinstance(callback.get("comment"), dict) else {}
    author = comment.get("author") if isinstance(comment.get("author"), dict) else {}

    return {
        **base,
        "source": text_or_empty(callback.get("source")),
        "from_slack": boolish_true(callback.get("from_slack")),
        "ticket_status": first_text(
            callback.get("ticket_status"),
            callback.get("status"),
            nested_get(callback, "issue", "status", "name"),
        ),
        "comment_id": first_text(callback.get("comment_id"), callback.get("commentId"), comment.get("id")),
        "comment_body": first_text(
            callback.get("comment_body"),
            callback.get("commentBody"),
            callback.get("body"),
            comment.get("body"),
        ),
        "comment_author": first_text(
            callback.get("comment_author"),
            callback.get("commentAuthor"),
            callback.get("author"),
            author.get("displayName"),
            author.get("name"),
        ),
        "comment_created": first_text(
            callback.get("comment_created"),
            callback.get("commentCreated"),
            callback.get("created"),
            comment.get("created"),
        ),
    }


def is_echo_loop_comment(callback):
    return (
        "[From Slack]" in callback.get("comment_body", "")
        or text_or_empty(callback.get("source")).lower() == "slack_user"
        or boolish_true(callback.get("from_slack"))
    )


def is_internal_resolution_summary_comment(callback):
    comment_body = text_or_empty(callback.get("comment_body"))
    if comment_body.startswith(LIVE_AGENT_RESOLUTION_COMMENT_MARKER):
        return True

    raw = callback.get("raw_callback")
    if isinstance(raw, dict):
        comment = raw.get("comment") if isinstance(raw.get("comment"), dict) else {}
        if comment.get("public") is False:
            return True
        if raw.get("public") is False:
            return True

    return False


def live_agent_event_marker_id(callback):
    if callback.get("comment_body") or callback.get("comment_id"):
        marker_payload = {
            "event_type": callback.get("event_type") or "",
            "ticket_key": callback.get("ticket_key") or "",
            "comment_id": callback.get("comment_id") or "",
        }
        if not marker_payload["comment_id"]:
            marker_payload.update({
                "comment_body": callback.get("comment_body") or "",
                "comment_author": callback.get("comment_author") or "",
                "comment_created": callback.get("comment_created") or "",
            })
    else:
        marker_payload = {
            "event_type": callback.get("event_type") or "",
            "ticket_key": callback.get("ticket_key") or "",
            "status": callback.get("ticket_status") or "",
            "transition_to": callback.get("transition_to") or "",
            "updated_at": callback.get("updated_at") or "",
        }
    marker_hash = hashlib.sha256(
        json.dumps(marker_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"live_agent_event:{marker_hash}"


def create_live_agent_event_marker(callback, pointer_session_id):
    if not session_table:
        return {
            "created": True,
            "session_id": None,
        }

    now_iso = utc_now_iso()
    marker_session_id = live_agent_event_marker_id(callback)
    try:
        session_table.put_item(
            Item={
                "session_id": marker_session_id,
                "record_type": "live_agent_event_marker",
                "pointer_session_id": pointer_session_id,
                "event_type": callback.get("event_type") or "",
                "ticket_key": callback.get("ticket_key") or "",
                "ticket_status": callback.get("ticket_status") or "",
                "live_agent_status": callback.get("live_agent_status") or "",
                "transition_to": callback.get("transition_to") or "",
                "source_updated_at": callback.get("updated_at") or "",
                "comment_id": callback.get("comment_id") or "",
                "comment_author": callback.get("comment_author") or "",
                "comment_created": callback.get("comment_created") or "",
                "created_at": now_iso,
                "updated_at": now_iso,
                "ttl": ttl_epoch(),
            },
            ConditionExpression=Attr("session_id").not_exists(),
        )
        return {
            "created": True,
            "session_id": marker_session_id,
        }
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return {
                "created": False,
                "session_id": marker_session_id,
            }
        raise


def get_live_agent_ticket_pointer(ticket_key):
    if not session_table:
        return None

    response = session_table.get_item(Key={"session_id": f"live_agent_ticket:{ticket_key}"})
    return response.get("Item")


def get_session_item(session_id):
    session_id = text_or_empty(session_id)
    if not (session_table and session_id):
        return {}

    response = session_table.get_item(Key={"session_id": session_id})
    item = response.get("Item")
    return item if isinstance(item, dict) else {}


def live_agent_status_message(ticket_key, ticket_status, live_agent_status):
    status_text = display_status(ticket_status, live_agent_status)

    if live_agent_status == "in_progress":
        return f"🔄 Live agent update for {ticket_key}: status changed to In Progress."

    if live_agent_status == "waiting_for_customer":
        return f"🙋 Support is waiting for your response on {ticket_key}."

    if live_agent_status == "waiting_for_support":
        return f"ℹ️ Live agent update for {ticket_key}: status changed to Waiting for support."

    if live_agent_status == "resolved":
        return f"✅ Live agent request {ticket_key} is now Resolved."

    if live_agent_status == "cancelled":
        return f"🚫 Live agent request {ticket_key} is now Cancelled."

    return f"ℹ️ Live agent update for {ticket_key}: status changed to {status_text}."


def live_agent_feedback_action_value(pointer, rating):
    return json.dumps(
        {
            "action": "feedback_rating",
            "session_id": text_or_empty(pointer.get("target_session_id")),
            "rating": rating,
            "summary_jira_ticket_key": text_or_empty(pointer.get("ticket_key")),
            "summary_jira_ticket_url": text_or_empty(pointer.get("ticket_url")),
            "comment_public": False,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def live_agent_customer_resolution_summary(pointer):
    pointer = pointer if isinstance(pointer, dict) else {}
    conversation = live_agent_resolution_conversation(pointer)
    user_messages = [entry for entry in conversation if entry["sender"] == "User"]
    agent_messages = [entry for entry in conversation if entry["sender"] == "Agent"]
    original_request = compact_text(
        first_text(pointer.get("user_request"), user_messages[0]["text"] if user_messages else ""),
        350,
    )
    final_agent_reply = compact_text(agent_messages[-1]["text"], 350) if agent_messages else ""

    if original_request and final_agent_reply:
        return f"Summary: Support handled your request about \"{original_request}\" and marked it resolved after replying: \"{final_agent_reply}\""
    if original_request:
        return f"Summary: Support handled your request about \"{original_request}\" and marked it resolved."
    if final_agent_reply:
        return f"Summary: Support marked the request resolved after replying: \"{final_agent_reply}\""
    return "Summary: Support marked this live-agent request resolved."


def live_agent_resolution_feedback_blocks(pointer, ticket_key):
    summary_text = live_agent_customer_resolution_summary(pointer)
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"✅ Live agent request *{ticket_key}* is now resolved.",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": summary_text,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "How was the support experience?",
            },
        },
        {
            "type": "actions",
            "block_id": "ivy_live_agent_feedback",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": " ".join([":star:"] * rating),
                        "emoji": True,
                    },
                    "action_id": f"{ACTION_ID_FEEDBACK_RATING}_{rating}",
                    "value": live_agent_feedback_action_value(pointer, rating),
                }
                for rating in (1, 2, 3, 4, 5)
            ],
        },
    ]


def suppress_duplicate_terminal_status_slack(pointer, callback):
    if not isinstance(pointer, dict) or not isinstance(callback, dict):
        return False

    live_agent_status = text_or_empty(callback.get("live_agent_status")).lower()
    if live_agent_status not in {"resolved", "cancelled"}:
        return False

    existing_bridge_status = text_or_empty(pointer.get("bridge_status")).lower()
    existing_live_agent_status = text_or_empty(pointer.get("live_agent_status")).lower()
    return live_agent_status in {existing_bridge_status, existing_live_agent_status}


def update_status_pointer(pointer_session_id, callback):
    if not session_table:
        return

    now_iso = utc_now_iso()
    live_agent_status = callback.get("live_agent_status") or ""
    bridge_status_expression = "bridge_status = :bridge_status," if live_agent_status in {"resolved", "cancelled"} else ""
    expression_values = {
        ":ticket_status": callback.get("ticket_status") or "",
        ":live_agent_status": live_agent_status,
        ":now": now_iso,
        ":ttl": ttl_epoch(),
    }
    if live_agent_status in {"resolved", "cancelled"}:
        expression_values[":bridge_status"] = live_agent_status

    session_table.update_item(
        Key={"session_id": pointer_session_id},
        UpdateExpression=f"""
            SET
                ticket_status = :ticket_status,
                live_agent_status = :live_agent_status,
                {bridge_status_expression}
                live_agent_status_changed_at = :now,
                live_agent_updated_at = :now,
                updated_at = :now,
                #ttl = :ttl
        """,
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues=expression_values,
    )


def update_status_target_session(target_session_id, callback):
    if not (session_table and target_session_id):
        return

    now_iso = utc_now_iso()
    live_agent_status = callback.get("live_agent_status") or ""
    expression_values = {
        ":live_agent_status": live_agent_status,
        ":ticket_status": callback.get("ticket_status") or "",
        ":now": now_iso,
        ":ttl": ttl_epoch(),
    }
    update_expression = """
        SET
            live_agent_status = :live_agent_status,
            live_agent_jira_status = :ticket_status,
            live_agent_updated_at = :now,
            updated_at = :now,
            #ttl = :ttl
    """

    if live_agent_status in {"resolved", "cancelled"}:
        update_expression += """,
            conversation_status = :conversation_closed,
            support_options_status = :support_resolved,
            live_agent_resolved_at = :now,
            bridge_status = :bridge_resolved
        """
        expression_values[":conversation_closed"] = "closed"
        expression_values[":support_resolved"] = (
            "live_agent_cancelled" if live_agent_status == "cancelled" else "live_agent_resolved"
        )
        expression_values[":bridge_resolved"] = live_agent_status

    session_table.update_item(
        Key={"session_id": target_session_id},
        UpdateExpression=update_expression,
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues=expression_values,
    )


def live_agent_comment_message(ticket_key, comment_author, comment_body):
    author = text_or_empty(comment_author) or "Support"
    return f"💬 Support update on {ticket_key} from {author}:\n\n{comment_body}"


def update_comment_pointer(pointer_session_id, callback):
    if not session_table:
        return

    now_iso = utc_now_iso()
    session_table.update_item(
        Key={"session_id": pointer_session_id},
        UpdateExpression="""
            SET
                last_agent_comment = :comment_body,
                last_agent_comment_author = :comment_author,
                last_agent_comment_at = :comment_at,
                updated_at = :now,
                #ttl = :ttl
        """,
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={
            ":comment_body": callback.get("comment_body") or "",
            ":comment_author": callback.get("comment_author") or "",
            ":comment_at": callback.get("comment_created") or now_iso,
            ":now": now_iso,
            ":ttl": ttl_epoch(),
        },
    )


def update_comment_target_session(target_session_id):
    if not (session_table and target_session_id):
        return

    now_iso = utc_now_iso()
    session_table.update_item(
        Key={"session_id": target_session_id},
        UpdateExpression="""
            SET
                live_agent_updated_at = :now,
                updated_at = :now,
                #ttl = :ttl
        """,
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={
            ":now": now_iso,
            ":ttl": ttl_epoch(),
        },
    )


def handle_live_agent_public_comment_added(event):
    callback = normalize_public_comment_callback(event)
    if not callback.get("ticket_key"):
        return {
            "ok": False,
            "error": "Missing callback ticket_key",
            "error_code": "missing_ticket_key",
        }

    if not callback.get("comment_body"):
        return {
            "ok": False,
            "error": "Missing callback comment_body",
            "error_code": "missing_comment_body",
            "ticket_key": callback.get("ticket_key"),
        }

    pointer_session_id = f"live_agent_ticket:{callback['ticket_key']}"
    pointer = get_live_agent_ticket_pointer(callback["ticket_key"])
    if not pointer:
        return {
            "ok": False,
            "error": "Missing live agent ticket pointer",
            "error_code": "missing_live_agent_ticket_pointer",
            "ticket_key": callback["ticket_key"],
        }

    if is_echo_loop_comment(callback):
        return {
            "ok": True,
            "ignored": True,
            "reason": "echo_loop_guard",
            "ticket_key": callback["ticket_key"],
        }

    if is_internal_resolution_summary_comment(callback):
        return {
            "ok": True,
            "ignored": True,
            "reason": "internal_resolution_summary_comment",
            "ticket_key": callback["ticket_key"],
            "comment_id": callback.get("comment_id"),
        }

    marker = create_live_agent_event_marker(callback, pointer_session_id)
    if not marker.get("created"):
        return {
            "ok": True,
            "duplicate": True,
            "ticket_key": callback["ticket_key"],
            "comment_id": callback.get("comment_id"),
            "event_marker_session_id": marker.get("session_id"),
            "slack": {
                "attempted": False,
                "deduped": True,
            },
        }

    update_comment_pointer(pointer_session_id, callback)

    target_session_id = text_or_empty(pointer.get("target_session_id"))
    if target_session_id:
        update_comment_target_session(target_session_id)

    slack_result = {"attempted": False}
    slack_channel = text_or_empty(pointer.get("slack_channel"))
    if slack_channel:
        message = live_agent_comment_message(
            callback["ticket_key"],
            callback.get("comment_author"),
            callback.get("comment_body"),
        )
        try:
            slack_result = post_slack_message(
                slack_channel,
                message,
                text_or_empty(pointer.get("slack_thread_ts")),
            )
        except Exception as error:
            log_json({
                "level": "ERROR",
                "message": "live_agent_comment_slack_notify_failed",
                "ticket_key": callback["ticket_key"],
                "comment_id": callback.get("comment_id"),
                "error": str(error),
            })
            slack_result = {
                "attempted": True,
                "ok": False,
                "error": str(error),
            }

    return {
        "ok": True,
        "event_type": callback.get("event_type"),
        "ticket_key": callback["ticket_key"],
        "comment_id": callback.get("comment_id"),
        "comment_author": callback.get("comment_author"),
        "pointer_session_id": pointer_session_id,
        "target_session_id": target_session_id,
        "event_marker_session_id": marker.get("session_id"),
        "slack": slack_result,
    }


TERMINAL_TICKET_STATUSES = {
    "resolved",
    "done",
    "closed",
    "cancelled",
    "canceled",
}


def normalized_ticket_status_text(status):
    return text_or_empty(status).lower().replace("_", " ").replace("-", " ").strip()


def is_terminal_ticket_status(status):
    return normalized_ticket_status_text(status) in TERMINAL_TICKET_STATUSES


def assign_jira_ticket(ticket_key, agent_account_id):
    result = oncall_user_helper.assign_jira_issue(ticket_key, agent_account_id)
    if "status" not in result:
        result["status"] = result.get("status_code")
    return result


def compact_text(value, limit):
    text = text_or_empty(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def jira_adf_doc(text):
    lines = str(text or "").splitlines() or [""]
    content = []
    for line in lines:
        paragraph = {"type": "paragraph"}
        if line:
            paragraph["content"] = [{"type": "text", "text": line}]
        content.append(paragraph)
    return {"version": 1, "type": "doc", "content": content}


def add_jira_issue_comment(ticket_key, text):
    ticket_key = text_or_empty(ticket_key)
    if not ticket_key:
        return {"ok": False, "error": "Missing ticket key", "error_code": "missing_ticket_key"}

    result = oncall_user_helper.jira_request(
        "POST",
        f"/rest/api/3/issue/{urllib.parse.quote(ticket_key, safe='')}/comment",
        body={"body": jira_adf_doc(text)},
    )
    if not result.get("ok"):
        return {
            **result,
            "error": result.get("error") or result.get("reason") or "Jira comment creation failed",
            "error_code": result.get("error_code") or result.get("reason") or "jira_comment_creation_failed",
        }

    body = result.get("body") or {}
    return {
        "ok": True,
        "ticket_key": ticket_key,
        "comment_id": body.get("id"),
        "status_code": result.get("status_code"),
    }


def add_jsm_internal_request_comment(ticket_key, text):
    ticket_key = text_or_empty(ticket_key)
    if not ticket_key:
        return {"ok": False, "error": "Missing ticket key", "error_code": "missing_ticket_key"}

    result = oncall_user_helper.jira_request(
        "POST",
        f"/rest/servicedeskapi/request/{urllib.parse.quote(ticket_key, safe='')}/comment",
        body={"body": text_or_empty(text), "public": False},
    )
    if not result.get("ok"):
        return {
            **result,
            "error": result.get("error") or result.get("reason") or "JSM internal comment creation failed",
            "error_code": result.get("error_code") or result.get("reason") or "jsm_internal_comment_creation_failed",
        }

    body = result.get("body") or {}
    return {
        "ok": True,
        "ticket_key": ticket_key,
        "comment_id": body.get("id"),
        "public": body.get("public"),
        "status_code": result.get("status_code"),
    }


def session_message_entries(session_item):
    if not isinstance(session_item, dict):
        return []
    messages = session_item.get("session_messages") or []
    return messages if isinstance(messages, list) else []


def live_agent_resolution_conversation(pointer):
    pointer = pointer if isinstance(pointer, dict) else {}
    try:
        target_item = get_session_item(pointer.get("target_session_id"))
    except Exception as error:
        log_json({
            "level": "WARN",
            "message": "live_agent_resolution_target_session_lookup_failed",
            "ticket_key": pointer.get("ticket_key"),
            "target_session_id": pointer.get("target_session_id"),
            "error": str(error),
        })
        target_item = {}
    entries = []
    seen = set()

    for item in [target_item, pointer]:
        for message in session_message_entries(item):
            if not isinstance(message, dict):
                continue
            sender = text_or_empty(message.get("sender"))
            if sender.lower() not in {"user", "agent"}:
                continue
            text = compact_text(message.get("text"), 4000)
            if not text:
                continue
            ts = first_text(message.get("ts"), message.get("recorded_at"))
            key = (sender.lower(), text, ts)
            if key in seen:
                continue
            seen.add(key)
            entries.append({
                "sender": "User" if sender.lower() == "user" else "Agent",
                "text": text,
                "ts": ts,
            })

    if not any(entry["sender"] == "User" for entry in entries):
        user_request = compact_text(pointer.get("user_request"), 4000)
        if user_request:
            entries.insert(0, {"sender": "User", "text": user_request, "ts": ""})

    return entries[-LIVE_AGENT_RESOLUTION_TRANSCRIPT_LIMIT:]


def live_agent_resolution_summary_comment_text(pointer, callback, resolved_at):
    pointer = pointer if isinstance(pointer, dict) else {}
    callback = callback if isinstance(callback, dict) else {}
    ticket_key = first_text(callback.get("ticket_key"), pointer.get("ticket_key"))
    conversation = live_agent_resolution_conversation(pointer)
    user_messages = [entry for entry in conversation if entry["sender"] == "User"]
    agent_messages = [entry for entry in conversation if entry["sender"] == "Agent"]
    original_request = compact_text(
        first_text(pointer.get("user_request"), user_messages[0]["text"] if user_messages else ""),
        1000,
    )
    final_agent_reply = compact_text(agent_messages[-1]["text"], 1000) if agent_messages else ""

    lines = [
        LIVE_AGENT_RESOLUTION_COMMENT_MARKER,
        "IVY live-agent session summary",
        f"Ticket: {ticket_key or '-'}",
        f"Resolved by: {callback.get('action_user') or '-'}",
        f"Resolved at: {resolved_at or utc_now_iso()}",
        "",
        "Summary:",
        f"- Original request: {original_request or '-'}",
        f"- User messages: {len(user_messages)}",
        f"- Agent messages: {len(agent_messages)}",
        f"- Final agent reply: {final_agent_reply or '-'}",
        "",
        "Conversation transcript:",
    ]

    if conversation:
        for entry in conversation:
            prefix = f"[{entry['ts']}] " if entry.get("ts") else ""
            lines.append(f"{prefix}{entry['sender']}: {entry['text']}")
    else:
        lines.append("- No user/agent transcript was stored for this session.")

    return compact_text("\n".join(lines), LIVE_AGENT_RESOLUTION_COMMENT_TEXT_LIMIT)


def add_live_agent_resolution_summary_comment(pointer, callback, resolved_at=None):
    ticket_key = first_text(
        (callback or {}).get("ticket_key") if isinstance(callback, dict) else "",
        (pointer or {}).get("ticket_key") if isinstance(pointer, dict) else "",
    )
    comment_text = live_agent_resolution_summary_comment_text(pointer, callback, resolved_at or utc_now_iso())
    result = add_jsm_internal_request_comment(ticket_key, comment_text)
    return {
        **result,
        "ticket_key": ticket_key,
        "comment_text_length": len(comment_text),
    }


def normalize_status_name(value):
    return text_or_empty(value).lower()


def jira_issue_status(ticket_key):
    ticket_key = text_or_empty(ticket_key)
    if not ticket_key:
        return {"ok": False, "error": "Missing ticket key", "error_code": "missing_ticket_key"}

    result = oncall_user_helper.jira_request(
        "GET",
        f"/rest/api/3/issue/{urllib.parse.quote(ticket_key, safe='')}",
        query={"fields": "status"},
    )
    if not result.get("ok"):
        return {
            **result,
            "error": result.get("error") or result.get("reason") or "Jira issue status lookup failed",
            "error_code": result.get("error_code") or result.get("reason") or "jira_issue_status_lookup_failed",
        }

    status_name = (
        ((result.get("body") or {}).get("fields") or {}).get("status") or {}
    ).get("name")
    if not status_name:
        return {
            "ok": False,
            "error": "Jira issue status missing from response",
            "error_code": "missing_jira_issue_status",
            "status_code": result.get("status_code"),
        }

    return {"ok": True, "ticket_status": status_name, "status_code": result.get("status_code")}


def jira_issue_transitions(ticket_key):
    ticket_key = text_or_empty(ticket_key)
    if not ticket_key:
        return {"ok": False, "error": "Missing ticket key", "error_code": "missing_ticket_key"}

    result = oncall_user_helper.jira_request(
        "GET",
        f"/rest/api/3/issue/{urllib.parse.quote(ticket_key, safe='')}/transitions",
    )
    if not result.get("ok"):
        return {
            **result,
            "error": result.get("error") or result.get("reason") or "Jira transition lookup failed",
            "error_code": result.get("error_code") or result.get("reason") or "jira_transition_lookup_failed",
        }

    return {
        "ok": True,
        "transitions": (result.get("body") or {}).get("transitions") or [],
        "status_code": result.get("status_code"),
    }


def transition_jira_issue_to_status(ticket_key, target_status_names, reason=None):
    ticket_key = text_or_empty(ticket_key)
    target_status_names = [name for name in (target_status_names or []) if text_or_empty(name)]
    if not ticket_key:
        return {"ok": False, "error": "Missing ticket key", "error_code": "missing_ticket_key"}
    if not target_status_names:
        return {"ok": False, "error": "Missing target status", "error_code": "missing_target_status"}

    normalized_targets = {normalize_status_name(name) for name in target_status_names}
    status_result = jira_issue_status(ticket_key)
    if not status_result.get("ok"):
        return status_result

    current_status = status_result.get("ticket_status")
    if normalize_status_name(current_status) in normalized_targets:
        return {
            "ok": True,
            "skipped": True,
            "reason": "already_in_target_status",
            "ticket_key": ticket_key,
            "ticket_status": current_status,
            "target_status": current_status,
        }

    transitions_result = jira_issue_transitions(ticket_key)
    if not transitions_result.get("ok"):
        return transitions_result

    matching_transition = None
    for transition in transitions_result.get("transitions") or []:
        to_status = ((transition.get("to") or {}).get("name") or "").strip()
        if normalize_status_name(to_status) in normalized_targets:
            matching_transition = transition
            break

    if not matching_transition:
        return {
            "ok": False,
            "error": f"No Jira transition available to {', '.join(target_status_names)}.",
            "error_code": "no_matching_jira_transition",
            "ticket_key": ticket_key,
            "ticket_status": current_status,
            "target_status_names": target_status_names,
            "available_transitions": [
                {
                    "id": transition.get("id"),
                    "name": transition.get("name"),
                    "to": ((transition.get("to") or {}).get("name") or ""),
                }
                for transition in transitions_result.get("transitions") or []
            ],
        }

    result = oncall_user_helper.jira_request(
        "POST",
        f"/rest/api/3/issue/{urllib.parse.quote(ticket_key, safe='')}/transitions",
        body={"transition": {"id": matching_transition.get("id")}},
    )
    if not result.get("ok"):
        return {
            **result,
            "error": result.get("error") or result.get("reason") or "Jira transition failed",
            "error_code": result.get("error_code") or result.get("reason") or "jira_transition_failed",
            "ticket_key": ticket_key,
            "ticket_status": current_status,
            "target_status": (matching_transition.get("to") or {}).get("name"),
            "transition_id": matching_transition.get("id"),
            "transition_name": matching_transition.get("name"),
        }

    return {
        "ok": True,
        "ticket_key": ticket_key,
        "previous_status": current_status,
        "target_status": (matching_transition.get("to") or {}).get("name"),
        "transition_id": matching_transition.get("id"),
        "transition_name": matching_transition.get("name"),
        "reason": reason,
        "status_code": result.get("status_code"),
    }


def support_control_transition_failed_message(ticket_key, target_status_names, result):
    error_text = result.get("error") or result.get("error_code") or "unknown Jira transition error"
    return (
        f"Could not update {ticket_key} to {', '.join(target_status_names)} in Jira. "
        f"No local Slack state was changed. Reason: {error_text}"
    )


def drain_live_agent_queue():
    assignments = []
    for _ in range(MAX_QUEUE_DRAIN_PER_INVOCATION):
        queue_item = chat_locks.pop_oldest_waiting_request()
        if not queue_item:
            return {
                "drained": bool(assignments),
                "reason": "queue_empty",
                "assignments": assignments,
                "assignment_count": len(assignments),
            }

        ticket_key = text_or_empty(queue_item.get("ticket_key"))
        oncall_user = get_current_oncall_user(ticket_key=ticket_key)
        if not oncall_user.get("ok"):
            chat_locks.mark_queue_item_waiting_again(queue_item, "no_oncall_user")
            return {
                "drained": bool(assignments),
                "reason": "no_oncall_user",
                "ticket_key": ticket_key,
                "assignments": assignments,
                "assignment_count": len(assignments),
            }

        agent = live_agent_agent_from_oncall(oncall_user)
        ticket = {
            "ticket_key": ticket_key,
            "ticket_url": queue_item.get("ticket_url") or "",
            "status": queue_item.get("ticket_status") or "",
        }
        slack_context = {
            "session_id": queue_item.get("session_id") or "",
            "slack_channel": queue_item.get("slack_channel") or "",
            "slack_thread_ts": queue_item.get("slack_thread_ts") or "",
            "slack_user": queue_item.get("slack_user") or "",
        }
        lock_result = chat_locks.acquire_chat_lock(agent, ticket, slack_context)

        if not lock_result.get("ok"):
            reason = lock_result.get("reason") or "lock_failed"
            if reason == "agent_at_capacity":
                chat_locks.mark_queue_item_waiting_again(queue_item, reason)
            else:
                chat_locks.mark_queue_item_failed(queue_item, reason)
            return {
                "drained": bool(assignments),
                "reason": reason,
                "ticket_key": ticket_key,
                "lock": lock_result,
                "assignments": assignments,
                "assignment_count": len(assignments),
            }

        jira_assignment = assign_jira_ticket(ticket_key, agent.get("account_id"))
        if not jira_assignment.get("ok"):
            release_result = chat_locks.release_chat_lock(
                ticket_key,
                close_reason="jira_assignment_failed",
            )
            queue_reason = jira_assignment.get("reason") or "jira_assignment_failed"
            if jira_assignment.get("retryable"):
                chat_locks.mark_queue_item_waiting_again(queue_item, queue_reason)
            else:
                chat_locks.mark_queue_item_failed(queue_item, queue_reason)
            log_json({
                "level": "ERROR",
                "message": "live_agent_queue_assignment_rolled_back",
                "ticket_key": ticket_key,
                "agent_account_id": agent.get("account_id"),
                "queue_retryable": jira_assignment.get("retryable"),
                "lock_released": release_result.get("released"),
            })
            return {
                "drained": bool(assignments),
                "reason": queue_reason,
                "ticket_key": ticket_key,
                "lock": lock_result,
                "lock_release": release_result,
                "jira_assignment": jira_assignment,
                "assignments": assignments,
                "assignment_count": len(assignments),
            }

        chat_locks.mark_queue_item_assigned(queue_item, agent, ticket_key)
        slack_result = {"attempted": False}
        if queue_item.get("slack_channel"):
            try:
                slack_result = post_slack_message(
                    queue_item["slack_channel"],
                    live_agent_queue_assigned_reply(ticket_key, oncall_user.get("display_name")),
                    text_or_empty(queue_item.get("slack_thread_ts")),
                )
            except Exception as error:
                log_json({
                    "level": "ERROR",
                    "message": "live_agent_queue_assignment_slack_failed",
                    "ticket_key": ticket_key,
                    "error": str(error),
                })
                slack_result = {
                    "attempted": True,
                    "ok": False,
                    "error": str(error),
                }

        assignment = {
            "ticket_key": ticket_key,
            "agent_account_id": agent.get("account_id"),
            "lock": lock_result,
            "jira_assignment": jira_assignment,
            "slack": slack_result,
        }
        assignments.append(assignment)
        log_json({
            "level": "INFO",
            "message": "live_agent_queue_item_assigned",
            "ticket_key": ticket_key,
            "agent_account_id": agent.get("account_id"),
            "assignment_count": len(assignments),
        })

    result = {
        "drained": bool(assignments),
        "reason": "max_assignments_reached",
        "assignments": assignments,
        "assignment_count": len(assignments),
    }
    if assignments:
        result.update(assignments[-1])
    return result


def recover_latest_requested_live_agent_session():
    if not session_table:
        return {}

    try:
        response = session_table.scan(
            FilterExpression=Attr("support_options_status").eq("live_agent_requested"),
            Limit=25,
        )
    except Exception as error:
        log_json({
            "level": "WARN",
            "message": "live_agent_recover_latest_requested_session_failed",
            "error": str(error),
        })
        return {}

    items = response.get("Items") or []
    if not items:
        return {}

    return max(
        items,
        key=lambda item: text_or_empty(
            item.get("live_agent_requested_at")
            or item.get("live_agent_updated_at")
            or item.get("updated_at")
            or item.get("created_at")
        ),
    )


def enrich_missing_slack_context(callback):
    if callback.get("session_id") and callback.get("slack_channel") and callback.get("slack_user"):
        return callback

    if any(callback.get(field) for field in ("session_id", "slack_channel", "slack_user")):
        return callback

    recovered = normalize_callback(recover_latest_requested_live_agent_session())
    if not any(recovered.get(field) for field in ("session_id", "slack_channel", "slack_thread_ts", "slack_user")):
        return callback

    enriched = {**callback}
    for field in ("session_id", "session_root_ts", "slack_channel", "slack_thread_ts", "slack_user"):
        if not enriched.get(field) and recovered.get(field):
            enriched[field] = recovered[field]
    enriched["slack"] = canonical_slack_context(
        enriched.get("session_id"),
        enriched.get("session_root_ts"),
        enriched.get("slack_channel"),
        enriched.get("slack_thread_ts"),
        enriched.get("slack_user"),
    )["slack"]
    return enriched


def save_capacity_target_state(callback, assignment):
    if not (session_table and callback.get("session_id")):
        return

    now_iso = utc_now_iso()
    expression_values = {
        ":assignment_status": assignment["assignment_status"],
        ":capacity_reserved": assignment["capacity_reserved"],
        ":capacity_released": False,
        ":updated_at": now_iso,
        ":ttl": ttl_epoch(),
    }
    update_expression = """
        SET
            live_agent_assignment_status = :assignment_status,
            live_agent_capacity_reserved = :capacity_reserved,
            live_agent_capacity_released = :capacity_released,
            live_agent_updated_at = :updated_at,
            updated_at = :updated_at,
            #ttl = :ttl
    """
    optional = {
        "live_agent_assigned_agent_id": assignment.get("assigned_agent_id"),
        "live_agent_assigned_agent_name": assignment.get("assigned_agent_name"),
        "live_agent_assigned_jira_account_id": assignment.get("assigned_jira_account_id"),
    }
    removes = []
    for name, value in optional.items():
        if value:
            token = f":{name}"
            update_expression += f", {name} = {token}"
            expression_values[token] = value
        else:
            removes.append(name)
    if removes:
        update_expression += " REMOVE " + ", ".join(removes)

    session_table.update_item(
        Key={"session_id": callback["session_id"]},
        UpdateExpression=update_expression,
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues=expression_values,
    )


def capacity_assignment_for_ticket(callback, existing_pointer=None):
    existing_pointer = existing_pointer or {}
    existing_status = text_or_empty(existing_pointer.get("assignment_status")).upper()
    if existing_status in {"ASSIGNED", "QUEUED"}:
        return {
            "assignment_status": existing_status,
            "capacity_reserved": existing_pointer.get("capacity_reserved") is True,
            "assigned_agent_id": existing_pointer.get("assigned_agent_id"),
            "assigned_agent_name": existing_pointer.get("assigned_agent_name"),
            "assigned_jira_account_id": existing_pointer.get("assigned_jira_account_id"),
            "idempotent": True,
            "jira_assignment": {"ok": True, "skipped": True},
        }

    reservation = capacity_dispatcher.choose_and_reserve_agent()
    assignment = {
        "assignment_status": "QUEUED",
        "capacity_reserved": False,
        "assigned_agent_id": None,
        "assigned_agent_name": None,
        "assigned_jira_account_id": None,
        "reservation": reservation,
        "jira_assignment": {"ok": False, "skipped": True},
    }

    if reservation.get("ok"):
        agent = reservation["agent"]
        jira_assignment = capacity_dispatcher.assign_jira_issue(
            callback["ticket_key"],
            agent["jira_account_id"],
        )
        assignment["jira_assignment"] = jira_assignment
        if jira_assignment.get("ok"):
            assignment.update({
                "assignment_status": "ASSIGNED",
                "capacity_reserved": True,
                "assigned_agent_id": agent["agent_id"],
                "assigned_agent_name": agent.get("display_name") or agent["agent_id"],
                "assigned_jira_account_id": agent["jira_account_id"],
            })
            capacity_dispatcher.mark_jira_ticket_assigned(callback["ticket_key"])
        else:
            rollback = capacity_dispatcher.release_agent_capacity(agent["agent_id"])
            assignment["rollback"] = rollback
            capacity_dispatcher.mark_jira_ticket_queued(callback["ticket_key"])
            if not rollback.get("released"):
                log_json({
                    "level": "CRITICAL",
                    "message": "live_agent_capacity_rollback_failed",
                    "ticket_key": callback["ticket_key"],
                    "session_id": callback.get("session_id"),
                    "agent_id": agent["agent_id"],
                    "rollback": rollback,
                })
    else:
        capacity_dispatcher.mark_jira_ticket_queued(callback["ticket_key"])

    raw = callback.get("raw_callback") or {}
    ticket_id = first_text(
        raw.get("ticket_id"),
        raw.get("issue_id"),
        nested_get(raw, "issue", "id"),
        nested_get(raw, "createdIssue", "id"),
    )
    mapping = {
        "target_session_id": callback.get("session_id"),
        "ticket_key": callback.get("ticket_key"),
        "ticket_id": ticket_id,
        "ticket_url": callback.get("ticket_url") or "",
        "slack_channel": callback.get("slack_channel") or "",
        "slack_thread_ts": callback.get("slack_thread_ts") or "",
        "slack_user": callback.get("slack_user") or "",
        "ticket_status": callback.get("ticket_status") or "",
        "assignment_status": assignment["assignment_status"],
        "capacity_reserved": assignment["capacity_reserved"],
        "capacity_released": False,
        "assigned_agent_id": assignment.get("assigned_agent_id"),
        "assigned_agent_name": assignment.get("assigned_agent_name"),
        "assigned_jira_account_id": assignment.get("assigned_jira_account_id"),
    }
    capacity_dispatcher.save_live_agent_session_mapping(callback["ticket_key"], mapping)
    save_capacity_target_state(callback, assignment)
    capacity_dispatcher.log_assignment_decision({
        "ok": assignment["assignment_status"] == "ASSIGNED",
        "event_type": callback.get("event_type"),
        "ticket_key": callback.get("ticket_key"),
        "slack_channel": callback.get("slack_channel"),
        "slack_thread_ts": callback.get("slack_thread_ts"),
        "slack_user": callback.get("slack_user"),
        "current_time_ist": reservation.get("current_time_ist"),
        "eligible_agents": reservation.get("eligible_agents"),
        "skipped_agents_with_reason": reservation.get("skipped_agents_with_reason"),
        "selected_agent": assignment.get("assigned_agent_id"),
        "assignment_status": assignment["assignment_status"],
        "active_count_before": reservation.get("active_count_before"),
        "active_count_after": reservation.get("active_count_after"),
        "jira_assignment_result": assignment.get("jira_assignment"),
        "capacity_reserved": assignment["capacity_reserved"],
        "capacity_released": False,
    })
    return assignment


def legacy_assignment_for_ticket(callback):
    ticket = callback_ticket(callback)
    slack_context = callback_slack_context(callback)
    oncall_user = get_current_oncall_user(
        ticket_key=callback["ticket_key"],
        webhook_payload=callback.get("raw_callback") or {},
    )
    lock_result = {"ok": False, "reason": "not_attempted"}
    queue_result = None
    if oncall_user.get("ok"):
        lock_result = chat_locks.acquire_chat_lock(
            live_agent_agent_from_oncall(oncall_user),
            ticket,
            slack_context,
        )
        if lock_result.get("ok"):
            return {
                "assignment_status": "ASSIGNED",
                "capacity_reserved": True,
                "assigned_agent_id": agent_key_from_user(oncall_user),
                "assigned_agent_name": oncall_user.get("display_name"),
                "assigned_jira_account_id": oncall_user.get("account_id"),
                "jira_assignment": {"ok": True, "skipped": True},
                "legacy_lock": lock_result,
                "oncall_user": oncall_user,
            }
        queue_result = chat_locks.enqueue_live_agent_request(
            ticket,
            slack_context,
            reason=lock_result.get("reason") or "lock_failed",
        )
    else:
        queue_result = chat_locks.enqueue_live_agent_request(
            ticket,
            slack_context,
            reason="no_oncall_user",
        )
    return {
        "assignment_status": "QUEUED",
        "capacity_reserved": False,
        "assigned_agent_id": None,
        "assigned_agent_name": None,
        "assigned_jira_account_id": None,
        "jira_assignment": {"ok": False, "skipped": True},
        "legacy_lock": lock_result,
        "legacy_queue": queue_result,
        "oncall_user": oncall_user,
    }


def notify_promoted_ticket(pointer, agent):
    channel = text_or_empty(pointer.get("slack_channel"))
    if not channel:
        return {"attempted": False}
    return post_slack_message(
        channel,
        live_agent_queue_assigned_reply(
            pointer.get("ticket_key"),
            agent.get("display_name") or agent.get("agent_id"),
        ),
        text_or_empty(pointer.get("slack_thread_ts")),
    )


def handle_live_agent_status_changed(event):
    callback = normalize_status_callback(event)
    if not callback.get("ticket_key"):
        return {
            "ok": False,
            "error": "Missing callback ticket_key",
            "error_code": "missing_ticket_key",
        }

    pointer_session_id = f"live_agent_ticket:{callback['ticket_key']}"
    pointer = get_live_agent_ticket_pointer(callback["ticket_key"])
    if not pointer:
        return {
            "ok": False,
            "error": "Missing live agent ticket pointer",
            "error_code": "missing_live_agent_ticket_pointer",
            "ticket_key": callback["ticket_key"],
        }

    marker = create_live_agent_event_marker(callback, pointer_session_id)
    if not marker.get("created"):
        return {
            "ok": True,
            "duplicate": True,
            "ticket_key": callback["ticket_key"],
            "ticket_status": callback.get("ticket_status"),
            "live_agent_status": callback.get("live_agent_status"),
            "event_marker_session_id": marker.get("session_id"),
            "slack": {
                "attempted": False,
                "deduped": True,
            },
        }

    update_status_pointer(pointer_session_id, callback)

    target_session_id = text_or_empty(pointer.get("target_session_id"))
    if target_session_id:
        update_status_target_session(target_session_id, callback)

    slack_result = {"attempted": False}
    slack_channel = text_or_empty(pointer.get("slack_channel"))
    if suppress_duplicate_terminal_status_slack(pointer, callback):
        slack_result = {
            "attempted": False,
            "deduped": True,
            "reason": "terminal_status_already_notified",
        }
    elif slack_channel:
        message = live_agent_status_message(
            callback["ticket_key"],
            callback.get("ticket_status"),
            callback.get("live_agent_status"),
        )
        try:
            slack_result = post_slack_message(
                slack_channel,
                message,
                text_or_empty(pointer.get("slack_thread_ts")),
            )
        except Exception as error:
            log_json({
                "level": "ERROR",
                "message": "live_agent_status_slack_notify_failed",
                "ticket_key": callback["ticket_key"],
                "error": str(error),
            })
            slack_result = {
                "attempted": True,
                "ok": False,
                "error": str(error),
            }

    terminal_status = (
        capacity_dispatcher.is_terminal_status(callback.get("ticket_status"))
        if ENABLE_CUSTOM_CAPACITY_DISPATCHER
        else is_terminal_ticket_status(callback.get("ticket_status"))
    )
    lock_release = {"ok": True, "released": False}
    queue_drain = {"drained": False, "reason": "not_terminal"}
    if terminal_status:
        try:
            if ENABLE_CUSTOM_CAPACITY_DISPATCHER:
                lock_release = capacity_dispatcher.release_capacity_for_ticket(
                    callback["ticket_key"],
                    callback.get("ticket_status"),
                )
                if lock_release.get("released"):
                    promotion = capacity_dispatcher.promote_oldest_queued_ticket(
                        notify=notify_promoted_ticket,
                    )
                    queue_drain = {
                        "drained": bool(promotion.get("promoted")),
                        **promotion,
                    }
                else:
                    queue_drain = {
                        "drained": False,
                        "reason": lock_release.get("reason") or "capacity_not_released",
                    }
            else:
                lock_release = chat_locks.release_chat_lock(
                    callback["ticket_key"],
                    close_reason=callback.get("ticket_status"),
                )
                queue_drain = (
                    drain_live_agent_queue()
                    if lock_release.get("released")
                    else {"drained": False, "reason": "lock_not_released"}
                )
        except Exception as error:
            log_json({
                "level": "ERROR",
                "message": "live_agent_capacity_release_failed",
                "ticket_key": callback["ticket_key"],
                "ticket_status": callback.get("ticket_status"),
                "error": str(error),
            })
            lock_release = {
                "ok": False,
                "released": False,
                "error": str(error),
            }

    log_json({
        "level": "INFO",
        "message": "live_agent_status_capacity_release_checked",
        "ticket_key": callback["ticket_key"],
        "ticket_status": callback.get("ticket_status"),
        "terminal_status": terminal_status,
        "lock_released": bool(lock_release.get("released")),
        "released_agent_id": lock_release.get("agent_id"),
    })

    return {
        "ok": True,
        "event_type": callback.get("event_type"),
        "ticket_key": callback["ticket_key"],
        "ticket_status": callback.get("ticket_status"),
        "live_agent_status": callback.get("live_agent_status"),
        "pointer_session_id": pointer_session_id,
        "target_session_id": target_session_id,
        "event_marker_session_id": marker.get("session_id"),
        "lock_release": lock_release,
        "queue_drain": queue_drain,
        "slack": slack_result,
    }


def is_live_agent_support_control(event):
    if not isinstance(event, dict):
        return False
    return text_or_empty(event.get("event_type")).lower() in {
        "live_agent_support_resolve",
        "live_agent_support_cancel",
        "live_agent_support_reassign",
    }


def support_control_callback(event):
    ticket_key = normalize_ticket_key(event)
    return {
        "event_type": text_or_empty(event.get("event_type")).lower(),
        "ticket_key": ticket_key,
        "ticket_status": first_text(event.get("ticket_status"), event.get("status")),
        "live_agent_status": first_text(event.get("live_agent_status")),
        "transition_to": first_text(event.get("transition_to"), event.get("action")),
        "updated_at": first_text(event.get("event_id"), event.get("action_ts"), event.get("message_ts"), event.get("updated_at")),
        "action_user": first_text(event.get("action_user"), event.get("user")),
        "support_channel": first_text(event.get("support_channel"), event.get("channel")),
        "support_thread_ts": first_text(event.get("support_thread_ts"), event.get("thread_ts"), event.get("message_ts")),
        "raw_callback": event,
    }


def post_support_control_message(pointer, text):
    support_channel = text_or_empty(pointer.get("support_channel"))
    support_thread_ts = text_or_empty(pointer.get("support_thread_ts"))
    if not (support_channel and support_thread_ts):
        return {"attempted": False, "reason": "missing_support_thread"}
    return post_slack_message(support_channel, text, support_thread_ts)


def requester_control_thread_ts(pointer):
    channel = text_or_empty(pointer.get("slack_channel"))
    if channel.startswith("D"):
        return ""
    return text_or_empty(pointer.get("slack_thread_ts"))


def notify_requester_from_control(pointer, text, blocks=None):
    channel = text_or_empty(pointer.get("slack_channel"))
    if not channel:
        return {"attempted": False, "reason": "missing_slack_channel"}
    return post_slack_message(channel, text, requester_control_thread_ts(pointer), blocks=blocks)


def support_control_release_capacity(ticket_key, reason):
    if ENABLE_CUSTOM_CAPACITY_DISPATCHER:
        return capacity_dispatcher.release_capacity_for_ticket(ticket_key, reason)
    return chat_locks.release_chat_lock(ticket_key, close_reason=reason)


def support_control_queue_drain(release_result):
    if not release_result.get("released"):
        return {"drained": False, "reason": release_result.get("reason") or "capacity_not_released"}
    if ENABLE_CUSTOM_CAPACITY_DISPATCHER:
        promotion = capacity_dispatcher.promote_oldest_queued_ticket(notify=notify_promoted_ticket)
        return {"drained": bool(promotion.get("promoted")), **promotion}
    return drain_live_agent_queue()


def update_support_assignment_fields(pointer, assignment):
    if not session_table:
        return ""

    assigned_slack_user_id = resolve_agent_slack_user_id(pointer, assignment)
    now_iso = utc_now_iso()
    values = {
        ":assignment_status": assignment.get("assignment_status") or "",
        ":capacity_reserved": assignment.get("capacity_reserved") is True,
        ":capacity_released": False,
        ":assigned_agent_id": assignment.get("assigned_agent_id") or "",
        ":assigned_agent_name": assignment.get("assigned_agent_name") or "",
        ":assigned_jira_account_id": assignment.get("assigned_jira_account_id") or "",
        ":assigned_slack_user_id": assigned_slack_user_id,
        ":bridge_status": "active",
        ":now": now_iso,
        ":ttl": ttl_epoch(),
    }
    expression = """
        SET
            assignment_status = :assignment_status,
            capacity_reserved = :capacity_reserved,
            capacity_released = :capacity_released,
            assigned_agent_id = :assigned_agent_id,
            assigned_agent_name = :assigned_agent_name,
            assigned_jira_account_id = :assigned_jira_account_id,
            assigned_agent_slack_user_id = :assigned_slack_user_id,
            bridge_status = :bridge_status,
            live_agent_reassigned_at = :now,
            updated_at = :now,
            #ttl = :ttl
    """
    for session_id in dict.fromkeys([
        f"live_agent_ticket:{pointer.get('ticket_key')}",
        pointer.get("target_session_id"),
    ]):
        if not session_id:
            continue
        session_table.update_item(
            Key={"session_id": session_id},
            UpdateExpression=expression,
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues=values,
        )
    return assigned_slack_user_id


def handle_live_agent_support_resolve(event):
    callback = support_control_callback(event)
    if not callback.get("ticket_key"):
        return {"ok": False, "error": "Missing ticket_key", "error_code": "missing_ticket_key"}

    pointer_session_id = f"live_agent_ticket:{callback['ticket_key']}"
    pointer = get_live_agent_ticket_pointer(callback["ticket_key"])
    if not pointer:
        return {"ok": False, "error": "Missing live agent ticket pointer", "error_code": "missing_live_agent_ticket_pointer"}

    transition_result = transition_jira_issue_to_status(
        callback["ticket_key"],
        LIVE_AGENT_RESOLVE_STATUS_NAMES,
        reason="slack_support_resolve",
    )
    if not transition_result.get("ok"):
        support_result = post_support_control_message(
            pointer,
            support_control_transition_failed_message(
                callback["ticket_key"],
                LIVE_AGENT_RESOLVE_STATUS_NAMES,
                transition_result,
            ),
        )
        return {
            "ok": False,
            "ticket_key": callback["ticket_key"],
            "error": transition_result.get("error"),
            "error_code": transition_result.get("error_code") or "jira_resolve_transition_failed",
            "jira_transition": transition_result,
            "support_slack": support_result,
        }

    marker = create_live_agent_event_marker({
        **callback,
        "ticket_status": transition_result.get("target_status") or "Resolved",
        "live_agent_status": "resolved",
        "transition_to": "support_resolve",
    }, pointer_session_id)
    if not marker.get("created"):
        return {"ok": True, "duplicate": True, "ticket_key": callback["ticket_key"]}

    status_callback = {
        **callback,
        "ticket_status": transition_result.get("target_status") or "Resolved",
        "live_agent_status": "resolved",
    }
    resolution_summary_comment = add_live_agent_resolution_summary_comment(
        pointer,
        status_callback,
        utc_now_iso(),
    )
    if not resolution_summary_comment.get("ok"):
        log_json({
            "level": "ERROR",
            "message": "live_agent_resolution_summary_comment_failed",
            "ticket_key": callback["ticket_key"],
            "error": resolution_summary_comment.get("error"),
            "error_code": resolution_summary_comment.get("error_code"),
        })

    update_status_pointer(pointer_session_id, status_callback)
    if pointer.get("target_session_id"):
        update_status_target_session(pointer["target_session_id"], status_callback)

    release_result = support_control_release_capacity(callback["ticket_key"], "slack_support_resolve")
    queue_drain = support_control_queue_drain(release_result)
    summary_suffix = (
        " Summary added to Jira."
        if resolution_summary_comment.get("ok")
        else " Jira summary comment could not be added; check Lambda logs."
    )
    support_result = post_support_control_message(
        pointer,
        f"✅ <@{callback.get('action_user')}> resolved {callback['ticket_key']}.{summary_suffix}",
    )
    requester_result = notify_requester_from_control(
        pointer,
        f"✅ Live agent request {callback['ticket_key']} is now resolved.",
        blocks=live_agent_resolution_feedback_blocks(pointer, callback["ticket_key"]),
    )
    return {
        "ok": True,
        "event_type": callback["event_type"],
        "ticket_key": callback["ticket_key"],
        "bridge_status": "resolved",
        "lock_release": release_result,
        "queue_drain": queue_drain,
        "support_slack": support_result,
        "requester_slack": requester_result,
        "jira_transition": transition_result,
        "resolution_summary_comment": resolution_summary_comment,
    }


def handle_live_agent_support_cancel(event):
    callback = support_control_callback(event)
    if not callback.get("ticket_key"):
        return {"ok": False, "error": "Missing ticket_key", "error_code": "missing_ticket_key"}

    pointer_session_id = f"live_agent_ticket:{callback['ticket_key']}"
    pointer = get_live_agent_ticket_pointer(callback["ticket_key"])
    if not pointer:
        return {"ok": False, "error": "Missing live agent ticket pointer", "error_code": "missing_live_agent_ticket_pointer"}

    transition_result = transition_jira_issue_to_status(
        callback["ticket_key"],
        LIVE_AGENT_CANCEL_STATUS_NAMES,
        reason="slack_support_cancel",
    )
    if not transition_result.get("ok"):
        support_result = post_support_control_message(
            pointer,
            support_control_transition_failed_message(
                callback["ticket_key"],
                LIVE_AGENT_CANCEL_STATUS_NAMES,
                transition_result,
            ),
        )
        return {
            "ok": False,
            "ticket_key": callback["ticket_key"],
            "error": transition_result.get("error"),
            "error_code": transition_result.get("error_code") or "jira_cancel_transition_failed",
            "jira_transition": transition_result,
            "support_slack": support_result,
        }

    marker = create_live_agent_event_marker({
        **callback,
        "ticket_status": transition_result.get("target_status") or "Cancelled",
        "live_agent_status": "cancelled",
        "transition_to": "support_cancel",
    }, pointer_session_id)
    if not marker.get("created"):
        return {"ok": True, "duplicate": True, "ticket_key": callback["ticket_key"]}

    status_callback = {
        **callback,
        "ticket_status": transition_result.get("target_status") or "Cancelled",
        "live_agent_status": "cancelled",
    }
    update_status_pointer(pointer_session_id, status_callback)
    if pointer.get("target_session_id"):
        update_status_target_session(pointer["target_session_id"], status_callback)

    release_result = support_control_release_capacity(callback["ticket_key"], "slack_support_cancel")
    queue_drain = support_control_queue_drain(release_result)
    support_result = post_support_control_message(
        pointer,
        f"🚫 <@{callback.get('action_user')}> cancelled {callback['ticket_key']}.",
    )
    requester_result = notify_requester_from_control(
        pointer,
        f"🚫 Live agent request {callback['ticket_key']} has been cancelled.",
    )
    return {
        "ok": True,
        "event_type": callback["event_type"],
        "ticket_key": callback["ticket_key"],
        "bridge_status": "cancelled",
        "lock_release": release_result,
        "queue_drain": queue_drain,
        "support_slack": support_result,
        "requester_slack": requester_result,
        "jira_transition": transition_result,
    }


def handle_live_agent_support_reassign(event):
    callback = support_control_callback(event)
    if not callback.get("ticket_key"):
        return {"ok": False, "error": "Missing ticket_key", "error_code": "missing_ticket_key"}

    pointer_session_id = f"live_agent_ticket:{callback['ticket_key']}"
    pointer = get_live_agent_ticket_pointer(callback["ticket_key"])
    if not pointer:
        return {"ok": False, "error": "Missing live agent ticket pointer", "error_code": "missing_live_agent_ticket_pointer"}

    marker = create_live_agent_event_marker({
        **callback,
        "ticket_status": "Reassign",
        "live_agent_status": "reassigning",
        "transition_to": "support_reassign",
    }, pointer_session_id)
    if not marker.get("created"):
        return {"ok": True, "duplicate": True, "ticket_key": callback["ticket_key"]}

    release_result = support_control_release_capacity(callback["ticket_key"], "slack_support_reassign")
    assign_callback = {
        "event_type": "live_agent_support_reassign",
        "ticket_key": pointer.get("ticket_key"),
        "ticket_url": pointer.get("ticket_url") or "",
        "ticket_status": pointer.get("ticket_status") or "",
        "session_id": pointer.get("target_session_id") or "",
        "slack_channel": pointer.get("slack_channel") or "",
        "slack_thread_ts": pointer.get("slack_thread_ts") or "",
        "slack_user": pointer.get("slack_user") or "",
        "user_request": pointer.get("user_request") or "",
        "raw_callback": event,
    }
    assignment = (
        capacity_assignment_for_ticket(assign_callback, {})
        if ENABLE_CUSTOM_CAPACITY_DISPATCHER
        else legacy_assignment_for_ticket(assign_callback)
    )
    assigned_slack_user_id = update_support_assignment_fields(pointer, assignment)
    if assignment.get("assignment_status") == "ASSIGNED":
        message = (
            f"🔁 <@{callback.get('action_user')}> reassigned {callback['ticket_key']} "
            f"to {slack_user_mention(assigned_slack_user_id)}."
        )
    else:
        message = f"🔁 <@{callback.get('action_user')}> requested reassignment. No agent is available, so {callback['ticket_key']} is queued."
    support_result = post_support_control_message(pointer, message)
    return {
        "ok": True,
        "event_type": callback["event_type"],
        "ticket_key": callback["ticket_key"],
        "release": release_result,
        "assignment": assignment,
        "assigned_agent_slack_user_id": assigned_slack_user_id,
        "support_slack": support_result,
    }


def handle_jsm_callback(event):
    callback = enrich_missing_slack_context(normalize_callback(event))

    log_json({
        "level": "INFO",
        "message": "live_agent_ticket_created_payload_normalized",
        "event_type": callback.get("event_type"),
        "ticket_key": callback.get("ticket_key"),
        "assignee_account_id": callback.get("assignee_account_id"),
        "assignee_display_name": callback.get("assignee_display_name"),
        "slack_channel": callback.get("slack_channel"),
        "slack_thread_ts": callback.get("slack_thread_ts"),
        "session_id": callback.get("session_id"),
    })

    required_fields = {
        "ticket_key": "missing_ticket_key",
        "slack_channel": "missing_slack_channel",
        "slack_user": "missing_slack_user",
        "session_id": "missing_session_id",
    }
    missing_fields = [
        field for field in required_fields
        if not callback.get(field)
    ]

    if missing_fields:
        return {
            "ok": False,
            "error": f"Missing required callback field(s): {', '.join(missing_fields)}",
            "error_code": required_fields[missing_fields[0]],
            "missing_fields": missing_fields,
            "event_type": callback.get("event_type"),
            "ticket_key": callback.get("ticket_key"),
            "slack_channel": callback.get("slack_channel"),
            "slack_user": callback.get("slack_user"),
            "session_id": callback.get("session_id"),
        }

    existing_pointer = (
        get_live_agent_ticket_pointer(callback["ticket_key"])
        if ENABLE_CUSTOM_CAPACITY_DISPATCHER
        else {}
    )
    update_live_agent_session(callback)
    pointer_session_id = put_live_agent_ticket_pointer(callback)
    assignment = (
        capacity_assignment_for_ticket(callback, existing_pointer)
        if ENABLE_CUSTOM_CAPACITY_DISPATCHER
        else legacy_assignment_for_ticket(callback)
    )
    assigned = assignment.get("assignment_status") == "ASSIGNED"
    queued = not assigned
    if assigned:
        if ENABLE_CUSTOM_CAPACITY_DISPATCHER:
            reply = live_agent_assigned_reply(
                callback.get("ticket_key"),
                callback.get("ticket_url"),
                assignment.get("assigned_agent_name"),
            )
        else:
            reply = (
                f"✅ Live agent ticket {callback.get('ticket_key')} has been created "
                f"and assigned to {assignment.get('assigned_agent_name')}."
            )
    elif (
        not ENABLE_CUSTOM_CAPACITY_DISPATCHER
        and not (assignment.get("oncall_user") or {}).get("ok")
    ):
        reply = live_agent_no_oncall_queue_reply(callback.get("ticket_key"))
    else:
        reply = live_agent_busy_queue_reply(callback.get("ticket_key"))
    live_agent_status = "ticket_created" if assigned else "queued"

    log_json({
        "level": "INFO",
        "message": "live_agent_ticket_created_assignment_decision",
        "event_type": callback.get("event_type"),
        "ticket_key": callback.get("ticket_key"),
        "session_id": callback.get("session_id"),
        "slack_channel": callback.get("slack_channel"),
        "slack_thread_ts": callback.get("slack_thread_ts"),
        "assigned_agent_id": assignment.get("assigned_agent_id"),
        "assignee_account_id": assignment.get("assigned_jira_account_id"),
        "assignee_display_name": assignment.get("assigned_agent_name"),
        "capacity_reserved": assignment.get("capacity_reserved"),
        "queued": queued,
        "assignment_status": assignment.get("assignment_status"),
        "jira_assignment_result": assignment.get("jira_assignment"),
    })

    slack_result = {"attempted": False, "deduped": False}
    if callback.get("slack_channel"):
        if acquire_slack_confirmation(callback, pointer_session_id):
            try:
                slack_result = post_slack_message(
                    callback["slack_channel"],
                    reply,
                    callback.get("slack_thread_ts"),
                )
                mark_slack_confirmation(pointer_session_id, "sent", result=slack_result)
            except Exception as error:
                mark_slack_confirmation(pointer_session_id, "failed", error=error)
                log_json({
                    "level": "ERROR",
                    "message": "live_agent_callback_slack_notify_failed",
                    "session_id": callback["session_id"],
                    "ticket_key": callback["ticket_key"],
                    "error": str(error),
                })
                slack_result = {
                    "attempted": True,
                    "ok": False,
                    "error": str(error),
                }
        else:
            slack_result = {"attempted": False, "deduped": True}

    support_thread_result = {"attempted": False}
    try:
        support_thread_result = post_live_agent_support_thread(callback, assignment)
        mark_live_agent_support_thread(
            pointer_session_id,
            callback.get("session_id"),
            support_thread_result,
        )
    except Exception as error:
        support_thread_result = {
            "attempted": True,
            "ok": False,
            "error": str(error),
        }
        log_json({
            "level": "ERROR",
            "message": "live_agent_support_thread_post_failed",
            "session_id": callback["session_id"],
            "ticket_key": callback["ticket_key"],
            "error": str(error),
        })

    return {
        "ok": True,
        "session_id": callback["session_id"],
        "ticket_key": callback["ticket_key"],
        "ticket_url": callback.get("ticket_url"),
        "pointer_session_id": pointer_session_id,
        "assignee_account_id": callback.get("assignee_account_id"),
        "assignee_display_name": callback.get("assignee_display_name"),
        "assignee_email": callback.get("assignee_email"),
        "slack_channel": callback.get("slack_channel"),
        "slack_thread_ts": callback.get("slack_thread_ts"),
        "slack_user": callback.get("slack_user"),
        "user_request": callback.get("user_request"),
        "live_agent_jira_project": callback.get("jira_project"),
        "live_agent_issue_type": callback.get("issue_type"),
        "live_agent_portal_request_type": callback.get("portal_request_type"),
        "ticket_status": callback.get("ticket_status"),
        "status": callback.get("ticket_status"),
        "live_agent_status": live_agent_status,
        "assigned_agent_id": assignment.get("assigned_agent_id"),
        "assigned_agent_name": assignment.get("assigned_agent_name"),
        "assigned_jira_account_id": assignment.get("assigned_jira_account_id"),
        "assignment_status": assignment.get("assignment_status"),
        "capacity_reserved": assignment.get("capacity_reserved"),
        "capacity_released": False,
        "jira_assignment": assignment.get("jira_assignment"),
        "oncall_user": assignment.get("oncall_user"),
        "lock": assignment.get("legacy_lock"),
        "queue": assignment.get("legacy_queue"),
        "lock_acquired": bool(
            assignment.get("legacy_lock", {}).get("ok")
            or (
                ENABLE_CUSTOM_CAPACITY_DISPATCHER
                and assignment.get("capacity_reserved")
            )
        ),
        "queued": queued,
        "reply": reply,
        "message": reply,
        "slack": slack_result,
        "support_thread": support_thread_result,
    }


def get_config(intent_name=None):
    if not config_table:
        return None

    key = intent_name or LIVE_AGENT_CONFIG_INTENT
    try:
        response = config_table.get_item(Key={"intentName": key})
        return response.get("Item")
    except Exception as error:
        log_json({
            "level": "WARN",
            "message": "live_agent_config_lookup_failed",
            "intent_name": key,
            "error": str(error),
        })
        return None


def compact_conversation(event, limit=40):
    conversation = event.get("conversation")
    if isinstance(conversation, list):
        return conversation[-limit:]

    session_messages = event.get("session_messages")
    if isinstance(session_messages, list):
        return session_messages[-limit:]

    return []


def lex_session_attributes(event):
    return ((event.get("sessionState") or {}).get("sessionAttributes") or {})


def lex_intent_name(event):
    return (((event.get("sessionState") or {}).get("intent") or {}).get("name") or event.get("intent_name"))


def get_slot_value(slots, slot_name):
    slot = (slots or {}).get(slot_name) or {}
    value = slot.get("value") or {}
    return value.get("interpretedValue") or value.get("originalValue") or ""


def lex_conversation_summary(event, description):
    attrs = lex_session_attributes(event)
    parts = []

    if description:
        parts.append(f"User request: {description}")

    if attrs.get("last_bot_reply"):
        parts.append(f"Last IVY reply: {attrs.get('last_bot_reply')}")

    if attrs.get("response_source"):
        parts.append(f"Response source before handoff: {attrs.get('response_source')}")

    return "\n".join(parts) or "User requested live agent support from IVY."


def handoff_slack_context_from_lex(event):
    normalized = normalize_callback(event)
    return canonical_slack_context(
        normalized.get("session_id"),
        normalized.get("session_root_ts"),
        normalized.get("slack_channel"),
        normalized.get("slack_thread_ts"),
        normalized.get("slack_user"),
    )


def handoff_slack_context_from_worker(event):
    normalized = normalize_callback(event)
    return canonical_slack_context(
        normalized.get("session_id"),
        normalized.get("session_root_ts"),
        normalized.get("slack_channel"),
        normalized.get("slack_thread_ts"),
        normalized.get("slack_user"),
    )


def from_lex_event(event, config):
    attrs = lex_session_attributes(event)
    slots = ((event.get("sessionState") or {}).get("intent") or {}).get("slots") or {}
    config_params = parse_json_map((config or {}).get("parameters"))
    slack_context = handoff_slack_context_from_lex(event)
    description = (
        get_slot_value(slots, "jira_description")
        or event.get("inputTranscript")
        or attrs.get("last_user_text")
        or ""
    )

    return {
        "type": "live_agent_handoff",
        "source": "lex",
        **slack_context,
        "intent_name": lex_intent_name(event),
        "title": attrs.get("title") or (config or {}).get("title") or "Live agent support request",
        "description": description,
        "conversation_summary": attrs.get("conversation_summary") or lex_conversation_summary(event, description),
        "conversation_text": attrs.get("conversation_text") or lex_conversation_summary(event, description),
        "requestType": (config or {}).get("requestType") or LIVE_AGENT_DEFAULT_REQUEST_TYPE,
        "branching": (config or {}).get("branching") or LIVE_AGENT_DEFAULT_BRANCHING,
        "assignment": {
            "assignee": config_params.get("assignee"),
            "projectParams": config_params.get("projectParams") or config_params.get("assignee"),
        },
        "businessNotification": {
            "description": (config or {}).get("description"),
            "additionsDetails": [],
            "slackMessage": [],
        },
        "user": slack_context["slack_user"],
        "email": attrs.get("email"),
        "atlassianAccountId": attrs.get("atlassianAccountId"),
        "config": config,
    }


def from_worker_event(event, config):
    config_params = parse_json_map((config or event.get("config") or {}).get("parameters"))
    incoming_config = event.get("config") if isinstance(event.get("config"), dict) else {}
    effective_config = config or incoming_config
    slack = event.get("slack") if isinstance(event.get("slack"), dict) else {}
    slack_context = handoff_slack_context_from_worker(event)
    canonical_slack = {
        **slack,
        **slack_context["slack"],
    }

    return {
        **event,
        "type": "live_agent_handoff",
        "source": event.get("source") or "slack",
        **{key: value for key, value in slack_context.items() if key != "slack"},
        "title": event.get("title") or effective_config.get("title") or "Live agent support request",
        "description": event.get("description") or event.get("raw_text") or "",
        "conversation_summary": event.get("conversation_summary"),
        "conversation_text": event.get("conversation_text"),
        "requestType": event.get("requestType") or effective_config.get("requestType") or LIVE_AGENT_DEFAULT_REQUEST_TYPE,
        "branching": event.get("branching") or effective_config.get("branching") or LIVE_AGENT_DEFAULT_BRANCHING,
        "assignment": {
            **(event.get("assignment") or {}),
            "assignee": (event.get("assignment") or {}).get("assignee") or config_params.get("assignee"),
            "projectParams": (
                (event.get("assignment") or {}).get("projectParams")
                or config_params.get("projectParams")
                or config_params.get("assignee")
            ),
        },
        "businessNotification": event.get("businessNotification") or {
            "description": effective_config.get("description"),
            "additionsDetails": [],
            "slackMessage": [],
        },
        "slack": canonical_slack,
        "user": slack_context["slack_user"],
        "conversation": compact_conversation(event),
        "config": effective_config,
    }


def build_handoff_payload(event):
    intent_name = lex_intent_name(event)
    config = get_config(intent_name) or get_config(LIVE_AGENT_CONFIG_INTENT)

    if event.get("sessionState"):
        return from_lex_event(event, config)

    return from_worker_event(event, config)


def call_webhook(payload):
    if not LIVE_AGENT_WEBHOOK_URL:
        return {
            "ok": False,
            "error": "Missing LIVE_AGENT_WEBHOOK_URL or AUTOMATION_WEBHOOK_URL",
            "error_code": "missing_live_agent_webhook_url",
        }

    headers = {"Content-Type": "application/json"}

    if LIVE_AGENT_WEBHOOK_SECRET:
          headers["X-Automation-Webhook-Token"] = LIVE_AGENT_WEBHOOK_SECRET

    request = urllib.request.Request(
         LIVE_AGENT_WEBHOOK_URL,
         data=json.dumps(payload).encode("utf-8"),
         headers=headers,
         method="POST",
    )

    with urllib.request.urlopen(request, timeout=LIVE_AGENT_WEBHOOK_TIMEOUT_SECONDS) as response:
        response_text = response.read().decode("utf-8").strip()
        if response.status < 200 or response.status >= 300:
            return {
                "ok": False,
                "error": f"Live agent webhook returned HTTP {response.status}",
                "error_code": "live_agent_webhook_failed",
                "status": response.status,
            }

    parsed_response = None
    if response_text:
        try:
            parsed_response = json.loads(response_text)
        except ValueError:
            parsed_response = {"message": response_text}

    return {
        "ok": True,
        "status": response.status,
        "response": parsed_response,
    }


def reply_from_webhook(result):
    response = result.get("response")
    if isinstance(response, dict):
        for key in ("reply", "message", "text"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return SUCCESS_REPLY if result.get("ok") else FAILURE_REPLY


def validate_api_handoff_payload(payload):
    context = handoff_slack_context_from_worker(payload)
    missing_fields = [
        field for field in ("session_id", "slack_channel", "slack_user")
        if not context.get(field)
    ]
    if not first_text(payload.get("description"), payload.get("conversation_summary")):
        missing_fields.append("description_or_conversation_summary")

    if missing_fields:
        return {
            "ok": False,
            "error": "Invalid live agent handoff payload",
            "error_code": "invalid_live_agent_handoff_payload",
            "missing_fields": missing_fields,
            "session_id": context.get("session_id"),
            "session_root_ts": context.get("session_root_ts"),
            "slack_channel": context.get("slack_channel"),
            "slack_thread_ts": context.get("slack_thread_ts"),
            "slack_user": context.get("slack_user"),
        }

    return {"ok": True, **context}


def lambda_handler(event, context):
    api_gateway_event = isinstance(event, dict) and "body" in event
    effective_event = parse_api_gateway_body(event) if api_gateway_event else event
    effective_event = effective_event if isinstance(effective_event, dict) else {}
    received_context = (
        normalize_callback(effective_event)
        if is_jsm_callback(effective_event) or is_unsupported_jsm_callback(effective_event)
        else handoff_slack_context_from_worker(effective_event)
    )

    log_json({
        "level": "INFO",
        "message": "live_agent_received",
        "session_id": received_context.get("session_id"),
        "session_root_ts": received_context.get("session_root_ts"),
        "slack_channel": received_context.get("slack_channel"),
        "slack_thread_ts": received_context.get("slack_thread_ts"),
        "slack_user": received_context.get("slack_user"),
        "intent_name": lex_intent_name(effective_event),
        "api_gateway_event": api_gateway_event,
        "callback": is_jsm_callback(effective_event),
    })

    try:
        if is_live_agent_support_control(effective_event):
            event_type = text_or_empty(effective_event.get("event_type")).lower()
            if event_type == "live_agent_support_resolve":
                result = handle_live_agent_support_resolve(effective_event)
            elif event_type == "live_agent_support_cancel":
                result = handle_live_agent_support_cancel(effective_event)
            else:
                result = handle_live_agent_support_reassign(effective_event)
            status_code = 200 if result.get("ok") else 400
            log_json({
                "level": "INFO" if result.get("ok") else "ERROR",
                "message": "live_agent_support_control_completed",
                "ok": result.get("ok"),
                "event_type": event_type,
                "ticket_key": result.get("ticket_key"),
                "error_code": result.get("error_code"),
            })
            return api_response(status_code, result) if api_gateway_event else result

        if is_jsm_callback(effective_event):
            if api_gateway_event and not validate_callback_secret(event):
                result = {
                    "ok": False,
                    "error": "Invalid callback secret",
                    "error_code": "invalid_callback_secret",
                }
                return api_response(401, result)

            if is_public_comment_callback(effective_event):
                result = handle_live_agent_public_comment_added(effective_event)
            elif is_status_changed_callback(effective_event):
                result = handle_live_agent_status_changed(effective_event)
            else:
                result = handle_jsm_callback(effective_event)
            status_code = 200 if result.get("ok") else 400

            log_json({
                "level": "INFO" if result.get("ok") else "ERROR",
                "message": "live_agent_callback_completed",
                "session_id": result.get("session_id") or effective_event.get("session_id"),
                "ok": result.get("ok"),
                "event_type": effective_event.get("event_type"),
                "ticket_key": result.get("ticket_key"),
                "error_code": result.get("error_code"),
            })

            return api_response(status_code, result) if api_gateway_event else result

        if is_unsupported_jsm_callback(effective_event):
            result = {
                "ok": True,
                "ignored": True,
                "message": "Unsupported JSM callback event_type ignored",
                "event_type": effective_event.get("event_type"),
            }
            return api_response(202, result) if api_gateway_event else result

        if api_gateway_event:
            validation = validate_api_handoff_payload(effective_event)
            if not validation.get("ok"):
                log_json({
                    "level": "ERROR",
                    "message": "live_agent_invalid_handoff_payload",
                    "session_id": validation.get("session_id"),
                    "session_root_ts": validation.get("session_root_ts"),
                    "slack_channel": validation.get("slack_channel"),
                    "slack_thread_ts": validation.get("slack_thread_ts"),
                    "slack_user": validation.get("slack_user"),
                    "missing_fields": validation.get("missing_fields"),
                    "error_code": validation.get("error_code"),
                })
                return api_response(400, validation)

        payload = build_handoff_payload(effective_event)
        result = call_webhook(payload)
        reply = reply_from_webhook(result)

        log_json({
            "level": "INFO" if result.get("ok") else "ERROR",
            "message": "live_agent_completed",
            "session_id": payload.get("session_id"),
            "request_type": payload.get("requestType"),
            "branching": payload.get("branching"),
            "ok": result.get("ok"),
            "status": result.get("status"),
            "error_code": result.get("error_code"),
        })

        final_result = {
            **result,
            "reply": reply,
            "message": reply,
        }

        return api_response(200 if result.get("ok") else 502, final_result) if api_gateway_event else final_result

    except urllib.error.HTTPError as error:
        log_json({
            "level": "ERROR",
            "message": "live_agent_http_error",
            "session_id": received_context.get("session_id"),
            "session_root_ts": received_context.get("session_root_ts"),
            "status": error.code,
        })
        result = {
            "ok": False,
            "error": f"Live agent webhook returned HTTP {error.code}",
            "error_code": "live_agent_webhook_failed",
            "status": error.code,
            "reply": FAILURE_REPLY,
            "message": FAILURE_REPLY,
        }
        return api_response(error.code, result) if api_gateway_event else result

    except Exception as error:
        log_json({
            "level": "ERROR",
            "message": "live_agent_failed",
            "session_id": received_context.get("session_id"),
            "session_root_ts": received_context.get("session_root_ts"),
            "error": str(error),
        })
        result = {
            "ok": False,
            "error": str(error),
            "error_code": "live_agent_failed",
            "reply": FAILURE_REPLY,
            "message": FAILURE_REPLY,
        }
        return api_response(500, result) if api_gateway_event else result
