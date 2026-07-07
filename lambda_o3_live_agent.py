import json
import os
import base64
import hashlib
from datetime import datetime, timezone
import time
import urllib.error
import urllib.request

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)

CONFIG_TABLE = os.environ.get("CONFIG_TABLE") or os.environ.get("LIVE_AGENT_CONFIG_TABLE")
SESSION_TABLE = os.environ.get("DYNAMODB_TABLE") or os.environ.get("SESSION_TABLE") or "o3_slack_sessions"
LIVE_AGENT_CONFIG_INTENT = os.environ.get("LIVE_AGENT_CONFIG_INTENT", "LiveAgent")
LIVE_AGENT_WEBHOOK_URL = os.environ.get("LIVE_AGENT_WEBHOOK_URL") or os.environ.get("AUTOMATION_WEBHOOK_URL")
LIVE_AGENT_WEBHOOK_SECRET = os.environ.get("LIVE_AGENT_WEBHOOK_SECRET", "").strip()
LIVE_AGENT_CALLBACK_SECRET = os.environ.get("LIVE_AGENT_CALLBACK_SECRET", "").strip()
LIVE_AGENT_WEBHOOK_TIMEOUT_SECONDS = int(os.environ.get("LIVE_AGENT_WEBHOOK_TIMEOUT_SECONDS", "10"))
LIVE_AGENT_TICKET_BASE_URL = os.environ.get("LIVE_AGENT_TICKET_BASE_URL", "https://innovyq.atlassian.net").rstrip("/")
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "86400"))
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()

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

    return {
        "session_id": first_text(callback.get("session_id"), callback.get("sessionId")),
        "session_root_ts": first_text(callback.get("session_root_ts"), callback.get("sessionRootTs")),
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
        "slack_channel": first_text(callback.get("slack_channel"), callback.get("slackChannel"), slack.get("channelId"), slack.get("channel")),
        "slack_thread_ts": first_text(callback.get("slack_thread_ts"), callback.get("slackThreadTs"), slack.get("threadTs"), slack.get("thread_ts")),
        "slack_user": first_text(callback.get("slack_user"), callback.get("slackUser"), slack.get("userId"), slack.get("user")),
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

    if any(marker in normalized for marker in ("failed", "cancelled", "canceled")):
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


def post_slack_message(channel, text, thread_ts=""):
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
    callback = parse_api_gateway_body(event) if isinstance(event, dict) and "body" in event else event
    callback = callback if isinstance(callback, dict) else {}
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
        "event_type": callback_event_type(callback),
        "ticket_key": normalize_ticket_key(callback),
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
        "raw_callback": callback,
    }


def boolish_true(value):
    if isinstance(value, bool):
        return value

    return text_or_empty(value).lower() in {"true", "1", "yes", "y"}


def normalize_public_comment_callback(event):
    callback = parse_api_gateway_body(event) if isinstance(event, dict) and "body" in event else event
    callback = callback if isinstance(callback, dict) else {}

    comment = callback.get("comment") if isinstance(callback.get("comment"), dict) else {}
    author = comment.get("author") if isinstance(comment.get("author"), dict) else {}

    return {
        "event_type": callback_event_type(callback),
        "source": text_or_empty(callback.get("source")),
        "from_slack": boolish_true(callback.get("from_slack")),
        "ticket_key": normalize_ticket_key(callback),
        "ticket_url": normalize_ticket_url(callback, normalize_ticket_key(callback)),
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
        "raw_callback": callback,
    }


def is_echo_loop_comment(callback):
    return (
        "[From Slack]" in callback.get("comment_body", "")
        or text_or_empty(callback.get("source")).lower() == "slack_user"
        or boolish_true(callback.get("from_slack"))
    )


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

    return f"ℹ️ Live agent update for {ticket_key}: status changed to {status_text}."


