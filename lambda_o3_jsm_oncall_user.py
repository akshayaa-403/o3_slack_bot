import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import boto3


AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
JSM_ONCALL_CACHE_TABLE = (
    os.environ.get("JSM_ONCALL_CACHE_TABLE")
    or os.environ.get("ONCALL_USER_TABLE")
    or "O3_JSMOps_Oncall"
)
LIVE_AGENT_SCHEDULE_NAME = os.environ.get("OPSGENIE_SCHEDULE_NAME", "live agent")
ONCALL_CACHE_TTL_SECONDS = int(os.environ.get("ONCALL_CACHE_TTL_SECONDS", os.environ.get("ONCALL_TTL_SECONDS", "900")))
JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL") or os.environ.get("JIRA_SITE_URL")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN")
JIRA_SECRET_ID = os.environ.get("JIRA_SECRET_ID")
JIRA_TIMEOUT_SECONDS = int(os.environ.get("JIRA_TIMEOUT_SECONDS", "15"))
JIRA_MAX_ATTEMPTS = max(1, int(os.environ.get("JIRA_MAX_ATTEMPTS", "3")))
JIRA_RETRY_DELAY_SECONDS = float(os.environ.get("JIRA_RETRY_DELAY_SECONDS", "1"))
UNASSIGN_WHEN_AGENT_FULL = os.environ.get("UNASSIGN_WHEN_AGENT_FULL", "false").lower() == "true"
OPSGENIE_API_KEY = os.environ.get("OPSGENIE_API_KEY")
OPSGENIE_SCHEDULE_ID = os.environ.get("OPSGENIE_SCHEDULE_ID")
OPSGENIE_API_BASE_URL = os.environ.get("OPSGENIE_API_BASE_URL", "https://api.opsgenie.com").rstrip("/")
OPSGENIE_TIMEOUT_SECONDS = int(os.environ.get("OPSGENIE_TIMEOUT_SECONDS", "15"))
OPSGENIE_JIRA_ACCOUNT_MAP = os.environ.get("OPSGENIE_JIRA_ACCOUNT_MAP", "{}")

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
secretsmanager = boto3.client("secretsmanager", region_name=AWS_REGION)
cache_table = dynamodb.Table(JSM_ONCALL_CACHE_TABLE)

_jira_secret_cache = None


def log_json(data):
    print(json.dumps(data, default=str))


def text_or_empty(value):
    if value is None:
        return ""

    return str(value).strip()


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ttl_epoch():
    return int(time.time()) + ONCALL_CACHE_TTL_SECONDS


def schedule_pk():
    return f"SCHEDULE#{LIVE_AGENT_SCHEDULE_NAME}"


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


def get_jira_secret():
    global _jira_secret_cache

    if _jira_secret_cache:
        return _jira_secret_cache

    if JIRA_SECRET_ID:
        response = secretsmanager.get_secret_value(SecretId=JIRA_SECRET_ID)
        secret_string = response.get("SecretString")
        if not secret_string:
            raise ValueError("Jira secret must be stored as a JSON SecretString")
        secret = json.loads(secret_string)
    else:
        secret = {
            "site_url": JIRA_BASE_URL,
            "email": JIRA_EMAIL,
            "api_token": JIRA_API_TOKEN,
        }

    normalized = {
        "site_url": first_text(secret.get("site_url"), secret.get("base_url"), secret.get("JIRA_BASE_URL")).rstrip("/"),
        "email": first_text(secret.get("email"), secret.get("JIRA_EMAIL")),
        "api_token": first_text(secret.get("api_token"), secret.get("token"), secret.get("JIRA_API_TOKEN")),
    }

    for key, value in normalized.items():
        if not value:
            raise ValueError(f"Jira credential missing required key: {key}")

    _jira_secret_cache = normalized
    return normalized


def jira_auth_header(secret):
    token = base64.b64encode(
        f"{secret['email']}:{secret['api_token']}".encode("utf-8")
    ).decode("utf-8")
    return f"Basic {token}"


