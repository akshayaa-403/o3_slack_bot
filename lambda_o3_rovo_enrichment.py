import json
import os
import urllib.request
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr

AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "o3_slack_sessions")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
ROVO_CALLBACK_SHARED_SECRET = os.environ.get("ROVO_CALLBACK_SHARED_SECRET")
SESSION_LOOKUP_SCAN_PAGES = int(os.environ.get("SESSION_LOOKUP_SCAN_PAGES", "10"))
TICKET_SESSION_KEY_PREFIX = "jira_ticket#"

ROVO_AGENT_NAME = os.environ.get("ROVO_AGENT_NAME", "Project IVY Jira Enrichment")
ROVO_CONFLUENCE_SCOPE = os.environ.get("ROVO_CONFLUENCE_SCOPE", "Confluence IS KB space/page tree")
ROVO_JIRA_TRIGGER = os.environ.get(
    "ROVO_JIRA_TRIGGER",
    "Jira automation rule triggered when O3Bot creates a ticket",
)
ROVO_FORGE_ACTION = os.environ.get(
    "ROVO_FORGE_ACTION",
    "Optional Forge action for automatic Jira commenting",
)
ROVO_AGENT_INSTRUCTION = (
    'Use only Confluence KB. If no article exists, say no KB article found.'
)

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
sessions_table = dynamodb.Table(DYNAMODB_TABLE)


def to_iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def log_json(data):
    print(json.dumps(data, default=str))


def text_or_empty(value):
    if value is None:
        return ""

    return str(value).strip()


def extract_json_string_field(body, field_name):
    marker = f'"{field_name}"'
    marker_index = body.find(marker)
    if marker_index < 0:
        return ""

    colon_index = body.find(":", marker_index + len(marker))
    if colon_index < 0:
        return ""

    quote_index = body.find('"', colon_index + 1)
    if quote_index < 0:
        return ""

    chars = []
    escaped = False
    for char in body[quote_index + 1:]:
        if escaped:
            escape_map = {
                "n": "\n",
                "r": "\r",
                "t": "\t",
                '"': '"',
                "\\": "\\",
            }
            chars.append(escape_map.get(char, char))
            escaped = False
            continue

        if char == "\\":
            escaped = True
            continue

        if char == '"':
            return "".join(chars).strip()

        chars.append(char)

    return "".join(chars).strip()


def lenient_callback_body(body):
    parsed = {}

    for field_name in (
        "task",
        "ticket_key",
        "ticket_url",
        "issue_key",
        "issue_url",
        "agent_response",
        "agentResponse",
        "rovo_response",
    ):
        value = extract_json_string_field(body, field_name)
        if value:
            parsed[field_name] = value

    if parsed:
        return parsed

    return None


def parsed_event(event):
    if not isinstance(event, dict):
        return {}

    body = event.get("body")
    if body:
        if event.get("isBase64Encoded"):
            raise ValueError("Base64 encoded callback bodies are not supported")

        try:
            parsed_body = json.loads(body)
        except json.JSONDecodeError as e:
            lenient_body = lenient_callback_body(body)
            if lenient_body:
                merged = dict(event)
                merged.update(lenient_body)
                return merged

            raise ValueError(f"Invalid JSON callback body: {e}")

        if isinstance(parsed_body, dict):
            merged = dict(event)
            merged.update(parsed_body)
            return merged

    return event


def require_callback_secret(event):
    if not ROVO_CALLBACK_SHARED_SECRET:
        return

    headers = event.get("headers") or {}
    provided_secret = (
        text_or_empty(event.get("callback_secret"))
        or text_or_empty(headers.get("x-rovo-callback-secret"))
        or text_or_empty(headers.get("X-Rovo-Callback-Secret"))
    )

    if provided_secret != ROVO_CALLBACK_SHARED_SECRET:
        raise ValueError("Invalid Rovo callback secret")


