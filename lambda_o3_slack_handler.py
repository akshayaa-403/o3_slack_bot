import json
import time
import os
import hmac
import hashlib
import base64
import urllib.parse
import urllib.request
import boto3
from botocore.exceptions import ClientError

sqs = boto3.client("sqs")
dynamodb = boto3.resource("dynamodb")

QUEUE_URL = os.environ["SQS_QUEUE_URL"]
# Slack signature verification is disabled by default for open testing.
# Set VERIFY_SLACK_SIGNATURE=true and SLACK_SIGNING_SECRET to enforce it again.
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_BOT_USER_ID = os.environ.get("SLACK_BOT_USER_ID", "")
VERIFY_SLACK_SIGNATURE = os.environ.get("VERIFY_SLACK_SIGNATURE", "false").lower() == "true"
DEDUP_TABLE = os.environ.get("DEDUP_TABLE", "O3_EventDedup2")
DEDUP_TTL_SECONDS = int(os.environ.get("DEDUP_TTL_SECONDS", "172800"))
SLACK_SIGNATURE_TOLERANCE_SECONDS = int(os.environ.get("SLACK_SIGNATURE_TOLERANCE_SECONDS", "300"))
ENABLE_FEEDBACK_RATING = os.environ.get("ENABLE_FEEDBACK_RATING", "true").lower() == "true"
ENABLE_FEEDBACK_FORM = os.environ.get("ENABLE_FEEDBACK_FORM", "true").lower() == "true"
LIVE_AGENT_SUPPORT_CHANNEL_ID = os.environ.get("LIVE_AGENT_SUPPORT_CHANNEL_ID", "").strip()

dedup_table = dynamodb.Table(DEDUP_TABLE)
ACTION_ID_FEEDBACK_RATING = "ivy_feedback_rating"
ACTION_ID_LIVE_AGENT_REPLY = "ivy_live_agent_reply"
CALLBACK_ID_FEEDBACK_FORM = "ivy_feedback_form"
CALLBACK_ID_LIVE_AGENT_REPLY = "ivy_live_agent_reply_form"


def log_json(data):
    print(json.dumps(data, default=str))


def get_header(headers, name):
    if not headers:
        return None

    name_lower = name.lower()

    for header_name, header_value in headers.items():
        if header_name.lower() == name_lower:
            return header_value

    return None


def get_raw_body(event):
    body = event.get("body", "")

    if event.get("isBase64Encoded"):
        return base64.b64decode(body).decode("utf-8")

    return body


def verify_slack_signature(event, raw_body):
    headers = event.get("headers", {})
    signature = get_header(headers, "X-Slack-Signature")
    timestamp = get_header(headers, "X-Slack-Request-Timestamp")

    if not signature or not timestamp:
        log_json({
            "level": "WARN",
            "message": "missing_slack_signature_headers"
        })
        return False

    try:
        timestamp_int = int(timestamp)
    except ValueError:
        log_json({
            "level": "WARN",
            "message": "invalid_slack_signature_timestamp",
            "timestamp": timestamp
        })
        return False

    now = int(time.time())

    if abs(now - timestamp_int) > SLACK_SIGNATURE_TOLERANCE_SECONDS:
        log_json({
            "level": "WARN",
            "message": "stale_slack_signature_timestamp",
            "timestamp": timestamp_int,
            "now": now
        })
        return False

    basestring = f"v0:{timestamp}:{raw_body}".encode("utf-8")
    expected_signature = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"),
        basestring,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_signature, signature):
        log_json({
            "level": "WARN",
            "message": "invalid_slack_signature"
        })
        return False

    return True


def is_direct_message(slack_event):
    return slack_event.get("channel_type") in {"im", "mpim"}


def message_mentions_bot(slack_event):
    text = slack_event.get("text") or ""

    if SLACK_BOT_USER_ID and f"<@{SLACK_BOT_USER_ID}>" in text:
        return True

    return slack_event.get("type") == "app_mention"


def is_live_agent_support_channel(slack_event):
    return bool(
        LIVE_AGENT_SUPPORT_CHANNEL_ID
        and slack_event.get("channel") == LIVE_AGENT_SUPPORT_CHANNEL_ID
    )