def http_get_json(url, headers, timeout):
    request = urllib.request.Request(
        url,
        headers=headers,
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def jira_get_json(path, query=None):
    secret = get_jira_secret()
    url = f"{secret['site_url'].rstrip('/')}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"

    return http_get_json(
        url,
        {
            "Accept": "application/json",
            "Authorization": jira_auth_header(secret),
        },
        JIRA_TIMEOUT_SECONDS,
    )


JIRA_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def jira_request(method, path, body=None, query=None):
    secret = get_jira_secret()
    url = f"{secret['site_url'].rstrip('/')}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"

    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Authorization": jira_auth_header(secret),
    }
    if data is not None:
        headers["Content-Type"] = "application/json"

    for attempt in range(1, JIRA_MAX_ATTEMPTS + 1):
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=JIRA_TIMEOUT_SECONDS) as response:
                response_body = response.read().decode("utf-8").strip()
                return {
                    "ok": 200 <= response.status < 300,
                    "status_code": response.status,
                    "body": json.loads(response_body) if response_body else None,
                    "attempts": attempt,
                }
        except urllib.error.HTTPError as error:
            retryable = error.code in JIRA_RETRYABLE_STATUS_CODES
            if not retryable or attempt >= JIRA_MAX_ATTEMPTS:
                return {
                    "ok": False,
                    "status_code": error.code,
                    "reason": "jira_http_error",
                    "retryable": retryable,
                    "attempts": attempt,
                }
            retry_after = error.headers.get("Retry-After") if error.headers else None
            try:
                delay = float(retry_after) if retry_after is not None else JIRA_RETRY_DELAY_SECONDS
            except ValueError:
                delay = JIRA_RETRY_DELAY_SECONDS
            time.sleep(max(0, delay))
        except (urllib.error.URLError, TimeoutError) as error:
            return {
                "ok": False,
                "status_code": None,
                "reason": "jira_network_error",
                "retryable": False,
                "attempts": attempt,
                "error": str(error),
            }


def assign_jira_issue(ticket_key, account_id):
    ticket_key = text_or_empty(ticket_key)
    account_id = text_or_empty(account_id)
    if not ticket_key or not account_id:
        return {
            "ok": False,
            "status_code": None,
            "reason": "missing_ticket_or_account_id",
        }

    result = jira_request(
        "PUT",
        f"/rest/api/3/issue/{urllib.parse.quote(ticket_key, safe='')}/assignee",
        body={"accountId": account_id},
    )
    log_json({
        "level": "INFO" if result.get("ok") else "ERROR",
        "message": "jira_issue_assignment_completed",
        "ticket_key": ticket_key,
        "status_code": result.get("status_code"),
        "result": "success" if result.get("ok") else result.get("reason"),
    })
    return result


def unassign_jira_issue(ticket_key):
    ticket_key = text_or_empty(ticket_key)
    if not UNASSIGN_WHEN_AGENT_FULL:
        return {
            "ok": False,
            "status_code": None,
            "reason": "unassign_when_agent_full_disabled",
            "skipped": True,
        }
    if not ticket_key:
        return {
            "ok": False,
            "status_code": None,
            "reason": "missing_ticket_key",
        }

    result = jira_request(
        "PUT",
        f"/rest/api/3/issue/{urllib.parse.quote(ticket_key, safe='')}/assignee",
        body={"accountId": None},
    )
    log_json({
        "level": "INFO" if result.get("ok") else "ERROR",
        "message": "jira_issue_unassignment_completed",
        "ticket_key": ticket_key,
        "status_code": result.get("status_code"),
        "result": "success" if result.get("ok") else result.get("reason"),
    })
    return result


def normalize_user(user, source, ticket_key=""):
    user = user if isinstance(user, dict) else {}
    account_id = first_text(
        user.get("account_id"),
        user.get("accountId"),
        user.get("assignee_account_id"),
        user.get("assigneeAccountId"),
    )
    display_name = first_text(
        user.get("display_name"),
        user.get("displayName"),
        user.get("assignee_display_name"),
        user.get("assigneeDisplayName"),
        user.get("name"),
        user.get("username"),
    )
    email = first_text(
        user.get("email"),
        user.get("emailAddress"),
        user.get("assignee_email"),
        user.get("assigneeEmail"),
    )

    if not (account_id or display_name or email):
        return {}

    return {
        "ok": True,
        "source": source,
        "account_id": account_id,
        "display_name": display_name,
        "email": email,
        "ticket_key": text_or_empty(ticket_key),
    }


