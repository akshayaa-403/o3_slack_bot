import json
import os
import urllib.error
import urllib.request

import boto3

AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)

CONFIG_TABLE = os.environ.get("CONFIG_TABLE") or os.environ.get("LIVE_AGENT_CONFIG_TABLE")
LIVE_AGENT_CONFIG_INTENT = os.environ.get("LIVE_AGENT_CONFIG_INTENT", "LiveAgent")
LIVE_AGENT_WEBHOOK_URL = os.environ.get("LIVE_AGENT_WEBHOOK_URL") or os.environ.get("AUTOMATION_WEBHOOK_URL")
LIVE_AGENT_WEBHOOK_SECRET = os.environ.get("LIVE_AGENT_WEBHOOK_SECRET", "").strip()
LIVE_AGENT_WEBHOOK_TIMEOUT_SECONDS = int(os.environ.get("LIVE_AGENT_WEBHOOK_TIMEOUT_SECONDS", "10"))

SUCCESS_REPLY = os.environ.get(
    "LIVE_AGENT_SUCCESS_REPLY",
    "I have sent this to live agent support. Someone from the support team will follow up."
)
FAILURE_REPLY = os.environ.get(
    "LIVE_AGENT_FAILURE_REPLY",
    "I could not send this to live agent support. Please try again later or create a Jira ticket."
)

config_table = dynamodb.Table(CONFIG_TABLE) if CONFIG_TABLE else None


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
    log_json({
        "level": "INFO",
        "message": "live_agent_received",
        "session_id": event.get("session_id") or event.get("sessionId"),
        "intent_name": lex_intent_name(event),
    })

    try:
        payload = build_handoff_payload(event)
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

        return {
            **result,
            "reply": reply,
            "message": reply,
        }

    except urllib.error.HTTPError as error:
        log_json({
            "level": "ERROR",
            "message": "live_agent_http_error",
            "session_id": event.get("session_id") or event.get("sessionId"),
            "status": error.code,
        })
        return {
            "ok": False,
            "error": f"Live agent webhook returned HTTP {error.code}",
            "error_code": "live_agent_webhook_failed",
            "status": error.code,
            "reply": FAILURE_REPLY,
            "message": FAILURE_REPLY,
        }

    except Exception as error:
        log_json({
            "level": "ERROR",
            "message": "live_agent_failed",
            "session_id": event.get("session_id") or event.get("sessionId"),
            "error": str(error),
        })
        return {
            "ok": False,
            "error": str(error),
            "error_code": "live_agent_failed",
            "reply": FAILURE_REPLY,
            "message": FAILURE_REPLY,
        }
