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
BEDROCK_GUARDRAIL_ID = os.environ.get("BEDROCK_GUARDRAIL_ID", "").strip()
BEDROCK_GUARDRAIL_VERSION = os.environ.get("BEDROCK_GUARDRAIL_VERSION", "").strip()
BEDROCK_GUARDRAIL_MODE = os.environ.get("BEDROCK_GUARDRAIL_MODE", "invoke").strip().lower()
BEDROCK_GUARDRAIL_TRACE = os.environ.get("BEDROCK_GUARDRAIL_TRACE", "ENABLED_FULL").strip()
CLAUDE_GUARDRAIL_BLOCK_REPLY = os.environ.get(
    "CLAUDE_GUARDRAIL_BLOCK_REPLY",
    (
        "I can't help with that request. I can still help with general IT "
        "support or create a Jira ticket if needed."
    )
)

VALID_GUARDRAIL_MODES = {"disabled", "invoke", "pre_post"}
GUARDRAIL_INTERVENED_ACTIONS = {"GUARDRAIL_INTERVENED", "INTERVENED"}


def log_json(data):
    print(json.dumps(data, default=str))


def compact_json(data):
    return json.dumps(data or {}, default=str, ensure_ascii=True, sort_keys=True)


def guardrail_mode():
    if BEDROCK_GUARDRAIL_MODE in VALID_GUARDRAIL_MODES:
        return BEDROCK_GUARDRAIL_MODE

    return "invoke"


def guardrails_enabled():
    return (
        guardrail_mode() != "disabled"
        and bool(BEDROCK_GUARDRAIL_ID)
        and bool(BEDROCK_GUARDRAIL_VERSION)
    )


def apply_text_guardrail(text, source):
    if not text:
        return {
            "action": "NONE",
            "outputs": []
        }

    return bedrock.apply_guardrail(
        guardrailIdentifier=BEDROCK_GUARDRAIL_ID,
        guardrailVersion=BEDROCK_GUARDRAIL_VERSION,
        source=source,
        content=[
            {
                "text": {
                    "text": text
                }
            }
        ]
    )


def guardrail_intervened(result):
    return (result or {}).get("action") in GUARDRAIL_INTERVENED_ACTIONS


def extract_guardrail_output(result, fallback):
    for item in (result or {}).get("outputs", []):
        text = (item.get("text") or "").strip()
        if text:
            return text

    return fallback


def guardrail_metadata(result, source):
    return {
        "guardrail_action": (result or {}).get("action"),
        "guardrail_source": source,
        "guardrail_mode": guardrail_mode()
    }


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
    prompt = build_user_prompt(event)
    mode = guardrail_mode()

    if guardrails_enabled() and mode == "pre_post":
        input_guardrail = apply_text_guardrail(event.get("text") or "", "INPUT")

        if guardrail_intervened(input_guardrail):
            return {
                "ok": True,
                "reply": extract_guardrail_output(input_guardrail, CLAUDE_GUARDRAIL_BLOCK_REPLY),
                "source": "claude_guardrail",
                "model_id": BEDROCK_MODEL_ID,
                "error": "input_guardrail_intervened",
                **guardrail_metadata(input_guardrail, "INPUT")
            }

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
                        "text": prompt
                    }
                ]
            }
        ]
    }

    invoke_params = {
        "modelId": BEDROCK_MODEL_ID,
        "body": json.dumps(body).encode("utf-8"),
        "contentType": "application/json",
        "accept": "application/json"
    }

    if guardrails_enabled() and mode == "invoke":
        invoke_params["guardrailIdentifier"] = BEDROCK_GUARDRAIL_ID
        invoke_params["guardrailVersion"] = BEDROCK_GUARDRAIL_VERSION
        invoke_params["trace"] = BEDROCK_GUARDRAIL_TRACE

    response = bedrock.invoke_model(**invoke_params)

    response_body = json.loads(response["body"].read().decode("utf-8"))
    reply = extract_claude_text(response_body)
    invoke_guardrail_action = response_body.get("amazon-bedrock-guardrailAction")

    if not reply:
        return {
            "ok": False,
            "error": "empty_claude_reply",
            "source": "claude",
            "model_id": BEDROCK_MODEL_ID,
            "guardrail_action": invoke_guardrail_action,
            "guardrail_mode": mode
        }

    if guardrails_enabled() and mode == "pre_post":
        output_guardrail = apply_text_guardrail(reply, "OUTPUT")

        if guardrail_intervened(output_guardrail):
            return {
                "ok": True,
                "reply": extract_guardrail_output(output_guardrail, CLAUDE_GUARDRAIL_BLOCK_REPLY),
                "source": "claude_guardrail",
                "model_id": BEDROCK_MODEL_ID,
                "stop_reason": response_body.get("stop_reason"),
                "usage": response_body.get("usage", {}),
                **guardrail_metadata(output_guardrail, "OUTPUT")
            }

    return {
        "ok": True,
        "reply": reply,
        "source": "claude",
        "model_id": BEDROCK_MODEL_ID,
        "stop_reason": response_body.get("stop_reason"),
        "usage": response_body.get("usage", {}),
        "guardrail_action": invoke_guardrail_action,
        "guardrail_mode": mode
    }


def lambda_handler(event, context):
    log_json({
        "level": "INFO",
        "message": "claude_fallback_received",
        "event_id": event.get("event_id"),
        "session_id": event.get("session_id"),
        "lex_intent": event.get("lex", {}).get("intent"),
        "lex_state": event.get("lex", {}).get("state"),
        "guardrail_enabled": guardrails_enabled(),
        "guardrail_mode": guardrail_mode()
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
            "error": result.get("error"),
            "guardrail_action": result.get("guardrail_action"),
            "guardrail_source": result.get("guardrail_source"),
            "guardrail_mode": result.get("guardrail_mode")
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