def user_from_webhook_payload(webhook_payload, ticket_key=""):
    payload = webhook_payload if isinstance(webhook_payload, dict) else {}
    assignee = payload.get("assignee") if isinstance(payload.get("assignee"), dict) else {}
    issue_assignee = nested_get(payload, "issue", "fields", "assignee")
    issue_assignee = issue_assignee if isinstance(issue_assignee, dict) else {}

    account_id = first_text(
        payload.get("assignee_account_id"),
        payload.get("assigneeAccountId"),
        payload.get("agent_account_id"),
        payload.get("agentAccountId"),
        assignee.get("accountId"),
        issue_assignee.get("accountId"),
    )
    if not account_id:
        return {}

    return normalize_user({
        "account_id": account_id,
        "display_name": first_text(
            payload.get("assignee_display_name"),
            payload.get("assigneeDisplayName"),
            payload.get("assignee_name"),
            payload.get("assigneeName"),
            assignee.get("displayName"),
            issue_assignee.get("displayName"),
        ),
        "email": first_text(
            payload.get("assignee_email"),
            payload.get("assigneeEmail"),
            assignee.get("emailAddress"),
            issue_assignee.get("emailAddress"),
        ),
    }, "webhook_assignee", ticket_key)


def get_jira_issue_assignee(ticket_key):
    ticket_key = text_or_empty(ticket_key)
    if not ticket_key:
        return {}

    result = jira_request(
        "GET",
        f"/rest/api/3/issue/{urllib.parse.quote(ticket_key, safe='')}",
        query={"fields": "assignee"},
    )
    if not result.get("ok"):
        log_json({
            "level": "ERROR",
            "message": "jira_issue_assignee_lookup_completed",
            "ticket_key": ticket_key,
            "status_code": result.get("status_code"),
            "result": result.get("reason"),
        })
        return {}

    assignee = nested_get(result.get("body"), "fields", "assignee")
    if not isinstance(assignee, dict):
        log_json({
            "level": "INFO",
            "message": "jira_issue_assignee_lookup_completed",
            "ticket_key": ticket_key,
            "status_code": result.get("status_code"),
            "result": "unassigned",
        })
        return {}

    user = normalize_user(assignee, "jira_issue_assignee", ticket_key)
    log_json({
        "level": "INFO",
        "message": "jira_issue_assignee_lookup_completed",
        "ticket_key": ticket_key,
        "status_code": result.get("status_code"),
        "result": "success",
    })
    return user


def jira_user_for_opsgenie_user(opsgenie_user):
    local_map = parse_json_map(OPSGENIE_JIRA_ACCOUNT_MAP)
    email = first_text(
        opsgenie_user.get("email"),
        opsgenie_user.get("username"),
        opsgenie_user.get("name"),
    )
    mapped = local_map.get(email) if email else None
    if isinstance(mapped, dict):
        return normalize_user(mapped, "opsgenie_api")
    if isinstance(mapped, str) and mapped.strip():
        return normalize_user({
            "account_id": mapped,
            "email": email,
            "display_name": opsgenie_user.get("name") or email,
        }, "opsgenie_api")

    if not email:
        return {}

    try:
        users = jira_get_json("/rest/api/3/user/search", {"query": email})
    except Exception as error:
        log_json({
            "level": "WARN",
            "message": "jira_user_search_for_opsgenie_failed",
            "error": str(error),
        })
        return normalize_user({
            "email": email,
            "display_name": opsgenie_user.get("name") or email,
        }, "opsgenie_api")

    if isinstance(users, list) and users:
        return normalize_user(users[0], "opsgenie_api")

    return normalize_user({
        "email": email,
        "display_name": opsgenie_user.get("name") or email,
    }, "opsgenie_api")


def opsgenie_participants(data):
    if not isinstance(data, dict):
        return []

    root = data.get("data") if isinstance(data.get("data"), dict) else data
    participants = root.get("onCallParticipants")
    if not isinstance(participants, list):
        participants = root.get("participants")
    if not isinstance(participants, list):
        participants = root.get("recipients")
    return participants if isinstance(participants, list) else []


def get_opsgenie_oncall_user(ticket_key=""):
    if not (OPSGENIE_API_KEY and OPSGENIE_SCHEDULE_ID):
        return {}

    schedule_identifier = urllib.parse.quote(OPSGENIE_SCHEDULE_ID, safe="")
    url = (
        f"{OPSGENIE_API_BASE_URL}/v2/schedules/{schedule_identifier}/on-calls"
        f"?{urllib.parse.urlencode({'scheduleIdentifierType': 'id', 'flat': 'true'})}"
    )
    data = http_get_json(
        url,
        {
            "Accept": "application/json",
            "Authorization": f"GenieKey {OPSGENIE_API_KEY}",
        },
        OPSGENIE_TIMEOUT_SECONDS,
    )

    participants = opsgenie_participants(data)
    if not participants:
        return {}

    user = jira_user_for_opsgenie_user(participants[0])
    if user:
        user["ticket_key"] = text_or_empty(ticket_key)
    return user


