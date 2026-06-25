import base64
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import boto3

AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
JIRA_SECRET_ID = os.environ.get("JIRA_SECRET_ID")
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "o3_slack_sessions")
ROVO_MODE = os.environ.get("ROVO_MODE", "stub")
ROVO_COMMENT_PREFIX = os.environ.get("ROVO_COMMENT_PREFIX", "Project IVY enrichment")
ROVO_TIMEOUT_SECONDS = int(os.environ.get("ROVO_TIMEOUT_SECONDS", "15"))

secretsmanager = boto3.client("secretsmanager", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
sessions_table = dynamodb.Table(DYNAMODB_TABLE)
_jira_secret_cache = None


INTENT_TITLES = {
    "AWSaccount": "AWS account access",
    "AWSRelatedQueries": "AWS related request",
    "AccessforCamtasia": "Camtasia access",
    "AccesstoOpsgenie": "Opsgenie access",
    "AccessToPCQ": "PCQ access",
    "AccessToIkbInnovyQCom": "ikb.InnovyQ.com access",
    "AccessToUemGpcloudserviceCom": "uem.gpcloudservice.com access",
}

INTENT_ACTIONS = {
    "AccessToPCQ": [
        "Verify the requester is approved for PCQ access.",
        "Check whether the user already has the required PCQ group membership.",
        "Route to the access management queue if approval or group assignment is needed.",
    ],
    "AccesstoOpsgenie": [
        "Confirm the requester needs Opsgenie access for an active support role.",
        "Check the required Opsgenie team or escalation policy.",
        "Assign to the operations owner for approval if role scope is unclear.",
    ],
    "AWSaccount": [
        "Confirm the required AWS account, role, and business reason.",
        "Check whether access should be temporary or permanent.",
        "Route to cloud operations for least-privilege review.",
    ],
    "AWSRelatedQueries": [
        "Identify the AWS account, service, and environment involved.",
        "Check whether this is an access request, support issue, or migration task.",
        "Assign to cloud operations with the captured request details.",
    ],
    "AccessforCamtasia": [
        "Confirm license availability and manager approval.",
        "Check whether the requester needs install access or application entitlement.",
        "Assign to the software access queue.",
    ],
    "AccessToIkbInnovyQCom": [
        "Confirm the requester needs ikb.InnovyQ.com access.",
        "Check whether SSO group membership or application-side permission is required.",
        "Assign to the application access owner.",
    ],
    "AccessToUemGpcloudserviceCom": [
        "Confirm the requester needs uem.gpcloudservice.com access.",
        "Check whether device management role membership is required.",
        "Assign to the endpoint management owner.",
    ],
}

DEFAULT_ACTIONS = [
    "Review the original Slack request and identify the owning support queue.",
    "Confirm the requester, business justification, and approval requirement.",
    "Add any missing details before moving the Jira ticket forward.",
]


def to_iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def log_json(data):
    print(json.dumps(data, default=str))


def require_env(name, value):
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")

    return value


def text_or_empty(value):
    if value is None:
        return ""

    return str(value).strip()


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

    secret["site_url"] = secret["site_url"].rstrip("/")
    _jira_secret_cache = secret
    return secret


def jira_auth_header(secret):
    raw_token = f"{secret['email']}:{secret['api_token']}".encode("utf-8")
    encoded = base64.b64encode(raw_token).decode("utf-8")
    return f"Basic {encoded}"


def adf_text(text):
    return {
        "type": "text",
        "text": text_or_empty(text) or "-"
    }


def adf_paragraph(text):
    return {
        "type": "paragraph",
        "content": [
            adf_text(text)
        ]
    }


def adf_bullet_item(text):
    return {
        "type": "listItem",
        "content": [
            adf_paragraph(text)
        ]
    }


def build_enrichment(event):
    lex = event.get("lex", {}) or {}
    intent_name = text_or_empty(lex.get("intent"))
    request_text = text_or_empty(event.get("text")) or text_or_empty(event.get("raw_text"))
    request_type = INTENT_TITLES.get(intent_name, intent_name or "Support request")
    suggested_actions = INTENT_ACTIONS.get(intent_name, DEFAULT_ACTIONS)
    summary = (
        f"{ROVO_COMMENT_PREFIX}: identified this as {request_type}. "
        "The notes below are generated for support triage and should be reviewed before action."
    )

    return {
        "request_type": request_type,
        "request_text": request_text,
        "summary": summary,
        "suggested_actions": suggested_actions,
    }


def build_comment_payload(event, enrichment):
    content = [
        adf_paragraph(ROVO_COMMENT_PREFIX),
        adf_paragraph(f"Mode: {ROVO_MODE}"),
        adf_paragraph(f"Project IVY request ID: {text_or_empty(event.get('jira_request_id')) or '-'}"),
        adf_paragraph(f"Request type: {enrichment['request_type']}"),
        adf_paragraph(f"Original Slack request: {enrichment['request_text'] or '-'}"),
        adf_paragraph("Suggested next actions:"),
        {
            "type": "bulletList",
            "content": [
                adf_bullet_item(action)
                for action in enrichment["suggested_actions"]
            ]
        },
    ]

    return {
        "body": {
            "version": 1,
            "type": "doc",
            "content": content
        }
    }


def add_jira_comment(ticket_key, comment_payload):
    secret = get_jira_secret()
    quoted_ticket_key = urllib.parse.quote(ticket_key, safe="")
    url = f"{secret['site_url']}/rest/api/3/issue/{quoted_ticket_key}/comment"

    request = urllib.request.Request(
        url,
        data=json.dumps(comment_payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": jira_auth_header(secret),
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=ROVO_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def update_rovo_session(event, status, enriched_at, **fields):
    session_id = text_or_empty(event.get("session_id"))
    if not session_id:
        log_json({
            "level": "WARN",
            "message": "rovo_session_update_skipped",
            "reason": "missing_session_id",
            "jira_request_id": event.get("jira_request_id"),
            "ticket_key": event.get("ticket_key")
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
        "rovo_comment_id": fields.get("comment_id"),
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
        ExpressionAttributeValues=expression_values
    )


def http_error_code(status):
    if status in {401, 403}:
        return "rovo_jira_auth_or_permission_error"

    if status == 404:
        return "rovo_jira_ticket_not_found"

    if status == 429:
        return "rovo_jira_rate_limited"

    if status >= 500:
        return "rovo_jira_service_error"

    return "rovo_jira_http_error"


def http_error_message(status):
    if status in {401, 403}:
        return "Jira rejected the enrichment comment request. Check comment permissions."

    if status == 404:
        return "Jira ticket was not found for enrichment."

    if status == 429:
        return "Jira rate limited the enrichment comment request."

    if status >= 500:
        return "Jira returned a temporary service error while adding enrichment."

    return f"Jira returned HTTP {status} while adding enrichment."


def failure_response(event, error, error_code):
    enriched_at = to_iso(datetime.now(timezone.utc))

    try:
        update_rovo_session(
            event,
            "failed",
            enriched_at,
            error=str(error),
            error_code=error_code
        )

    except Exception as update_error:
        log_json({
            "level": "ERROR",
            "message": "rovo_failure_session_update_failed",
            "jira_request_id": event.get("jira_request_id"),
            "ticket_key": event.get("ticket_key"),
            "error": str(update_error),
            "original_error": str(error),
            "original_error_code": error_code
        })

    return {
        "ok": False,
        "rovo_status": "failed",
        "error": str(error),
        "error_code": error_code
    }


def lambda_handler(event, context):
    log_json({
        "level": "INFO",
        "message": "rovo_enrichment_received",
        "jira_request_id": event.get("jira_request_id"),
        "ticket_key": event.get("ticket_key"),
        "session_id": event.get("session_id"),
        "mode": ROVO_MODE
    })

    try:
        ticket_key = text_or_empty(event.get("ticket_key"))
        if not ticket_key:
            raise ValueError("Missing required event field: ticket_key")

        enrichment = build_enrichment(event)
        comment_payload = build_comment_payload(event, enrichment)
        comment_response = add_jira_comment(ticket_key, comment_payload)
        enriched_at = to_iso(datetime.now(timezone.utc))
        comment_id = comment_response.get("id")

        update_rovo_session(
            event,
            "completed",
            enriched_at,
            summary=enrichment["summary"],
            comment_id=comment_id
        )

        log_json({
            "level": "INFO",
            "message": "rovo_enrichment_completed",
            "jira_request_id": event.get("jira_request_id"),
            "ticket_key": ticket_key,
            "session_id": event.get("session_id"),
            "comment_id": comment_id
        })

        return {
            "ok": True,
            "rovo_status": "completed",
            "enrichment_summary": enrichment["summary"],
            "suggested_actions": enrichment["suggested_actions"],
            "comment_id": comment_id
        }

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        error = http_error_message(e.code)

        log_json({
            "level": "ERROR",
            "message": "rovo_enrichment_http_error",
            "jira_request_id": event.get("jira_request_id"),
            "ticket_key": event.get("ticket_key"),
            "session_id": event.get("session_id"),
            "status": e.code,
            "body": body
        })

        return failure_response(event, error, http_error_code(e.code))

    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        log_json({
            "level": "ERROR",
            "message": "rovo_enrichment_network_error",
            "jira_request_id": event.get("jira_request_id"),
            "ticket_key": event.get("ticket_key"),
            "session_id": event.get("session_id"),
            "error": str(e)
        })

        return failure_response(event, "Jira did not respond to the enrichment request.", "rovo_jira_network_error")

    except Exception as e:
        log_json({
            "level": "ERROR",
            "message": "rovo_enrichment_failed",
            "jira_request_id": event.get("jira_request_id"),
            "ticket_key": event.get("ticket_key"),
            "session_id": event.get("session_id"),
            "error": str(e)
        })

        return failure_response(event, e, "rovo_enrichment_failed")
