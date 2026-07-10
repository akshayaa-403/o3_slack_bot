import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError


AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
CAPACITY_TABLE = os.environ.get("CAPACITY_TABLE", "live_agent_capacity")
SESSION_TABLE = os.environ.get("SESSION_TABLE") or os.environ.get("DYNAMODB_TABLE", "o3_slack_sessions")
JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "https://innovyq.atlassian.net").rstrip("/")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "")
LIVE_AGENT_TIMEZONE = os.environ.get("LIVE_AGENT_TIMEZONE", "Asia/Kolkata")
DEFAULT_AGENT_CAPACITY = int(
    os.environ.get(
        "MAX_ACTIVE_CHATS_PER_AGENT",
        os.environ.get("LIVE_AGENT_MAX_ACTIVE_CHATS", os.environ.get("DEFAULT_AGENT_CAPACITY", "5")),
    )
)
CAPACITY_RECONCILE_ENABLED = os.environ.get("CAPACITY_RECONCILE_ENABLED", "true").lower() == "true"
CAPACITY_RECONCILE_MAX_ITEMS = max(0, int(os.environ.get("CAPACITY_RECONCILE_MAX_ITEMS", "25")))
JSM_SCHEDULE_SYNC_ENABLED = os.environ.get("JSM_SCHEDULE_SYNC_ENABLED", "true").lower() == "true"
AUTO_CREATE_JSM_ONCALL_AGENTS = os.environ.get("AUTO_CREATE_JSM_ONCALL_AGENTS", "true").lower() == "true"
AUTO_DISABLE_JSM_ONCALL_AGENTS = os.environ.get("AUTO_DISABLE_JSM_ONCALL_AGENTS", "true").lower() == "true"
AUTO_ONCALL_AGENT_PRIORITY = int(os.environ.get("AUTO_ONCALL_AGENT_PRIORITY", "500"))
ENABLE_JSM_ONCALL_SOURCE = os.environ.get("ENABLE_JSM_ONCALL_SOURCE", "false").lower() == "true"
JSM_OPS_BASE_URL = os.environ.get("JSM_OPS_BASE_URL", "https://api.atlassian.com/jsm/ops").rstrip("/")
JSM_OPS_CLOUD_ID = os.environ.get("JSM_OPS_CLOUD_ID", "")
JSM_ONCALL_SCHEDULE_IDS = [
    value.strip()
    for value in os.environ.get("JSM_ONCALL_SCHEDULE_IDS", os.environ.get("JSM_ONCALL_SCHEDULE_ID", "")).split(",")
    if value.strip()
]
JSM_OPS_API_KEY = os.environ.get("JSM_OPS_API_KEY", "")
JSM_OPS_AUTH_HEADER = os.environ.get("JSM_OPS_AUTH_HEADER", "")
ATLASSIAN_EMAIL = os.environ.get("ATLASSIAN_EMAIL") or JIRA_EMAIL
ATLASSIAN_API_TOKEN = os.environ.get("ATLASSIAN_API_TOKEN") or JIRA_API_TOKEN
JSM_ONCALL_FALLBACK_TO_SHIFT = os.environ.get("JSM_ONCALL_FALLBACK_TO_SHIFT", "true").lower() == "true"
TERMINAL_STATUSES = {
    value.strip().lower()
    for value in os.environ.get(
        "TERMINAL_STATUSES",
        "Done,Resolved,Closed,Cancelled,Canceled",
    ).split(",")
    if value.strip()
}

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
capacity_table = dynamodb.Table(CAPACITY_TABLE)
session_table = dynamodb.Table(SESSION_TABLE)


def log_json(data):
    print(json.dumps(data, default=str))


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_current_time_in_timezone(timezone_name=LIVE_AGENT_TIMEZONE):
    return datetime.now(ZoneInfo(timezone_name))


def parse_hhmm(value):
    return datetime.strptime(str(value or "").strip(), "%H:%M").time()


def is_agent_on_shift(agent, now):
    timezone_name = agent.get("timezone") or LIVE_AGENT_TIMEZONE
    local_now = now.astimezone(ZoneInfo(timezone_name))
    current = local_now.time().replace(tzinfo=None)
    start = parse_hhmm(agent.get("shift_start"))
    end = parse_hhmm(agent.get("shift_end"))

    if start == end:
        return True
    if start < end:
        return start <= current < end
    return current >= start or current < end


