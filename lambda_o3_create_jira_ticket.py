import base64
import json
import os
import re
import socket
import urllib.error
import urllib.request

import boto3

AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
JIRA_SECRET_ID = os.environ.get("JIRA_SECRET_ID")
JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY")
JIRA_ISSUE_TYPE_NAME = os.environ.get("JIRA_ISSUE_TYPE_NAME", "Task")
JIRA_LABELS = [
    label.strip()
    for label in os.environ.get("JIRA_LABELS", "project-ivy,o3-slack").split(",")
    if label.strip()
]
JIRA_TIMEOUT_SECONDS = int(os.environ.get("JIRA_TIMEOUT_SECONDS", "15"))

secretsmanager = boto3.client("secretsmanager", region_name=AWS_REGION)
_jira_secret_cache = None


INTENT_TITLES = {
    "AWSaccount": "AWS account access",
    "AWSRelatedQueries": "AWS related request",
    "AccessforCamtasia": "Camtasia access",
    "AccesstoOpsgenie": "Opsgenie access",
    "AccessToPCQ": "PCQ access",
    "AccessToIkbInnovyQCom": "ikb.InnovyQ.com access",
    "AccessToUemGpcloudserviceCom": "uem.gpcloudservice.com access",
    "CreateJiraTicket": "Support request",
    "SessionSummary": "Closed session summary",
}


def log_json(data):
    print(json.dumps(data, default=str))


def require_env(name, value):
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")

    return value


def get_jira_secret():
    global _jira_secret_cache

    if _jira_secret_cache:
        return _jira_secret_cache

    secret_id = require_env("JIRA_SECRET_ID", JIRA_SECRET_ID)
    response = secretsmanager.get_secret_value(SecretId=secret_id)
    secret_string = response.get("SecretString")

    if not secret_string:
        raise ValueError("Jira secret must be stored as a JSON SecretString")

    secret = json.loads(secret_string)
    for key in ("site_url", "email", "api_token"):
        if not secret.get(key):
            raise ValueError(f"Jira secret is missing required key: {key}")
        secret[key] = str(secret[key]).strip()

    secret["site_url"] = secret["site_url"].rstrip("/")
    _jira_secret_cache = secret
    return secret


def text_or_empty(value):
    if value is None:
        return ""

    return str(value).strip()


def shorten(text, limit):
    text = re.sub(r"\s+", " ", text_or_empty(text))

    if len(text) <= limit:
        return text

    return text[: limit - 3].rstrip() + "..."


def safe_request_label(request_id):
    request_id = text_or_empty(request_id)

    if not request_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id):
        return None

    return f"ivy-request-{request_id[:32]}"


def jira_labels(event):
    labels = list(JIRA_LABELS)
    request_label = safe_request_label(event.get("jira_request_id"))

    if request_label and request_label not in labels:
        labels.append(request_label)

    return labels


def humanize_intent(intent_name):
    if intent_name in INTENT_TITLES:
        return INTENT_TITLES[intent_name]

    spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", text_or_empty(intent_name))
    return spaced or "Support request"


def adf_paragraph(text):
    return {
        "type": "paragraph",
        "content": [
            {
                "type": "text",
                "text": text_or_empty(text) or "-"
            }
        ]
    }


def adf_description(event):
    lex = event.get("lex", {}) or {}
    slack = event.get("slack", {}) or {}
    request_text = text_or_empty(event.get("text"))
    raw_text = text_or_empty(event.get("raw_text"))
    jira_request_id = text_or_empty(event.get("jira_request_id"))
    conversation_summary = text_or_empty(event.get("conversation_summary"))
    conversation_text = text_or_empty(event.get("conversation_text"))

    lines = [
        f"Slack user: {text_or_empty(event.get('user')) or text_or_empty(slack.get('user'))}",
        f"Slack channel: {text_or_empty(event.get('channel')) or text_or_empty(slack.get('channel'))}",
        f"Session ID: {text_or_empty(event.get('session_id'))}",
        f"Session root timestamp: {text_or_empty(event.get('session_root_ts')) or '-'}",
        f"Project IVY request ID: {jira_request_id or '-'}",
        f"Requested at: {text_or_empty(event.get('jira_requested_at')) or '-'}",
        f"Confirmed at: {text_or_empty(event.get('jira_confirmed_at')) or '-'}",
        f"Matched Lex intent: {text_or_empty(lex.get('intent'))}",
        f"Routing source: Project IVY Slack bot",
        "",
        "Conversation summary:",
        conversation_summary or "-",
        "",
        "Conversation transcript:",
        conversation_text or "-",
        "",
        "Original user request:",
        request_text or raw_text or "-",
    ]

    return {
        "version": 1,
        "type": "doc",
        "content": [adf_paragraph(line) for line in lines]
    }