def clean_slack_text(text):
    value = (text or "").strip()

    if SLACK_BOT_USER_ID:
        value = value.replace(f"<@{SLACK_BOT_USER_ID}>", "").strip()

    return value


def is_image_file(file_info):
    mimetype = (file_info.get("mimetype") or "").lower()
    filetype = (file_info.get("filetype") or "").lower()

    return (
        mimetype.startswith("image/")
        or filetype in {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff"}
    )


def simplify_slack_file(file_info):
    return {
        "id": file_info.get("id"),
        "name": file_info.get("name") or file_info.get("title"),
        "title": file_info.get("title"),
        "mimetype": file_info.get("mimetype"),
        "filetype": file_info.get("filetype"),
        "url_private": file_info.get("url_private"),
        "url_private_download": file_info.get("url_private_download"),
        "thumb_1024": file_info.get("thumb_1024"),
        "size": file_info.get("size"),
        "created": file_info.get("created"),
        "user": file_info.get("user"),
    }


def extract_image_files(slack_event):
    return [
        simplify_slack_file(file_info)
        for file_info in slack_event.get("files") or []
        if is_image_file(file_info)
    ]


def parse_interactive_payload(raw_body):
    parsed = urllib.parse.parse_qs(raw_body or "", keep_blank_values=True)
    payload_values = parsed.get("payload")

    if not payload_values:
        return None

    return json.loads(payload_values[0])


def parse_action_value(value):
    if not isinstance(value, str) or not value.strip():
        return {}

    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass

    return {"action": value}


def slack_api(method, payload):
    if not SLACK_BOT_TOKEN:
        raise ValueError("Missing SLACK_BOT_TOKEN")

    request = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise ValueError(f"Slack API {method} failed: {result.get('error')}")

    return result


def feedback_stars(rating):
    rating = max(1, min(5, int(rating or 1)))
    return "★" * rating + "☆" * (5 - rating)


def feedback_modal_metadata(payload, action):
    user = payload.get("user", {}) or {}
    channel = payload.get("channel", {}) or {}
    action_payload = parse_action_value(action.get("value"))
    metadata = {
        **action_payload,
        "user": user.get("id"),
        "channel": channel.get("id"),
    }
    return metadata


def open_feedback_modal(payload, action):
    metadata = feedback_modal_metadata(payload, action)
    rating = int(metadata.get("rating") or 0)
    modal = {
        "type": "modal",
        "callback_id": CALLBACK_ID_FEEDBACK_FORM,
        "private_metadata": json.dumps(metadata, ensure_ascii=True, separators=(",", ":")),
        "title": {"type": "plain_text", "text": "IVY feedback"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"Rating: *{feedback_stars(rating)}*",
                },
            },
            {
                "type": "input",
                "block_id": "feedback_details",
                "optional": True,
                "label": {
                    "type": "plain_text",
                    "text": "Tell us more",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "feedback_text",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "What worked well or what should improve?",
                    },
                },
            },
        ],
    }
    return slack_api(
        "views.open",
        {
            "trigger_id": payload.get("trigger_id"),
            "view": modal,
        },
    )


def support_reply_metadata(payload, action):
    user = payload.get("user", {}) or {}
    channel = payload.get("channel", {}) or {}
    message = payload.get("message", {}) or {}
    container = payload.get("container", {}) or {}
    action_payload = parse_action_value(action.get("value"))
    return {
        **action_payload,
        "user": user.get("id"),
        "channel": channel.get("id"),
        "message_ts": container.get("message_ts") or message.get("ts"),
        "thread_ts": message.get("thread_ts") or container.get("thread_ts") or container.get("message_ts") or message.get("ts"),
    }


