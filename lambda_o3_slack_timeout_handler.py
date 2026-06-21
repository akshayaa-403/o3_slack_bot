import json
import os
import time
import hashlib
import boto3
import urllib.request
from datetime import datetime, timezone, timedelta
from botocore.exceptions import ClientError

AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
scheduler = boto3.client("scheduler", region_name=AWS_REGION)
lambda_client = boto3.client("lambda", region_name=AWS_REGION)

DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "o3_slack_sessions")
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "86400"))
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
TIMEOUT_CLOSE_GRACE_SECONDS = int(os.environ.get("TIMEOUT_CLOSE_GRACE_SECONDS", "300"))
TIMEOUT_PROMPT_TEXT = os.environ.get(
    "TIMEOUT_PROMPT_TEXT",
    "Are you still there? I will close this conversation if I do not hear back soon."
)
SCHEDULER_ROLE_ARN = os.environ.get("SCHEDULER_ROLE_ARN")
TIMEOUT_HANDLER_ARN = os.environ.get("TIMEOUT_HANDLER_ARN")
SCHEDULER_GROUP_NAME = os.environ.get("SCHEDULER_GROUP_NAME", "default")
SCHEDULER_NAME_PREFIX = os.environ.get("SCHEDULER_NAME_PREFIX", "o3-slack-timeout")
SUMMARIZER_FUNCTION_NAME = os.environ.get("SUMMARIZER_FUNCTION_NAME")

sessions_table = dynamodb.Table(DYNAMODB_TABLE)


def log_json(data):
    print(json.dumps(data, default=str))


def to_iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value):
    if not value:
        return None

    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def ttl_epoch():
    return int(time.time()) + SESSION_TTL_SECONDS


def send_slack_message(channel, text):
    url = "https://slack.com/api/chat.postMessage"

    data = json.dumps({
        "channel": channel,
        "text": text
    }).encode("utf-8")

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


def timeout_schedule_name(session_id, phase):
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    suffix = f"-{phase}"
    max_prefix_length = 64 - len(digest) - len(suffix) - 1
    prefix = SCHEDULER_NAME_PREFIX[:max_prefix_length]

    return f"{prefix}-{digest}{suffix}"


def scheduler_at_expression(due_at):
    utc_due_at = due_at.astimezone(timezone.utc).replace(microsecond=0)
    return f"at({utc_due_at.strftime('%Y-%m-%dT%H:%M:%S')})"


def get_timeout_handler_arn(context):
    if TIMEOUT_HANDLER_ARN:
        return TIMEOUT_HANDLER_ARN

    return getattr(context, "invoked_function_arn", None)


