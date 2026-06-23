import json
import os
import boto3

AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)

BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID",
    "anthropic.claude-haiku-4-5-20251001-v1:0"
)
CLAUDE_MAX_TOKENS = int(os.environ.get("CLAUDE_MAX_TOKENS", "500"))
CLAUDE_TEMPERATURE = float(os.environ.get("CLAUDE_TEMPERATURE", "0.2"))
CLAUDE_SYSTEM_PROMPT = os.environ.get(
    "CLAUDE_SYSTEM_PROMPT",
    (
        "You are IVY, a concise IT and support assistant. "
        "Use only the current user request and session context. "
        "If the request is ambiguous, ask one clear clarifying question. "
        "Do not claim that a ticket was created."
    )
)


def log_json(data):
    print(json.dumps(data, default=str))


def compact_json(data):
    return json.dumps(data or {}, default=str, ensure_ascii=True, sort_keys=True)


def build_user_prompt(event):
    lex = event.get("lex", {})
    session = event.get("session", {})

    return "\n".join([
        "Current Slack user request:",
        event.get("text") or "",
        "",
        "Lex interpretation:",
        compact_json({
            "intent": lex.get("intent"),
            "state": lex.get("state"),
            "slots": lex.get("slots"),
            "reply": lex.get("reply")
        }),
        "",
        "Session context:",
        compact_json({
            "session_id": event.get("session_id"),
            "channel_type": event.get("channel_type"),
            "routing_reason": event.get("routing_reason"),
            "conversation_status": session.get("conversation_status")
        }),
        "",
        "Respond with the message IVY should send to the Slack user."
    ])


def extract_claude_text(response_body):
    parts = []

    for item in response_body.get("content", []):
        if item.get("type") == "text":
            text = (item.get("text") or "").strip()
            if text:
                parts.append(text)

    return "\n".join(parts).strip()


def invoke_claude(event):
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": CLAUDE_MAX_TOKENS,
        "temperature": CLAUDE_TEMPERATURE,
        "system": CLAUDE_SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": build_user_prompt(event)
                    }
                ]
            }
        ]
    }

    response = bedrock.invoke_model(
        modelId=BEDROCK_MODEL_ID,
        body=json.dumps(body).encode("utf-8"),
        contentType="application/json",
        accept="application/json"
    )

    response_body = json.loads(response["body"].read().decode("utf-8"))
    reply = extract_claude_text(response_body)

    if not reply:
        return {
            "ok": False,
            "error": "empty_claude_reply",
            "source": "claude",
            "model_id": BEDROCK_MODEL_ID
        }

    return {
        "ok": True,
        "reply": reply,
        "source": "claude",
        "model_id": BEDROCK_MODEL_ID,
        "stop_reason": response_body.get("stop_reason"),
        "usage": response_body.get("usage", {})
    }


def lambda_handler(event, context):
    log_json({
        "level": "INFO",
        "message": "claude_fallback_received",
        "event_id": event.get("event_id"),
        "session_id": event.get("session_id"),
        "lex_intent": event.get("lex", {}).get("intent"),
        "lex_state": event.get("lex", {}).get("state")
    })

    try:
        result = invoke_claude(event)

        log_json({
            "level": "INFO" if result.get("ok") else "WARN",
            "message": "claude_fallback_completed",
            "event_id": event.get("event_id"),
            "session_id": event.get("session_id"),
            "ok": result.get("ok"),
            "model_id": result.get("model_id"),
            "error": result.get("error")
        })

        return result

    except Exception as e:
        log_json({
            "level": "ERROR",
            "message": "claude_fallback_failed",
            "event_id": event.get("event_id"),
            "session_id": event.get("session_id"),
            "error": str(e)
        })

        return {
            "ok": False,
            "error": str(e),
            "source": "claude",
            "model_id": BEDROCK_MODEL_ID
        }