def get_enabled_agents(include_disabled=False):
    items = []
    request = {}
    while True:
        response = capacity_table.scan(**request)
        items.extend(response.get("Items") or [])
        if not response.get("LastEvaluatedKey"):
            break
        request["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    if include_disabled:
        return items
    return [item for item in items if item.get("enabled") is True]


def get_current_shift_agents(now=None):
    now = now or get_current_time_in_timezone()
    return [
        agent for agent in get_enabled_agents()
        if agent.get("jira_account_id") and is_agent_on_shift(agent, now)
    ]


def jsm_ops_auth_header():
    if JSM_OPS_AUTH_HEADER:
        return JSM_OPS_AUTH_HEADER
    if ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN:
        token = base64.b64encode(f"{ATLASSIAN_EMAIL}:{ATLASSIAN_API_TOKEN}".encode("utf-8")).decode("ascii")
        return f"Basic {token}"
    if JSM_OPS_API_KEY:
        return f"GenieKey {JSM_OPS_API_KEY}"
    return ""


def jsm_ops_request(method, path, params=None):
    if not (JSM_OPS_CLOUD_ID and jsm_ops_auth_header()):
        return {
            "ok": False,
            "error": "Missing JSM on-call configuration",
            "error_code": "missing_jsm_oncall_configuration",
        }

    url = f"{JSM_OPS_BASE_URL}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    request = urllib.request.Request(
        url,
        headers={
            "Authorization": jsm_ops_auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8").strip()
            return {
                "ok": 200 <= response.status < 300,
                "status": response.status,
                "response": json.loads(body) if body else {},
            }
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")[:2000]
        return {
            "ok": False,
            "status": error.code,
            "error": body or str(error),
            "error_code": "jsm_oncall_http_error",
        }
    except Exception as error:
        return {
            "ok": False,
            "error": str(error),
            "error_code": "jsm_oncall_request_failed",
        }


def extract_oncall_user_ids(value):
    ids = set()

    if isinstance(value, str) and value.strip():
        ids.add(value.strip())
        return ids

    if isinstance(value, list):
        for item in value:
            ids.update(extract_oncall_user_ids(item))
        return ids

    if not isinstance(value, dict):
        return ids

    for key in (
        "accountId",
        "account_id",
        "userId",
        "user_id",
        "id",
        "identifier",
        "userIdentifier",
    ):
        item_value = value.get(key)
        if isinstance(item_value, str) and item_value.strip():
            ids.add(item_value.strip())

    for key in ("user", "responder", "participant", "account"):
        ids.update(extract_oncall_user_ids(value.get(key)))

    for key in (
        "users",
        "values",
        "participants",
        "responders",
        "onCalls",
        "onCallUsers",
        "onCallParticipants",
    ):
        ids.update(extract_oncall_user_ids(value.get(key)))

    return ids


def get_jsm_oncall_user_ids(now=None):
    if not ENABLE_JSM_ONCALL_SOURCE:
        return {
            "ok": False,
            "enabled": False,
            "error_code": "jsm_oncall_source_disabled",
        }

    if not JSM_ONCALL_SCHEDULE_IDS:
        return {
            "ok": False,
            "enabled": True,
            "error": "Missing JSM_ONCALL_SCHEDULE_IDS",
            "error_code": "missing_jsm_oncall_schedule_ids",
        }

    user_ids = set()
    schedule_results = []
    params = {"flat": "true"}
    if now:
        params["date"] = now.astimezone(timezone.utc).replace(microsecond=0).isoformat()

    for schedule_id in JSM_ONCALL_SCHEDULE_IDS:
        safe_schedule_id = urllib.parse.quote(schedule_id, safe="")
        result = jsm_ops_request(
            "GET",
            f"/api/{urllib.parse.quote(JSM_OPS_CLOUD_ID, safe='')}/v1/schedules/{safe_schedule_id}/on-calls",
            params,
        )
        extracted = extract_oncall_user_ids(result.get("response")) if result.get("ok") else set()
        user_ids.update(extracted)
        schedule_results.append({
            "schedule_id": schedule_id,
            "ok": result.get("ok"),
            "status": result.get("status"),
            "error_code": result.get("error_code"),
            "oncall_user_count": len(extracted),
        })

    ok = all(item.get("ok") for item in schedule_results)
    return {
        "ok": ok,
        "enabled": True,
        "user_ids": user_ids,
        "schedule_results": schedule_results,
        "error_code": None if ok else "jsm_oncall_lookup_failed",
    }


def get_jsm_schedule_definitions():
    if not JSM_ONCALL_SCHEDULE_IDS:
        return {
            "ok": False,
            "schedules": [],
            "error_code": "missing_jsm_oncall_schedule_ids",
        }

    schedules = []
    results = []
    for schedule_id in JSM_ONCALL_SCHEDULE_IDS:
        safe_schedule_id = urllib.parse.quote(schedule_id, safe="")
        result = jsm_ops_request(
            "GET",
            f"/api/{urllib.parse.quote(JSM_OPS_CLOUD_ID, safe='')}/v1/schedules/{safe_schedule_id}",
        )
        if result.get("ok") and isinstance(result.get("response"), dict):
            schedule = result["response"]
            schedule["id"] = schedule.get("id") or schedule_id
            schedules.append(schedule)
        results.append({
            "schedule_id": schedule_id,
            "ok": result.get("ok"),
            "status": result.get("status"),
            "error_code": result.get("error_code"),
        })

    ok = all(item.get("ok") for item in results)
    return {
        "ok": ok,
        "schedules": schedules,
        "schedule_results": results,
        "error_code": None if ok else "jsm_schedule_lookup_failed",
    }


def hhmm_from_restriction(restriction, prefix):
    hour = int(restriction.get(f"{prefix}Hour", 0))
    minute = int(restriction.get(f"{prefix}Min", 0))
    return f"{hour:02d}:{minute:02d}"


def rotation_shift_window(rotation):
    restrictions = (
        (rotation.get("timeRestriction") or {}).get("restrictions")
        if isinstance(rotation, dict)
        else None
    )
    if not restrictions:
        return {"shift_start": "00:00", "shift_end": "00:00"}

    restriction = restrictions[0]
    return {
        "shift_start": hhmm_from_restriction(restriction, "start"),
        "shift_end": hhmm_from_restriction(restriction, "end"),
    }


def rotation_participant_ids(rotation):
    participant_ids = set()
    for participant in rotation.get("participants") or []:
        participant_ids.update(extract_oncall_user_ids(participant))
    return participant_ids


def schedule_metadata_by_oncall_user(schedule_result):
    metadata = {}
    if not schedule_result.get("ok"):
        return metadata

    for schedule in schedule_result.get("schedules") or []:
        timezone_name = (
            schedule.get("timezone")
            or schedule.get("timeZone")
            or LIVE_AGENT_TIMEZONE
        )
        for rotation in schedule.get("rotations") or []:
            shift = rotation_shift_window(rotation)
            for account_id in rotation_participant_ids(rotation):
                metadata[account_id] = {
                    **shift,
                    "shift_name": rotation.get("name") or schedule.get("name") or "JSM_ONCALL",
                    "timezone": timezone_name,
                    "source": "jsm_oncall_sync",
                }
    return metadata


def agent_matches_oncall_user(agent, oncall_user_ids):
    identifiers = {
        str(agent.get("jira_account_id") or "").strip(),
        str(agent.get("atlassian_account_id") or "").strip(),
        str(agent.get("account_id") or "").strip(),
        str(agent.get("agent_id") or "").strip(),
        str(agent.get("email") or "").strip(),
    }
    identifiers.discard("")
    return bool(identifiers & set(oncall_user_ids or []))


def sync_oncall_schedule_fields(oncall_user_ids, existing_agents):
    if not JSM_SCHEDULE_SYNC_ENABLED:
        return {"ok": True, "enabled": False, "updated_agents": [], "disabled_agents": []}

    oncall_user_ids = {
        str(value).strip()
        for value in oncall_user_ids or []
        if str(value).strip()
    }
    if not oncall_user_ids:
        return {
            "ok": True,
            "enabled": True,
            "updated_agents": [],
            "disabled_agents": [],
            "reason": "no_current_oncall_users",
        }

    schedule_result = get_jsm_schedule_definitions()
    metadata_by_account = schedule_metadata_by_oncall_user(schedule_result)
    updated = []
    disabled = []
    errors = []

    for agent in existing_agents or []:
        identifiers = agent_oncall_identifiers(agent)
        matched_ids = identifiers & oncall_user_ids
        if not matched_ids:
            continue

        account_id = sorted(matched_ids)[0]
        metadata = metadata_by_account.get(account_id)
        if not metadata:
            continue

        assignable = jira_user_assignable(account_id)
        enabled = bool(assignable.get("assignable"))
        now_iso = utc_now_iso()
        values = {
            ":shift_start": metadata["shift_start"],
            ":shift_end": metadata["shift_end"],
            ":shift_name": metadata["shift_name"],
            ":timezone": metadata["timezone"],
            ":enabled": enabled,
            ":updated_at": now_iso,
            ":source": "jsm_oncall_sync",
        }
        names = {
            "#timezone": "timezone",
            "#source": "source",
            "#reason": "auto_disabled_reason",
        }
        expression = (
            "SET shift_start = :shift_start, shift_end = :shift_end, "
            "shift_name = :shift_name, #timezone = :timezone, enabled = :enabled, "
            "updated_at = :updated_at, #source = :source"
        )
        if enabled:
            expression += " REMOVE #reason"
        else:
            expression += ", #reason = :reason"
            values[":reason"] = assignable.get("reason") or "jira_user_not_assignable"

        try:
            capacity_table.update_item(
                Key={"agent_id": agent["agent_id"]},
                UpdateExpression=expression,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            item = {
                "agent_id": agent.get("agent_id"),
                "account_id": account_id,
                "shift_start": metadata["shift_start"],
                "shift_end": metadata["shift_end"],
                "shift_name": metadata["shift_name"],
                "timezone": metadata["timezone"],
            }
            if enabled:
                updated.append(item)
            else:
                disabled.append({**item, "reason": values[":reason"]})
        except Exception as error:
            errors.append({"agent_id": agent.get("agent_id"), "error": str(error)})

    return {
        "ok": bool(schedule_result.get("ok")) and not errors,
        "enabled": True,
        "updated_agents": updated,
        "disabled_agents": disabled,
        "errors": errors,
        "schedule_results": schedule_result.get("schedule_results") or [],
        "error_code": None if schedule_result.get("ok") else schedule_result.get("error_code"),
    }


def sanitize_oncall_result(result):
    if not result:
        return None

    user_ids = result.get("user_ids") or set()
    return {
        "enabled": result.get("enabled"),
        "ok": result.get("ok"),
        "error_code": result.get("error_code"),
        "oncall_user_count": len(user_ids),
        "schedule_results": result.get("schedule_results") or [],
    }


def jira_user(account_id):
    if not account_id:
        return {}

    result = jira_request(
        "GET",
        f"/rest/api/3/user?accountId={urllib.parse.quote(str(account_id), safe='')}",
    )
    if not result.get("ok"):
        return {}

    response = result.get("response") or {}
    return response if isinstance(response, dict) else {}


def jira_user_assignable(account_id):
    if not (account_id and JIRA_PROJECT_KEY):
        return {
            "ok": False,
            "assignable": False,
            "reason": "missing_jira_project_key",
        }

    result = jira_request(
        "GET",
        (
            "/rest/api/3/user/assignable/search"
            f"?project={urllib.parse.quote(str(JIRA_PROJECT_KEY), safe='')}"
            f"&accountId={urllib.parse.quote(str(account_id), safe='')}"
        ),
    )
    if not result.get("ok"):
        return {
            "ok": False,
            "assignable": False,
            "reason": "jira_assignable_lookup_failed",
            "status": result.get("status"),
            "error": result.get("error"),
        }

    users = result.get("response") or []
    assignable = any(
        isinstance(user, dict)
        and str(user.get("accountId") or "").strip() == str(account_id).strip()
        for user in users
    )
    return {
        "ok": True,
        "assignable": assignable,
        "reason": None if assignable else "jira_user_not_assignable",
    }


def generated_agent_id(account_id, user=None):
    user = user or {}
    display_name = str(user.get("displayName") or "").strip()
    if display_name:
        candidate = "".join(
            char.upper() if char.isalnum() else "_"
            for char in display_name
        ).strip("_")
        candidate = "_".join(part for part in candidate.split("_") if part)
        if candidate:
            return candidate[:64]

    suffix = str(account_id or "").split(":")[-1].replace("-", "").upper()
    return f"JSM_{suffix[:24] or 'ONCALL'}"


def build_auto_oncall_agent(account_id):
    user = jira_user(account_id)
    assignable = jira_user_assignable(account_id)
    display_name = str(user.get("displayName") or "").strip()
    now_iso = utc_now_iso()
    enabled = bool(assignable.get("assignable"))
    return {
        "agent_id": generated_agent_id(account_id, user),
        "jira_account_id": account_id,
        "atlassian_account_id": account_id,
        "display_name": display_name or account_id,
        "enabled": enabled,
        "active_count": 0,
        "max_capacity": DEFAULT_AGENT_CAPACITY,
        "priority": AUTO_ONCALL_AGENT_PRIORITY,
        "timezone": user.get("timeZone") or LIVE_AGENT_TIMEZONE,
        "shift_start": "00:00",
        "shift_end": "00:00",
        "shift_name": "JSM_ONCALL",
        "source": "jsm_oncall_auto",
        "auto_created": True,
        "auto_disabled_reason": None if enabled else assignable.get("reason"),
        "created_at": now_iso,
        "updated_at": now_iso,
    }


def agent_oncall_identifiers(agent):
    identifiers = {
        str(agent.get("jira_account_id") or "").strip(),
        str(agent.get("atlassian_account_id") or "").strip(),
        str(agent.get("account_id") or "").strip(),
    }
    identifiers.discard("")
    return identifiers


def update_auto_oncall_agent_enabled(agent, enabled, reason=None):
    agent_id = str(agent.get("agent_id") or "").strip()
    if not agent_id:
        return None

    names = {"#reason": "auto_disabled_reason"}
    values = {
        ":enabled": enabled,
        ":now": utc_now_iso(),
    }
    expression = "SET enabled = :enabled, updated_at = :now"
    if reason:
        expression += ", #reason = :reason"
        values[":reason"] = reason
    else:
        expression += " REMOVE #reason"

    try:
        capacity_table.update_item(
            Key={"agent_id": agent_id},
            UpdateExpression=expression,
            ConditionExpression=Attr("auto_created").eq(True),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return agent_id
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise


def ensure_oncall_capacity_agents(oncall_user_ids, existing_agents):
    if not AUTO_CREATE_JSM_ONCALL_AGENTS:
        return []

    oncall_user_ids = {
        str(value).strip()
        for value in oncall_user_ids or []
        if str(value).strip()
    }
    existing_ids = set()
    existing_agent_ids = set()
    reenabled = []
    disabled = []
    for agent in existing_agents or []:
        existing_agent_ids.add(str(agent.get("agent_id") or "").strip())
        identifiers = agent_oncall_identifiers(agent)
        existing_ids.update(identifiers)

        if agent.get("auto_created") is True:
            is_currently_oncall = bool(identifiers & oncall_user_ids)
            assignable = (
                jira_user_assignable(next(iter(identifiers))) if is_currently_oncall else {}
            )
            if (
                is_currently_oncall
                and assignable.get("assignable")
                and agent.get("enabled") is not True
            ):
                agent_id = update_auto_oncall_agent_enabled(agent, True)
                if agent_id:
                    reenabled.append(agent_id)
            elif (
                is_currently_oncall
                and not assignable.get("assignable")
                and agent.get("enabled") is True
            ):
                agent_id = update_auto_oncall_agent_enabled(
                    agent,
                    False,
                    assignable.get("reason") or "jira_user_not_assignable",
                )
                if agent_id:
                    disabled.append(agent_id)
            elif (
                AUTO_DISABLE_JSM_ONCALL_AGENTS
                and not is_currently_oncall
                and agent.get("enabled") is True
            ):
                agent_id = update_auto_oncall_agent_enabled(agent, False, "not_currently_on_call")
                if agent_id:
                    disabled.append(agent_id)

    created = []
    for account_id in sorted(oncall_user_ids):
        if account_id in existing_ids:
            continue

        agent = build_auto_oncall_agent(account_id)
        base_agent_id = agent["agent_id"]
        suffix = 2
        while agent["agent_id"] in existing_agent_ids:
            agent["agent_id"] = f"{base_agent_id[:58]}_{suffix}"
            suffix += 1

        try:
            capacity_table.put_item(
                Item=agent,
                ConditionExpression=Attr("agent_id").not_exists(),
            )
            created.append(agent)
            existing_ids.add(account_id)
            existing_agent_ids.add(agent["agent_id"])
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise

    if created:
        log_json({
            "level": "INFO",
            "message": "jsm_oncall_capacity_agents_auto_created",
            "created_agents": [
                {
                    "agent_id": agent.get("agent_id"),
                    "jira_account_id": agent.get("jira_account_id"),
                    "display_name": agent.get("display_name"),
                }
                for agent in created
            ],
        })

    if reenabled or disabled:
        log_json({
            "level": "INFO",
            "message": "jsm_oncall_capacity_agents_auto_synced",
            "reenabled_agents": reenabled,
            "disabled_agents": disabled,
        })

    return created


def agent_capacity(agent):
    row_capacity = int(agent.get("max_capacity") or DEFAULT_AGENT_CAPACITY)
    return min(row_capacity, DEFAULT_AGENT_CAPACITY)


def agent_active_count(agent):
    return int(agent.get("active_count", 0))


def sort_agents_for_assignment(agents):
    return sorted(
        agents,
        key=lambda agent: (
            agent_active_count(agent),
            int(agent.get("priority", 999)),
            str(agent.get("display_name") or agent.get("agent_id") or "").lower(),
        ),
    )


def reserve_agent_capacity(agent):
    maximum = agent_capacity(agent)
    before = agent_active_count(agent)
    now_iso = utc_now_iso()
    try:
        response = capacity_table.update_item(
            Key={"agent_id": agent["agent_id"]},
            UpdateExpression=(
                "SET active_count = if_not_exists(active_count, :zero) + :one, "
                "updated_at = :now"
            ),
            ConditionExpression=(
                Attr("enabled").eq(True)
                & (Attr("active_count").not_exists() | Attr("active_count").lt(maximum))
            ),
            ExpressionAttributeValues={
                ":zero": 0,
                ":one": 1,
                ":now": now_iso,
            },
            ReturnValues="ALL_NEW",
        )
        updated = response.get("Attributes") or {}
        return {
            "ok": True,
            "agent": {**agent, **updated},
            "active_count_before": before,
            "active_count_after": agent_active_count(updated),
        }
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return {
                "ok": False,
                "reason": "CONDITIONAL_UPDATE_FAILED",
                "agent": agent,
            }
        raise


def release_agent_capacity(agent_id):
    if not agent_id:
        return {"ok": False, "released": False, "reason": "MISSING_AGENT_ID"}

    try:
        response = capacity_table.update_item(
            Key={"agent_id": agent_id},
            UpdateExpression="SET active_count = active_count - :one, updated_at = :now",
            ConditionExpression=Attr("active_count").gt(0),
            ExpressionAttributeValues={":one": 1, ":now": utc_now_iso()},
            ReturnValues="ALL_NEW",
        )
        return {
            "ok": True,
            "released": True,
            "agent_id": agent_id,
            "active_count_after": agent_active_count(response.get("Attributes") or {}),
        }
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return {
                "ok": True,
                "released": False,
                "agent_id": agent_id,
                "reason": "ACTIVE_COUNT_ALREADY_ZERO",
            }
        raise


def choose_and_reserve_agent(now=None):
    now = now or get_current_time_in_timezone()
    reconcile_result = reconcile_active_assignments()
    oncall_result = get_jsm_oncall_user_ids(now) if ENABLE_JSM_ONCALL_SOURCE else None
    use_oncall_source = bool(oncall_result and oncall_result.get("ok"))
    oncall_user_ids = oncall_result.get("user_ids") if use_oncall_source else set()
    all_agents = get_enabled_agents(include_disabled=True)
    auto_created_agents = ensure_oncall_capacity_agents(oncall_user_ids, all_agents) if use_oncall_source else []
    if auto_created_agents:
        all_agents = [*all_agents, *auto_created_agents]
    schedule_sync_result = (
        sync_oncall_schedule_fields(oncall_user_ids, all_agents)
        if use_oncall_source
        else None
    )
    if schedule_sync_result and (
        schedule_sync_result.get("updated_agents")
        or schedule_sync_result.get("disabled_agents")
    ):
        all_agents = get_enabled_agents(include_disabled=True)
    eligible = []
    skipped = []

    for agent in all_agents:
        reason = None
        if agent.get("enabled") is not True:
            reason = "DISABLED"
        elif not agent.get("jira_account_id"):
            reason = "MISSING_JIRA_ACCOUNT_ID"
        elif use_oncall_source and not agent_matches_oncall_user(agent, oncall_user_ids):
            reason = "NOT_CURRENTLY_ON_CALL"
        elif (
            not use_oncall_source
            and (not oncall_result or JSM_ONCALL_FALLBACK_TO_SHIFT)
            and not is_agent_on_shift(agent, now)
        ):
            reason = "OFF_SHIFT"
        elif not use_oncall_source and oncall_result and not JSM_ONCALL_FALLBACK_TO_SHIFT:
            reason = oncall_result.get("error_code") or "JSM_ONCALL_LOOKUP_FAILED"
        elif agent_active_count(agent) >= agent_capacity(agent):
            reason = "FULL_CAPACITY"

        if reason:
            skipped.append({"agent_id": agent.get("agent_id"), "reason": reason})
        else:
            eligible.append(agent)

    for agent in sort_agents_for_assignment(eligible):
        reservation = reserve_agent_capacity(agent)
        if reservation.get("ok"):
            result = {
                **reservation,
                "current_time_ist": now.astimezone(ZoneInfo(LIVE_AGENT_TIMEZONE)).isoformat(),
                "assignment_source": "jsm_oncall" if use_oncall_source else "dynamodb_shift",
                "jsm_oncall": sanitize_oncall_result(oncall_result),
                "jsm_schedule_sync": schedule_sync_result,
                "capacity_reconcile": reconcile_result,
                "auto_created_agents": [item.get("agent_id") for item in auto_created_agents],
                "eligible_agents": [item.get("agent_id") for item in eligible],
                "skipped_agents_with_reason": skipped,
            }
            log_assignment_decision({**result, "assignment_status": "RESERVED"})
            return result
        skipped.append({
            "agent_id": agent.get("agent_id"),
            "reason": reservation.get("reason", "CONDITIONAL_UPDATE_FAILED"),
        })

    result = {
        "ok": False,
        "reason": "NO_CAPACITY",
        "current_time_ist": now.astimezone(ZoneInfo(LIVE_AGENT_TIMEZONE)).isoformat(),
        "assignment_source": "jsm_oncall" if use_oncall_source else "dynamodb_shift",
        "jsm_oncall": sanitize_oncall_result(oncall_result),
        "jsm_schedule_sync": schedule_sync_result,
        "capacity_reconcile": reconcile_result,
        "auto_created_agents": [item.get("agent_id") for item in auto_created_agents],
        "eligible_agents": [item.get("agent_id") for item in eligible],
        "skipped_agents_with_reason": skipped,
    }
    log_assignment_decision({**result, "assignment_status": "QUEUED"})
    return result


def jira_request(method, path, payload=None):
    if not (JIRA_BASE_URL and JIRA_EMAIL and JIRA_API_TOKEN):
        return {"ok": False, "error": "Missing Jira credentials", "error_code": "jira_configuration_error"}

    auth = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        f"{JIRA_BASE_URL}{path}",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8").strip()
            return {
                "ok": 200 <= response.status < 300,
                "status": response.status,
                "response": json.loads(body) if body else {},
            }
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")[:2000]
        return {"ok": False, "status": error.code, "error": body or str(error)}
    except Exception as error:
        return {"ok": False, "error": str(error)}


def create_jira_ticket(payload):
    return jira_request("POST", "/rest/api/3/issue", payload)


def assign_jira_issue(issue_key, jira_account_id):
    key = urllib.parse.quote(str(issue_key), safe="")
    return jira_request(
        "PUT",
        f"/rest/api/3/issue/{key}/assignee",
        {"accountId": jira_account_id},
    )


def jira_issue_status(issue_key):
    if not issue_key:
        return {"ok": False, "error": "Missing issue key", "error_code": "missing_issue_key"}

    key = urllib.parse.quote(str(issue_key), safe="")
    result = jira_request("GET", f"/rest/api/3/issue/{key}?fields=status")
    if not result.get("ok"):
        return {
            "ok": False,
            "status": result.get("status"),
            "error": result.get("error", "jira_issue_status_lookup_failed"),
            "error_code": "jira_issue_status_lookup_failed",
        }

    response = result.get("response") or {}
    status = (((response.get("fields") or {}).get("status") or {}).get("name") or "").strip()
    if not status:
        return {
            "ok": False,
            "error": "Jira issue status missing from response",
            "error_code": "missing_jira_issue_status",
        }

    return {"ok": True, "ticket_status": status}


def update_jira_labels(issue_key, add_labels=None, remove_labels=None):
    updates = [{"add": label} for label in (add_labels or [])]
    updates.extend({"remove": label} for label in (remove_labels or []))
    if not updates:
        return {"ok": True, "skipped": True}
    key = urllib.parse.quote(str(issue_key), safe="")
    return jira_request(
        "PUT",
        f"/rest/api/3/issue/{key}",
        {"update": {"labels": updates}},
    )


def mark_jira_ticket_queued(issue_key):
    return update_jira_labels(
        issue_key,
        add_labels=["live-agent", "live-agent-queued"],
        remove_labels=["live-agent-assigned"],
    )


def mark_jira_ticket_assigned(issue_key):
    return update_jira_labels(
        issue_key,
        add_labels=["live-agent", "live-agent-assigned"],
        remove_labels=["live-agent-queued"],
    )


def get_live_agent_session_by_ticket(ticket_key):
    response = session_table.get_item(Key={"session_id": f"live_agent_ticket:{ticket_key}"})
    return response.get("Item") or {}


def save_live_agent_session_mapping(ticket_key, values):
    now_iso = utc_now_iso()
    names = {}
    expression_values = {":updated_at": now_iso}
    sets = ["updated_at = :updated_at"]
    removes = []

    for index, (name, value) in enumerate(values.items()):
        if name in {"session_id", "updated_at"}:
            continue
        name_token = f"#n{index}"
        names[name_token] = name
        if value is None:
            removes.append(name_token)
        else:
            value_token = f":v{index}"
            expression_values[value_token] = value
            sets.append(f"{name_token} = {value_token}")

    expression = "SET " + ", ".join(sets)
    if removes:
        expression += " REMOVE " + ", ".join(removes)
    session_table.update_item(
        Key={"session_id": f"live_agent_ticket:{ticket_key}"},
        UpdateExpression=expression,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=expression_values,
    )


def is_terminal_status(status):
    return str(status or "").strip().lower() in TERMINAL_STATUSES


def active_reserved_ticket_items(max_items=None):
    max_items = CAPACITY_RECONCILE_MAX_ITEMS if max_items is None else max(0, int(max_items))
    if max_items == 0:
        return []

    items = []
    request = {
        "FilterExpression": (
            Attr("assignment_status").eq("ASSIGNED")
            & Attr("capacity_reserved").eq(True)
            & (Attr("capacity_released").not_exists() | Attr("capacity_released").eq(False))
        )
    }
    while True:
        response = session_table.scan(**request)
        for item in response.get("Items") or []:
            items.append(item)
            if len(items) >= max_items:
                return items
        if not response.get("LastEvaluatedKey"):
            break
        request["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    return items


def save_reconcile_status(ticket_key, status=None, error=None):
    values = {
        "last_reconciled_at": utc_now_iso(),
    }
    if status:
        values["last_jira_status_checked"] = status
    if error:
        values["reconcile_error"] = str(error)[:1000]
    else:
        values["reconcile_error"] = None
    save_live_agent_session_mapping(ticket_key, values)


def reconcile_active_assignments(max_items=None, status_lookup=None, release_fn=None):
    if not CAPACITY_RECONCILE_ENABLED:
        return {
            "ok": True,
            "enabled": False,
            "checked": 0,
            "released": 0,
            "errors": 0,
            "released_tickets": [],
        }

    status_lookup = status_lookup or jira_issue_status
    release_fn = release_fn or release_capacity_for_ticket
    checked = 0
    released = 0
    errors = 0
    released_tickets = []
    retained_tickets = []
    error_tickets = []

    for pointer in active_reserved_ticket_items(max_items=max_items):
        ticket_key = str(pointer.get("ticket_key") or "").strip()
        if not ticket_key:
            continue
        checked += 1

        result = status_lookup(ticket_key)
        if not result.get("ok"):
            errors += 1
            error = result.get("error") or result.get("error_code") or "jira_status_lookup_failed"
            save_reconcile_status(ticket_key, error=error)
            error_tickets.append({"ticket_key": ticket_key, "error": error})
            continue

        status = result.get("ticket_status") or result.get("status") or ""
        save_reconcile_status(ticket_key, status=status)
        if is_terminal_status(status):
            release = release_fn(ticket_key, status)
            if release.get("released"):
                released += 1
                released_tickets.append(ticket_key)
            else:
                retained_tickets.append({
                    "ticket_key": ticket_key,
                    "status": status,
                    "reason": release.get("reason", "not_released"),
                })
        else:
            retained_tickets.append({"ticket_key": ticket_key, "status": status})

    result = {
        "ok": True,
        "enabled": True,
        "checked": checked,
        "released": released,
        "errors": errors,
        "released_tickets": released_tickets,
        "retained_tickets": retained_tickets,
        "error_tickets": error_tickets,
    }
    log_json({
        "level": "INFO" if errors == 0 else "WARN",
        "message": "live_agent_capacity_reconcile_completed",
        **result,
    })
    return result


def release_capacity_for_ticket(ticket_key, status):
    pointer_id = f"live_agent_ticket:{ticket_key}"
    pointer = get_live_agent_session_by_ticket(ticket_key)
    if not pointer:
        return {"ok": True, "released": False, "reason": "MISSING_TICKET_MAPPING"}
    if not is_terminal_status(status):
        return {"ok": True, "released": False, "reason": "NON_TERMINAL_STATUS"}
    if pointer.get("assignment_status") != "ASSIGNED" or pointer.get("capacity_reserved") is not True:
        return {"ok": True, "released": False, "reason": "CAPACITY_NOT_RESERVED"}

    try:
        session_table.update_item(
            Key={"session_id": pointer_id},
            UpdateExpression=(
                "SET capacity_released = :true, ticket_status = :status, updated_at = :now"
            ),
            ConditionExpression=(
                Attr("capacity_released").not_exists() | Attr("capacity_released").eq(False)
            ),
            ExpressionAttributeValues={
                ":true": True,
                ":status": status,
                ":now": utc_now_iso(),
            },
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return {"ok": True, "released": False, "reason": "ALREADY_RELEASED"}
        raise

    release = release_agent_capacity(pointer.get("assigned_agent_id"))
    return {**release, "ticket_key": ticket_key, "pointer": pointer}


def queued_ticket_items():
    items = []
    request = {
        "FilterExpression": (
            Attr("assignment_status").eq("QUEUED")
            & Attr("capacity_reserved").eq(False)
        )
    }
    while True:
        response = session_table.scan(**request)
        items.extend(response.get("Items") or [])
        if not response.get("LastEvaluatedKey"):
            break
        request["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    active_items = [
        item for item in items
        if not is_terminal_status(item.get("ticket_status"))
    ]
    return sorted(active_items, key=lambda item: str(item.get("created_at") or ""))


def claim_queued_ticket(pointer):
    try:
        session_table.update_item(
            Key={"session_id": pointer["session_id"]},
            UpdateExpression="SET queue_promotion_status = :claimed, updated_at = :now",
            ConditionExpression=(
                Attr("assignment_status").eq("QUEUED")
                & Attr("capacity_reserved").eq(False)
                & (
                    Attr("queue_promotion_status").not_exists()
                    | Attr("queue_promotion_status").eq("FAILED")
                )
            ),
            ExpressionAttributeValues={":claimed": "CLAIMED", ":now": utc_now_iso()},
        )
        return True
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def promote_oldest_queued_ticket(assign_issue=None, notify=None, now=None):
    assign_issue = assign_issue or assign_jira_issue
    for pointer in queued_ticket_items():
        if not claim_queued_ticket(pointer):
            continue

        reservation = choose_and_reserve_agent(now=now)
        if not reservation.get("ok"):
            save_live_agent_session_mapping(
                pointer["ticket_key"],
                {"queue_promotion_status": "FAILED"},
            )
            return {"promoted": False, "reason": "NO_CAPACITY"}

        agent = reservation["agent"]
        assignment = assign_issue(pointer["ticket_key"], agent["jira_account_id"])
        if not assignment.get("ok"):
            rollback = release_agent_capacity(agent["agent_id"])
            save_live_agent_session_mapping(
                pointer["ticket_key"],
                {"queue_promotion_status": "FAILED"},
            )
            return {
                "promoted": False,
                "reason": "JIRA_ASSIGNMENT_FAILED",
                "rollback": rollback,
            }

        save_live_agent_session_mapping(pointer["ticket_key"], {
            "assignment_status": "ASSIGNED",
            "capacity_reserved": True,
            "capacity_released": False,
            "assigned_agent_id": agent["agent_id"],
            "assigned_agent_name": agent.get("display_name") or agent["agent_id"],
            "assigned_jira_account_id": agent["jira_account_id"],
            "queue_promotion_status": "ASSIGNED",
        })
        mark_jira_ticket_assigned(pointer["ticket_key"])
        if notify:
            notify(pointer, agent)
        return {
            "promoted": True,
            "ticket_key": pointer["ticket_key"],
            "agent": agent,
            "jira_assignment": assignment,
        }

    return {"promoted": False, "reason": "QUEUE_EMPTY"}


def log_assignment_decision(data):
    log_json({
        "level": "INFO" if data.get("ok") else "WARN",
        "message": "live_agent_capacity_assignment_decision",
        **data,
    })