def build_issue_payload(event):
    require_env("JIRA_PROJECT_KEY", JIRA_PROJECT_KEY)

    lex = event.get("lex", {}) or {}
    intent_name = text_or_empty(lex.get("intent"))
    request_text = text_or_empty(event.get("text")) or text_or_empty(event.get("raw_text"))
    action_title = humanize_intent(intent_name)
    summary_tail = shorten(request_text, 100) or "Support request"
    summary = shorten(f"[Project IVY] {action_title} - {summary_tail}", 255)

    fields = {
        "project": {
            "key": JIRA_PROJECT_KEY
        },
        "summary": summary,
        "issuetype": {
            "name": JIRA_ISSUE_TYPE_NAME
        },
        "description": adf_description(event),
    }

    labels = jira_labels(event)
    if labels:
        fields["labels"] = labels

    return {
        "fields": fields
    }


def jira_auth_header(secret):
    raw_token = f"{secret['email']}:{secret['api_token']}".encode("utf-8")
    encoded = base64.b64encode(raw_token).decode("utf-8")
    return f"Basic {encoded}"


def create_jira_issue(event):
    secret = get_jira_secret()
    url = f"{secret['site_url']}/rest/api/3/issue"
    payload = build_issue_payload(event)

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": jira_auth_header(secret),
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=JIRA_TIMEOUT_SECONDS) as response:
        response_payload = json.loads(response.read().decode("utf-8"))

    issue_key = response_payload.get("key")
    if not issue_key:
        raise ValueError("Jira create issue response did not include an issue key")

    return {
        "issue_id": response_payload.get("id"),
        "ticket_key": issue_key,
        "ticket_url": f"{secret['site_url']}/browse/{issue_key}",
    }


def parse_jira_error_body(body):
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {}


def jira_error_mentions_project_or_permission(error_payload):
    messages = error_payload.get("errorMessages") or []
    errors = error_payload.get("errors") or {}
    combined = " ".join(
        [str(message) for message in messages]
        + [str(key) for key in errors.keys()]
        + [str(value) for value in errors.values()]
    ).lower()

    return (
        "project" in combined
        and (
            "permission" in combined
            or "doesn't exist" in combined
            or "does not exist" in combined
        )
    )


def http_error_code(status, error_payload=None):
    if status in {401, 403}:
        return "jira_auth_or_permission_error"

    if status == 404:
        return "jira_project_or_permission_error"

    if status == 429:
        return "jira_rate_limited"

    if status >= 500:
        return "jira_service_error"

    if status == 400:
        if jira_error_mentions_project_or_permission(error_payload or {}):
            return "jira_project_or_permission_error"

        return "jira_request_rejected"

    return "jira_http_error"


def http_error_message(status, error_payload=None):
    if status in {401, 403, 404}:
        return "Jira rejected the request. Check Jira credentials, project key, issue type, and create-issue permissions."

    if status == 429:
        return "Jira rate limited the request."

    if status >= 500:
        return "Jira returned a temporary service error."

    if status == 400:
        if jira_error_mentions_project_or_permission(error_payload or {}):
            return "Jira rejected the request. Check Jira credentials, project key, issue type, and create-issue permissions."

        return "Jira rejected the issue payload. Check project key, issue type, and required fields."

    return f"Jira returned HTTP {status}."


def error_response(error, code="jira_create_failed", status=None):
    response = {
        "ok": False,
        "error": str(error),
        "error_code": code,
    }

    if status is not None:
        response["status"] = status

    return {
        **response
    }


def lambda_handler(event, context):
    log_json({
        "level": "INFO",
        "message": "jira_create_received",
        "session_id": event.get("session_id"),
        "jira_request_id": event.get("jira_request_id"),
        "intent": (event.get("lex") or {}).get("intent"),
    })

    try:
        issue = create_jira_issue(event)

        log_json({
            "level": "INFO",
            "message": "jira_create_completed",
            "session_id": event.get("session_id"),
            "jira_request_id": event.get("jira_request_id"),
            "ticket_key": issue["ticket_key"],
        })

        return {
            "ok": True,
            **issue,
        }

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        error_payload = parse_jira_error_body(body)
        log_json({
            "level": "ERROR",
            "message": "jira_create_http_error",
            "session_id": event.get("session_id"),
            "jira_request_id": event.get("jira_request_id"),
            "project_key": JIRA_PROJECT_KEY,
            "issue_type": JIRA_ISSUE_TYPE_NAME,
            "status": e.code,
            "body": body,
        })
        return error_response(
            http_error_message(e.code, error_payload),
            http_error_code(e.code, error_payload),
            e.code
        )

    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        log_json({
            "level": "ERROR",
            "message": "jira_create_network_error",
            "session_id": event.get("session_id"),
            "jira_request_id": event.get("jira_request_id"),
            "error": str(e),
        })
        return error_response("Jira did not respond in time.", "jira_network_error")

    except ValueError as e:
        log_json({
            "level": "ERROR",
            "message": "jira_create_configuration_error",
            "session_id": event.get("session_id"),
            "jira_request_id": event.get("jira_request_id"),
            "error": str(e),
        })
        return error_response(
            "Jira ticket creation is not configured correctly.",
            "jira_configuration_error"
        )

    except Exception as e:
        log_json({
            "level": "ERROR",
            "message": "jira_create_failed",
            "session_id": event.get("session_id"),
            "jira_request_id": event.get("jira_request_id"),
            "error": str(e),
        })
        return error_response("Jira ticket creation failed.")