def open_live_agent_reply_modal(payload, action):
    metadata = support_reply_metadata(payload, action)
    ticket_key = metadata.get("ticket_key") or "live-agent request"
    modal = {
        "type": "modal",
        "callback_id": CALLBACK_ID_LIVE_AGENT_REPLY,
        "private_metadata": json.dumps(metadata, ensure_ascii=True, separators=(",", ":")),
        "title": {"type": "plain_text", "text": "Reply to customer"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"Ticket: *{ticket_key}*",
                },
            },
            {
                "type": "input",
                "block_id": "live_agent_reply",
                "label": {
                    "type": "plain_text",
                    "text": "Message",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "reply_text",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Write the message to send to the requester",
                    },
                },
            },
        ],
    }
    return slack_api(
        "views.open",
        {
            "trigger_id": payload.get("trigger_id"),
            "view": modal,
        },
    )


def extract_feedback_text(view):
    state_values = ((view or {}).get("state") or {}).get("values") or {}
    for block in state_values.values():
        if not isinstance(block, dict):
            continue
        for action in block.values():
            if isinstance(action, dict) and "value" in action:
                return action.get("value") or ""
    return ""


def extract_live_agent_reply_text(view):
    state_values = ((view or {}).get("state") or {}).get("values") or {}
    reply_block = state_values.get("live_agent_reply") or {}
    reply_action = reply_block.get("reply_text") or {}
    return reply_action.get("value") or ""