def cache_oncall_user(user):
    user = user if isinstance(user, dict) else {}
    if not user.get("ok"):
        return {}

    now = utc_now_iso()
    item = {
        "PK": schedule_pk(),
        "SK": "CURRENT",
        "account_id": text_or_empty(user.get("account_id")),
        "display_name": text_or_empty(user.get("display_name")),
        "email": text_or_empty(user.get("email")),
        "source": text_or_empty(user.get("source")),
        "ticket_key": text_or_empty(user.get("ticket_key")),
        "fetched_at": now,
        "updated_at": now,
        "ttl": ttl_epoch(),
    }
    cache_table.put_item(Item=item)
    log_json({
        "level": "INFO",
        "message": "oncall_user_cached",
        "source": item["source"],
        "ticket_key": item["ticket_key"],
        "has_account_id": bool(item["account_id"]),
    })
    return item


def get_cached_oncall_user():
    response = cache_table.get_item(Key={"PK": schedule_pk(), "SK": "CURRENT"})
    item = response.get("Item") or {}
    if not item:
        return {}

    user = {
        "ok": True,
        "source": "manual_fallback",
        "account_id": text_or_empty(item.get("account_id")),
        "display_name": text_or_empty(item.get("display_name")),
        "email": text_or_empty(item.get("email")),
        "ticket_key": text_or_empty(item.get("ticket_key")),
    }
    log_json({
        "level": "INFO",
        "message": "oncall_user_cache_hit",
        "source": item.get("source"),
        "ticket_key": item.get("ticket_key"),
        "has_account_id": bool(user["account_id"]),
    })
    return user


def get_current_oncall_user(ticket_key=None, webhook_payload=None) -> dict:
    payload = webhook_payload if isinstance(webhook_payload, dict) else {}
    effective_ticket_key = first_text(ticket_key, payload.get("ticket_key"), payload.get("issue_key"), payload.get("key"))

    log_json({
        "level": "INFO",
        "message": "oncall_user_resolution_started",
        "ticket_key": effective_ticket_key,
        "has_webhook_payload": bool(payload),
        "has_opsgenie_config": bool(OPSGENIE_API_KEY and OPSGENIE_SCHEDULE_ID),
    })

    webhook_user = user_from_webhook_payload(payload, effective_ticket_key)
    if webhook_user:
        cache_oncall_user(webhook_user)
        return webhook_user

    if effective_ticket_key:
        try:
            jira_user = get_jira_issue_assignee(effective_ticket_key)
            if jira_user:
                cache_oncall_user(jira_user)
                return jira_user
        except urllib.error.HTTPError as error:
            log_json({
                "level": "WARN",
                "message": "jira_issue_assignee_lookup_http_failed",
                "ticket_key": effective_ticket_key,
                "status": error.code,
            })
        except Exception as error:
            log_json({
                "level": "WARN",
                "message": "jira_issue_assignee_lookup_failed",
                "ticket_key": effective_ticket_key,
                "error": str(error),
            })

    if OPSGENIE_API_KEY and OPSGENIE_SCHEDULE_ID:
        try:
            opsgenie_user = get_opsgenie_oncall_user(effective_ticket_key)
            if opsgenie_user:
                cache_oncall_user(opsgenie_user)
                return opsgenie_user
        except urllib.error.HTTPError as error:
            log_json({
                "level": "WARN",
                "message": "opsgenie_oncall_lookup_http_failed",
                "status": error.code,
            })
        except Exception as error:
            log_json({
                "level": "WARN",
                "message": "opsgenie_oncall_lookup_failed",
                "error": str(error),
            })

    cached_user = get_cached_oncall_user()
    if cached_user:
        return cached_user

    log_json({
        "level": "WARN",
        "message": "oncall_user_not_available",
        "ticket_key": effective_ticket_key,
    })
    return {
        "ok": False,
        "reason": "no_oncall_user_available",
        "ticket_key": effective_ticket_key,
    }


def lambda_handler(event, context):
    event = event if isinstance(event, dict) else {}
    result = get_current_oncall_user(
        ticket_key=event.get("ticket_key"),
        webhook_payload=event.get("webhook_payload") or event,
    )

    if result.get("ok"):
        return {
            **result,
            "assignee": {
                "account_id": result.get("account_id", ""),
                "display_name": result.get("display_name", ""),
                "email": result.get("email", ""),
            },
            "current_user": {
                "account_id": result.get("account_id", ""),
                "display_name": result.get("display_name", ""),
                "email": result.get("email", ""),
                "source": result.get("source", ""),
                "ticket_key": result.get("ticket_key", ""),
            },
        }

    return result
