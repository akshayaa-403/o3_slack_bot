import json
import os
import boto3

AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

lambda_client = boto3.client("lambda", region_name=AWS_REGION)

CREATE_JIRA_TICKET_FUNCTION = os.environ.get("CREATE_JIRA_TICKET_FUNCTION")
IMAGE_REK_FUNCTION = os.environ.get("IMAGE_REK_FUNCTION")
LIVE_AGENT_FUNCTION = os.environ.get("LIVE_AGENT_FUNCTION")
ESCALATION_FUNCTION = os.environ.get("ESCALATION_FUNCTION")
LLM_FALLBACK_FUNCTION = os.environ.get("LLM_FALLBACK_FUNCTION")

DEFAULT_REPLY = os.environ.get(
    "DEFAULT_ROUTER_REPLY",
    "I understood the request, but that action is not wired yet."
)
JIRA_DEFERRED_REPLY = os.environ.get(
    "JIRA_DEFERRED_REPLY",
    "I identified this as a request that should become a Jira ticket. Jira is not connected yet, so I have marked it for ticket creation once Jira is wired."
)

JIRA_DEFERRED_INTENTS = {
    "AWSaccount",
    "AWSRelatedQueries",
    "AccessforCamtasia",
    "AccesstoOpsgenie",
    "AccessToPCQ",
    "AccessToIkbInnovyQCom",
    "AccessToUemGpcloudserviceCom",
}

INTENT_ROUTES = {
    **{
        intent_name: {
            "action": "jira_deferred",
            "stub_reply": JIRA_DEFERRED_REPLY
        }
        for intent_name in JIRA_DEFERRED_INTENTS
    },
    "CreateJiraTicket": {
        "action": "jira_deferred",
        "function_env": "CREATE_JIRA_TICKET_FUNCTION",
        "function_name": CREATE_JIRA_TICKET_FUNCTION,
        "stub_reply": JIRA_DEFERRED_REPLY
    },
    "ImageRek": {
        "action": "invoke_or_stub",
        "function_env": "IMAGE_REK_FUNCTION",
        "function_name": IMAGE_REK_FUNCTION,
        "stub_reply": "I can help analyze the image. That integration is not wired yet."
    },
    "LiveAgent": {
        "action": "invoke_or_stub",
        "function_env": "LIVE_AGENT_FUNCTION",
        "function_name": LIVE_AGENT_FUNCTION,
        "stub_reply": "I can help connect you to a live agent. That handoff is not wired yet."
    },
    "Escalation": {
        "action": "invoke_or_stub",
        "function_env": "ESCALATION_FUNCTION",
        "function_name": ESCALATION_FUNCTION,
        "stub_reply": "I can help escalate this request. That escalation path is not wired yet."
    },
    "FallbackToLLM": {
        "action": "invoke_or_stub",
        "function_env": "LLM_FALLBACK_FUNCTION",
        "function_name": LLM_FALLBACK_FUNCTION,
        "stub_reply": "I can hand this to the fallback assistant. That integration is not wired yet."
    }
}


def log_json(data):
    print(json.dumps(data, default=str))


def get_intent(event):
    return event.get("sessionState", {}).get("intent", {})


def get_session_attributes(event):
    return event.get("sessionState", {}).get("sessionAttributes", {}) or {}


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


def invoke_downstream(function_name, payload):
    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8")
    )

    raw_payload = response.get("Payload").read().decode("utf-8")
    if not raw_payload:
        return {}

    return json.loads(raw_payload)


def lex_close_response(intent, session_attributes, message, state="Fulfilled"):
    intent_name = intent.get("name", "UNKNOWN")
    slots = intent.get("slots", {})

    return {
        "sessionState": {
            "dialogAction": {
                "type": "Close"
            },
            "intent": {
                "name": intent_name,
                "slots": slots,
                "state": state
            },
            "sessionAttributes": session_attributes
        },
        "messages": [
            {
                "contentType": "PlainText",
                "content": message
            }
        ]
    }


def with_jira_deferred_attributes(session_attributes):
    updated = dict(session_attributes)
    updated.update({
        "response_source": "router",
        "next_action": "O3_CreateJiraTicket",
        "jira_status": "deferred"
    })
    return updated


def normalize_downstream_reply(result, fallback):
    if not isinstance(result, dict):
        return fallback

    for key in ("message", "reply", "text"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return fallback


def route_intent(event):
    intent = get_intent(event)
    intent_name = intent.get("name", "UNKNOWN")
    route = INTENT_ROUTES.get(intent_name)
    slots = simplify_slots(intent.get("slots", {}))
    session_attributes = get_session_attributes(event)

    router_payload = {
        "intent_name": intent_name,
        "slots": slots,
        "session_id": event.get("sessionId"),
        "input_transcript": event.get("inputTranscript"),
        "session_attributes": session_attributes,
        "request_attributes": event.get("requestAttributes", {})
    }

    if not route:
        log_json({
            "level": "INFO",
            "message": "router_intent_unmapped",
            "intent_name": intent_name
        })
        return lex_close_response(intent, session_attributes, DEFAULT_REPLY)

    if route.get("action") == "jira_deferred":
        log_json({
            "level": "INFO",
            "message": "router_jira_deferred",
            "intent_name": intent_name
        })
        return lex_close_response(
            intent,
            with_jira_deferred_attributes(session_attributes),
            route["stub_reply"]
        )

    function_name = route["function_name"]
    if not function_name:
        log_json({
            "level": "INFO",
            "message": "router_intent_stubbed",
            "intent_name": intent_name,
            "function_env": route["function_env"]
        })
        return lex_close_response(intent, session_attributes, route["stub_reply"])

    try:
        result = invoke_downstream(function_name, router_payload)
        reply = normalize_downstream_reply(result, route["stub_reply"])

        log_json({
            "level": "INFO",
            "message": "router_downstream_completed",
            "intent_name": intent_name,
            "function_name": function_name
        })

        return lex_close_response(intent, session_attributes, reply)

    except Exception as e:
        log_json({
            "level": "ERROR",
            "message": "router_downstream_failed",
            "intent_name": intent_name,
            "function_name": function_name,
            "error": str(e)
        })

        return lex_close_response(
            intent,
            session_attributes,
            "I could not complete that action yet. Please try again or rephrase your request.",
            state="Failed"
        )


def lambda_handler(event, context):
    intent = get_intent(event)

    log_json({
        "level": "INFO",
        "message": "router_event_received",
        "intent_name": intent.get("name", "UNKNOWN"),
        "invocation_source": event.get("invocationSource")
    })

    return route_intent(event)