def enqueue_feedback_submission(payload):
    view = payload.get("view") or {}
    metadata = parse_action_value(view.get("private_metadata"))
    user = payload.get("user", {}) or {}
    event_id = "feedback-" + hashlib.sha256(
        "|".join([
            metadata.get("session_id") or "",
            str(metadata.get("rating") or ""),
            user.get("id") or "",
            view.get("id") or "",
        ]).encode("utf-8")
    ).hexdigest()[:32]
    now = int(time.time())

    try:
        dedup_table.put_item(
            Item={
                "event_id": event_id,
                "event_time": now,
                "created_at": now,
                "ttl": now + DEDUP_TTL_SECONDS,
            },
            ConditionExpression="attribute_not_exists(event_id)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return "duplicate ignored"
        raise

    sqs.send_message(
        QueueUrl=QUEUE_URL,
        MessageBody=json.dumps({
            "event_id": event_id,
            "event_type": "feedback_submission",
            "routing_reason": "feedback_submission",
            "channel": metadata.get("channel"),
            "user": user.get("id") or metadata.get("user"),
            "feedback_rating": metadata.get("rating"),
            "feedback_text": extract_feedback_text(view),
            "feedback_metadata": metadata,
        }),
    )
    return "OK"


def enqueue_live_agent_reply_submission(payload):
    view = payload.get("view") or {}
    metadata = parse_action_value(view.get("private_metadata"))
    user = payload.get("user", {}) or {}
    reply_text = extract_live_agent_reply_text(view).strip()
    event_id = "live-agent-reply-" + hashlib.sha256(
        "|".join([
            metadata.get("ticket_key") or "",
            user.get("id") or "",
            view.get("id") or "",
            reply_text,
        ]).encode("utf-8")
    ).hexdigest()[:32]
    now = int(time.time())

    try:
        dedup_table.put_item(
            Item={
                "event_id": event_id,
                "event_time": now,
                "created_at": now,
                "ttl": now + DEDUP_TTL_SECONDS,
            },
            ConditionExpression="attribute_not_exists(event_id)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return "duplicate ignored"
        raise

    sqs.send_message(
        QueueUrl=QUEUE_URL,
        MessageBody=json.dumps({
            "event_id": event_id,
            "event_type": "live_agent_reply_submission",
            "routing_reason": "live_agent_reply_submission",
            "channel": metadata.get("channel"),
            "user": user.get("id") or metadata.get("user"),
            "text": reply_text,
            "raw_text": reply_text,
            "ts": metadata.get("message_ts"),
            "thread_ts": metadata.get("thread_ts"),
            "message_ts": metadata.get("message_ts"),
            "ticket_key": metadata.get("ticket_key"),
        }),
    )
    return "OK"


def build_interactive_event_id(payload, action):
    user = payload.get("user", {}) or {}
    channel = payload.get("channel", {}) or {}
    container = payload.get("container", {}) or {}
    raw = "|".join([
        payload.get("type", ""),
        payload.get("trigger_id", ""),
        user.get("id", ""),
        channel.get("id", ""),
        container.get("message_ts", ""),
        action.get("action_id", ""),
        action.get("action_ts", ""),
        action.get("value", "")
    ])
    return "interactive-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def enqueue_interactive_action(payload):
    actions = payload.get("actions") or []

    if not actions:
        log_json({
            "level": "WARN",
            "message": "interactive_payload_ignored",
            "reason": "missing_actions"
        })
        return "missing actions"

    action = actions[0]
    user = payload.get("user", {}) or {}
    channel = payload.get("channel", {}) or {}
    message = payload.get("message", {}) or {}
    container = payload.get("container", {}) or {}
    channel_id = channel.get("id")
    channel_type = "im" if str(channel_id or "").startswith("D") else None
    thread_ts = None if channel_type == "im" else (message.get("thread_ts") or container.get("thread_ts"))
    action_id = action.get("action_id")
    action_value = action.get("value") or action_id or ""
    event_id = build_interactive_event_id(payload, action)
    now = int(time.time())

    try:
        dedup_table.put_item(
            Item={
                "event_id": event_id,
                "event_time": now,
                "created_at": now,
                "ttl": now + DEDUP_TTL_SECONDS
            },
            ConditionExpression="attribute_not_exists(event_id)"
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log_json({
                "level": "INFO",
                "message": "duplicate_interactive_action_ignored",
                "event_id": event_id,
                "action_id": action_id
            })
            return "duplicate ignored"

        raise

    sqs.send_message(
        QueueUrl=QUEUE_URL,
        MessageBody=json.dumps({
            "event_id": event_id,
            "channel": channel.get("id"),
            "text": action_value,
            "raw_text": action_value,
            "user": user.get("id"),
            "ts": action.get("action_ts") or container.get("message_ts") or message.get("ts"),
            "thread_ts": thread_ts,
            "event_type": "interactive_action",
            "channel_type": channel_type,
            "conversation_type": channel_type,
            "routing_reason": "interactive_action",
            "action_id": action_id,
            "action_value": action_value,
            "callback_id": payload.get("callback_id") or action.get("block_id"),
            "message_ts": container.get("message_ts") or message.get("ts"),
            "response_url": payload.get("response_url"),
            "trigger_id": payload.get("trigger_id")
        })
    )

    log_json({
        "level": "INFO",
        "message": "interactive_action_enqueued",
        "event_id": event_id,
        "channel": channel.get("id"),
        "user": user.get("id"),
        "action_id": action_id,
        "action_value": action_value
    })
    return "OK"


def should_process_slack_event(slack_event):
    event_type = slack_event.get("type")

    if event_type == "app_mention":
        return True, "app_mention"

    if event_type != "message":
        return False, "unsupported_event_type"

    if is_direct_message(slack_event):
        return True, "direct_message"

    if is_live_agent_support_channel(slack_event):
        return True, "live_agent_support_channel"

    if message_mentions_bot(slack_event):
        return True, "bot_mentioned"

    return False, "non_dm_ignored"


def lambda_handler(event, context):
    raw_body = get_raw_body(event)

    if VERIFY_SLACK_SIGNATURE and not verify_slack_signature(event, raw_body):
        return {
            "statusCode": 401,
            "body": "invalid signature"
        }

    interactive_payload = parse_interactive_payload(raw_body)

    if interactive_payload:
        if interactive_payload.get("type") == "view_submission":
            callback_id = (interactive_payload.get("view") or {}).get("callback_id")
            if callback_id == CALLBACK_ID_LIVE_AGENT_REPLY:
                enqueue_live_agent_reply_submission(interactive_payload)
            elif ENABLE_FEEDBACK_FORM:
                enqueue_feedback_submission(interactive_payload)
            return {
                "statusCode": 200,
                "body": "",
            }

        actions = interactive_payload.get("actions") or []
        action = actions[0] if actions else {}
        if str(action.get("action_id") or "").startswith(ACTION_ID_FEEDBACK_RATING):
            if not ENABLE_FEEDBACK_RATING:
                return {
                    "statusCode": 200,
                    "body": "",
                }

            try:
                open_feedback_modal(interactive_payload, action)
                return {
                    "statusCode": 200,
                    "body": "OK",
                }
            except Exception as error:
                log_json({
                    "level": "ERROR",
                    "message": "feedback_modal_open_failed",
                    "error": str(error),
                })
                return {
                    "statusCode": 200,
                    "body": "feedback modal failed",
                }

        if action.get("action_id") == ACTION_ID_LIVE_AGENT_REPLY:
            try:
                open_live_agent_reply_modal(interactive_payload, action)
                return {
                    "statusCode": 200,
                    "body": "OK",
                }
            except Exception as error:
                log_json({
                    "level": "ERROR",
                    "message": "live_agent_reply_modal_open_failed",
                    "error": str(error),
                })
                return {
                    "statusCode": 200,
                    "body": "live agent reply modal failed",
                }

        result = enqueue_interactive_action(interactive_payload)
        return {
            "statusCode": 200,
            "body": result
        }

    body = json.loads(raw_body or "{}")

    # Slack URL verification
    if body.get("type") == "url_verification":
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "text/plain"},
            "body": body["challenge"]
        }

    event_id = body.get("event_id")
    event_time = body.get("event_time")
    now = int(time.time())

    slack_event = body.get("event", {})
    event_type = slack_event.get("type")
    channel_type = slack_event.get("channel_type")

    # Ignore bot messages
    if slack_event.get("bot_id") or slack_event.get("subtype") == "bot_message":
        return {
            "statusCode": 200,
            "body": "ignore bot"
        }

    if slack_event.get("subtype") and slack_event.get("subtype") != "file_share":
        log_json({
            "level": "INFO",
            "message": "slack_event_ignored",
            "event_id": event_id,
            "reason": "unsupported_message_subtype",
            "subtype": slack_event.get("subtype")
        })

        return {
            "statusCode": 200,
            "body": "unsupported message subtype"
        }

    should_process, routing_reason = should_process_slack_event(slack_event)

    if not should_process:
        log_json({
            "level": "INFO",
            "message": "slack_event_ignored",
            "event_id": event_id,
            "reason": routing_reason,
            "event_type": event_type,
            "channel_type": channel_type
        })

        return {
            "statusCode": 200,
            "body": routing_reason
        }

    # Deduplicate accepted DM retry events before sending to SQS.
    if event_id:
        try:
            dedup_table.put_item(
                Item={
                    "event_id": event_id,
                    "event_time": event_time,
                    "created_at": now,
                    "ttl": now + DEDUP_TTL_SECONDS
                },
                ConditionExpression="attribute_not_exists(event_id)"
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                log_json({
                    "level": "INFO",
                    "message": "duplicate_event_ignored",
                    "event_id": event_id
                })

                return {
                    "statusCode": 200,
                    "body": "duplicate ignored"
                }

            raise

    channel = slack_event.get("channel")
    raw_text = slack_event.get("text", "")
    text = clean_slack_text(raw_text)
    image_files = extract_image_files(slack_event)
    user = slack_event.get("user")
    ts = slack_event.get("ts")
    thread_ts = slack_event.get("thread_ts")

    log_json({
        "level": "INFO",
        "message": "slack_event_received",
        "event_id": event_id,
        "channel": channel,
        "event_type": event_type,
        "channel_type": channel_type,
        "routing_reason": routing_reason,
        "user": user,
        "text": text,
        "thread_ts": thread_ts,
        "image_file_count": len(image_files)
    })

    if channel and user:
        sqs.send_message(
            QueueUrl=QUEUE_URL,
            MessageBody=json.dumps({
                "event_id": event_id,
                "channel": channel,
                "text": text,
                "raw_text": raw_text,
                "user": user,
                "ts": ts,
                "thread_ts": thread_ts,
                "event_type": event_type,
                "channel_type": channel_type,
                "conversation_type": channel_type,
                "routing_reason": routing_reason,
                "files": image_files,
                "has_image": bool(image_files)
            })
        )

    return {
        "statusCode": 200,
        "body": "OK"
    }