def update_status_pointer(pointer_session_id, callback):
    if not session_table:
        return

    now_iso = utc_now_iso()
    session_table.update_item(
        Key={"session_id": pointer_session_id},
        UpdateExpression="""
            SET
                ticket_status = :ticket_status,
                live_agent_status = :live_agent_status,
                live_agent_status_changed_at = :now,
                live_agent_updated_at = :now,
                updated_at = :now,
                #ttl = :ttl
        """,
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={
            ":ticket_status": callback.get("ticket_status") or "",
            ":live_agent_status": callback.get("live_agent_status") or "",
            ":now": now_iso,
            ":ttl": ttl_epoch(),
        },
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

    if live_agent_status == "resolved":
        update_expression += """,
            conversation_status = :conversation_closed,
            support_options_status = :support_resolved,
            live_agent_resolved_at = :now
        """
        expression_values[":conversation_closed"] = "closed"
        expression_values[":support_resolved"] = "live_agent_resolved"

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
    if slack_channel:
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

    return {
        "ok": True,
        "event_type": callback.get("event_type"),
        "ticket_key": callback["ticket_key"],
        "ticket_status": callback.get("ticket_status"),
        "live_agent_status": callback.get("live_agent_status"),
        "pointer_session_id": pointer_session_id,
        "target_session_id": target_session_id,
        "event_marker_session_id": marker.get("session_id"),
        "slack": slack_result,
    }


def handle_jsm_callback(event):
    callback = normalize_callback(event)
    if not callback.get("session_id"):
        return {
            "ok": False,
            "error": "Missing callback session_id",
            "error_code": "missing_session_id",
        }

    if not callback.get("ticket_key"):
        return {
            "ok": False,
            "error": "Missing callback ticket_key",
            "error_code": "missing_ticket_key",
        }

    update_live_agent_session(callback)
    pointer_session_id = put_live_agent_ticket_pointer(callback)

    slack_result = {"attempted": False, "deduped": False}
    if callback.get("slack_channel"):
        if acquire_slack_confirmation(callback, pointer_session_id):
            try:
                slack_result = post_slack_ticket(callback)
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

    reply = live_agent_ticket_reply(callback.get("ticket_key"), callback.get("ticket_url"))
    return {
        "ok": True,
        "session_id": callback["session_id"],
        "ticket_key": callback["ticket_key"],
        "ticket_url": callback.get("ticket_url"),
        "pointer_session_id": pointer_session_id,
        "live_agent_jira_project": callback.get("jira_project"),
        "live_agent_issue_type": callback.get("issue_type"),
        "live_agent_portal_request_type": callback.get("portal_request_type"),
        "ticket_status": callback.get("ticket_status"),
        "status": callback.get("ticket_status"),
        "live_agent_status": "ticket_created",
        "reply": reply,
        "message": reply,
        "slack": slack_result,
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


def from_lex_event(event, config):
    attrs = lex_session_attributes(event)
    slots = ((event.get("sessionState") or {}).get("intent") or {}).get("slots") or {}
    config_params = parse_json_map((config or {}).get("parameters"))
    description = (
        get_slot_value(slots, "jira_description")
        or event.get("inputTranscript")
        or attrs.get("last_user_text")
        or ""
    )

    return {
        "type": "live_agent_handoff",
        "source": "lex",
        "session_id": event.get("sessionId"),
        "intent_name": lex_intent_name(event),
        "title": attrs.get("title") or (config or {}).get("title") or "Live agent support request",
        "description": description,
        "conversation_summary": attrs.get("conversation_summary") or lex_conversation_summary(event, description),
        "conversation_text": attrs.get("conversation_text") or lex_conversation_summary(event, description),
        "requestType": (config or {}).get("requestType"),
        "branching": (config or {}).get("branching"),
        "assignment": {
            "assignee": config_params.get("assignee"),
            "projectParams": config_params.get("projectParams") or config_params.get("assignee"),
        },
        "businessNotification": {
            "description": (config or {}).get("description"),
            "additionsDetails": [],
            "slackMessage": [],
        },
        "slack": {
            "threadTs": attrs.get("slackThreadTs"),
            "channelId": attrs.get("slackChannelId"),
            "userId": attrs.get("slackUserId"),
        },
        "user": attrs.get("slackUserId"),
        "email": attrs.get("email"),
        "atlassianAccountId": attrs.get("atlassianAccountId"),
        "config": config,
    }


def from_worker_event(event, config):
    config_params = parse_json_map((config or event.get("config") or {}).get("parameters"))
    incoming_config = event.get("config") if isinstance(event.get("config"), dict) else {}
    effective_config = config or incoming_config
    slack = event.get("slack") or {}

    return {
        **event,
        "type": "live_agent_handoff",
        "source": event.get("source") or "slack",
        "title": event.get("title") or effective_config.get("title") or "Live agent support request",
        "description": event.get("description") or event.get("raw_text") or "",
        "conversation_summary": event.get("conversation_summary"),
        "conversation_text": event.get("conversation_text"),
        "requestType": event.get("requestType") or effective_config.get("requestType"),
        "branching": event.get("branching") or effective_config.get("branching"),
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
        "slack": slack,
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


def lambda_handler(event, context):
    api_gateway_event = isinstance(event, dict) and "body" in event
    effective_event = parse_api_gateway_body(event) if api_gateway_event else event
    effective_event = effective_event if isinstance(effective_event, dict) else {}

    log_json({
        "level": "INFO",
        "message": "live_agent_received",
        "session_id": effective_event.get("session_id") or effective_event.get("sessionId"),
        "intent_name": lex_intent_name(effective_event),
        "api_gateway_event": api_gateway_event,
        "callback": is_jsm_callback(effective_event),
    })

    try:
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

        payload = build_handoff_payload(effective_event)
        result = call_webhook(payload)
        reply = reply_from_webhook(result)

        log_json({
            "level": "INFO" if result.get("ok") else "ERROR",
            "message": "live_agent_completed",
            "session_id": payload.get("session_id"),
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
            "session_id": effective_event.get("session_id") or effective_event.get("sessionId"),
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
            "session_id": effective_event.get("session_id") or effective_event.get("sessionId"),
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