def slack_mrkdwn(text, limit=2900):
    value = text_or_empty(text)

    if len(value) > limit:
        value = value[: max(0, limit - 3)].rstrip() + "..."

    return value


def send_slack_message(channel, text):
    if not SLACK_BOT_TOKEN:
        raise ValueError("Missing required environment variable: SLACK_BOT_TOKEN")

    data = json.dumps({
        "channel": channel,
        "text": text,
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": slack_mrkdwn(text),
                },
            }
        ],
    }).encode("utf-8")

    request = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise ValueError(f"Slack API error: {result.get('error')}")

    return result


def update_slack_message(channel, ts, text):
    if not SLACK_BOT_TOKEN:
        raise ValueError("Missing required environment variable: SLACK_BOT_TOKEN")

    data = json.dumps({
        "channel": channel,
        "ts": ts,
        "text": text,
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": slack_mrkdwn(text),
                },
            }
        ],
    }).encode("utf-8")

    request = urllib.request.Request(
        "https://slack.com/api/chat.update",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise ValueError(f"Slack API error: {result.get('error')}")

    return result


def slack_channel_from_event(event):
    channel = (
        text_or_empty(event.get("slack_channel"))
        or text_or_empty(event.get("channel"))
    )

    if channel:
        return channel

    session_id = text_or_empty(event.get("session_id"))
    if ":" in session_id:
        return session_id.split(":", 1)[0]

    return ""


def ticket_session_key(ticket_key):
    return f"{TICKET_SESSION_KEY_PREFIX}{ticket_key}"


def put_ticket_session_mapping(event, mapped_at):
    ticket_key = text_or_empty(event.get("ticket_key")) or text_or_empty(event.get("issue_key"))
    session_id = text_or_empty(event.get("session_id"))
    channel = slack_channel_from_event(event)

    if not (ticket_key and session_id and channel):
        return

    sessions_table.put_item(
        Item={
            "session_id": ticket_session_key(ticket_key),
            "record_type": "jira_ticket_slack_mapping",
            "ticket_key": ticket_key,
            "source_session_id": session_id,
            "channel": channel,
            "user": text_or_empty(event.get("user")),
            "jira_request_id": text_or_empty(event.get("jira_request_id")),
            "rovo_slack_message_ts": text_or_empty(event.get("rovo_slack_message_ts")),
            "rovo_slack_original_text": text_or_empty(event.get("rovo_slack_original_text")),
            "created_at": mapped_at,
            "updated_at": mapped_at,
        }
    )


def find_session_by_ticket_key(ticket_key):
    if not ticket_key:
        return None

    mapping_response = sessions_table.get_item(
        Key={
            "session_id": ticket_session_key(ticket_key)
        }
    )
    mapping_item = mapping_response.get("Item")
    if mapping_item:
        return mapping_item

    filter_expression = (
        Attr("jira_ticket_key").eq(ticket_key)
        | Attr("last_jira_ticket_key").eq(ticket_key)
    )
    scan_kwargs = {
        "FilterExpression": filter_expression,
        "Limit": 50,
    }

    for _ in range(max(1, SESSION_LOOKUP_SCAN_PAGES)):
        response = sessions_table.scan(**scan_kwargs)
        items = response.get("Items") or []
        if items:
            return items[0]

        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break

        scan_kwargs["ExclusiveStartKey"] = last_key

    return None


def enrich_callback_from_session(event):
    if slack_channel_from_event(event) and text_or_empty(event.get("session_id")):
        return event

    ticket_key = (
        text_or_empty(event.get("ticket_key"))
        or text_or_empty(event.get("issue_key"))
    )
    session_item = find_session_by_ticket_key(ticket_key)
    if not session_item:
        return event

    source_session = None
    source_session_id = text_or_empty(session_item.get("source_session_id"))
    if source_session_id:
        source_response = sessions_table.get_item(
            Key={
                "session_id": source_session_id
            }
        )
        source_session = source_response.get("Item")

    enriched = dict(event)
    enriched.setdefault(
        "session_id",
        source_session_id
        or text_or_empty(session_item.get("session_id")),
    )
    enriched.setdefault(
        "slack_channel",
        text_or_empty(session_item.get("channel"))
        or text_or_empty((source_session or {}).get("channel")),
    )
    enriched.setdefault(
        "jira_request_id",
        text_or_empty(session_item.get("jira_request_id"))
        or text_or_empty((source_session or {}).get("jira_request_id")),
    )
    enriched.setdefault(
        "slack_message_ts",
        text_or_empty(session_item.get("rovo_slack_message_ts"))
        or text_or_empty((source_session or {}).get("rovo_slack_message_ts")),
    )
    enriched.setdefault(
        "slack_original_text",
        text_or_empty(session_item.get("rovo_slack_original_text"))
        or text_or_empty((source_session or {}).get("rovo_slack_original_text")),
    )
    return enriched


def callback_text(event):
    agent_response = (
        text_or_empty(event.get("agent_response"))
        or text_or_empty(event.get("agentResponse"))
        or text_or_empty(event.get("rovo_response"))
    )
    if not agent_response:
        raise ValueError("Missing required callback field: agent_response")

    ticket_key = text_or_empty(event.get("ticket_key")) or text_or_empty(event.get("issue_key"))
    ticket_url = text_or_empty(event.get("ticket_url")) or text_or_empty(event.get("issue_url"))

    lines = ["*Rovo KB enrichment*"]
    if ticket_key and ticket_url:
        lines.append(f"<{ticket_url}|{ticket_key}>")
    elif ticket_key:
        lines.append(ticket_key)
    elif ticket_url:
        lines.append(ticket_url)

    lines.extend(["", agent_response])
    return "\n".join(lines)


def callback_update_text(event):
    original_text = text_or_empty(event.get("slack_original_text"))
    enrichment_text = callback_text(event)

    if original_text:
        return f"{original_text}\n\n{enrichment_text}"

    return enrichment_text


def rovo_native_configuration():
    return {
        "agent": {
            "type": "native_atlassian_rovo_agent",
            "name": ROVO_AGENT_NAME,
            "knowledge_source": ROVO_CONFLUENCE_SCOPE,
            "instruction": ROVO_AGENT_INSTRUCTION,
        },
        "trigger": {
            "source": "jira",
            "mechanism": ROVO_JIRA_TRIGGER,
            "ticket_creator": "O3Bot",
        },
        "jira_enrichment": {
            "producer": "native_rovo_agent",
            "automatic_commenting": ROVO_FORGE_ACTION,
        },
    }


def build_handoff_summary(event):
    ticket_key = text_or_empty(event.get("ticket_key")) or "-"
    return (
        f"Native Atlassian Rovo enrichment delegated for Jira ticket {ticket_key}. "
        f"Agent scope: {ROVO_CONFLUENCE_SCOPE}. "
        f'Instruction: "{ROVO_AGENT_INSTRUCTION}"'
    )


def update_rovo_session(event, status, enriched_at, **fields):
    session_id = text_or_empty(event.get("session_id"))
    if not session_id:
        log_json({
            "level": "WARN",
            "message": "rovo_session_update_skipped",
            "reason": "missing_session_id",
            "jira_request_id": event.get("jira_request_id"),
            "ticket_key": event.get("ticket_key"),
        })
        return

    expression_values = {
        ":status": status,
        ":enriched_at": enriched_at,
        ":updated_at": enriched_at,
    }
    update_expression = """
        SET
            rovo_status = :status,
            rovo_enriched_at = :enriched_at,
            updated_at = :updated_at
    """

    optional_fields = {
        "rovo_error": fields.get("error"),
        "rovo_error_code": fields.get("error_code"),
        "rovo_summary": fields.get("summary"),
    }

    for field_name, value in optional_fields.items():
        if value:
            placeholder = f":{field_name}"
            update_expression += f", {field_name} = {placeholder}"
            expression_values[placeholder] = value

    sessions_table.update_item(
        Key={
            "session_id": session_id
        },
        UpdateExpression=update_expression,
        ExpressionAttributeValues=expression_values,
    )


def failure_response(event, error, error_code):
    enriched_at = to_iso(datetime.now(timezone.utc))

    try:
        update_rovo_session(
            event,
            "failed",
            enriched_at,
            error=str(error),
            error_code=error_code,
        )

    except Exception as update_error:
        log_json({
            "level": "ERROR",
            "message": "rovo_failure_session_update_failed",
            "jira_request_id": event.get("jira_request_id"),
            "ticket_key": event.get("ticket_key"),
            "error": str(update_error),
            "original_error": str(error),
            "original_error_code": error_code,
        })

    return {
        "ok": False,
        "rovo_status": "failed",
        "error": str(error),
        "error_code": error_code,
    }


def handle_slack_callback(event):
    require_callback_secret(event)
    event = enrich_callback_from_session(event)

    channel = slack_channel_from_event(event)
    if not channel:
        raise ValueError("Missing Slack channel. Provide slack_channel or session_id.")

    text = callback_update_text(event)
    slack_ts = text_or_empty(event.get("slack_message_ts"))
    if slack_ts:
        slack_response = update_slack_message(channel, slack_ts, text)
    else:
        slack_response = send_slack_message(channel, text)

    completed_at = to_iso(datetime.now(timezone.utc))

    update_rovo_session(
        event,
        "completed",
        completed_at,
        summary=text,
    )

    log_json({
        "level": "INFO",
        "message": "native_rovo_enrichment_slack_posted",
        "jira_request_id": event.get("jira_request_id"),
        "ticket_key": event.get("ticket_key") or event.get("issue_key"),
        "session_id": event.get("session_id"),
        "slack_channel": channel,
        "slack_message_updated": bool(slack_ts),
        "slack_ts": slack_response.get("ts"),
    })

    return {
        "ok": True,
        "rovo_status": "completed",
        "slack_channel": channel,
        "slack_message_updated": bool(slack_ts),
        "slack_ts": slack_response.get("ts"),
    }


def lambda_handler(raw_event, context):
    event = parsed_event(raw_event)

    log_json({
        "level": "INFO",
        "message": "native_rovo_enrichment_handoff_received",
        "jira_request_id": event.get("jira_request_id"),
        "ticket_key": event.get("ticket_key"),
        "session_id": event.get("session_id"),
    })

    try:
        if event.get("task") == "slack_callback" or event.get("agent_response") or event.get("agentResponse"):
            return handle_slack_callback(event)

        ticket_key = text_or_empty(event.get("ticket_key"))
        if not ticket_key:
            raise ValueError("Missing required event field: ticket_key")

        handed_off_at = to_iso(datetime.now(timezone.utc))
        summary = build_handoff_summary(event)
        configuration = rovo_native_configuration()

        update_rovo_session(
            event,
            "delegated",
            handed_off_at,
            summary=summary,
        )
        put_ticket_session_mapping(event, handed_off_at)

        log_json({
            "level": "INFO",
            "message": "native_rovo_enrichment_delegated",
            "jira_request_id": event.get("jira_request_id"),
            "ticket_key": ticket_key,
            "session_id": event.get("session_id"),
            "rovo_agent_name": ROVO_AGENT_NAME,
            "confluence_scope": ROVO_CONFLUENCE_SCOPE,
        })

        return {
            "ok": True,
            "rovo_status": "delegated",
            "enrichment_summary": summary,
            "native_rovo_configuration": configuration,
            "handed_off_at": handed_off_at,
        }

    except Exception as e:
        log_json({
            "level": "ERROR",
            "message": "native_rovo_enrichment_handoff_failed",
            "jira_request_id": event.get("jira_request_id"),
            "ticket_key": event.get("ticket_key"),
            "session_id": event.get("session_id"),
            "error": str(e),
        })

        return failure_response(event, e, "native_rovo_handoff_failed")