def upsert_schedule(name, due_at, payload, target_arn):
    request = {
        "GroupName": SCHEDULER_GROUP_NAME,
        "ScheduleExpression": scheduler_at_expression(due_at),
        "ScheduleExpressionTimezone": "UTC",
        "FlexibleTimeWindow": {
            "Mode": "OFF"
        },
        "Target": {
            "Arn": target_arn,
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


def schedule_close(session_id, timeout_token, timeout_due_at, close_due_at, context):
    target_arn = get_timeout_handler_arn(context)

    if not target_arn or not SCHEDULER_ROLE_ARN:
        log_json({
            "level": "WARN",
            "message": "timeout_close_schedule_skipped",
            "session_id": session_id,
            "reason": "missing_timeout_scheduler_env",
            "has_timeout_handler_arn": bool(target_arn),
            "has_scheduler_role_arn": bool(SCHEDULER_ROLE_ARN)
        })
        return False

    schedule_name = timeout_schedule_name(session_id, "close")
    payload = {
        "action": "close",
        "session_id": session_id,
        "timeout_token": timeout_token,
        "timeout_due_at": timeout_due_at,
        "timeout_close_due_at": to_iso(close_due_at)
    }

    try:
        action = upsert_schedule(schedule_name, close_due_at, payload, target_arn)

        log_json({
            "level": "INFO",
            "message": "timeout_close_schedule_refreshed",
            "session_id": session_id,
            "schedule_name": schedule_name,
            "timeout_close_due_at": to_iso(close_due_at),
            "scheduler_action": action
        })
        return True

    except Exception as e:
        log_json({
            "level": "ERROR",
            "message": "timeout_close_schedule_failed",
            "session_id": session_id,
            "schedule_name": schedule_name,
            "error": str(e)
        })
        return False


def get_session(session_id):
    response = sessions_table.get_item(Key={"session_id": session_id})
    return response.get("Item")


def ignored(reason, session_id, extra=None):
    payload = {
        "status": "ignored",
        "reason": reason,
        "session_id": session_id
    }

    if extra:
        payload.update(extra)

    log_json({
        "level": "INFO",
        "message": "timeout_event_ignored",
        **payload
    })

    return payload


def validate_active_timeout(event, item):
    if not item:
        return "missing_session"

    if item.get("timeout_token") != event.get("timeout_token"):
        return "stale_timeout_token"

    if item.get("conversation_status") != "active":
        return "conversation_not_active"

    return None


def claim_prompt(session_id, timeout_token_value, now):
    try:
        sessions_table.update_item(
            Key={"session_id": session_id},
            UpdateExpression="""
                SET
                    timeout_status = :prompting,
                    timeout_prompt_started_at = :now,
                    updated_at = :now,
                    #ttl = :ttl
            """,
            ConditionExpression="""
                timeout_token = :timeout_token
                AND conversation_status = :active
                AND (
                    attribute_not_exists(timeout_status)
                    OR timeout_status = :scheduled
                    OR timeout_status = :prompting
                )
            """,
            ExpressionAttributeNames={
                "#ttl": "ttl"
            },
            ExpressionAttributeValues={
                ":timeout_token": timeout_token_value,
                ":active": "active",
                ":scheduled": "scheduled",
                ":prompting": "prompting",
                ":now": to_iso(now),
                ":ttl": ttl_epoch()
            }
        )
        return True

    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False

        raise


def mark_prompted(session_id, timeout_token_value, prompted_at, close_due_at):
    sessions_table.update_item(
        Key={"session_id": session_id},
        UpdateExpression="""
            SET
                timeout_status = :prompted,
                timeout_prompted_at = :prompted_at,
                timeout_close_due_at = :close_due_at,
                updated_at = :prompted_at,
                #ttl = :ttl
        """,
        ConditionExpression="""
            timeout_token = :timeout_token
            AND conversation_status = :active
        """,
        ExpressionAttributeNames={
            "#ttl": "ttl"
        },
        ExpressionAttributeValues={
            ":timeout_token": timeout_token_value,
            ":active": "active",
            ":prompted": "prompted",
            ":prompted_at": to_iso(prompted_at),
            ":close_due_at": to_iso(close_due_at),
            ":ttl": ttl_epoch()
        }
    )


def close_session(session_id, timeout_token_value, closed_at):
    sessions_table.update_item(
        Key={"session_id": session_id},
        UpdateExpression="""
            SET
                conversation_status = :closed,
                timeout_status = :closed,
                timeout_closed_at = :closed_at,
                updated_at = :closed_at,
                #ttl = :ttl
        """,
        ConditionExpression="""
            timeout_token = :timeout_token
            AND conversation_status = :active
            AND timeout_status = :prompted
        """,
        ExpressionAttributeNames={
            "#ttl": "ttl"
        },
        ExpressionAttributeValues={
            ":timeout_token": timeout_token_value,
            ":active": "active",
            ":prompted": "prompted",
            ":closed": "closed",
            ":closed_at": to_iso(closed_at),
            ":ttl": ttl_epoch()
        }
    )


def invoke_summarizer(session_id, timeout_token_value, closed_at):
    if not SUMMARIZER_FUNCTION_NAME:
        return False

    payload = {
        "session_id": session_id,
        "timeout_token": timeout_token_value,
        "closed_at": to_iso(closed_at),
        "reason": "inactivity_timeout"
    }

    try:
        lambda_client.invoke(
            FunctionName=SUMMARIZER_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8")
        )

        log_json({
            "level": "INFO",
            "message": "timeout_summarizer_invoked",
            "session_id": session_id,
            "summarizer_function": SUMMARIZER_FUNCTION_NAME
        })
        return True

    except Exception as e:
        log_json({
            "level": "ERROR",
            "message": "timeout_summarizer_invoke_failed",
            "session_id": session_id,
            "summarizer_function": SUMMARIZER_FUNCTION_NAME,
            "error": str(e)
        })
        return False


def handle_prompt(event, context):
    session_id = event.get("session_id")
    timeout_token_value = event.get("timeout_token")
    item = get_session(session_id)
    validation_error = validate_active_timeout(event, item)

    if validation_error:
        return ignored(validation_error, session_id)

    if item.get("timeout_status") == "prompted":
        return ignored("already_prompted", session_id)

    channel = item.get("channel")
    if not channel:
        return ignored("missing_channel", session_id)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    due_at = parse_iso(event.get("timeout_due_at") or item.get("timeout_due_at"))

    if due_at and now < due_at:
        return ignored(
            "too_early",
            session_id,
            {
                "now": to_iso(now),
                "timeout_due_at": to_iso(due_at)
            }
        )

    if not claim_prompt(session_id, timeout_token_value, now):
        return ignored("prompt_claim_failed", session_id)

    slack_response = send_slack_message(channel, TIMEOUT_PROMPT_TEXT)
    close_due_at = now + timedelta(seconds=TIMEOUT_CLOSE_GRACE_SECONDS)
    schedule_close(session_id, timeout_token_value, item.get("timeout_due_at"), close_due_at, context)

    try:
        mark_prompted(session_id, timeout_token_value, now, close_due_at)

    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return ignored("mark_prompted_condition_failed", session_id)

        raise

    result = {
        "status": "prompted",
        "session_id": session_id,
        "timeout_token": timeout_token_value,
        "timeout_close_due_at": to_iso(close_due_at),
        "slack_ts": slack_response.get("ts")
    }

    log_json({
        "level": "INFO",
        "message": "timeout_prompt_sent",
        **result
    })

    return result


def handle_close(event):
    session_id = event.get("session_id")
    timeout_token_value = event.get("timeout_token")
    item = get_session(session_id)
    validation_error = validate_active_timeout(event, item)

    if validation_error:
        return ignored(validation_error, session_id)

    if item.get("timeout_status") != "prompted":
        return ignored(
            "not_prompted",
            session_id,
            {
                "timeout_status": item.get("timeout_status")
            }
        )

    now = datetime.now(timezone.utc).replace(microsecond=0)
    close_due_at = parse_iso(event.get("timeout_close_due_at") or item.get("timeout_close_due_at"))

    if close_due_at and now < close_due_at:
        return ignored(
            "too_early",
            session_id,
            {
                "now": to_iso(now),
                "timeout_close_due_at": to_iso(close_due_at)
            }
        )

    try:
        close_session(session_id, timeout_token_value, now)

    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return ignored("close_condition_failed", session_id)

        raise

    invoke_summarizer(session_id, timeout_token_value, now)

    result = {
        "status": "closed",
        "session_id": session_id,
        "timeout_token": timeout_token_value,
        "closed_at": to_iso(now)
    }

    log_json({
        "level": "INFO",
        "message": "timeout_session_closed",
        **result
    })

    return result


def lambda_handler(event, context):
    log_json({
        "level": "INFO",
        "message": "timeout_event_received",
        "event": event
    })

    action = event.get("action", "prompt")

    if action == "prompt":
        return handle_prompt(event, context)

    if action == "close":
        return handle_close(event)

    return ignored("unsupported_action", event.get("session_id"), {"action": action})
