import json
import os
import time
import boto3
import urllib.request
from datetime import datetime, timezone

AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

lex = boto3.client("lexv2-runtime", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)

BOT_ID = os.environ["BOT_ID"]
BOT_ALIAS_ID = os.environ["BOT_ALIAS_ID"]
LOCALE_ID = os.environ.get("LOCALE_ID", "en_US")

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "o3_slack_sessions")
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "86400"))
EMPTY_USER_TEXT_REPLY = os.environ.get("EMPTY_USER_TEXT_REPLY", "Hi, how can I help?")
EMPTY_LEX_REPLY = os.environ.get(
    "EMPTY_LEX_REPLY",
    "I could not generate a response for that. Please try rephrasing your message."
)

sessions_table = dynamodb.Table(DYNAMODB_TABLE)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def ttl_epoch():
    return int(time.time()) + SESSION_TTL_SECONDS


def log_json(data):
    print(json.dumps(data, default=str))


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


def get_lex_reply(messages):
    replies = []

    for message in messages or []:
        content = (message.get("content") or "").strip()

        if content:
            replies.append(content)

    if replies:
        return "\n".join(replies)

    log_json({
        "level": "WARN",
        "message": "empty_lex_reply"
    })

    return EMPTY_LEX_REPLY


def process_record(record):
    body = json.loads(record["body"])

    event_id = body.get("event_id")
    text = (body.get("text") or "").strip()
    raw_text = body.get("raw_text", text)
    user = body.get("user", "unknown-user")
    channel = body.get("channel")
    ts = body.get("ts")
    event_type = body.get("event_type")
    channel_type = body.get("channel_type")
    routing_reason = body.get("routing_reason")

    session_id = f"{channel}:{user}"
    lex_session_id = session_id

    log_json({
        "level": "INFO",
        "message": "worker_processing_started",
        "event_id": event_id,
        "channel": channel,
        "event_type": event_type,
        "channel_type": channel_type,
        "routing_reason": routing_reason,
        "user": user,
        "text": text
    })

    if text:
        response = lex.recognize_text(
            botId=BOT_ID,
            botAliasId=BOT_ALIAS_ID,
            localeId=LOCALE_ID,
            sessionId=lex_session_id,
            text=text
        )

        session_state = response.get("sessionState", {})
        intent = session_state.get("intent", {})

        lex_intent = intent.get("name", "UNKNOWN")
        lex_state = intent.get("state", "UNKNOWN")
        lex_slots = simplify_slots(intent.get("slots", {}))
        lex_reply = get_lex_reply(response.get("messages", []))
    else:
        lex_intent = "EMPTY_MESSAGE"
        lex_state = "Ignored"
        lex_slots = {}
        lex_reply = EMPTY_USER_TEXT_REPLY

    conversation_status = get_conversation_status(lex_state)
    updated_at = now_iso()

    sessions_table.update_item(
        Key={
            "session_id": session_id
        },
        UpdateExpression="""
            SET
                #channel = :channel,
                #user = :user,
                last_event_id = :event_id,
                last_user_text = :last_user_text,
                last_raw_user_text = :last_raw_user_text,
                last_bot_reply = :last_bot_reply,
                last_ts = :last_ts,
                event_type = :event_type,
                channel_type = :channel_type,
                routing_reason = :routing_reason,
                lex_session_id = :lex_session_id,
                lex_intent = :lex_intent,
                lex_state = :lex_state,
                lex_slots = :lex_slots,
                conversation_status = :conversation_status,
                created_at = if_not_exists(created_at, :created_at),
                updated_at = :updated_at,
                #ttl = :ttl
            ADD
                message_count :one
        """,
        ExpressionAttributeNames={
            "#channel": "channel",
            "#user": "user",
            "#ttl": "ttl"
        },
        ExpressionAttributeValues={
            ":channel": channel,
            ":user": user,
            ":event_id": event_id,
            ":last_user_text": text,
            ":last_raw_user_text": raw_text,
            ":last_bot_reply": lex_reply,
            ":last_ts": ts,
            ":event_type": event_type,
            ":channel_type": channel_type,
            ":routing_reason": routing_reason,
            ":lex_session_id": lex_session_id,
            ":lex_intent": lex_intent,
            ":lex_state": lex_state,
            ":lex_slots": lex_slots,
            ":conversation_status": conversation_status,
            ":created_at": updated_at,
            ":updated_at": updated_at,
            ":ttl": ttl_epoch(),
            ":one": 1
        }
    )

    slack_response = send_slack_message(channel, lex_reply)

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
        "lex_intent": lex_intent,
        "lex_state": lex_state,
        "lex_slots": lex_slots,
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
