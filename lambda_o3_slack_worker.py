import json
import os
import re
import math
import time
import hashlib
import base64
import boto3
import contextvars
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from boto3.dynamodb.conditions import Attr
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError

AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

lex = boto3.client("lexv2-runtime", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
s3_client = boto3.client("s3", region_name=AWS_REGION)
scheduler = boto3.client("scheduler", region_name=AWS_REGION)
lambda_client = boto3.client("lambda", region_name=AWS_REGION)
bedrock_runtime = boto3.client("bedrock-runtime", region_name=AWS_REGION)
bedrock_agent_runtime = boto3.client("bedrock-agent-runtime", region_name=AWS_REGION)
secretsmanager = boto3.client("secretsmanager", region_name=AWS_REGION)
sqs_client = boto3.client("sqs", region_name=AWS_REGION)

BOT_ID = os.environ["BOT_ID"]
BOT_ALIAS_ID = os.environ["BOT_ALIAS_ID"]
LOCALE_ID = os.environ.get("LOCALE_ID", "en_US")

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "o3_slack_sessions")

# --- Multi-tenant resolution -------------------------------------------------
# Workspaces onboarded through slack_tenant_app get their own bot token and
# Lex bot. The handler already stamps every SQS message with
# `slack_tenant: {team_id, ...}`; this is the consumer side of that.
#
# Off by default. With MULTITENANT_ENABLED unset, every lookup short-circuits
# and the module-level env vars above are used exactly as before — so this
# whole block is inert until deliberately switched on.
MULTITENANT_ENABLED = os.environ.get("MULTITENANT_ENABLED", "false").lower() == "true"
TENANTS_TABLE = os.environ.get("TENANTS_TABLE", "tenants")

# Per-record, not per-invocation: one SQS batch can carry messages from
# different workspaces, so a value cached at module scope would post one
# tenant's reply using another tenant's token. A ContextVar is used rather
# than a plain global because it is the construct built for this, and it
# stays correct if the worker is ever made concurrent.
_tenant_ctx = contextvars.ContextVar("tenant_ctx", default=None)

_tenants_table = None
_tenant_kms_client = None


def current_slack_token():
    """Bot token for the record being processed.

    Falls back to the single-workspace env var whenever no tenant resolved —
    which is every message until workspaces start onboarding.
    """
    tenant = _tenant_ctx.get()
    if tenant and tenant.get("bot_token"):
        return tenant["bot_token"]
    return SLACK_BOT_TOKEN


def current_lex_config():
    """(botId, botAliasId, localeId) for the record being processed."""
    answering = (_tenant_ctx.get() or {}).get("answering") or {}
    return (
        answering.get("lex_bot_id") or BOT_ID,
        answering.get("lex_bot_alias_id") or BOT_ALIAS_ID,
        answering.get("lex_locale_id") or LOCALE_ID,
    )


def with_tenant_token(payload):
    """Stamp the current tenant's bot token onto a downstream invoke payload.

    Lambdas we invoke (summarizer, live agent) post to Slack themselves, so
    they need the same per-tenant token this record is being handled with.
    Passing it on the payload keeps the DynamoDB lookup and KMS decrypt in
    one place here, rather than every downstream function re-implementing
    resolution and needing its own IAM.

    Adds nothing when no tenant is in context, so today's payloads are
    unchanged and the downstream env-var fallback keeps applying.
    """
    tenant = _tenant_ctx.get()
    if not tenant or not tenant.get("bot_token"):
        return payload
    if not isinstance(payload, dict):
        return payload
    return {**payload, "slack_bot_token": tenant["bot_token"]}


def _decrypt_tenant_bot_token(tenant):
    """Decrypt a tenant's Slack bot token.

    Mirrors slack_tenant_app/app/crypto.py. The encryption context must match
    the writer's exactly or KMS refuses to decrypt — that is what prevents one
    tenant's ciphertext being replayed as another's, so it is a security
    control, not a formality.
    """
    stored = tenant.get("bot_access_token")
    if not isinstance(stored, dict) or stored.get("__enc__") != "kms.v1":
        # Written before encryption existed, or absent.
        return stored if isinstance(stored, str) else None

    global _tenant_kms_client
    if _tenant_kms_client is None:
        _tenant_kms_client = boto3.client("kms", region_name=AWS_REGION)

    resp = _tenant_kms_client.decrypt(
        CiphertextBlob=base64.b64decode(stored["data"]),
        EncryptionContext={
            "tenant_id": tenant["tenant_id"],
            "purpose": "connector-oauth-token",
        },
    )
    return json.loads(resp["Plaintext"].decode("utf-8")).get("token")


def resolve_tenant(slack_tenant):
    """Resolve an incoming message to its tenant, or None for the env-var path.

    Never raises. A lookup failure falls back to existing single-workspace
    behaviour, which is strictly better than dropping the message — the
    caller cannot do anything useful with an exception here.
    """
    if not MULTITENANT_ENABLED:
        return None

    team_id = (slack_tenant or {}).get("team_id")
    if not team_id:
        return None

    try:
        global _tenants_table
        if _tenants_table is None:
            _tenants_table = boto3.resource(
                "dynamodb", region_name=AWS_REGION).Table(TENANTS_TABLE)

        items = _tenants_table.query(
            IndexName="slack_team_id-index",
            KeyConditionExpression="slack_team_id = :t",
            ExpressionAttributeValues={":t": team_id},
        ).get("Items") or []
        if not items:
            return None

        tenant = items[0]
        return {
            "tenant_id": tenant.get("tenant_id"),
            "status": tenant.get("status", "active"),
            "answering": tenant.get("answering") or {},
            "bot_token": _decrypt_tenant_bot_token(tenant),
        }
    except Exception as exc:
        log_json({
            "level": "ERROR",
            "message": "tenant_resolution_failed",
            "team_id": team_id,
            "error": exc.__class__.__name__,
        })
        return None
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "86400"))
INACTIVITY_TIMEOUT_SECONDS = int(os.environ.get("INACTIVITY_TIMEOUT_SECONDS", "30"))
TIMEOUT_SCHEDULING_ENABLED = os.environ.get("TIMEOUT_SCHEDULING_ENABLED", "true").lower() == "true"
TIMEOUT_HANDLER_ARN = os.environ.get("TIMEOUT_HANDLER_ARN")
SCHEDULER_ROLE_ARN = os.environ.get("SCHEDULER_ROLE_ARN")
SCHEDULER_GROUP_NAME = os.environ.get("SCHEDULER_GROUP_NAME", "default")
SCHEDULER_NAME_PREFIX = os.environ.get("SCHEDULER_NAME_PREFIX", "o3-slack-timeout")
ENABLE_CLAUDE_FALLBACK = os.environ.get("ENABLE_CLAUDE_FALLBACK", "false").lower() == "true"
AUTO_CLAUDE_FALLBACK_ENABLED = os.environ.get("AUTO_CLAUDE_FALLBACK_ENABLED", "false").lower() == "true"
CLAUDE_FALLBACK_FUNCTION = os.environ.get("CLAUDE_FALLBACK_FUNCTION")
CLAUDE_FALLBACK_INTENTS = {
    intent_name.strip()
    for intent_name in os.environ.get(
        "CLAUDE_FALLBACK_INTENTS",
        "FallbackIntent,AMAZON.FallbackIntent,FallbackToLLM"
    ).split(",")
    if intent_name.strip()
}
CLAUDE_FAILURE_REPLY = os.environ.get(
    "CLAUDE_FAILURE_REPLY",
    "I could not resolve this automatically. Do you want me to create a Jira ticket? Reply yes to create it, or no to cancel."
)
CREATE_JIRA_TICKET_FUNCTION = os.environ.get("CREATE_JIRA_TICKET_FUNCTION")
ENABLE_CREATE_JIRA_TICKET = os.environ.get("ENABLE_CREATE_JIRA_TICKET", "true").lower() == "true"
ENABLE_CLOSE_SUMMARY = (
    os.environ.get("ENABLE_CLOSE_SUMMARY")
    or os.environ.get("ENABLE_SUMMARIZATION")
    or "true"
).lower() == "true"
ENABLE_ROVO_ENRICHMENT = os.environ.get("ENABLE_ROVO_ENRICHMENT", "false").lower() == "true"
ROVO_ENRICHMENT_FUNCTION = os.environ.get("ROVO_ENRICHMENT_FUNCTION")
ENABLE_MCP_ASSIST = os.environ.get("ENABLE_MCP_ASSIST", "false").lower() == "true"
MCP_ASSIST_FUNCTION_NAME = os.environ.get("MCP_ASSIST_FUNCTION_NAME")
MCP_QUEUE_URL = os.environ.get("MCP_QUEUE_URL")
MCP_ASSIST_PROGRESS_REPLY = os.environ.get(
    "MCP_ASSIST_PROGRESS_REPLY",
    "Searching approved Jira, Confluence, SharePoint, and Slack knowledge sources..."
)
MCP_ASSIST_DETAILS_PROMPT_TEXT = os.environ.get(
    "MCP_ASSIST_DETAILS_PROMPT_TEXT",
    "What should I search for in company knowledge? Send the exact issue, error, or topic."
)
MCP_ASSIST_NOT_CONFIGURED_REPLY = os.environ.get(
    "MCP_ASSIST_NOT_CONFIGURED_REPLY",
    "Company knowledge search is not configured yet."
)
MCP_ASSIST_FAILED_REPLY = os.environ.get(
    "MCP_ASSIST_FAILED_REPLY",
    "I could not start the company knowledge search right now."
)
LIVE_AGENT_FUNCTION = os.environ.get("LIVE_AGENT_FUNCTION")
LIVE_AGENT_WEBHOOK_URL = os.environ.get("LIVE_AGENT_WEBHOOK_URL") or os.environ.get("AUTOMATION_WEBHOOK_URL")
LIVE_AGENT_CONFIG_TABLE = os.environ.get("LIVE_AGENT_CONFIG_TABLE") or os.environ.get("CONFIG_TABLE")
LIVE_AGENT_CONFIG_INTENT = os.environ.get("LIVE_AGENT_CONFIG_INTENT", "LiveAgent")
LIVE_AGENT_DEFAULT_REQUEST_TYPE = os.environ.get("LIVE_AGENT_DEFAULT_REQUEST_TYPE", "live_agent")
LIVE_AGENT_DEFAULT_BRANCHING = os.environ.get("LIVE_AGENT_DEFAULT_BRANCHING", "live_agent")
LIVE_AGENT_WEBHOOK_TIMEOUT_SECONDS = int(os.environ.get("LIVE_AGENT_WEBHOOK_TIMEOUT_SECONDS", "10"))
ENABLE_REQUEST_AI_SUMMARY = os.environ.get("ENABLE_REQUEST_AI_SUMMARY", "false").lower() == "true"
REQUEST_SUMMARY_MODEL_ID = os.environ.get("REQUEST_SUMMARY_MODEL_ID", "global.amazon.nova-2-lite-v1:0")
REQUEST_SUMMARY_MAX_TOKENS = int(os.environ.get("REQUEST_SUMMARY_MAX_TOKENS", "180"))
REQUEST_SUMMARY_TEMPERATURE = float(os.environ.get("REQUEST_SUMMARY_TEMPERATURE", "0.1"))
SUMMARIZER_FUNCTION_NAME = os.environ.get("SUMMARIZER_FUNCTION_NAME")
SUMMARIZER_INVOKE_TIMEOUT_SECONDS = int(os.environ.get("SUMMARIZER_INVOKE_TIMEOUT_SECONDS", "25"))
IMAGE_REK_FUNCTION = os.environ.get("IMAGE_REK_FUNCTION")
IMAGE_ANALYSIS_UNAVAILABLE_REPLY = os.environ.get(
    "IMAGE_ANALYSIS_UNAVAILABLE_REPLY",
    "I received the image, but image analysis is not configured yet."
)
SCREENSHOT_MATCH_ENABLED = os.environ.get("SCREENSHOT_MATCH_ENABLED", "true").lower() == "true"
SCREENSHOT_ISSUE_TABLE = os.environ.get("SCREENSHOT_ISSUE_TABLE", "o3_screenshot_issue_kb")
SCREENSHOT_VECTOR_ENDPOINT = os.environ.get("SCREENSHOT_VECTOR_ENDPOINT", "").rstrip("/")
SCREENSHOT_VECTOR_BACKEND = os.environ.get("SCREENSHOT_VECTOR_BACKEND").lower()
SCREENSHOT_VECTOR_INDEX = os.environ.get("SCREENSHOT_VECTOR_INDEX", "o3-screenshot-issues")
SCREENSHOT_VECTOR_FIELD = os.environ.get("SCREENSHOT_VECTOR_FIELD", "image_vector")
SCREENSHOT_OPENSEARCH_SERVICE = os.environ.get("SCREENSHOT_OPENSEARCH_SERVICE", "aoss")
SCREENSHOT_EMBEDDING_MODEL_ID = os.environ.get("SCREENSHOT_EMBEDDING_MODEL_ID", "amazon.titan-embed-image-v1")
SCREENSHOT_MATCH_THRESHOLD = float(os.environ.get("SCREENSHOT_MATCH_THRESHOLD", "0.80"))
SCREENSHOT_VECTOR_K = int(os.environ.get("SCREENSHOT_VECTOR_K", "1"))
SCREENSHOT_EMBEDDING_MAX_BYTES = int(os.environ.get("SCREENSHOT_EMBEDDING_MAX_BYTES", "5000000"))
BEDROCK_KNOWLEDGE_BASE_ID = os.environ.get("BEDROCK_KNOWLEDGE_BASE_ID")
BEDROCK_KB_MODEL_ARN = os.environ.get("BEDROCK_KB_MODEL_ARN")
ENABLE_BEDROCK_KB_ASSIST = os.environ.get("ENABLE_BEDROCK_KB_ASSIST", "true").lower() == "true"
BEDROCK_KB_INTENT_NAME = os.environ.get("BEDROCK_KB_INTENT_NAME", "KBAtlassianAssist")
BEDROCK_KB_NUMBER_OF_RESULTS = int(os.environ.get("BEDROCK_KB_NUMBER_OF_RESULTS", "5"))
BEDROCK_KB_NO_ANSWER_MARKERS = [
    marker.strip().lower()
    for marker in os.environ.get(
        "BEDROCK_KB_NO_ANSWER_MARKERS",
        "no relevant information,no kb article found,i don't know,i do not know"
    ).split(",")
    if marker.strip()
]
# Local (xlsx-derived) knowledge base — a temporary stand-in for a Bedrock KB so the
# L2 "Explore Knowledge base" layer works without provisioning a vector store. Backed
# by a JSON export of All_Intents_Full_Export.xlsx stored in S3, loaded at cold start
# and keyword-searched in-memory. Set ENABLE_LOCAL_KB=true to turn it on.
ENABLE_LOCAL_KB = os.environ.get("ENABLE_LOCAL_KB", "false").lower() == "true"
LOCAL_KB_S3_BUCKET = os.environ.get("LOCAL_KB_S3_BUCKET", "o3-ivy-kb-docs-661779458398")
LOCAL_KB_S3_KEY = os.environ.get("LOCAL_KB_S3_KEY", "intent_kb.json")
# Minimum keyword-overlap score (fraction of query tokens matched) to count as a hit.
LOCAL_KB_MIN_SCORE = float(os.environ.get("LOCAL_KB_MIN_SCORE", "0.30"))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_TIMEOUT_SECONDS = int(os.environ.get("GEMINI_TIMEOUT_SECONDS", "20"))
# Mistral (OpenAI-compatible chat API). The image screenshot->resolution step
# uses this instead of Gemini when IMAGE_LLM_PROVIDER=mistral (the default).
MIA_KEY = os.environ.get("MIA_KEY")
MISTRAL_MODEL = os.environ.get("MISTRAL_MODEL", "mistral-large-latest")
MISTRAL_ENDPOINT = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_TIMEOUT_SECONDS = int(os.environ.get("MISTRAL_TIMEOUT_SECONDS", "20"))
IMAGE_LLM_PROVIDER = os.environ.get("IMAGE_LLM_PROVIDER", "mistral").strip().lower()
# Escalation ladder (flag-gated, OFF by default). When on, each answer carries a
# "Not helpful -> try another way" button that advances L1 Lex -> L2 KB -> L3 LLM
# -> L4 ticket; layers that aren't provisioned (no KB, ticketing off) are skipped.
ENABLE_ESCALATION_LADDER = os.environ.get("ENABLE_ESCALATION_LADDER", "false").lower() == "true"
# L3 provider — replaceable per the requirement. claude (default) | mistral | gemini.
LADDER_LLM_PROVIDER = os.environ.get("LADDER_LLM_PROVIDER", "claude").strip().lower()
# Escalation button labels (env-overridable) and the human-handoff terminal option.
ESCALATE_BUTTON_TEXT = os.environ.get("ESCALATE_BUTTON_TEXT", "Try another solution")
# Distinct label for the L1 -> L2 step so the knowledge-base layer is visibly separate.
KB_ESCALATE_BUTTON_TEXT = os.environ.get("KB_ESCALATE_BUTTON_TEXT", "Explore Knowledge base")
LIVE_AGENT_BUTTON_TEXT = os.environ.get("LIVE_AGENT_BUTTON_TEXT", "Talk to a support agent")
# Shown when Lex (L1) can't answer, inviting the user to search the knowledge base (L2).
LEX_NO_ANSWER_EXPLORE_KB_TEXT = os.environ.get(
    "LEX_NO_ANSWER_EXPLORE_KB_TEXT",
    "I couldn't find a direct answer for that. Want me to search the knowledge base?"
)
# When the automated layers are exhausted, offer a live agent instead of a dead end.
ENABLE_LIVE_AGENT_ESCALATION = os.environ.get("ENABLE_LIVE_AGENT_ESCALATION", "true").lower() == "true"
# LLM chat mode: once the ladder reaches L3 (LLM), let the user keep chatting with the
# LLM for follow-up turns (with conversation context) instead of re-routing each message
# to Lex. Capped at LLM_CHAT_MAX_MESSAGES turns; a live agent is reachable at any point.
ENABLE_LLM_CHAT = os.environ.get("ENABLE_LLM_CHAT", "true").lower() == "true"
LLM_CHAT_MAX_MESSAGES = int(os.environ.get("LLM_CHAT_MAX_MESSAGES", "50"))
LLM_CHAT_HISTORY_MAX_CHARS = int(os.environ.get("LLM_CHAT_HISTORY_MAX_CHARS", "6000"))
LLM_CHAT_LIMIT_REPLY = os.environ.get(
    "LLM_CHAT_LIMIT_REPLY",
    "We've gone back and forth quite a few times on this. Let me hand you to a support "
    "agent who can take it from here — tap the button below."
)
JIRA_UNCLEAR_CONFIRMATION_REPLY = os.environ.get(
    "JIRA_UNCLEAR_CONFIRMATION_REPLY",
    "Please reply yes to create the Jira ticket, or no to cancel."
)
JIRA_CANCELLED_REPLY = os.environ.get(
    "JIRA_CANCELLED_REPLY",
    "Cancelled. I did not create a Jira ticket."
)
JIRA_CREATE_FAILED_REPLY = os.environ.get(
    "JIRA_CREATE_FAILED_REPLY",
    "I could not create the Jira ticket. Please try again later or contact support."
)
JIRA_CREATE_CONFIG_FAILED_REPLY = os.environ.get(
    "JIRA_CREATE_CONFIG_FAILED_REPLY",
    "Jira ticket creation is not configured correctly."
)
JIRA_CREATE_PERMISSION_FAILED_REPLY = os.environ.get(
    "JIRA_CREATE_PERMISSION_FAILED_REPLY",
    "I could not create the Jira ticket because Jira rejected the request. Please check Jira permissions or project settings."
)
JIRA_CREATE_TIMEOUT_REPLY = os.environ.get(
    "JIRA_CREATE_TIMEOUT_REPLY",
    "Jira did not respond in time. Please try again later."
)
JIRA_CREATE_IN_PROGRESS_REPLY = os.environ.get(
    "JIRA_CREATE_IN_PROGRESS_REPLY",
    "Jira ticket creation is already in progress. Please wait a moment."
)
JIRA_CREATE_STALE_REPLY = os.environ.get(
    "JIRA_CREATE_STALE_REPLY",
    "A Jira ticket creation was already started but did not finish cleanly. I will not create a second ticket automatically. Please check Jira or contact support."
)
JIRA_CREATING_STALE_SECONDS = int(os.environ.get("JIRA_CREATING_STALE_SECONDS", "300"))
JIRA_CREATED_DUPLICATE_WINDOW_SECONDS = int(os.environ.get("JIRA_CREATED_DUPLICATE_WINDOW_SECONDS", "600"))
EMPTY_USER_TEXT_REPLY = os.environ.get("EMPTY_USER_TEXT_REPLY", "Hi, how can I help?")
EMPTY_LEX_REPLY = os.environ.get(
    "EMPTY_LEX_REPLY",
    "I could not generate a response for that. Please try rephrasing your message."
)
LEX_ASSISTANCE_PROMPT_TEXT = os.environ.get(
    "LEX_ASSISTANCE_PROMPT_TEXT",
    "Was this helpful?"
)
LEX_ASSISTANCE_CLOSED_REPLY = os.environ.get(
    "LEX_ASSISTANCE_CLOSED_REPLY",
    "Okay, I will close this for now."
)
LEX_ASSISTANCE_DETAILS_PROMPT_TEXT = os.environ.get(
    "LEX_ASSISTANCE_DETAILS_PROMPT_TEXT",
    "What still failed? Please include the error message or the step where you are blocked."
)
CLAUDE_FINAL_ACTION_PROMPT_TEXT = os.environ.get(
    "CLAUDE_FINAL_ACTION_PROMPT_TEXT",
    "Would you like live agent support or a Jira ticket?"
)
CLAUDE_UNRESOLVED_REPLY = os.environ.get(
    "CLAUDE_UNRESOLVED_REPLY",
    "I could not resolve this automatically."
)
LIVE_AGENT_DEFERRED_REPLY = os.environ.get(
    "LIVE_AGENT_DEFERRED_REPLY",
    "I have sent this to live agent support. Someone from the support team will follow up."
)
LIVE_AGENT_FAILED_REPLY = os.environ.get(
    "LIVE_AGENT_FAILED_REPLY",
    "I could not send this to live agent support. Please try again later or create a Jira ticket."
)
ATLASSIAN_DOMAIN = os.environ.get("ATLASSIAN_DOMAIN", "").rstrip("/")
ATLASSIAN_EMAIL = os.environ.get("ATLASSIAN_EMAIL", "")
ATLASSIAN_API_TOKEN = os.environ.get("ATLASSIAN_API_TOKEN", "")
JSM_COMMENT_PUBLIC = os.environ.get("JSM_COMMENT_PUBLIC", "true").lower() == "true"
LIVE_AGENT_SUPPORT_MODE = os.environ.get("LIVE_AGENT_SUPPORT_MODE", "").strip().lower()
LIVE_AGENT_SUPPORT_CHANNEL_ID = os.environ.get("LIVE_AGENT_SUPPORT_CHANNEL_ID", "").strip()
LIVE_AGENT_SYNC_AGENT_REPLIES_TO_JSM = (
    os.environ.get("LIVE_AGENT_SYNC_AGENT_REPLIES_TO_JSM", "true").lower() == "true"
)
LIVE_AGENT_START_STATUS_NAMES = [
    name.strip()
    for name in os.environ.get("LIVE_AGENT_START_STATUS_NAMES", "In Progress").split(",")
    if name.strip()
]
LIVE_AGENT_BLOCK_REPLY_ON_START_TRANSITION_FAILURE = (
    os.environ.get("LIVE_AGENT_BLOCK_REPLY_ON_START_TRANSITION_FAILURE", "true").lower() == "true"
)
MS_GRAPH_FEEDBACK_SYNC_ENABLED = os.environ.get("MS_GRAPH_FEEDBACK_SYNC_ENABLED", "false").lower() == "true"
MS_GRAPH_TENANT_ID = os.environ.get("MS_GRAPH_TENANT_ID", "").strip()
MS_GRAPH_CLIENT_ID = os.environ.get("MS_GRAPH_CLIENT_ID", "").strip()
MS_GRAPH_CLIENT_SECRET_ID = os.environ.get("MS_GRAPH_CLIENT_SECRET_ID", "").strip()
MS_GRAPH_SHAREPOINT_SITE_ID = os.environ.get("MS_GRAPH_SHAREPOINT_SITE_ID", "").strip()
MS_GRAPH_SHAREPOINT_FEEDBACK_LIST_ID = os.environ.get("MS_GRAPH_SHAREPOINT_FEEDBACK_LIST_ID", "").strip()
MS_GRAPH_TIMEOUT_SECONDS = int(os.environ.get("MS_GRAPH_TIMEOUT_SECONDS", "10"))
MS_GRAPH_MAX_ATTEMPTS = max(1, int(os.environ.get("MS_GRAPH_MAX_ATTEMPTS", "2")))
MS_GRAPH_RETRY_DELAY_SECONDS = float(os.environ.get("MS_GRAPH_RETRY_DELAY_SECONDS", "1"))
MS_GRAPH_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

_ms_graph_client_secret_cache = None
_ms_graph_token_cache = None

NEXT_ACTION_CREATE_JIRA_TICKET = "O3_CreateJiraTicket"
NEXT_ACTION_CLAUDE_ASSISTANCE = "O3_ClaudeFurtherAssistance"
NEXT_ACTION_FINAL_SUPPORT_OPTIONS = "O3_FinalSupportOptions"
NEXT_ACTION_LIVE_AGENT_SUPPORT = "O3_LiveAgentSupport"
NEXT_ACTION_MCP_ASSIST = "O3_McpAssist"

ACTION_ID_ASSISTANCE_YES = "ivy_assistance_yes"
ACTION_ID_ASSISTANCE_NO = "ivy_assistance_no"
ACTION_ID_ASSISTANCE_SOLVED = "ivy_assistance_solved"
ACTION_ID_ASSISTANCE_NEED_MORE_HELP = "ivy_assistance_need_more_help"
ACTION_ID_ASSISTANCE_CREATE_JIRA_TICKET = "ivy_assistance_create_jira_ticket"
ACTION_ID_MCP_ASSIST = "o3_request_mcp_assist"
ACTION_ID_CLAUDE_ASSIST = "o3_request_claude_assist"
ACTION_ID_LIVE_AGENT_SUPPORT = "ivy_live_agent_support"
ACTION_ID_LIVE_AGENT_REPLY = "ivy_live_agent_reply"
ACTION_ID_LIVE_AGENT_RESOLVE = "ivy_live_agent_resolve"
ACTION_ID_LIVE_AGENT_CANCEL = "ivy_live_agent_cancel"
ACTION_ID_LIVE_AGENT_REASSIGN = "ivy_live_agent_reassign"
ACTION_ID_CREATE_JIRA_TICKET = "ivy_create_jira_ticket"
ACTION_ID_CLOSE_AND_SUMMARIZE = "ivy_close_and_summarize"
ACTION_ID_FEEDBACK_RATING = "ivy_feedback_rating"
ACTION_ID_ESCALATE = "ivy_escalate"

SESSION_STATE_OPEN = "OPEN"
SESSION_STATE_COLLECTING_DETAILS = "COLLECTING_DETAILS"
SESSION_STATE_WAITING_FOR_USER = "WAITING_FOR_USER"
SESSION_STATE_SUMMARIZING = "SUMMARIZING"
SESSION_STATE_CLOSED = "CLOSED"
SESSION_STATE_FAILED = "FAILED"

GREETING_ONLY_TEXTS = {
    "hi",
    "hello",
    "hey",
    "good morning",
    "good afternoon",
    "good evening",
    "test",
}

CLOSE_SUMMARY_RESPONSE_SOURCES = {
    "lex",
    "router",
    "claude",
    "image",
    "image_screenshot_match_lex",
    "image_bedrock_kb",
    "image_gemini",
    "image_mistral",
}

JIRA_CONFIRM_YES = {
    "yes",
    "y",
    "yeah",
    "yep",
    "confirm",
    "create",
    "create it",
    "please create",
    "ok",
    "okay",
    "sure"
}
JIRA_CONFIRM_NO = {
    "no",
    "n",
    "nope",
    "cancel",
    "stop",
    "do not create",
    "dont create",
    "don't create"
}

sessions_table = dynamodb.Table(DYNAMODB_TABLE)
screenshot_issue_table = dynamodb.Table(SCREENSHOT_ISSUE_TABLE) if SCREENSHOT_ISSUE_TABLE else None
live_agent_config_table = dynamodb.Table(LIVE_AGENT_CONFIG_TABLE) if LIVE_AGENT_CONFIG_TABLE else None


def to_iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def ttl_epoch():
    return int(time.time()) + SESSION_TTL_SECONDS


def parse_iso_datetime(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)

    except ValueError:
        return None


def make_jira_request_id(session_id, event_id, intent_name, request_text):
    raw = "|".join([
        session_id or "",
        event_id or "",
        intent_name or "",
        request_text or ""
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def log_json(data):
    print(json.dumps(data, default=str))


def text_or_empty(value):
    if value is None:
        return ""

    return str(value).strip()


def value_is_false(value):
    return str(value).strip().lower() in {"false", "0", "no", "disabled"}


def send_slack_message(channel, text, blocks=None, thread_ts=None):
    url = "https://slack.com/api/chat.postMessage"

    message = {
        "channel": channel,
        "text": text
    }

    if thread_ts:
        message["thread_ts"] = thread_ts

    if blocks:
        message["blocks"] = blocks

    data = json.dumps(message).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {current_slack_token()}"
        },
        method="POST"
    )

    with urllib.request.urlopen(req, timeout=10) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise Exception(f"Slack API error: {result.get('error')}")

    return result


def update_slack_message(channel, ts, text, blocks=None):
    payload = {
        "channel": channel,
        "ts": ts,
        "text": text,
    }
    if blocks:
        payload["blocks"] = blocks
    else:
        payload["blocks"] = []

    return slack_api("chat.update", payload=payload)


def send_slack_ephemeral(channel, user, text, thread_ts=None):
    if not channel or not user:
        return None

    payload = {
        "channel": channel,
        "user": user,
        "text": text,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts

    return slack_api("chat.postEphemeral", payload=payload)


def post_processing_message(channel, text, thread_ts=None):
    if not channel:
        return None

    try:
        return send_slack_message(channel, text, thread_ts=thread_ts)
    except Exception as error:
        log_json({
            "level": "WARN",
            "message": "processing_message_post_failed",
            "channel": channel,
            "thread_ts": thread_ts,
            "error": str(error),
        })
        return None


def update_processing_message(processing_message, text, blocks=None):
    if not processing_message:
        return None

    channel = processing_message.get("channel")
    ts = processing_message.get("ts")
    if not channel or not ts:
        return None

    try:
        return update_slack_message(channel, ts, text, blocks)
    except Exception as error:
        log_json({
            "level": "WARN",
            "message": "processing_message_update_failed",
            "channel": channel,
            "ts": ts,
            "error": str(error),
        })
        return None


def maybe_send_ephemeral(channel, user, text, thread_ts=None):
    try:
        return send_slack_ephemeral(channel, user, text, thread_ts)
    except Exception as error:
        log_json({
            "level": "WARN",
            "message": "ephemeral_processing_message_failed",
            "channel": channel,
            "user": user,
            "thread_ts": thread_ts,
            "error": str(error),
        })
        return None


def interactive_processing_text(action_id):
    if action_id in {ACTION_ID_CREATE_JIRA_TICKET, ACTION_ID_ASSISTANCE_CREATE_JIRA_TICKET}:
        return "Creating Jira ticket..."
    if action_id == ACTION_ID_MCP_ASSIST:
        return None
    if action_id == ACTION_ID_CLAUDE_ASSIST:
        return "Asking Claude..."
    if action_id == ACTION_ID_LIVE_AGENT_SUPPORT:
        return "Connecting you to a live agent..."
    if action_id == ACTION_ID_CLOSE_AND_SUMMARIZE:
        return "Closing and summarizing this session..."
    return None


def support_control_processing_text(action_id):
    if action_id == ACTION_ID_LIVE_AGENT_RESOLVE:
        return "Resolving..."
    if action_id == ACTION_ID_LIVE_AGENT_CANCEL:
        return "Cancelling..."
    if action_id == ACTION_ID_LIVE_AGENT_REASSIGN:
        return "Reassigning..."
    return None


def atlassian_auth_header():
    if not (ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN):
        raise ValueError("Missing ATLASSIAN_EMAIL or ATLASSIAN_API_TOKEN")

    token = base64.b64encode(f"{ATLASSIAN_EMAIL}:{ATLASSIAN_API_TOKEN}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def add_jsm_request_comment(ticket_key, body, public=True):
    if not ATLASSIAN_DOMAIN:
        raise ValueError("Missing ATLASSIAN_DOMAIN")

    safe_ticket_key = urllib.parse.quote(str(ticket_key), safe="")
    url = f"{ATLASSIAN_DOMAIN}/rest/servicedeskapi/request/{safe_ticket_key}/comment"
    payload = {
        "body": body,
        "public": bool(public),
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": atlassian_auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=10) as response:
        response_text = response.read().decode("utf-8").strip()

    if response_text:
        try:
            return json.loads(response_text)
        except ValueError:
            return {"message": response_text}

    return {"ok": True}


def jira_adf_doc(text):
    lines = str(text or "").splitlines() or [""]
    return {
        "version": 1,
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": line or " "}],
            }
            for line in lines
        ],
    }


def add_jira_issue_comment(issue_key, body):
    if not ATLASSIAN_DOMAIN:
        raise ValueError("Missing ATLASSIAN_DOMAIN")

    safe_issue_key = urllib.parse.quote(str(issue_key), safe="")
    url = f"{ATLASSIAN_DOMAIN}/rest/api/3/issue/{safe_issue_key}/comment"
    payload = {"body": jira_adf_doc(body)}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": atlassian_auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=10) as response:
        response_text = response.read().decode("utf-8").strip()

    if response_text:
        try:
            return json.loads(response_text)
        except ValueError:
            return {"message": response_text}

    return {"ok": True}


def get_ms_graph_client_secret():
    global _ms_graph_client_secret_cache

    if _ms_graph_client_secret_cache:
        return _ms_graph_client_secret_cache

    if not MS_GRAPH_CLIENT_SECRET_ID:
        raise ValueError("Missing MS_GRAPH_CLIENT_SECRET_ID")

    response = secretsmanager.get_secret_value(SecretId=MS_GRAPH_CLIENT_SECRET_ID)
    secret_string = response.get("SecretString") or ""
    if not secret_string:
        raise ValueError("Microsoft Graph client secret must be stored as SecretString")

    try:
        secret_json = json.loads(secret_string)
        secret_value = (
            secret_json.get("client_secret")
            or secret_json.get("clientSecret")
            or secret_json.get("secret")
            or secret_json.get("value")
        ) if isinstance(secret_json, dict) else None
    except ValueError:
        secret_value = secret_string

    secret_value = text_or_empty(secret_value)
    if not secret_value:
        raise ValueError("Microsoft Graph client secret is empty")

    _ms_graph_client_secret_cache = secret_value
    return secret_value


def ms_graph_config_ready():
    return all([
        MS_GRAPH_TENANT_ID,
        MS_GRAPH_CLIENT_ID,
        MS_GRAPH_CLIENT_SECRET_ID,
        MS_GRAPH_SHAREPOINT_SITE_ID,
        MS_GRAPH_SHAREPOINT_FEEDBACK_LIST_ID,
    ])


def ms_graph_http_json(url, method="GET", headers=None, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request_headers = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        request_headers["Content-Type"] = "application/json"

    last_error = None
    for attempt in range(1, MS_GRAPH_MAX_ATTEMPTS + 1):
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=MS_GRAPH_TIMEOUT_SECONDS) as response:
                response_text = response.read().decode("utf-8").strip()
                return {
                    "ok": 200 <= response.status < 300,
                    "status_code": response.status,
                    "body": json.loads(response_text) if response_text else None,
                    "attempts": attempt,
                }
        except urllib.error.HTTPError as error:
            retryable = error.code in MS_GRAPH_RETRYABLE_STATUS_CODES
            try:
                error_text = error.read().decode("utf-8").strip()
            except Exception:
                error_text = ""
            last_error = {
                "ok": False,
                "status_code": error.code,
                "error": error_text or error.reason,
                "error_code": "ms_graph_http_error",
                "retryable": retryable,
                "attempts": attempt,
            }
            if not retryable or attempt >= MS_GRAPH_MAX_ATTEMPTS:
                return last_error
            retry_after = error.headers.get("Retry-After") if error.headers else None
            try:
                delay = float(retry_after) if retry_after is not None else MS_GRAPH_RETRY_DELAY_SECONDS
            except ValueError:
                delay = MS_GRAPH_RETRY_DELAY_SECONDS
            time.sleep(max(0, delay))
        except (urllib.error.URLError, TimeoutError) as error:
            return {
                "ok": False,
                "status_code": None,
                "error": str(error),
                "error_code": "ms_graph_network_error",
                "retryable": False,
                "attempts": attempt,
            }

    return last_error or {"ok": False, "error": "Microsoft Graph request failed", "error_code": "ms_graph_request_failed"}


def ms_graph_access_token():
    global _ms_graph_token_cache

    now = int(time.time())
    if _ms_graph_token_cache and _ms_graph_token_cache.get("expires_at", 0) > now + 60:
        return _ms_graph_token_cache["access_token"]

    if not (MS_GRAPH_TENANT_ID and MS_GRAPH_CLIENT_ID):
        raise ValueError("Missing Microsoft Graph tenant/client configuration")

    token_url = f"https://login.microsoftonline.com/{urllib.parse.quote(MS_GRAPH_TENANT_ID, safe='')}/oauth2/v2.0/token"
    form = urllib.parse.urlencode({
        "client_id": MS_GRAPH_CLIENT_ID,
        "client_secret": get_ms_graph_client_secret(),
        "grant_type": "client_credentials",
        "scope": "https://graph.microsoft.com/.default",
    }).encode("utf-8")
    request = urllib.request.Request(
        token_url,
        data=form,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=MS_GRAPH_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            error_text = error.read().decode("utf-8").strip()
        except Exception:
            error_text = ""
        raise RuntimeError(f"Microsoft Graph token request failed: HTTP {error.code} {error_text}") from error

    access_token = text_or_empty(body.get("access_token"))
    if not access_token:
        raise ValueError("Microsoft Graph token response did not include access_token")

    _ms_graph_token_cache = {
        "access_token": access_token,
        "expires_at": now + int(body.get("expires_in") or 3600),
    }
    return access_token


def feedback_source(metadata):
    source = text_or_empty((metadata or {}).get("feedback_source") or (metadata or {}).get("source"))
    if source:
        return source
    return "live_agent" if (metadata or {}).get("comment_public") is False else "summary"


def sharepoint_feedback_fields(session_id, rating, feedback_text, user, user_name, channel, ticket_key, now_iso, metadata, comment_result):
    return {
        "Title": f"IVY feedback - {ticket_key or session_id or 'unknown'}",
        "SubmittedAt": now_iso,
        "Rating": int(rating),
        "RatingStars": feedback_stars(rating),
        "FeedbackText": feedback_text or "",
        "SlackUser": user or "",
        "SlackUserName": user_name or "",
        "SlackChannel": channel or "",
        "SessionId": session_id or "",
        "JiraTicketKey": ticket_key or "",
        "CommentPublic": feedback_comment_is_public(metadata),
        "FeedbackSource": feedback_source(metadata),
        "JiraCommentStatus": "posted" if (comment_result or {}).get("ok") else "not_posted",
        "DynamoStatus": "stored",
    }


def create_sharepoint_feedback_item(fields):
    if not MS_GRAPH_FEEDBACK_SYNC_ENABLED:
        return {"ok": False, "skipped": True, "status": "skipped", "reason": "graph_sync_disabled"}
    if not ms_graph_config_ready():
        return {"ok": False, "skipped": True, "status": "skipped", "reason": "graph_config_incomplete"}

    token = ms_graph_access_token()
    safe_site_id = urllib.parse.quote(MS_GRAPH_SHAREPOINT_SITE_ID, safe="")
    safe_list_id = urllib.parse.quote(MS_GRAPH_SHAREPOINT_FEEDBACK_LIST_ID, safe="")
    result = ms_graph_http_json(
        f"https://graph.microsoft.com/v1.0/sites/{safe_site_id}/lists/{safe_list_id}/items",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
        body={"fields": fields},
    )
    if not result.get("ok"):
        return {
            **result,
            "status": "failed",
            "error_code": result.get("error_code") or "sharepoint_feedback_write_failed",
        }

    body = result.get("body") or {}
    return {
        "ok": True,
        "status": "posted",
        "item_id": body.get("id"),
        "web_url": body.get("webUrl"),
        "status_code": result.get("status_code"),
        "attempts": result.get("attempts"),
    }


def sync_feedback_to_sharepoint(session_id, rating, feedback_text, user, user_name, channel, ticket_key, now_iso, metadata, comment_result):
    fields = sharepoint_feedback_fields(
        session_id,
        rating,
        feedback_text,
        user,
        user_name,
        channel,
        ticket_key,
        now_iso,
        metadata,
        comment_result,
    )
    try:
        result = create_sharepoint_feedback_item(fields)
        return {**result, "fields": fields}
    except Exception as error:
        return {
            "ok": False,
            "status": "failed",
            "error": str(error),
            "error_code": "sharepoint_feedback_sync_exception",
            "fields": fields,
        }


def jira_api_request(method, path, payload=None):
    if not ATLASSIAN_DOMAIN:
        return {"ok": False, "error": "Missing ATLASSIAN_DOMAIN", "error_code": "jira_configuration_error"}

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    try:
        headers = {
            "Authorization": atlassian_auth_header(),
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            f"{ATLASSIAN_DOMAIN}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            response_text = response.read().decode("utf-8").strip()
            return {
                "ok": 200 <= response.status < 300,
                "status_code": response.status,
                "body": json.loads(response_text) if response_text else {},
            }
    except urllib.error.HTTPError as error:
        response_text = error.read().decode("utf-8", errors="replace")[:2000]
        return {
            "ok": False,
            "status_code": error.code,
            "error": response_text or str(error),
            "error_code": "jira_http_error",
        }
    except (urllib.error.URLError, TimeoutError) as error:
        return {
            "ok": False,
            "status_code": None,
            "error": str(error),
            "error_code": "jira_network_error",
        }
    except Exception as error:
        return {
            "ok": False,
            "status_code": None,
            "error": str(error),
            "error_code": "jira_configuration_error",
        }


def normalize_status_name(value):
    return text_or_empty(value).lower()


def jira_issue_status(ticket_key):
    safe_ticket_key = urllib.parse.quote(str(ticket_key), safe="")
    result = jira_api_request("GET", f"/rest/api/3/issue/{safe_ticket_key}?fields=status")
    if not result.get("ok"):
        return {
            **result,
            "error_code": result.get("error_code") or "jira_issue_status_lookup_failed",
        }

    status_name = (
        ((result.get("body") or {}).get("fields") or {}).get("status") or {}
    ).get("name")
    if not status_name:
        return {
            "ok": False,
            "error": "Jira issue status missing from response",
            "error_code": "missing_jira_issue_status",
            "status_code": result.get("status_code"),
        }

    return {"ok": True, "ticket_status": status_name, "status_code": result.get("status_code")}


def jira_issue_transitions(ticket_key):
    safe_ticket_key = urllib.parse.quote(str(ticket_key), safe="")
    result = jira_api_request("GET", f"/rest/api/3/issue/{safe_ticket_key}/transitions")
    if not result.get("ok"):
        return {
            **result,
            "error_code": result.get("error_code") or "jira_transition_lookup_failed",
        }
    return {
        "ok": True,
        "transitions": (result.get("body") or {}).get("transitions") or [],
        "status_code": result.get("status_code"),
    }


def transition_jira_issue_to_status(ticket_key, target_status_names, reason=None):
    ticket_key = text_or_empty(ticket_key)
    target_status_names = [name for name in (target_status_names or []) if text_or_empty(name)]
    if not ticket_key:
        return {"ok": False, "error": "Missing ticket key", "error_code": "missing_ticket_key"}
    if not target_status_names:
        return {"ok": False, "error": "Missing target status", "error_code": "missing_target_status"}

    normalized_targets = {normalize_status_name(name) for name in target_status_names}
    status_result = jira_issue_status(ticket_key)
    if not status_result.get("ok"):
        return status_result

    current_status = status_result.get("ticket_status")
    if normalize_status_name(current_status) in normalized_targets:
        return {
            "ok": True,
            "skipped": True,
            "reason": "already_in_target_status",
            "ticket_key": ticket_key,
            "ticket_status": current_status,
            "target_status": current_status,
        }

    transitions_result = jira_issue_transitions(ticket_key)
    if not transitions_result.get("ok"):
        return transitions_result

    matching_transition = None
    for transition in transitions_result.get("transitions") or []:
        to_status = ((transition.get("to") or {}).get("name") or "").strip()
        if normalize_status_name(to_status) in normalized_targets:
            matching_transition = transition
            break

    if not matching_transition:
        return {
            "ok": False,
            "error": f"No Jira transition available to {', '.join(target_status_names)}.",
            "error_code": "no_matching_jira_transition",
            "ticket_key": ticket_key,
            "ticket_status": current_status,
            "target_status_names": target_status_names,
            "available_transitions": [
                {
                    "id": transition.get("id"),
                    "name": transition.get("name"),
                    "to": ((transition.get("to") or {}).get("name") or ""),
                }
                for transition in transitions_result.get("transitions") or []
            ],
        }

    safe_ticket_key = urllib.parse.quote(ticket_key, safe="")
    payload = {"transition": {"id": matching_transition.get("id")}}
    result = jira_api_request("POST", f"/rest/api/3/issue/{safe_ticket_key}/transitions", payload)
    if not result.get("ok"):
        return {
            **result,
            "error_code": result.get("error_code") or "jira_transition_failed",
            "ticket_key": ticket_key,
            "ticket_status": current_status,
            "target_status": (matching_transition.get("to") or {}).get("name"),
            "transition_id": matching_transition.get("id"),
            "transition_name": matching_transition.get("name"),
        }

    return {
        "ok": True,
        "ticket_key": ticket_key,
        "previous_status": current_status,
        "target_status": (matching_transition.get("to") or {}).get("name"),
        "transition_id": matching_transition.get("id"),
        "transition_name": matching_transition.get("name"),
        "reason": reason,
        "status_code": result.get("status_code"),
    }


def live_agent_start_transition_failure_text(ticket_key, transition_result):
    error_text = (
        transition_result.get("error")
        or transition_result.get("error_code")
        or "unknown Jira transition error"
    )
    return (
        f"Could not send this reply to the requester because Jira did not transition "
        f"{ticket_key or 'the ticket'} to {', '.join(LIVE_AGENT_START_STATUS_NAMES)}. "
        f"Reason: {error_text}"
    )


def slack_api(method, params=None, payload=None, http_method=None):
    params = params or {}
    url = f"https://slack.com/api/{method}"
    data = None
    headers = {"Authorization": f"Bearer {current_slack_token()}"}

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        http_method = http_method or "POST"
    else:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        http_method = http_method or "GET"

    req = urllib.request.Request(url, data=data, headers=headers, method=http_method)

    with urllib.request.urlopen(req, timeout=10) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise Exception(f"Slack API {method} failed: {result.get('error')}")

    return result


def fetch_conversation_metadata(channel, fallback_type=None):
    inferred_type = fallback_type
    if not inferred_type and str(channel or "").startswith("D"):
        inferred_type = "im"

    metadata = {
        "conversation_type": inferred_type,
        "is_dm": inferred_type == "im",
        "is_mpim": inferred_type == "mpim",
        "is_channel": inferred_type == "channel",
        "is_private": inferred_type == "group",
    }

    if not channel:
        return metadata

    try:
        response = slack_api("conversations.info", {"channel": channel})
        info = response.get("channel") or {}
        conversation_type = (
            "im" if info.get("is_im")
            else "mpim" if info.get("is_mpim")
            else "group" if info.get("is_group") or info.get("is_private")
            else "channel" if info.get("is_channel")
            else inferred_type
        )
        metadata.update({
            "conversation_type": conversation_type,
            "conversation_name": info.get("name") or info.get("user"),
            "is_dm": bool(info.get("is_im")),
            "is_mpim": bool(info.get("is_mpim")),
            "is_channel": bool(info.get("is_channel")),
            "is_private": bool(info.get("is_group") or info.get("is_private")),
        })

    except Exception as e:
        log_json({
            "level": "WARN",
            "message": "conversation_metadata_lookup_failed",
            "channel": channel,
            "error": str(e),
        })

    return metadata


def is_dm_like_conversation(metadata):
    return metadata.get("is_dm") or metadata.get("is_mpim") or metadata.get("conversation_type") in {"im", "mpim"}


def is_one_to_one_dm_conversation(metadata):
    return bool(metadata.get("is_dm") or metadata.get("conversation_type") == "im")


def slack_thread_ts_for_conversation(session_root_ts, metadata):
    if is_one_to_one_dm_conversation(metadata):
        return None

    return session_root_ts


def slack_mrkdwn(text, limit=2900):
    value = (text or "").strip()

    if len(value) <= limit:
        return value

    return value[:limit - 3].rstrip() + "..."


def compact_json(data):
    return json.dumps(data or {}, default=str, ensure_ascii=True, sort_keys=True)


def action_button_value(action, session_id=None, session_root_ts=None):
    if not (session_id or session_root_ts):
        return action

    return json.dumps(
        {
            "action": action,
            "session_id": session_id,
            "session_root_ts": session_root_ts,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def parse_action_value(value):
    if not isinstance(value, str) or not value.strip():
        return {}

    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass

    return {
        "action": value
    }


def root_ts_from_session_id(session_id):
    value = (session_id or "").strip()
    if value.startswith("issue:"):
        parts = value.split(":", 2)
        if len(parts) == 3:
            return parts[2]

    return None


def active_dm_pointer_id(channel, user):
    if not channel or not user:
        return None

    return f"dm_active:{channel}:{user}"


def resolve_session_identity(body, channel, user):
    action_payload = body.get("action_payload")
    if not isinstance(action_payload, dict):
        action_payload = parse_action_value(body.get("action_value"))

    explicit_session_id = (action_payload.get("session_id") or "").strip()
    explicit_root_ts = (action_payload.get("session_root_ts") or action_payload.get("root_ts") or "").strip()
    ts_candidates = [(body.get("thread_ts") or "").strip()]
    if body.get("event_type") == "interactive_action":
        ts_candidates.extend([
            (body.get("message_ts") or "").strip(),
            (body.get("ts") or "").strip(),
        ])
    else:
        ts_candidates.extend([
            (body.get("ts") or "").strip(),
            (body.get("message_ts") or "").strip(),
        ])
    session_root_ts = explicit_root_ts or next((value for value in ts_candidates if value), "")

    if explicit_session_id:
        return {
            "session_id": explicit_session_id,
            "session_root_ts": session_root_ts or root_ts_from_session_id(explicit_session_id),
            "session_id_version": "v2_issue_thread" if explicit_session_id.startswith("issue:") else "legacy",
            "session_scope": "support_issue" if explicit_session_id.startswith("issue:") else "legacy_user_channel",
            "legacy": not explicit_session_id.startswith("issue:"),
            "explicit": True,
        }

    if channel and session_root_ts:
        return {
            "session_id": f"issue:{channel}:{session_root_ts}",
            "session_root_ts": session_root_ts,
            "session_id_version": "v2_issue_thread",
            "session_scope": "support_issue",
            "legacy": False,
            "explicit": False,
        }

    return {
        "session_id": f"{channel}:{user}",
        "session_root_ts": None,
        "session_id_version": "legacy",
        "session_scope": "legacy_user_channel",
        "legacy": True,
        "explicit": False,
    }


ACTIVE_LIVE_AGENT_STATUSES = {
    "requested",
    "ticket_created",
    "in_progress",
    "waiting_for_customer",
    "waiting_for_support",
    "status_changed",
    "user_replied",
}

TERMINAL_LIVE_AGENT_STATUSES = {
    "resolved",
    "closed",
    "failed",
    "cancelled",
    "canceled",
}


def is_live_agent_dm_session_pending_or_active(session_item):
    if not session_item:
        return False

    return (
        session_item.get("conversation_status") not in {"closed", "failed"}
        and session_item.get("summary_status") not in {"started", "completed"}
        and session_item.get("live_agent_status") not in TERMINAL_LIVE_AGENT_STATUSES
        and (
            session_item.get("support_options_status") in {"live_agent_requested", "live_agent_creating"}
            or session_item.get("live_agent_status") in ACTIVE_LIVE_AGENT_STATUSES
        )
    )


def is_active_live_agent_session(session_item):
    if not session_item:
        return False

    has_live_agent_ticket = bool(
        session_item.get("live_agent_ticket_key")
        or session_item.get("last_live_agent_ticket_key")
    )

    return (
        has_live_agent_ticket
        and session_item.get("live_agent_status") in ACTIVE_LIVE_AGENT_STATUSES
        and session_item.get("conversation_status") not in {"closed", "failed"}
        and session_item.get("summary_status") not in {"started", "completed"}
    )


def session_is_terminal(session_item):
    if not session_item:
        return False

    if is_active_live_agent_session(session_item):
        return False

    return (
        session_item.get("conversation_status") in {"closed", "failed"}
        or session_item.get("summary_status") in {"started", "completed"}
        or session_item.get("jira_status") == "created"
        or session_item.get("support_options_status") == "jira_created"
        or session_item.get("live_agent_status") in TERMINAL_LIVE_AGENT_STATUSES
    )


def terminal_session_reply(session_item):
    ticket_key = session_item.get("jira_ticket_key") or session_item.get("last_jira_ticket_key")
    ticket_url = session_item.get("jira_ticket_url") or session_item.get("last_jira_ticket_url")
    live_agent_ticket_key = session_item.get("live_agent_ticket_key") or session_item.get("last_live_agent_ticket_key")
    live_agent_ticket_url = session_item.get("live_agent_ticket_url") or session_item.get("last_live_agent_ticket_url")

    if session_item.get("jira_status") == "created":
        return existing_jira_ticket_reply(ticket_key, ticket_url)

    if session_item.get("live_agent_status") in {"ticket_created", "waiting_for_customer", "resolved"} or live_agent_ticket_key:
        return existing_live_agent_ticket_reply(live_agent_ticket_key, live_agent_ticket_url)

    if session_item.get("live_agent_status") == "requested":
        return "This issue has already been sent to live agent support. Please start a new message for a different issue."

    if session_item.get("conversation_status") == "failed":
        return "This IVY session is marked as closed. Please start a new message for a new issue."

    return "This IVY session is already closed. Please start a new message for a new issue."


def assistance_reply_text(lex_reply):
    return f"{(lex_reply or '').strip()}\n\n{LEX_ASSISTANCE_PROMPT_TEXT}"


def mcp_assist_available():
    return ENABLE_MCP_ASSIST and bool(MCP_QUEUE_URL or MCP_ASSIST_FUNCTION_NAME)


def assistance_help_button():
    if mcp_assist_available():
        return {
            "type": "button",
            "text": {
                "type": "plain_text",
                "text": "Search company knowledge"
            },
            "action_id": ACTION_ID_MCP_ASSIST,
            "value": action_button_value("mcp_assist")
        }

    return {
        "type": "button",
        "text": {
            "type": "plain_text",
            "text": "I need more help"
        },
        "action_id": ACTION_ID_ASSISTANCE_NEED_MORE_HELP,
        "value": action_button_value("need_more_help")
    }


def assistance_blocks(lex_reply, session_id=None, session_root_ts=None):
    help_button = assistance_help_button()
    if isinstance(help_button.get("value"), str):
        action = parse_action_value(help_button["value"]).get("action") or help_button["value"]
        help_button["value"] = action_button_value(action, session_id, session_root_ts)

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": slack_mrkdwn(lex_reply)
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": LEX_ASSISTANCE_PROMPT_TEXT
            }
        },
        {
            "type": "actions",
            "block_id": "ivy_assistance_actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Resolved"
                    },
                    "action_id": ACTION_ID_CLOSE_AND_SUMMARIZE,
                    "value": action_button_value("close_and_summarize", session_id, session_root_ts)
                },
                help_button
            ]
        }
    ]


# --- Escalation ladder (flag-gated: ENABLE_ESCALATION_LADDER) ----------------
# L1 Lex -> L2 KB -> L3 LLM -> L4 ticket. Each answer carries a "Not helpful ->
# try another way" button (ACTION_ID_ESCALATE) whose value packs the target level
# and the original query, so the flow is self-contained (no extra session state).
# Automated answer-producing layers. Live agent (human) is a terminal handoff shown
# after these are exhausted, not an auto-run layer.
ESCALATION_LEVELS = ["lex", "kb", "llm"]
ESCALATION_MAX_QUERY_CHARS = 1500
ESCALATION_NO_ANSWER_TEXT = "I couldn't find a confident answer for that."
# Shown when a specific layer runs but finds nothing, so the layer is visible (not a
# silent skip) and the user is offered the next one.
ESCALATION_KB_NO_ANSWER_TEXT = os.environ.get(
    "ESCALATION_KB_NO_ANSWER_TEXT",
    "I couldn't find anything specific in the knowledge base for this. "
    "Want me to try our AI assistant?"
)
ESCALATION_EXHAUSTED_REPLY = (
    "I've tried every automated option I have and still couldn't resolve this. "
    "Please reach out to the IT support team directly and reference this chat."
)


def escalation_layer_available(level):
    """A layer only shows up in the ladder if its backing service is provisioned."""
    if level == "lex":
        return True
    if level == "kb":
        # Available if either a real Bedrock KB is provisioned OR the local
        # (xlsx-derived) KB stand-in is enabled.
        return bool(
            (ENABLE_BEDROCK_KB_ASSIST and BEDROCK_KNOWLEDGE_BASE_ID and BEDROCK_KB_MODEL_ARN)
            or ENABLE_LOCAL_KB
        )
    if level == "llm":
        return True
    if level == "ticket":
        return bool(ENABLE_CREATE_JIRA_TICKET and CREATE_JIRA_TICKET_FUNCTION)
    return False


def next_escalation_index(start_index):
    """First available layer index at/after start_index, or None if none remain."""
    for index in range(max(start_index, 0), len(ESCALATION_LEVELS)):
        if escalation_layer_available(ESCALATION_LEVELS[index]):
            return index
    return None


def escalation_llm_answer(query, session_id=None, channel=None):
    """L3 — pluggable LLM. claude (Bedrock Lambda) by default; mistral/gemini too."""
    if LADDER_LLM_PROVIDER == "mistral":
        return invoke_mistral_from_text(query)
    if LADDER_LLM_PROVIDER == "gemini":
        return invoke_gemini_from_text(query)
    result = invoke_claude_fallback({
        "text": query, "raw_text": query, "session_id": session_id, "channel": channel,
        "lex": {"intent": "EscalationLLM", "state": "Fulfilled", "slots": {}, "reply": ""},
        "session": {"conversation_status": "active"},
    })
    reply = (result.get("reply") or "").strip()
    return {"ok": bool(result.get("ok") and reply), "reply": reply, "error": result.get("error")}


def run_escalation_layer(level, query, session_id=None, channel=None):
    """Run one non-Lex layer. Returns (reply_text, ok)."""
    if level == "kb":
        # Prefer a real Bedrock KB when provisioned; otherwise fall back to the local
        # xlsx-derived KB stand-in.
        if BEDROCK_KNOWLEDGE_BASE_ID and BEDROCK_KB_MODEL_ARN:
            result = invoke_bedrock_knowledge_base(query)
            if result.get("ok") or not ENABLE_LOCAL_KB:
                return (result.get("reply") or "", bool(result.get("ok")))
        result = invoke_local_kb(query)
        return (result.get("reply") or "", bool(result.get("ok")))
    if level == "llm":
        result = escalation_llm_answer(query, session_id, channel)
        return (result.get("reply") or "", bool(result.get("ok")))
    if level == "ticket":
        result = invoke_create_jira_ticket({
            "text": query, "raw_text": query, "session_id": session_id,
            "channel": channel, "intent_name": "EscalationTicket", "request_text": query,
        })
        if result.get("ok"):
            key = result.get("ticket_key") or result.get("jira_ticket_key") or ""
            url = result.get("ticket_url") or result.get("jira_ticket_url") or ""
            message = "I've raised a support ticket" + (f" ({key})" if key else "") + " for you."
            if url:
                message += f" <{url}|View ticket>"
            return (message, True)
        return (result.get("error") or "I couldn't raise a ticket right now.", False)
    return ("", False)


def escalation_layer_no_answer_text(level):
    """User-facing line when a layer ran but found nothing — keeps L2/L3 visible."""
    if level == "kb":
        return ESCALATION_KB_NO_ANSWER_TEXT
    return ESCALATION_NO_ANSWER_TEXT


def escalate_autoadvance(start_index, query, session_id=None, channel=None):
    """Run layers from start_index onward, auto-skipping any that return no usable
    answer, and stop at the first that DOES answer. This is why the user never has
    to click through empty layers: a click (or the initial fallthrough) advances in
    the background until a real answer appears, or all layers are exhausted.

    Returns (answered_index, reply_text). answered_index is None if nothing answered.
    """
    index = next_escalation_index(start_index)
    while index is not None:
        level = ESCALATION_LEVELS[index]
        reply_text, ok = run_escalation_layer(level, query, session_id, channel)
        if ok and has_meaningful_bot_answer(reply_text):
            return index, reply_text
        index = next_escalation_index(index + 1)
    return None, ESCALATION_EXHAUSTED_REPLY


def has_llm_chat_active(session_item):
    """True when the session has reached L3 and is in a running LLM chat (not capped)."""
    return bool(
        ENABLE_LLM_CHAT
        and session_item.get("llm_chat_status") == "active"
    )


def trim_llm_chat_history(history):
    """Keep the running transcript bounded so it never blows up the prompt or the item."""
    history = history or ""
    if len(history) <= LLM_CHAT_HISTORY_MAX_CHARS:
        return history
    return history[-LLM_CHAT_HISTORY_MAX_CHARS:]


def build_llm_chat_prompt(original_issue, history, user_message):
    """Compose a context-carrying prompt so follow-up LLM turns feel like a real chat."""
    parts = []
    if original_issue:
        parts.append("The user's original issue:\n" + original_issue)
    if history:
        parts.append("Conversation so far:\n" + history)
    parts.append("User's latest message:\n" + (user_message or ""))
    parts.append(
        "Continue helping the user with a clear, concrete next step. If the issue now "
        "looks resolved, say so briefly."
    )
    return "\n\n".join(parts)


def escalation_blocks(reply_text, query, next_index, session_id=None, session_root_ts=None):
    """Answer section + a Resolved button and (if a further layer exists) an escalate button."""
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": slack_mrkdwn(reply_text)}}]
    elements = []
    if next_index is not None:
        # Another automated layer remains -> escalate to it. The L1->L2 (KB) step gets
        # its own "Explore Knowledge base" label so the layers read as distinct.
        next_button_text = (
            KB_ESCALATE_BUTTON_TEXT
            if ESCALATION_LEVELS[next_index] == "kb"
            else ESCALATE_BUTTON_TEXT
        )
        elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": next_button_text},
            "style": "primary",
            "action_id": ACTION_ID_ESCALATE,
            "value": json.dumps({
                "level": next_index,
                "q": (query or "")[:ESCALATION_MAX_QUERY_CHARS],
                "sid": session_id,
                "srt": session_root_ts,
            }),
        })
    elif ENABLE_LIVE_AGENT_ESCALATION:
        # Automated layers exhausted -> offer a human. Reuses the live-agent handler.
        elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": LIVE_AGENT_BUTTON_TEXT},
            "style": "primary",
            "action_id": ACTION_ID_LIVE_AGENT_SUPPORT,
            "value": action_button_value("live_agent_support", session_id, session_root_ts),
        })
    elements.append({
        "type": "button",
        "text": {"type": "plain_text", "text": "Resolved"},
        "action_id": ACTION_ID_CLOSE_AND_SUMMARIZE,
        "value": action_button_value("close_and_summarize", session_id, session_root_ts),
    })
    blocks.append({"type": "actions", "block_id": "ivy_escalation_actions", "elements": elements})
    return blocks


def mcp_followup_reply_text(reply):
    return (reply or "").strip()


def mcp_followup_blocks(reply, session_id=None, session_root_ts=None, citations=None):
    text = (reply or "").strip()
    citation_lines = []
    for index, citation in enumerate(citations or [], start=1):
        if not isinstance(citation, dict):
            continue
        title = text_or_empty(citation.get("title")) or text_or_empty(citation.get("source")) or f"Source {index}"
        url = text_or_empty(citation.get("url"))
        if url:
            citation_lines.append(f"{index}. <{url}|{slack_mrkdwn(title, 180)}>")
        else:
            citation_lines.append(f"{index}. {slack_mrkdwn(title, 180)}")

    if citation_lines:
        text = "\n\n*Sources:*\n" + "\n".join(citation_lines) if not text else text + "\n\n*Sources:*\n" + "\n".join(citation_lines)

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": slack_mrkdwn(text or "Company knowledge search completed.")
            }
        },
        {
            "type": "actions",
            "block_id": "ivy_mcp_followup_actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Resolved"
                    },
                    "action_id": ACTION_ID_CLOSE_AND_SUMMARIZE,
                    "value": action_button_value("close_and_summarize", session_id, session_root_ts)
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "I need more help"
                    },
                    "action_id": ACTION_ID_CLAUDE_ASSIST,
                    "value": action_button_value("claude_assist", session_id, session_root_ts)
                }
            ]
        }
    ]


def final_support_reply_text(reply):
    return f"{(reply or '').strip()}\n\n{CLAUDE_FINAL_ACTION_PROMPT_TEXT}"


def final_support_blocks(reply, session_id=None, session_root_ts=None):
    elements = [
        {
            "type": "button",
            "text": {
                "type": "plain_text",
                "text": "Live agent support"
            },
            "action_id": ACTION_ID_LIVE_AGENT_SUPPORT,
            "value": action_button_value("live_agent_support", session_id, session_root_ts)
        }
    ]
    if ENABLE_CREATE_JIRA_TICKET:
        elements.append({
            "type": "button",
            "text": {
                "type": "plain_text",
                "text": "Create Jira ticket"
            },
            "style": "primary",
            "action_id": ACTION_ID_CREATE_JIRA_TICKET,
            "value": action_button_value("create_jira_ticket", session_id, session_root_ts)
        })
    if ENABLE_CLOSE_SUMMARY:
        elements.append({
            "type": "button",
            "text": {
                "type": "plain_text",
                "text": "Resolved"
            },
            "action_id": ACTION_ID_CLOSE_AND_SUMMARIZE,
            "value": action_button_value("close_and_summarize", session_id, session_root_ts)
        })

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": slack_mrkdwn(reply)
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": CLAUDE_FINAL_ACTION_PROMPT_TEXT
            }
        },
        {
            "type": "actions",
            "block_id": "ivy_final_support_actions",
            "elements": elements
        }
    ]


def close_summary_actions_block(session_id=None, session_root_ts=None):
    elements = []
    if ENABLE_CLOSE_SUMMARY:
        elements.append({
            "type": "button",
            "text": {
                "type": "plain_text",
                "text": "Resolved"
            },
            "action_id": ACTION_ID_CLOSE_AND_SUMMARIZE,
            "value": action_button_value("close_and_summarize", session_id, session_root_ts)
        })
    elements.append({
        "type": "button",
        "text": {
            "type": "plain_text",
            "text": "I need more help"
        },
        "action_id": ACTION_ID_ASSISTANCE_NEED_MORE_HELP,
        "value": action_button_value("need_more_help", session_id, session_root_ts)
    })
    return {
        "type": "actions",
        "block_id": "ivy_close_summary_actions",
        "elements": elements
    }


def add_close_summary_actions(blocks, reply=None, session_id=None, session_root_ts=None):
    if not ENABLE_CLOSE_SUMMARY:
        return list(blocks or [])

    value = list(blocks or [])

    if not value and reply:
        value.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": slack_mrkdwn(reply)
            }
        })

    if any(block.get("block_id") == "ivy_close_summary_actions" for block in value):
        return value

    value.append(close_summary_actions_block(session_id, session_root_ts))
    return value


def normalized_user_text(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def has_meaningful_user_issue(text):
    value = normalized_user_text(text)
    if not value or value in GREETING_ONLY_TEXTS:
        return False

    return len(value) >= 8 or any(char in value for char in "?!.") or len(value.split()) >= 3


def has_meaningful_bot_answer(reply):
    value = normalized_user_text(reply)
    if not value:
        return False

    return len(value) >= 20 or len(value.split()) >= 5


def should_offer_close_summary(
    response_source,
    jira_status,
    assistance_status,
    support_options_status,
    session_item,
    user_text,
    bot_reply,
):
    if session_item.get("summary_status") in {"started", "completed"}:
        return False

    if session_item.get("conversation_status") in {"closed", "failed"}:
        return False

    return (
        response_source in CLOSE_SUMMARY_RESPONSE_SOURCES
        and not jira_status
        and assistance_status not in {"pending_confirmation", "awaiting_details"}
        and support_options_status not in {"pending", "creating_jira"}
        and has_meaningful_user_issue(user_text)
        and has_meaningful_bot_answer(bot_reply)
    )


def transcript_entry(sender, text, ts=None):
    clean_text = (text or "").strip()
    if not clean_text:
        return None

    return {
        "sender": sender,
        "text": clean_text,
        "ts": ts,
        "recorded_at": to_iso(datetime.now(timezone.utc)),
    }


def build_transcript_append(user_text, bot_text, user_ts, bot_ts=None):
    entries = []
    user_entry = transcript_entry("User", user_text, user_ts) if has_meaningful_user_issue(user_text) else None
    bot_entry = transcript_entry("Bot", bot_text, bot_ts) if has_meaningful_bot_answer(bot_text) else None

    if user_entry:
        entries.append(user_entry)
    if bot_entry:
        entries.append(bot_entry)

    return entries


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


def timeout_schedule_name(session_id, phase):
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    suffix = f"-{phase}"
    max_prefix_length = 64 - len(digest) - len(suffix) - 1
    prefix = SCHEDULER_NAME_PREFIX[:max_prefix_length]

    return f"{prefix}-{digest}{suffix}"


def timeout_token(session_id, event_id, activity_at):
    raw_token = f"{session_id}|{event_id or ''}|{activity_at}"
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()[:32]


def scheduler_at_expression(due_at):
    utc_due_at = due_at.astimezone(timezone.utc).replace(microsecond=0)
    return f"at({utc_due_at.strftime('%Y-%m-%dT%H:%M:%S')})"


def upsert_schedule(name, due_at, payload):
    request = {
        "GroupName": SCHEDULER_GROUP_NAME,
        "ScheduleExpression": scheduler_at_expression(due_at),
        "ScheduleExpressionTimezone": "UTC",
        "FlexibleTimeWindow": {
            "Mode": "OFF"
        },
        "Target": {
            "Arn": TIMEOUT_HANDLER_ARN,
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


def refresh_timeout_schedule(session_id, timeout_state):
    if not TIMEOUT_SCHEDULING_ENABLED:
        log_json({
            "level": "INFO",
            "message": "timeout_schedule_skipped",
            "session_id": session_id,
            "reason": "disabled"
        })
        return False

    if not TIMEOUT_HANDLER_ARN or not SCHEDULER_ROLE_ARN:
        log_json({
            "level": "WARN",
            "message": "timeout_schedule_skipped",
            "session_id": session_id,
            "reason": "missing_timeout_scheduler_env",
            "has_timeout_handler_arn": bool(TIMEOUT_HANDLER_ARN),
            "has_scheduler_role_arn": bool(SCHEDULER_ROLE_ARN)
        })
        return False

    payload = {
        "action": "prompt",
        "session_id": session_id,
        "timeout_token": timeout_state["timeout_token"],
        "timeout_due_at": timeout_state["timeout_due_at"]
    }

    try:
        action = upsert_schedule(
            timeout_state["timeout_schedule_name"],
            timeout_state["timeout_due_at_dt"],
            payload
        )

        log_json({
            "level": "INFO",
            "message": "timeout_schedule_refreshed",
            "session_id": session_id,
            "schedule_name": timeout_state["timeout_schedule_name"],
            "timeout_due_at": timeout_state["timeout_due_at"],
            "scheduler_action": action
        })
        return True

    except Exception as e:
        log_json({
            "level": "ERROR",
            "message": "timeout_schedule_refresh_failed",
            "session_id": session_id,
            "schedule_name": timeout_state["timeout_schedule_name"],
            "error": str(e)
        })
        return False


def delete_timeout_schedule(session_id, phase):
    if not TIMEOUT_SCHEDULING_ENABLED:
        return

    name = timeout_schedule_name(session_id, phase)

    try:
        scheduler.delete_schedule(Name=name, GroupName=SCHEDULER_GROUP_NAME)

        log_json({
            "level": "INFO",
            "message": "timeout_schedule_deleted",
            "session_id": session_id,
            "schedule_name": name
        })

    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return

        log_json({
            "level": "ERROR",
            "message": "timeout_schedule_delete_failed",
            "session_id": session_id,
            "schedule_name": name,
            "error": str(e)
        })


def should_use_claude_fallback(text, lex_intent, lex_state, lex_reply_empty):
    if not text:
        return False

    return (
        lex_state == "Failed"
        or lex_reply_empty
        or lex_intent in CLAUDE_FALLBACK_INTENTS
    )


def disabled_claude_fallback_result(previous_reply):
    reply = (previous_reply or "").strip()
    if not reply:
        return {
            "ok": False,
            "error": "claude_fallback_disabled_no_previous_reply",
            "fallback_disabled": True,
        }

    return {
        "ok": True,
        "reply": reply,
        "error": None,
        "model_id": None,
        "fallback_disabled": True,
    }


def invoke_claude_fallback(payload):
    if not CLAUDE_FALLBACK_FUNCTION:
        return {
            "ok": False,
            "error": "missing_claude_fallback_function"
        }

    response = lambda_client.invoke(
        FunctionName=CLAUDE_FALLBACK_FUNCTION,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8")
    )

    raw_payload = response.get("Payload").read().decode("utf-8")

    if response.get("FunctionError"):
        return {
            "ok": False,
            "error": raw_payload or response.get("FunctionError")
        }

    if not raw_payload:
        return {
            "ok": False,
            "error": "empty_claude_lambda_response"
        }

    try:
        return json.loads(raw_payload)

    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": "invalid_claude_lambda_response",
            "raw_response": raw_payload
        }


def invoke_create_jira_ticket(payload):
    if not ENABLE_CREATE_JIRA_TICKET:
        return {
            "ok": False,
            "skipped": True,
            "error": "Jira ticket creation is disabled.",
            "error_code": "jira_ticket_creation_disabled"
        }

    if not CREATE_JIRA_TICKET_FUNCTION:
        return {
            "ok": False,
            "error": "missing_create_jira_ticket_function",
            "error_code": "missing_create_jira_ticket_function"
        }

    try:
        response = lambda_client.invoke(
            FunctionName=CREATE_JIRA_TICKET_FUNCTION,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8")
        )

    except ClientError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "jira_lambda_invoke_failed"
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "jira_lambda_invoke_failed"
        }

    raw_payload = response.get("Payload").read().decode("utf-8")

    if response.get("FunctionError"):
        return {
            "ok": False,
            "error": raw_payload or response.get("FunctionError"),
            "error_code": "jira_lambda_function_error"
        }

    if not raw_payload:
        return {
            "ok": False,
            "error": "empty_create_jira_response",
            "error_code": "invalid_create_jira_response"
        }

    try:
        return json.loads(raw_payload)

    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": "invalid_create_jira_response",
            "error_code": "invalid_create_jira_response",
            "raw_response": raw_payload
        }


def invoke_rovo_enrichment(payload):
    if not ENABLE_ROVO_ENRICHMENT:
        return {
            "ok": True,
            "skipped": True,
            "reason": "disabled"
        }

    if not ROVO_ENRICHMENT_FUNCTION:
        return {
            "ok": False,
            "error": "missing_rovo_enrichment_function",
            "error_code": "missing_rovo_enrichment_function"
        }

    try:
        response = lambda_client.invoke(
            FunctionName=ROVO_ENRICHMENT_FUNCTION,
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8")
        )

    except ClientError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "rovo_lambda_invoke_failed"
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "rovo_lambda_invoke_failed"
        }

    status_code = response.get("StatusCode")
    if status_code and 200 <= int(status_code) < 300:
        return {
            "ok": True,
            "status_code": status_code
        }

    return {
        "ok": False,
        "error": f"Unexpected Rovo Lambda invoke status: {status_code}",
        "error_code": "rovo_lambda_invoke_rejected",
        "status_code": status_code
    }


def invoke_mcp_assist(payload):
    if not mcp_assist_available():
        return {
            "ok": False,
            "error": "missing_mcp_assist_configuration",
            "error_code": "missing_mcp_assist_configuration",
        }

    try:
        if MCP_QUEUE_URL:
            message = {
                "QueueUrl": MCP_QUEUE_URL,
                "MessageBody": json.dumps(payload, default=str),
            }
            if MCP_QUEUE_URL.endswith(".fifo"):
                message["MessageGroupId"] = payload.get("session_id") or "mcp-assist"
            sqs_client.send_message(**message)
            return {"ok": True, "target": "sqs", "queue_url": MCP_QUEUE_URL}

        response = lambda_client.invoke(
            FunctionName=MCP_ASSIST_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps(payload, default=str).encode("utf-8")
        )
        status_code = response.get("StatusCode")
        return {
            "ok": bool(status_code and 200 <= int(status_code) < 300),
            "target": "lambda",
            "status_code": status_code,
        }

    except ClientError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "mcp_assist_enqueue_failed",
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "mcp_assist_enqueue_failed",
        }


def invoke_summarizer(payload):
    if not SUMMARIZER_FUNCTION_NAME:
        return {
            "ok": False,
            "error": "missing_summarizer_function",
            "error_code": "missing_summarizer_function"
        }

    try:
        response = lambda_client.invoke(
            FunctionName=SUMMARIZER_FUNCTION_NAME,
            InvocationType="RequestResponse",
            Payload=json.dumps(with_tenant_token(payload)).encode("utf-8")
        )

    except ClientError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "summarizer_lambda_invoke_failed"
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "summarizer_lambda_invoke_failed"
        }

    response_payload = {}
    if response.get("Payload"):
        response_payload = json.loads(response["Payload"].read().decode("utf-8") or "{}")

    if response.get("FunctionError"):
        return {
            "ok": False,
            "error": response_payload.get("error") or response.get("FunctionError"),
            "error_code": response_payload.get("error_code") or "summarizer_lambda_error",
            "status_code": response.get("StatusCode"),
        }

    status_code = response.get("StatusCode")
    if status_code and 200 <= int(status_code) < 300 and response_payload.get("ok"):
        return {**response_payload, "status_code": status_code}

    return {
        "ok": False,
        "error": response_payload.get("error") or f"Unexpected summarizer Lambda invoke status: {status_code}",
        "error_code": response_payload.get("error_code") or "summarizer_lambda_invoke_rejected",
        "status_code": status_code
    }


def invoke_image_analysis(payload):
    if not IMAGE_REK_FUNCTION:
        return {
            "ok": False,
            "error": "missing_image_rek_function",
            "error_code": "missing_image_rek_function"
        }

    try:
        response = lambda_client.invoke(
            FunctionName=IMAGE_REK_FUNCTION,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8")
        )

    except ClientError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "image_lambda_invoke_failed"
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "image_lambda_invoke_failed"
        }

    raw_payload = response.get("Payload").read().decode("utf-8")

    if response.get("FunctionError"):
        return {
            "ok": False,
            "error": raw_payload or response.get("FunctionError"),
            "error_code": "image_lambda_function_error"
        }

    if not raw_payload:
        return {
            "ok": False,
            "error": "empty_image_analysis_response",
            "error_code": "invalid_image_analysis_response"
        }

    try:
        return json.loads(raw_payload)

    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": "invalid_image_analysis_response",
            "error_code": "invalid_image_analysis_response",
            "raw_response": raw_payload
        }


def get_s3_object_bytes(s3_object):
    if not s3_object:
        return None

    bucket = s3_object.get("bucket")
    key = s3_object.get("key")
    if not bucket or not key:
        return None

    response = s3_client.get_object(Bucket=bucket, Key=key)
    return response["Body"].read()


def create_image_embedding_from_bytes(image_bytes):
    if not image_bytes:
        return {
            "ok": False,
            "error": "missing_image_bytes",
            "error_code": "missing_image_bytes"
        }

    if len(image_bytes) > SCREENSHOT_EMBEDDING_MAX_BYTES:
        return {
            "ok": False,
            "error": "image_too_large_for_embedding",
            "error_code": "image_too_large_for_embedding"
        }

    request_body = {
        "inputImage": base64.b64encode(image_bytes).decode("utf-8")
    }

    try:
        response = bedrock_runtime.invoke_model(
            modelId=SCREENSHOT_EMBEDDING_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(request_body).encode("utf-8")
        )
        response_body = json.loads(response["body"].read().decode("utf-8"))

    except ClientError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "embedding_request_failed"
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "embedding_request_failed"
        }

    embedding = response_body.get("embedding")
    if not embedding:
        return {
            "ok": False,
            "error": "empty_embedding_response",
            "error_code": "empty_embedding_response",
            "raw_response": response_body
        }

    return {
        "ok": True,
        "embedding": embedding,
        "model_id": SCREENSHOT_EMBEDDING_MODEL_ID
    }


def create_screenshot_embedding(image_result):
    try:
        image_bytes = get_s3_object_bytes(image_result.get("s3_object"))
        return create_image_embedding_from_bytes(image_bytes)

    except ClientError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "image_s3_read_failed"
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "image_s3_read_failed"
        }


def signed_opensearch_request(method, path, payload=None):
    if not SCREENSHOT_VECTOR_ENDPOINT:
        return {
            "ok": False,
            "error": "missing_screenshot_vector_endpoint",
            "error_code": "missing_screenshot_vector_endpoint"
        }

    body = json.dumps(payload or {}).encode("utf-8")
    payload_hash = hashlib.sha256(body).hexdigest()
    url = f"{SCREENSHOT_VECTOR_ENDPOINT}{path}"
    request = AWSRequest(
        method=method,
        url=url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Host": urllib.parse.urlparse(SCREENSHOT_VECTOR_ENDPOINT).netloc,
            "X-Amz-Content-Sha256": payload_hash
        }
    )
    SigV4Auth(
        boto3.Session().get_credentials(),
        SCREENSHOT_OPENSEARCH_SERVICE,
        AWS_REGION
    ).add_auth(request)

    prepared = request.prepare()
    urllib_request = urllib.request.Request(
        url,
        data=body,
        headers=dict(prepared.headers.items()),
        method=method
    )

    try:
        with urllib.request.urlopen(urllib_request, timeout=10) as response:
            response_body = response.read().decode("utf-8")

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "opensearch_request_failed"
        }

    if not response_body:
        return {
            "ok": True,
            "response": {}
        }

    try:
        return {
            "ok": True,
            "response": json.loads(response_body)
        }

    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": "invalid_opensearch_response",
            "error_code": "invalid_opensearch_response",
            "raw_response": response_body
        }


def search_screenshot_vector_index(embedding):
    payload = {
        "size": SCREENSHOT_VECTOR_K,
        "query": {
            "knn": {
                SCREENSHOT_VECTOR_FIELD: {
                    "vector": embedding,
                    "k": SCREENSHOT_VECTOR_K
                }
            }
        }
    }
    path = f"/{urllib.parse.quote(SCREENSHOT_VECTOR_INDEX, safe='')}/_search"
    result = signed_opensearch_request("POST", path, payload)
    if not result.get("ok"):
        return {
            **result,
            "backend": "opensearch",
            "opensearch_status": "failed",
            "vector_index": SCREENSHOT_VECTOR_INDEX,
            "vector_endpoint": SCREENSHOT_VECTOR_ENDPOINT,
        }

    hits = ((result.get("response") or {}).get("hits") or {}).get("hits") or []
    if not hits:
        return {
            "ok": True,
            "matched": False,
            "reason": "no_vector_hits",
            "backend": "opensearch",
            "opensearch_status": "ok",
            "vector_index": SCREENSHOT_VECTOR_INDEX,
            "vector_endpoint": SCREENSHOT_VECTOR_ENDPOINT,
            "hit_count": 0,
        }

    top_hit = hits[0]
    source = top_hit.get("_source") or {}
    return {
        "ok": True,
        "matched": True,
        "backend": "opensearch",
        "opensearch_status": "ok",
        "vector_index": SCREENSHOT_VECTOR_INDEX,
        "vector_endpoint": SCREENSHOT_VECTOR_ENDPOINT,
        "hit_count": len(hits),
        "issue_id": source.get("issue_id") or top_hit.get("_id"),
        "score": float(top_hit.get("_score") or 0),
        "vector_id": top_hit.get("_id"),
        "source": source
    }


def cosine_similarity(left, right):
    dot_product = 0.0
    left_norm = 0.0
    right_norm = 0.0

    for left_value, right_value in zip(left, right):
        left_float = float(left_value)
        right_float = float(right_value)
        dot_product += left_float * right_float
        left_norm += left_float * left_float
        right_norm += right_float * right_float

    if left_norm == 0 or right_norm == 0:
        return 0.0

    return dot_product / ((left_norm ** 0.5) * (right_norm ** 0.5))


def search_screenshot_dynamodb_embeddings(embedding):
    if not screenshot_issue_table:
        return {
            "ok": False,
            "error": "missing_screenshot_issue_table",
            "error_code": "missing_screenshot_issue_table"
        }

    try:
        response = screenshot_issue_table.scan()
        items = response.get("Items") or []

        while response.get("LastEvaluatedKey"):
            response = screenshot_issue_table.scan(
                ExclusiveStartKey=response["LastEvaluatedKey"]
            )
            items.extend(response.get("Items") or [])

    except ClientError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "screenshot_issue_scan_failed"
        }

    best_item = None
    best_score = 0.0
    for item in items:
        if item.get("enabled") is False or value_is_false(item.get("enabled")):
            continue

        item_embedding = item.get("image_embedding")
        if not item_embedding:
            continue

        score = cosine_similarity(embedding, item_embedding)
        if score > best_score:
            best_item = item
            best_score = score

    if not best_item:
        return {
            "ok": True,
            "matched": False,
            "reason": "no_dynamodb_embedding_hits"
        }

    return {
        "ok": True,
        "matched": True,
        "issue_id": best_item.get("issue_id"),
        "score": best_score,
        "vector_id": best_item.get("issue_id"),
        "source": best_item,
        "issue": best_item,
    }


def get_screenshot_issue(issue_id):
    if not screenshot_issue_table:
        return {
            "ok": False,
            "error": "missing_screenshot_issue_table",
            "error_code": "missing_screenshot_issue_table"
        }

    try:
        response = screenshot_issue_table.get_item(
            Key={
                "issue_id": issue_id
            }
        )

    except ClientError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "screenshot_issue_lookup_failed"
        }

    item = response.get("Item")
    if not item:
        return {
            "ok": False,
            "error": "screenshot_issue_not_found",
            "error_code": "screenshot_issue_not_found"
        }

    if item.get("enabled") is False or value_is_false(item.get("enabled")):
        return {
            "ok": False,
            "error": "screenshot_issue_disabled",
            "error_code": "screenshot_issue_disabled"
        }

    return {
        "ok": True,
        "item": item
    }


def find_matching_screenshot_issue(image_result):
    if not SCREENSHOT_MATCH_ENABLED:
        return {
            "ok": True,
            "matched": False,
            "reason": "screenshot_match_disabled"
        }

    if SCREENSHOT_VECTOR_BACKEND == "opensearch" and not SCREENSHOT_VECTOR_ENDPOINT:
        return {
            "ok": True,
            "matched": False,
            "reason": "missing_screenshot_vector_endpoint"
        }

    embedding_result = create_screenshot_embedding(image_result)
    if not embedding_result.get("ok"):
        return {
            "ok": False,
            "matched": False,
            "reason": embedding_result.get("error_code", "embedding_failed"),
            "error": embedding_result.get("error")
        }

    if SCREENSHOT_VECTOR_BACKEND == "dynamodb":
        search_result = search_screenshot_dynamodb_embeddings(embedding_result["embedding"])
    else:
        search_result = search_screenshot_vector_index(embedding_result["embedding"])

    log_json({
        "level": "INFO" if search_result.get("ok") else "ERROR",
        "message": "screenshot_vector_search_completed",
        "vector_backend": SCREENSHOT_VECTOR_BACKEND,
        "opensearch_status": search_result.get("opensearch_status"),
        "vector_index": search_result.get("vector_index") or SCREENSHOT_VECTOR_INDEX,
        "matched": search_result.get("matched", False),
        "issue_id": search_result.get("issue_id"),
        "score": search_result.get("score"),
        "hit_count": search_result.get("hit_count"),
        "reason": search_result.get("reason") or search_result.get("error_code"),
    })

    if not search_result.get("ok"):
        return {
            "ok": False,
            "matched": False,
            "reason": search_result.get("error_code", "vector_search_failed"),
            "error": search_result.get("error")
        }

    if not search_result.get("matched"):
        return search_result

    score = search_result.get("score", 0)
    if score < SCREENSHOT_MATCH_THRESHOLD:
        return {
            **search_result,
            "matched": False,
            "reason": "below_threshold",
            "threshold": SCREENSHOT_MATCH_THRESHOLD
        }

    issue = search_result.get("issue")
    if not issue:
        issue_result = get_screenshot_issue(search_result.get("issue_id"))
        if not issue_result.get("ok"):
            return {
                **search_result,
                "matched": False,
                "reason": issue_result.get("error_code", "issue_lookup_failed"),
                "error": issue_result.get("error"),
                "threshold": SCREENSHOT_MATCH_THRESHOLD
            }

        issue = issue_result["item"]

    expected_lex_intent = (issue.get("expected_lex_intent") or "").strip()
    if not expected_lex_intent:
        return {
            **search_result,
            "matched": False,
            "reason": "missing_expected_lex_intent",
            "threshold": SCREENSHOT_MATCH_THRESHOLD
        }

    lex_query = (issue.get("lex_query") or "").strip()
    if not lex_query:
        return {
            **search_result,
            "matched": False,
            "reason": "missing_lex_query",
            "threshold": SCREENSHOT_MATCH_THRESHOLD
        }

    return {
        **search_result,
        "matched": True,
        "threshold": SCREENSHOT_MATCH_THRESHOLD,
        "issue": issue,
        "lex_query": lex_query,
        "expected_lex_intent": expected_lex_intent
    }


def build_image_payload(body, session_id, text, raw_text, image_files, session_root_ts=None, slack_thread_ts=None):
    return {
        "event_id": body.get("event_id"),
        "session_id": session_id,
        "session_root_ts": session_root_ts,
        "channel": body.get("channel"),
        "channel_type": body.get("channel_type"),
        "routing_reason": body.get("routing_reason"),
        "user": body.get("user"),
        "text": text,
        "raw_text": raw_text,
        "files": image_files,
        "slack": {
            "channel": body.get("channel"),
            "user": body.get("user"),
            "event_ts": body.get("ts"),
            "thread_ts": slack_thread_ts,
        }
    }


def image_reply_from_result(result):
    if not result.get("ok"):
        if result.get("error_code") == "missing_image_rek_function":
            return IMAGE_ANALYSIS_UNAVAILABLE_REPLY

        return "I could not analyze that image. Please try again or describe the error in text."

    for key in ("reply", "message", "summary"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return "I analyzed the image, but there was no readable result."


def image_issue_text(text, image_result):
    parts = []

    if text:
        parts.append(f"User caption: {text}")

    detected_text = []
    for item in image_result.get("detected_text") or []:
        value = item.get("text") if isinstance(item, dict) else item
        if value:
            detected_text.append(str(value).strip())

    if detected_text:
        parts.append("Text visible in image: " + " | ".join(detected_text[:12]))

    labels = []
    for item in image_result.get("labels") or []:
        value = item.get("name") if isinstance(item, dict) else item
        if value:
            labels.append(str(value).strip())

    if labels:
        parts.append("Image labels: " + ", ".join(labels[:8]))

    summary = (image_result.get("summary") or "").strip()
    if summary and not parts:
        parts.append(summary)

    return "\n".join(parts).strip()


def lex_is_resolved(lex_intent, lex_state, lex_reply_empty):
    return (
        lex_state not in {"Failed", "Ignored"}
        and not lex_reply_empty
        and lex_intent not in CLAUDE_FALLBACK_INTENTS
    )


def bedrock_kb_answer_is_useful(answer):
    value = (answer or "").strip()
    if not value:
        return False

    value_lower = value.lower()
    return not any(marker in value_lower for marker in BEDROCK_KB_NO_ANSWER_MARKERS)


_local_kb_cache = None

# Common words that carry no matching signal, so they don't inflate overlap scores.
_LOCAL_KB_STOPWORDS = {
    "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "is", "are",
    "am", "i", "my", "me", "you", "your", "it", "this", "that", "how", "do",
    "can", "cant", "cannot", "not", "with", "at", "be", "have", "has", "get",
    "getting", "unable", "issue", "issues", "problem", "help", "please", "need",
    "want", "when", "what", "why", "if", "im", "ive", "was", "were", "from",
}


def _local_kb_tokens(text):
    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {t for t in tokens if len(t) > 2 and t not in _LOCAL_KB_STOPWORDS}


def load_local_kb():
    """Load and cache the xlsx-derived intent KB from S3. Returns a list of dicts,
    each pre-tokenized as `_tokens` (utterances + intent name + category)."""
    global _local_kb_cache
    if _local_kb_cache is not None:
        return _local_kb_cache

    if not ENABLE_LOCAL_KB:
        _local_kb_cache = {"entries": [], "idf": {}}
        return _local_kb_cache

    try:
        response = s3_client.get_object(Bucket=LOCAL_KB_S3_BUCKET, Key=LOCAL_KB_S3_KEY)
        entries = json.loads(response["Body"].read())
    except Exception as e:
        log_json({
            "level": "ERROR",
            "message": "local_kb_load_failed",
            "bucket": LOCAL_KB_S3_BUCKET,
            "key": LOCAL_KB_S3_KEY,
            "error": str(e),
        })
        _local_kb_cache = {"entries": [], "idf": {}}
        return _local_kb_cache

    prepared = []
    document_frequency = {}
    for entry in entries:
        if not isinstance(entry, dict) or not (entry.get("response") or "").strip():
            continue
        blob = " ".join([
            entry.get("intent") or "",
            entry.get("category") or "",
            " ".join(entry.get("utterances") or []),
        ])
        tokens = _local_kb_tokens(blob)
        for token in tokens:
            document_frequency[token] = document_frequency.get(token, 0) + 1
        prepared.append({
            "intent": entry.get("intent") or "",
            "response": (entry.get("response") or "").strip(),
            "_tokens": tokens,
        })

    # Inverse document frequency so distinctive words (e.g. "pcq", "adam") outweigh
    # generic ones (e.g. "keeps", "access") when scoring a match.
    total_docs = len(prepared)
    idf = {
        token: math.log((total_docs + 1) / (freq + 1)) + 1.0
        for token, freq in document_frequency.items()
    }

    _local_kb_cache = {"entries": prepared, "idf": idf}
    log_json({
        "level": "INFO",
        "message": "local_kb_loaded",
        "entries": len(prepared),
    })
    return _local_kb_cache


def invoke_local_kb(query):
    """L2 stand-in: keyword-overlap search over the xlsx-derived intent KB.

    Scores each entry by the fraction of the query's meaningful tokens that appear in
    the entry (utterances + intent name + category), and returns the best-scoring
    article's response when it clears LOCAL_KB_MIN_SCORE. No vector store required.
    """
    kb = load_local_kb()
    entries = kb["entries"]
    idf = kb["idf"]
    if not entries:
        return {"ok": False, "error": "local_kb_empty", "error_code": "local_kb_empty"}

    query_tokens = _local_kb_tokens(query)
    if not query_tokens:
        return {"ok": False, "error": "local_kb_no_query_tokens", "error_code": "local_kb_no_query_tokens"}

    # IDF-weighted overlap, normalized by the query's total IDF weight so the score is
    # the fraction of the query's *distinctive* content the article covers.
    denominator = sum(idf.get(token, 1.0) for token in query_tokens)
    best_entry = None
    best_score = 0.0
    for entry in entries:
        matched = query_tokens & entry["_tokens"]
        if not matched:
            continue
        score = sum(idf.get(token, 1.0) for token in matched) / denominator
        if score > best_score:
            best_score = score
            best_entry = entry

    if best_entry is None or best_score < LOCAL_KB_MIN_SCORE:
        return {
            "ok": False,
            "error": "local_kb_no_match",
            "error_code": "local_kb_no_match",
            "best_score": round(best_score, 3),
        }

    return {
        "ok": True,
        "reply": best_entry["response"],
        "summary": best_entry["response"],
        "source": "local_knowledge_base",
        "matched_intent": best_entry["intent"],
        "score": round(best_score, 3),
    }


def invoke_bedrock_knowledge_base(query):
    if not BEDROCK_KNOWLEDGE_BASE_ID or not BEDROCK_KB_MODEL_ARN:
        return {
            "ok": False,
            "error": "missing_bedrock_kb_configuration",
            "error_code": "missing_bedrock_kb_configuration"
        }

    try:
        response = bedrock_agent_runtime.retrieve_and_generate(
            input={
                "text": query
            },
            retrieveAndGenerateConfiguration={
                "type": "KNOWLEDGE_BASE",
                "knowledgeBaseConfiguration": {
                    "knowledgeBaseId": BEDROCK_KNOWLEDGE_BASE_ID,
                    "modelArn": BEDROCK_KB_MODEL_ARN,
                    "retrievalConfiguration": {
                        "vectorSearchConfiguration": {
                            "numberOfResults": BEDROCK_KB_NUMBER_OF_RESULTS
                        }
                    }
                }
            }
        )

    except ClientError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "bedrock_kb_request_failed"
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "bedrock_kb_request_failed"
        }

    answer = ((response.get("output") or {}).get("text") or "").strip()
    if not bedrock_kb_answer_is_useful(answer):
        return {
            "ok": False,
            "error": "bedrock_kb_no_useful_answer",
            "error_code": "bedrock_kb_no_useful_answer",
            "raw_answer": answer,
            "citations": response.get("citations") or []
        }

    return {
        "ok": True,
        "reply": answer,
        "summary": answer,
        "source": "bedrock_knowledge_base",
        "citations": response.get("citations") or [],
        "session_id": response.get("sessionId")
    }


_gemini_api_key_cache = None


def get_gemini_api_key():
    global _gemini_api_key_cache

    if _gemini_api_key_cache:
        return _gemini_api_key_cache

    if GEMINI_API_KEY:
        _gemini_api_key_cache = GEMINI_API_KEY
        return _gemini_api_key_cache

    return ""


def extract_gemini_text(response_body):
    parts = []

    for candidate in response_body.get("candidates") or []:
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            text = (part.get("text") or "").strip()
            if text:
                parts.append(text)

    return "\n".join(parts).strip()


def invoke_gemini_from_text(query):
    api_key = get_gemini_api_key()
    if not api_key:
        return {
            "ok": False,
            "error": "missing_gemini_api_key",
            "error_code": "missing_gemini_api_key"
        }

    prompt = "\n".join([
        "You are IVY, a concise IT support assistant.",
        "The text below was extracted from a screenshot the user shared.",
        "Use it to suggest the most likely resolution.",
        "If the extracted text is ambiguous, ask one clear clarifying question.",
        "Do not claim that a Jira ticket was created.",
        "",
        "Screenshot text:",
        query,
    ])

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{urllib.parse.quote(GEMINI_MODEL, safe='')}:generateContent"
    )
    data = json.dumps({
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ]
    }).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST"
    )

    try:
        started_at = time.time()
        with urllib.request.urlopen(request, timeout=GEMINI_TIMEOUT_SECONDS) as response:
            response_body = json.loads(response.read().decode("utf-8"))

    except Exception as e:
        error_body = None
        if hasattr(e, "read"):
            try:
                error_body = e.read().decode("utf-8", errors="replace")[:1000]
            except Exception:
                error_body = None

        log_json({
            "level": "ERROR",
            "message": "gemini_request_failed",
            "model_id": GEMINI_MODEL,
            "timeout_seconds": GEMINI_TIMEOUT_SECONDS,
            "latency_seconds": round(time.time() - started_at, 2) if "started_at" in locals() else None,
            "error": str(e),
            "error_body": error_body,
        })
        return {
            "ok": False,
            "error": str(e),
            "error_code": "gemini_request_failed",
            "error_body": error_body,
        }

    reply = extract_gemini_text(response_body)
    if not reply:
        log_json({
            "level": "ERROR",
            "message": "gemini_empty_reply",
            "model_id": GEMINI_MODEL,
            "latency_seconds": round(time.time() - started_at, 2),
            "candidate_count": len(response_body.get("candidates") or []),
        })
        return {
            "ok": False,
            "error": "empty_gemini_reply",
            "error_code": "empty_gemini_reply",
            "raw_response": response_body
        }

    log_json({
        "level": "INFO",
        "message": "gemini_request_completed",
        "model_id": GEMINI_MODEL,
        "latency_seconds": round(time.time() - started_at, 2),
        "candidate_count": len(response_body.get("candidates") or []),
        "reply_length": len(reply),
    })

    return {
        "ok": True,
        "reply": reply,
        "summary": reply,
        "source": "gemini",
        "model_id": GEMINI_MODEL
    }


def _image_llm_prompt(query):
    return "\n".join([
        "You are IVY, a concise IT support assistant.",
        "The text below was extracted from a screenshot the user shared.",
        "Use it to suggest the most likely resolution.",
        "If the extracted text is ambiguous, ask one clear clarifying question.",
        "Do not claim that a Jira ticket was created.",
        "",
        "Screenshot text:",
        query,
    ])


def invoke_mistral_from_text(query):
    """Screenshot-text -> resolution via the Mistral chat API (mirror of the
    Gemini path). Returns the same {ok, reply, summary, source, model_id} shape."""
    if not MIA_KEY:
        return {
            "ok": False,
            "error": "missing_mistral_api_key",
            "error_code": "missing_mistral_api_key",
        }

    data = json.dumps({
        "model": MISTRAL_MODEL,
        "messages": [{"role": "user", "content": _image_llm_prompt(query)}],
        "temperature": 0.2,
    }).encode("utf-8")

    request = urllib.request.Request(
        MISTRAL_ENDPOINT,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {MIA_KEY}",
        },
        method="POST",
    )

    started_at = time.time()
    try:
        with urllib.request.urlopen(request, timeout=MISTRAL_TIMEOUT_SECONDS) as response:
            response_body = json.loads(response.read().decode("utf-8"))
    except Exception as e:
        error_body = None
        if hasattr(e, "read"):
            try:
                error_body = e.read().decode("utf-8", errors="replace")[:1000]
            except Exception:
                error_body = None
        log_json({
            "level": "ERROR",
            "message": "mistral_request_failed",
            "model_id": MISTRAL_MODEL,
            "timeout_seconds": MISTRAL_TIMEOUT_SECONDS,
            "latency_seconds": round(time.time() - started_at, 2),
            "error": str(e),
            "error_body": error_body,
        })
        return {
            "ok": False,
            "error": str(e),
            "error_code": "mistral_request_failed",
            "error_body": error_body,
        }

    choices = response_body.get("choices") or []
    reply = ""
    if choices:
        reply = ((choices[0].get("message") or {}).get("content") or "").strip()
    if not reply:
        log_json({
            "level": "ERROR",
            "message": "mistral_empty_reply",
            "model_id": MISTRAL_MODEL,
            "latency_seconds": round(time.time() - started_at, 2),
            "choice_count": len(choices),
        })
        return {
            "ok": False,
            "error": "empty_mistral_reply",
            "error_code": "empty_mistral_reply",
            "raw_response": response_body,
        }

    log_json({
        "level": "INFO",
        "message": "mistral_request_completed",
        "model_id": MISTRAL_MODEL,
        "latency_seconds": round(time.time() - started_at, 2),
        "choice_count": len(choices),
        "reply_length": len(reply),
    })

    return {
        "ok": True,
        "reply": reply,
        "summary": reply,
        "source": "mistral",
        "model_id": MISTRAL_MODEL,
    }


def invoke_image_llm(query):
    """Resolve screenshot text with the configured provider (Mistral by default,
    Gemini when IMAGE_LLM_PROVIDER=gemini). Each returns a `source` field the
    caller uses for telemetry labels."""
    if IMAGE_LLM_PROVIDER == "gemini":
        return invoke_gemini_from_text(query)
    return invoke_mistral_from_text(query)


def get_session_item(session_id):
    response = sessions_table.get_item(
        Key={
            "session_id": session_id
        }
    )
    return response.get("Item") or {}


def get_active_dm_session(channel, user, text=None):
    pointer_id = active_dm_pointer_id(channel, user)
    if not pointer_id:
        return {}

    pointer = get_session_item(pointer_id)
    active_session_id = pointer.get("active_session_id")
    if not active_session_id:
        return {}

    session_item = get_session_item(active_session_id)
    if not session_item or session_is_terminal(session_item):
        delete_active_dm_session(channel, user)
        return {}

    if not session_waits_for_dm_text(session_item, text):
        delete_active_dm_session(channel, user)
        return {}

    return session_item


def upsert_active_dm_session(channel, user, session_id, session_root_ts, now_iso):
    pointer_id = active_dm_pointer_id(channel, user)
    if not pointer_id or not session_id:
        return

    sessions_table.put_item(
        Item={
            "session_id": pointer_id,
            "active_session_id": session_id,
            "session_root_ts": session_root_ts,
            "channel": channel,
            "user": user,
            "session_id_version": "v2_dm_active_pointer",
            "session_scope": "one_to_one_dm_active_issue",
            "updated_at": now_iso,
            "ttl": ttl_epoch(),
        }
    )


def delete_active_dm_session(channel, user):
    pointer_id = active_dm_pointer_id(channel, user)
    if not pointer_id:
        return

    sessions_table.update_item(
        Key={
            "session_id": pointer_id
        },
        UpdateExpression="""
            SET
                pointer_status = :inactive,
                updated_at = :updated_at,
                #ttl = :ttl
            REMOVE
                active_session_id,
                session_root_ts
        """,
        ExpressionAttributeNames={
            "#ttl": "ttl"
        },
        ExpressionAttributeValues={
            ":inactive": "inactive",
            ":updated_at": to_iso(datetime.now(timezone.utc)),
            ":ttl": ttl_epoch(),
        }
    )


def supersede_other_dm_sessions(channel, user, current_session_id, now_iso):
    if not channel or not user or not current_session_id:
        return

    try:
        response = sessions_table.scan(
            FilterExpression=(
                Attr("channel").eq(channel)
                & Attr("user").eq(user)
                & Attr("conversation_status").eq("active")
            ),
            ProjectionExpression="""
                session_id,
                session_root_ts,
                timeout_token,
                conversation_status,
                summary_status,
                next_action,
                assistance_status,
                support_options_status,
                jira_status,
                live_agent_status,
                live_agent_ticket_key,
                last_live_agent_ticket_key
            """,
        )
    except Exception as error:
        log_json({
            "level": "ERROR",
            "message": "dm_supersede_scan_failed",
            "channel": channel,
            "user": user,
            "current_session_id": current_session_id,
            "error": str(error),
        })
        return

    for item in response.get("Items") or []:
        old_session_id = item.get("session_id")
        if not old_session_id or old_session_id == current_session_id:
            continue

        if not str(old_session_id).startswith("issue:"):
            continue

        if session_waits_for_dm_text(item):
            continue

        try:
            sessions_table.update_item(
                Key={"session_id": old_session_id},
                UpdateExpression="""
                    SET
                        conversation_status = :closed,
                        session_state = :closed_state,
                        timeout_status = :superseded,
                        superseded_by_session_id = :current_session_id,
                        superseded_at = :now,
                        updated_at = :now,
                        #ttl = :ttl
                    REMOVE
                        timeout_due_at,
                        timeout_token,
                        timeout_schedule_name,
                        timeout_prompt_started_at,
                        timeout_prompted_at,
                        timeout_close_due_at,
                        timeout_closing_started_at,
                        timeout_closed_at
                """,
                ConditionExpression="conversation_status = :active",
                ExpressionAttributeNames={
                    "#ttl": "ttl",
                },
                ExpressionAttributeValues={
                    ":active": "active",
                    ":closed": "closed",
                    ":closed_state": SESSION_STATE_CLOSED,
                    ":superseded": "superseded",
                    ":current_session_id": current_session_id,
                    ":now": now_iso,
                    ":ttl": ttl_epoch(),
                },
            )
            delete_timeout_schedule(old_session_id, "prompt")
            delete_timeout_schedule(old_session_id, "close")
            log_json({
                "level": "INFO",
                "message": "dm_session_superseded",
                "session_id": old_session_id,
                "superseded_by_session_id": current_session_id,
            })
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                continue
            log_json({
                "level": "ERROR",
                "message": "dm_session_supersede_failed",
                "session_id": old_session_id,
                "superseded_by_session_id": current_session_id,
                "error": str(error),
            })
        except Exception as error:
            log_json({
                "level": "ERROR",
                "message": "dm_session_supersede_failed",
                "session_id": old_session_id,
                "superseded_by_session_id": current_session_id,
                "error": str(error),
            })


def has_pending_jira_confirmation(session_item):
    return (
        session_item.get("next_action") == NEXT_ACTION_CREATE_JIRA_TICKET
        and session_item.get("jira_status") == "pending_confirmation"
    )


def has_pending_assistance_confirmation(session_item):
    return (
        session_item.get("next_action") == NEXT_ACTION_CLAUDE_ASSISTANCE
        and session_item.get("assistance_status") == "pending_confirmation"
    )


def has_pending_assistance_details(session_item):
    return (
        session_item.get("next_action") == NEXT_ACTION_CLAUDE_ASSISTANCE
        and session_item.get("assistance_status") == "awaiting_details"
    )


def has_pending_mcp_query(session_item):
    return (
        session_item.get("next_action") == NEXT_ACTION_MCP_ASSIST
        and session_item.get("assistance_status") == "awaiting_mcp_query"
    )


def has_pending_final_support_options(session_item):
    return (
        session_item.get("next_action") == NEXT_ACTION_FINAL_SUPPORT_OPTIONS
        and session_item.get("support_options_status") == "pending"
    )


def session_waits_for_dm_text(session_item, text=None):
    if not session_item or session_is_terminal(session_item):
        return False

    if is_live_agent_dm_session_pending_or_active(session_item):
        return True

    if has_pending_jira_confirmation(session_item):
        return True

    if has_pending_assistance_details(session_item):
        return True

    if has_pending_mcp_query(session_item):
        return True

    if has_jira_confirmation_state(session_item, text or ""):
        return True

    return (
        session_item.get("support_options_status") in {"creating_jira", "live_agent_creating"}
        or session_item.get("live_agent_status") in {"creating", "in_progress"}
    )


def jira_status_is_recent(session_item, timestamp_field, max_age_seconds):
    started_at = parse_iso_datetime(session_item.get(timestamp_field))

    if not started_at:
        return False

    age = datetime.now(timezone.utc) - started_at
    return age.total_seconds() <= max_age_seconds


def has_jira_confirmation_state(session_item, text):
    if has_pending_jira_confirmation(session_item):
        return True

    jira_status = session_item.get("jira_status")
    decision = classify_jira_confirmation(text)

    if (
        session_item.get("next_action") == "O3_CreateJiraTicket"
        and jira_status == "creating"
    ):
        return True

    return (
        jira_status == "created"
        and decision == "yes"
        and bool(session_item.get("jira_ticket_key") or session_item.get("last_jira_ticket_key"))
        and jira_status_is_recent(
            session_item,
            "jira_created_at",
            JIRA_CREATED_DUPLICATE_WINDOW_SECONDS
        )
    )


def normalize_confirmation_text(text):
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    normalized = normalized.strip(" .,!?:;\"'")
    return normalized


def classify_jira_confirmation(text):
    normalized = normalize_confirmation_text(text)

    if normalized in JIRA_CONFIRM_YES:
        return "yes"

    if normalized in JIRA_CONFIRM_NO:
        return "no"

    return "unclear"


def ensure_jira_confirmation_prompt(reply):
    prompt = (reply or "").strip()

    if (
        re.search(r"\byes\b", prompt, flags=re.IGNORECASE)
        and re.search(r"\bno\b", prompt, flags=re.IGNORECASE)
    ):
        return prompt

    if not prompt:
        return "Do you want me to create a Jira ticket? Reply yes to create it, or no to cancel."

    return f"{prompt} Reply yes to create a Jira ticket, or no to cancel."


def jira_ticket_reply(ticket_key, ticket_url):
    if ticket_key and ticket_url:
        return f"Created Jira ticket {ticket_key}: {ticket_url}"

    if ticket_key:
        return f"Created Jira ticket {ticket_key}."

    return "Created the Jira ticket."


def existing_jira_ticket_reply(ticket_key, ticket_url):
    if ticket_key and ticket_url:
        return f"Jira ticket already created {ticket_key}: {ticket_url}"

    if ticket_key:
        return f"Jira ticket already created {ticket_key}."

    return "Jira ticket already created."


def live_agent_ticket_reply(ticket_key, ticket_url):
    if ticket_key and ticket_url:
        return f"Live agent support ticket created: {ticket_key} {ticket_url}"

    if ticket_key:
        return f"Live agent support ticket created: {ticket_key}"

    return LIVE_AGENT_DEFERRED_REPLY


def existing_live_agent_ticket_reply(ticket_key, ticket_url):
    if ticket_key and ticket_url:
        return f"Live agent support ticket already created: {ticket_key} {ticket_url}"

    if ticket_key:
        return f"Live agent support ticket already created: {ticket_key}"

    return "This issue has already been sent to live agent support. Please start a new message for a different issue."


def jira_failure_reply(jira_result):
    error_code = jira_result.get("error_code")
    status = jira_result.get("status")

    if error_code in {"jira_configuration_error", "missing_create_jira_ticket_function"}:
        return JIRA_CREATE_CONFIG_FAILED_REPLY

    if error_code in {"jira_network_error", "jira_timeout"}:
        return JIRA_CREATE_TIMEOUT_REPLY

    if error_code in {"jira_auth_or_permission_error", "jira_project_or_permission_error"}:
        return JIRA_CREATE_PERMISSION_FAILED_REPLY

    if status in {401, 403, 404}:
        return JIRA_CREATE_PERMISSION_FAILED_REPLY

    return JIRA_CREATE_FAILED_REPLY


def jira_creation_is_stale(session_item):
    create_started_at = parse_iso_datetime(session_item.get("jira_create_started_at"))

    if not create_started_at:
        return True

    age = datetime.now(timezone.utc) - create_started_at
    return age.total_seconds() > JIRA_CREATING_STALE_SECONDS


def acquire_jira_creation_lock(session_id, jira_request_id, event_id, now_iso):
    try:
        sessions_table.update_item(
            Key={
                "session_id": session_id
            },
            UpdateExpression="""
                SET
                    jira_status = :creating,
                    jira_request_id = :jira_request_id,
                    jira_confirm_event_id = :event_id,
                    jira_confirmed_at = :now,
                    jira_create_started_at = :now,
                    updated_at = :now
                REMOVE
                    jira_error,
                    jira_error_code,
                    jira_error_status
            """,
            ConditionExpression="""
                next_action = :next_action
                AND jira_status = :pending_confirmation
            """,
            ExpressionAttributeValues={
                ":creating": "creating",
                ":jira_request_id": jira_request_id,
                ":event_id": event_id or "unknown-event",
                ":now": now_iso,
                ":next_action": "O3_CreateJiraTicket",
                ":pending_confirmation": "pending_confirmation"
            }
        )

        log_json({
            "level": "INFO",
            "message": "jira_creation_lock_acquired",
            "session_id": session_id,
            "jira_request_id": jira_request_id
        })
        return True

    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log_json({
                "level": "INFO",
                "message": "jira_creation_lock_conflict",
                "session_id": session_id,
                "jira_request_id": jira_request_id
            })
            return False

        raise


def existing_jira_state_result(session_item, base_result):
    ticket_key = session_item.get("jira_ticket_key") or session_item.get("last_jira_ticket_key")
    ticket_url = session_item.get("jira_ticket_url") or session_item.get("last_jira_ticket_url")
    jira_status = session_item.get("jira_status")
    preserved_metadata = {
        "jira_request_id": session_item.get("jira_request_id") or base_result.get("jira_request_id"),
        "jira_requested_at": session_item.get("jira_requested_at") or base_result.get("jira_requested_at"),
        "jira_confirmed_at": session_item.get("jira_confirmed_at"),
        "jira_create_started_at": session_item.get("jira_create_started_at"),
        "jira_created_at": session_item.get("jira_created_at"),
        "last_jira_ticket_key": session_item.get("last_jira_ticket_key"),
        "last_jira_ticket_url": session_item.get("last_jira_ticket_url"),
        "last_jira_created_at": session_item.get("last_jira_created_at"),
    }

    if jira_status == "created":
        return {
            **base_result,
            **preserved_metadata,
            "lex_state": "Fulfilled",
            "response_source": "jira",
            "next_action": None,
            "jira_status": "created",
            "jira_ticket_key": ticket_key,
            "jira_ticket_url": ticket_url,
            "last_jira_ticket_key": ticket_key,
            "last_jira_ticket_url": ticket_url,
            "last_jira_created_at": session_item.get("jira_created_at") or session_item.get("last_jira_created_at"),
            "reply": existing_jira_ticket_reply(ticket_key, ticket_url),
        }

    if jira_status == "creating" and jira_creation_is_stale(session_item):
        return {
            **base_result,
            **preserved_metadata,
            "lex_state": "Failed",
            "response_source": "jira",
            "next_action": None,
            "jira_status": "create_failed",
            "jira_error": "stale_jira_creation",
            "jira_error_code": "stale_jira_creation",
            "reply": JIRA_CREATE_STALE_REPLY,
        }

    if jira_status == "creating":
        return {
            **base_result,
            **preserved_metadata,
            "lex_state": "InProgress",
            "response_source": "jira",
            "jira_status": "creating",
            "reply": JIRA_CREATE_IN_PROGRESS_REPLY,
        }

    return {
        **base_result,
        "reply": JIRA_UNCLEAR_CONFIRMATION_REPLY,
    }


def build_jira_payload(session_item, body, session_id, text, raw_text):
    intent_name = (
        session_item.get("jira_intent_name")
        or session_item.get("lex_intent")
        or "UNKNOWN"
    )
    request_text = (
        session_item.get("jira_request_text")
        or session_item.get("last_user_text")
        or raw_text
        or text
    )
    request_raw_text = session_item.get("last_raw_user_text") or request_text
    summary_result = build_request_conversation_summary({
        **session_item,
        "session_id": session_id,
    })
    conversation_text = request_conversation_text(session_item)
    session_root_ts = session_item.get("session_root_ts") or session_item.get("thread_ts") or body.get("thread_ts")
    conversation_metadata = session_item.get("conversation_metadata") or {}
    slack_thread_ts = None if is_one_to_one_dm_conversation(conversation_metadata) else (
        session_item.get("thread_ts") or body.get("thread_ts") or session_root_ts
    )

    return {
        "jira_request_id": session_item.get("jira_request_id"),
        "jira_requested_at": session_item.get("jira_requested_at"),
        "jira_confirmed_at": session_item.get("jira_confirmed_at"),
        "event_id": body.get("event_id"),
        "session_id": session_id,
        "session_root_ts": session_root_ts,
        "channel": body.get("channel"),
        "user": body.get("user"),
        "text": request_text,
        "raw_text": request_raw_text,
        "conversation_summary": summary_result.get("text"),
        "conversation_text": conversation_text,
        "conversation_summary_model_id": summary_result.get("model_id"),
        "conversation_summary_usage": summary_result.get("usage", {}),
        "conversation_summary_error": summary_result.get("error"),
        "conversation_summary_fallback_used": summary_result.get("fallback_used", False),
        "confirmation_text": text,
        "lex": {
            "intent": intent_name,
            "state": session_item.get("lex_state"),
            "slots": session_item.get("lex_slots", {})
        },
        "slack": {
            "channel": body.get("channel"),
            "user": body.get("user"),
            "event_ts": body.get("ts"),
            "thread_ts": slack_thread_ts,
        }
    }


def split_rovo_support_text(text):
    value = (text or "").strip()
    marker = "\n\nUser follow-up:\n"

    if value.startswith("Original question:\n") and marker in value:
        original, followup = value[len("Original question:\n"):].split(marker, 1)
        return original.strip(), followup.strip()

    return value, ""


def build_rovo_payload(
    body,
    session_id,
    lex_intent,
    lex_state,
    lex_slots,
    jira_request_id,
    jira_requested_at,
    jira_created_at,
    jira_ticket_key,
    jira_ticket_url,
    jira_request_text,
    raw_text,
    support_original_text_value=None,
    support_raw_text_value=None,
    support_lex_reply=None,
    support_claude_reply=None,
    support_claude_error=None,
):
    support_original, user_followup = split_rovo_support_text(support_original_text_value)
    support_raw, raw_followup = split_rovo_support_text(support_raw_text_value)
    original_request = support_original or jira_request_text or raw_text or body.get("text") or ""
    original_raw_request = support_raw or raw_text or original_request
    request_text = jira_request_text or original_request
    followup_text = user_followup or raw_followup

    return {
        "jira_request_id": jira_request_id,
        "jira_requested_at": jira_requested_at,
        "jira_created_at": jira_created_at,
        "ticket_key": jira_ticket_key,
        "ticket_url": jira_ticket_url,
        "session_id": session_id,
        "channel": body.get("channel"),
        "user": body.get("user"),
        "text": request_text,
        "raw_text": raw_text or original_raw_request or request_text,
        "request": {
            "original_text": original_request,
            "raw_text": original_raw_request,
            "user_followup": followup_text,
            "jira_request_text": jira_request_text or request_text,
        },
        "answers": {
            "lex_answer_shown": support_lex_reply,
            "claude_answer": support_claude_reply,
            "claude_error": support_claude_error,
        },
        "lex": {
            "intent": lex_intent,
            "state": lex_state,
            "slots": lex_slots or {}
        },
        "slack": {
            "channel": body.get("channel"),
            "user": body.get("user"),
            "event_ts": body.get("ts"),
            "thread_ts": body.get("thread_ts"),
        }
    }


def mark_rovo_invoke_failed(session_id, error, error_code):
    failed_at = to_iso(datetime.now(timezone.utc))

    try:
        sessions_table.update_item(
            Key={
                "session_id": session_id
            },
            UpdateExpression="""
                SET
                    rovo_status = :failed,
                    rovo_error = :error,
                    rovo_error_code = :error_code,
                    rovo_enriched_at = :failed_at,
                    updated_at = :failed_at
            """,
            ExpressionAttributeValues={
                ":failed": "failed",
                ":error": error,
                ":error_code": error_code,
                ":failed_at": failed_at
            }
        )

    except Exception as e:
        log_json({
            "level": "ERROR",
            "message": "rovo_invoke_failure_update_failed",
            "session_id": session_id,
            "error": str(e),
            "original_error": error,
            "original_error_code": error_code
        })


def mark_mcp_invoke_failed(session_id, request_id, error, error_code):
    failed_at = to_iso(datetime.now(timezone.utc))
    try:
        sessions_table.update_item(
            Key={"session_id": session_id},
            UpdateExpression="""
                SET
                    workflow_state = :workflow_state,
                    mcp_status = :status,
                    mcp_errors = :errors,
                    mcp_completed_at = :failed_at,
                    next_action = :next_action,
                    updated_at = :failed_at,
                    last_updated_at = :failed_at
            """,
            ExpressionAttributeValues={
                ":workflow_state": "MCP_ERROR",
                ":status": "ERROR",
                ":errors": [{
                    "source": "mcp",
                    "category": error_code or "mcp_assist_enqueue_failed",
                    "message": error or "MCP assist enqueue failed",
                }],
                ":failed_at": failed_at,
                ":next_action": NEXT_ACTION_CLAUDE_ASSISTANCE,
                ":request_id": request_id or "",
            },
            ConditionExpression="attribute_not_exists(mcp_request_id) OR mcp_request_id = :request_id",
        )

    except Exception as e:
        log_json({
            "level": "ERROR",
            "message": "mcp_invoke_failure_update_failed",
            "session_id": session_id,
            "request_id": request_id,
            "error": str(e),
            "original_error": error,
            "original_error_code": error_code,
        })


def store_rovo_slack_message_target(session_id, slack_ts, slack_text):
    if not (session_id and slack_ts):
        return

    try:
        sessions_table.update_item(
            Key={
                "session_id": session_id
            },
            UpdateExpression="""
                SET
                    rovo_slack_message_ts = :slack_ts,
                    rovo_slack_original_text = :slack_text,
                    updated_at = :updated_at
            """,
            ExpressionAttributeValues={
                ":slack_ts": slack_ts,
                ":slack_text": slack_text or "",
                ":updated_at": to_iso(datetime.now(timezone.utc)),
            }
        )

    except Exception as e:
        log_json({
            "level": "ERROR",
            "message": "rovo_slack_target_update_failed",
            "session_id": session_id,
            "error": str(e),
        })


def mark_session_summarizing(session_id, timeout_token_value, started_at):
    remove_attributes = [
        "next_action",
        "jira_status",
        "jira_intent_name",
        "jira_request_text",
        "jira_request_id",
        "jira_requested_at",
        "jira_confirmed_at",
        "jira_create_started_at",
        "jira_error",
        "jira_error_code",
        "jira_error_status",
        "assistance_status",
        "assistance_original_text",
        "assistance_raw_text",
        "assistance_lex_intent",
        "assistance_lex_state",
        "assistance_lex_slots",
        "assistance_lex_reply",
        "assistance_requested_at",
        "assistance_followup_text",
        "assistance_followup_raw_text",
        "assistance_followup_at",
        "support_options_status",
        "support_original_text",
        "support_raw_text",
        "support_lex_intent",
        "support_lex_state",
        "support_lex_slots",
        "support_lex_reply",
        "support_claude_reply",
        "support_claude_error",
        "support_requested_at",
        "live_agent_status",
        "live_agent_requested_at",
        "live_agent_error",
        "live_agent_error_code",
        "timeout_due_at",
        "timeout_schedule_name",
        "timeout_prompt_started_at",
        "timeout_prompted_at",
        "timeout_close_due_at",
    ]
    now_iso = to_iso(started_at)

    expression_values = {
        ":summarizing": "summarizing",
        ":session_state": SESSION_STATE_SUMMARIZING,
        ":manual_reason": "button_close_summary",
        ":now": now_iso,
        ":ttl": ttl_epoch(),
        ":active": "active",
    }
    condition = "conversation_status = :active"

    if timeout_token_value:
        condition += " AND timeout_token = :timeout_token"
        expression_values[":timeout_token"] = timeout_token_value

    sessions_table.update_item(
        Key={"session_id": session_id},
        UpdateExpression=f"""
            SET
                conversation_status = :summarizing,
                session_state = :session_state,
                timeout_status = :summarizing,
                summary_status = :summarizing,
                summary_started_at = :now,
                manual_close_reason = :manual_reason,
                updated_at = :now,
                #ttl = :ttl
            REMOVE {", ".join(remove_attributes)}
        """,
        ConditionExpression=condition,
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues=expression_values,
    )


def base_interactive_result(session_item):
    return {
        "lex_intent": session_item.get("lex_intent") or "INTERACTIVE_ACTION",
        "lex_state": "InProgress",
        "lex_slots": session_item.get("lex_slots", {}),
        "response_source": "interactive_action",
        "next_action": None,
        "reply": "That action is no longer active. Please send a new message.",
        "blocks": None,
        "jira_status": None,
        "jira_intent_name": None,
        "jira_request_text": None,
        "jira_request_id": None,
        "jira_requested_at": None,
        "jira_confirmed_at": None,
        "jira_create_started_at": None,
        "jira_created_at": None,
        "jira_ticket_key": None,
        "jira_ticket_url": None,
        "jira_error": None,
        "jira_error_code": None,
        "jira_error_status": None,
        "last_jira_ticket_key": None,
        "last_jira_ticket_url": None,
        "last_jira_created_at": None,
        "rovo_status": None,
        "rovo_requested_at": None,
        "rovo_enriched_at": None,
        "rovo_error": None,
        "rovo_error_code": None,
        "rovo_should_invoke": False,
        "claude_fallback_attempted": False,
        "claude_fallback_error": None,
        "claude_model_id": None,
        "assistance_status": None,
        "assistance_original_text": None,
        "assistance_raw_text": None,
        "assistance_lex_intent": None,
        "assistance_lex_state": None,
        "assistance_lex_slots": None,
        "assistance_lex_reply": None,
        "assistance_requested_at": None,
        "assistance_closed_at": None,
        "assistance_resolved_at": None,
        "support_options_status": None,
        "support_original_text": None,
        "support_raw_text": None,
        "support_lex_intent": None,
        "support_lex_state": None,
        "support_lex_slots": None,
        "support_lex_reply": None,
        "support_claude_reply": None,
        "support_claude_error": None,
        "support_requested_at": None,
        "support_resolved_at": None,
        "live_agent_status": None,
        "live_agent_requested_at": None,
        "live_agent_ticket_key": None,
        "live_agent_ticket_url": None,
        "live_agent_jira_project": None,
        "live_agent_issue_type": None,
        "live_agent_portal_request_type": None,
        "live_agent_updated_at": None,
        "last_live_agent_ticket_key": None,
        "last_live_agent_ticket_url": None,
        "live_agent_error": None,
        "live_agent_error_code": None,
        "manual_close_summary": False,
        "manual_close_timeout_token": None,
        "workflow_state": None,
        "state_version": None,
        "mcp_status": None,
        "mcp_request_id": None,
        "mcp_attempt_count": None,
        "mcp_answer": None,
        "mcp_confidence": None,
        "mcp_confidence_reasons": None,
        "mcp_sources_queried": None,
        "mcp_sources_used": None,
        "mcp_citations": None,
        "mcp_errors": None,
        "mcp_requested_at": None,
        "mcp_started_at": None,
        "mcp_completed_at": None,
        "mcp_latency_ms": None,
        "mcp_should_invoke": False,
    }


def build_assistance_claude_payload(session_item, body, session_id, followup_text=None, followup_raw_text=None):
    original_text = (
        session_item.get("assistance_original_text")
        or session_item.get("last_user_text")
        or ""
    )
    raw_text = (
        session_item.get("assistance_raw_text")
        or session_item.get("last_raw_user_text")
        or original_text
    )
    lex_reply = (
        session_item.get("assistance_lex_reply")
        or session_item.get("last_bot_reply")
        or ""
    )
    user_followup = (followup_text or session_item.get("assistance_followup_text") or original_text).strip()
    raw_followup = followup_raw_text or session_item.get("assistance_followup_raw_text") or user_followup

    return {
        "event_id": body.get("event_id"),
        "session_id": session_id,
        "channel": body.get("channel"),
        "channel_type": body.get("channel_type"),
        "routing_reason": "lex_assistance_details",
        "user": body.get("user"),
        "text": user_followup,
        "raw_text": raw_followup,
        "lex": {
            "intent": session_item.get("assistance_lex_intent") or session_item.get("lex_intent"),
            "state": session_item.get("assistance_lex_state") or session_item.get("lex_state"),
            "slots": session_item.get("assistance_lex_slots") or session_item.get("lex_slots", {}),
            "reply": slack_mrkdwn(lex_reply, 900)
        },
        "assistance": {
            "original_question": original_text,
            "user_followup": user_followup,
            "lex_answer_summary": slack_mrkdwn(lex_reply, 900)
        },
        "session": {
            "conversation_status": "active",
            "trigger": "lex_assistance_details"
        }
    }


def make_mcp_request_id(session_id, event_id, action_id):
    raw = "|".join([session_id or "", event_id or "", action_id or ACTION_ID_MCP_ASSIST])
    return "mcp-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def terminal_mcp_status(value):
    return value in {"ANSWER", "NO_ANSWER", "AUTH_REQUIRED", "PARTIAL", "ERROR"}


def has_terminal_mcp_state(session_item):
    return (
        terminal_mcp_status(session_item.get("mcp_status"))
        or session_item.get("workflow_state") in {
            "MCP_ANSWERED",
            "MCP_NO_ANSWER",
            "MCP_AUTH_REQUIRED",
            "MCP_PARTIAL",
            "MCP_ERROR",
        }
    )


def build_mcp_assist_payload(session_item, body, session_id, request_id, channel, thread_ts, user):
    original_question = (
        session_item.get("mcp_query_text")
        or session_item.get("support_original_text")
        or session_item.get("assistance_original_text")
        or session_item.get("last_user_text")
        or body.get("text")
        or ""
    )
    lex_reply = (
        session_item.get("assistance_lex_reply")
        or session_item.get("last_bot_reply")
        or ""
    )
    transcript = request_summary_transcript(session_item, limit=12)
    return {
        "schema_version": "1.0",
        "request_id": request_id,
        "session_id": session_id,
        "question": original_question,
        "lex": {
            "intent": session_item.get("assistance_lex_intent") or session_item.get("lex_intent"),
            "state": session_item.get("assistance_lex_state") or session_item.get("lex_state"),
            "slots": session_item.get("assistance_lex_slots") or session_item.get("lex_slots", {}),
            "reply": lex_reply,
        },
        "slack": {
            "channel_id": channel,
            "thread_ts": thread_ts,
            "user_id": user,
            "message_ts": body.get("message_ts") or body.get("ts"),
        },
        "conversation": {
            "thread_messages": transcript,
            "conversation_type": body.get("conversation_type") or body.get("channel_type"),
        },
        "access_policy": {
            "identity_mode": os.environ.get("MCP_IDENTITY_MODE", "mixed"),
        },
        "correlation": {
            "event_id": body.get("event_id"),
            "action_id": body.get("action_id"),
        },
    }


def support_original_text(session_item):
    return (
        session_item.get("support_original_text")
        or session_item.get("assistance_original_text")
        or session_item.get("jira_request_text")
        or session_item.get("last_user_text")
        or ""
    )


def support_raw_text(session_item):
    return (
        session_item.get("support_raw_text")
        or session_item.get("assistance_raw_text")
        or session_item.get("last_raw_user_text")
        or support_original_text(session_item)
    )


def build_support_jira_request_text(session_item):
    parts = []
    original_text = support_original_text(session_item)
    lex_reply = session_item.get("support_lex_reply") or session_item.get("assistance_lex_reply")
    claude_reply = session_item.get("support_claude_reply")
    claude_error = session_item.get("support_claude_error")

    if original_text:
        parts.append(f"User request:\n{original_text}")

    if lex_reply:
        parts.append(f"Lex answer already shown:\n{lex_reply}")

    if claude_reply:
        parts.append(f"Claude follow-up answer:\n{claude_reply}")
    elif claude_error:
        parts.append(f"Claude follow-up result:\nUnable to resolve automatically ({claude_error}).")

    return "\n\n".join(parts) or original_text or "User requested support from IVY."


def compact_text(value, limit=1200):
    text = re.sub(r"\s+", " ", (value or "").strip())

    if len(text) <= limit:
        return text

    return text[:limit - 3].rstrip() + "..."


def request_summary_transcript(session_item, limit=40):
    messages = session_item.get("session_messages") or []
    transcript = []

    if isinstance(messages, list):
        for message in messages[-limit:]:
            if not isinstance(message, dict):
                continue

            sender = compact_text(message.get("sender") or "Unknown", 80)
            text = compact_text(message.get("text"), 1200)
            if text:
                transcript.append({
                    "sender": sender,
                    "text": text,
                    "ts": message.get("ts"),
                    "recorded_at": message.get("recorded_at")
                })

    if transcript:
        return transcript

    fallback_entries = [
        ("User", support_original_text(session_item)),
        ("IVY Lex", session_item.get("support_lex_reply") or session_item.get("assistance_lex_reply")),
        ("IVY Claude", session_item.get("support_claude_reply")),
        (
            "IVY Claude",
            f"Unable to resolve automatically ({session_item.get('support_claude_error')})."
            if session_item.get("support_claude_error")
            else None
        ),
        ("User", session_item.get("last_user_text")),
        ("IVY", session_item.get("last_bot_reply")),
    ]

    seen = set()
    for sender, text in fallback_entries:
        clean_text = compact_text(text, 1200)
        if not clean_text:
            continue

        key = (sender, clean_text)
        if key in seen:
            continue

        seen.add(key)
        transcript.append({
            "sender": sender,
            "text": clean_text,
            "ts": None,
            "recorded_at": None
        })

    return transcript


def transcript_for_request_summary(transcript):
    lines = []

    for message in transcript or []:
        sender = compact_text(message.get("sender") or "Unknown", 80)
        text = compact_text(message.get("text"), 1200)
        if sender and text:
            lines.append(f"{sender}: {text}")

    return "\n".join(lines)


def request_conversation_text(session_item):
    return transcript_for_request_summary(request_summary_transcript(session_item))


def deterministic_request_summary(session_item, transcript):
    parts = []
    original_text = compact_text(support_original_text(session_item), 800)
    lex_reply = compact_text(session_item.get("support_lex_reply") or session_item.get("assistance_lex_reply"), 800)
    claude_reply = compact_text(session_item.get("support_claude_reply"), 800)
    claude_error = compact_text(session_item.get("support_claude_error"), 300)
    last_bot_reply = compact_text(session_item.get("last_bot_reply"), 800)

    if original_text:
        parts.append(f"User request: {original_text}")

    if lex_reply:
        parts.append(f"Lex answer shown: {lex_reply}")

    if claude_reply:
        parts.append(f"Claude answer shown: {claude_reply}")
    elif claude_error:
        parts.append(f"Claude fallback result: unable to resolve automatically ({claude_error}).")

    if not lex_reply and not claude_reply and last_bot_reply:
        parts.append(f"Last IVY reply: {last_bot_reply}")

    if not parts and transcript:
        first_message = transcript[0]
        parts.append(f"Conversation: {compact_text(first_message.get('text'), 1000)}")

    return "\n".join(parts) or "User requested support from IVY."


def request_summary_prompt(session_item, transcript, fallback_summary):
    return "\n".join([
        "Summarize this IVY Slack support conversation for a human IT support agent.",
        "Use only the supplied transcript and metadata.",
        "Include the user's issue, what IVY already answered or tried, and what still needs agent attention.",
        "Do not invent ticket keys, user names, troubleshooting steps, or resolution details.",
        "Return 2-4 concise sentences only.",
        "",
        "Session metadata:",
        compact_json({
            "session_id": session_item.get("session_id"),
            "lex_intent": session_item.get("support_lex_intent") or session_item.get("lex_intent"),
            "lex_state": session_item.get("support_lex_state") or session_item.get("lex_state"),
            "response_source": session_item.get("response_source"),
            "jira_status": session_item.get("jira_status"),
        }),
        "",
        "Deterministic context:",
        fallback_summary,
        "",
        "Transcript:",
        transcript_for_request_summary(transcript),
    ])


def extract_converse_text(response):
    parts = []
    message = ((response or {}).get("output") or {}).get("message") or {}

    for item in message.get("content", []):
        text = (item.get("text") or "").strip()
        if text:
            parts.append(text)

    return "\n".join(parts).strip()


def build_request_conversation_summary(session_item):
    transcript = request_summary_transcript(session_item)
    fallback_summary = deterministic_request_summary(session_item, transcript)

    if not ENABLE_REQUEST_AI_SUMMARY:
        return {
            "text": fallback_summary,
            "model_id": None,
            "usage": {},
            "error": None,
            "fallback_used": True,
            "transcript": transcript,
        }

    try:
        response = bedrock_runtime.converse(
            modelId=REQUEST_SUMMARY_MODEL_ID,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "text": request_summary_prompt(session_item, transcript, fallback_summary)
                        }
                    ]
                }
            ],
            inferenceConfig={
                "maxTokens": REQUEST_SUMMARY_MAX_TOKENS,
                "temperature": REQUEST_SUMMARY_TEMPERATURE,
            },
        )
        summary_text = extract_converse_text(response)

        if not summary_text:
            raise ValueError("Bedrock returned an empty request summary")

        return {
            "text": summary_text,
            "model_id": REQUEST_SUMMARY_MODEL_ID,
            "usage": response.get("usage", {}),
            "error": None,
            "fallback_used": False,
            "transcript": transcript,
        }

    except Exception as e:
        log_json({
            "level": "WARN",
            "message": "request_conversation_summary_failed",
            "model_id": REQUEST_SUMMARY_MODEL_ID,
            "error": str(e),
        })
        return {
            "text": fallback_summary,
            "model_id": REQUEST_SUMMARY_MODEL_ID,
            "usage": {},
            "error": str(e),
            "fallback_used": True,
            "transcript": transcript,
        }


def build_final_support_result(session_item, claude_result, now_iso):
    result = base_interactive_result(session_item)
    original_text = (
        session_item.get("assistance_original_text")
        or session_item.get("support_original_text")
        or session_item.get("last_user_text")
        or ""
    )
    raw_text = (
        session_item.get("assistance_raw_text")
        or session_item.get("support_raw_text")
        or session_item.get("last_raw_user_text")
        or original_text
    )
    followup_text = session_item.get("assistance_followup_text")
    followup_raw_text = session_item.get("assistance_followup_raw_text") or followup_text
    lex_reply = (
        session_item.get("assistance_lex_reply")
        or session_item.get("support_lex_reply")
        or session_item.get("last_bot_reply")
        or ""
    )
    claude_reply = (claude_result.get("reply") or "").strip()
    claude_ok = bool(claude_result.get("ok") and claude_reply)
    claude_disabled = claude_result.get("error") == "claude_fallback_disabled"
    final_answer = claude_reply if claude_ok else (
        lex_reply if claude_disabled and lex_reply else CLAUDE_UNRESOLVED_REPLY
    )
    support_text = original_text
    support_raw = raw_text

    if followup_text:
        support_text = "\n\n".join([
            f"Original question:\n{original_text}",
            f"User follow-up:\n{followup_text}"
        ])
        support_raw = "\n\n".join([
            f"Original question:\n{raw_text}",
            f"User follow-up:\n{followup_raw_text}"
        ])

    result.update({
        "lex_intent": session_item.get("assistance_lex_intent") or session_item.get("lex_intent") or "ClaudeAssistance",
        "lex_state": "Fulfilled" if claude_ok else "Failed",
        "lex_slots": session_item.get("assistance_lex_slots") or session_item.get("lex_slots", {}),
        "response_source": "claude" if claude_ok else "claude_failed",
        "next_action": NEXT_ACTION_FINAL_SUPPORT_OPTIONS,
        "reply": final_support_reply_text(final_answer),
        "blocks": final_support_blocks(
            final_answer,
            session_item.get("session_id"),
            session_item.get("session_root_ts") or session_item.get("thread_ts"),
        ),
        "claude_fallback_attempted": True,
        "claude_fallback_error": None if claude_ok else claude_result.get("error", "unknown_claude_error"),
        "claude_model_id": claude_result.get("model_id"),
        "assistance_resolved_at": now_iso,
        "support_options_status": "pending",
        "support_original_text": support_text,
        "support_raw_text": support_raw,
        "support_lex_intent": session_item.get("assistance_lex_intent") or session_item.get("lex_intent"),
        "support_lex_state": session_item.get("assistance_lex_state") or session_item.get("lex_state"),
        "support_lex_slots": session_item.get("assistance_lex_slots") or session_item.get("lex_slots", {}),
        "support_lex_reply": lex_reply,
        "support_claude_reply": claude_reply if claude_ok else None,
        "support_claude_error": None if claude_ok else claude_result.get("error", "unknown_claude_error"),
        "support_requested_at": now_iso,
    })
    return result


def acquire_support_jira_creation_lock(session_id, jira_request_id, event_id, now_iso):
    try:
        sessions_table.update_item(
            Key={
                "session_id": session_id
            },
            UpdateExpression="""
                SET
                    next_action = :jira_next_action,
                    support_options_status = :creating_jira,
                    jira_status = :creating,
                    jira_request_id = :jira_request_id,
                    jira_confirm_event_id = :event_id,
                    jira_confirmed_at = :now,
                    jira_create_started_at = :now,
                    updated_at = :now
                REMOVE
                    jira_error,
                    jira_error_code,
                    jira_error_status
            """,
            ConditionExpression="""
                (
                    next_action = :support_next_action
                    AND support_options_status = :support_pending
                )
                OR (
                    next_action = :assistance_next_action
                    AND assistance_status IN (:assistance_pending_confirmation, :awaiting_details)
                )
            """,
            ExpressionAttributeValues={
                ":jira_next_action": NEXT_ACTION_CREATE_JIRA_TICKET,
                ":support_next_action": NEXT_ACTION_FINAL_SUPPORT_OPTIONS,
                ":assistance_next_action": NEXT_ACTION_CLAUDE_ASSISTANCE,
                ":creating_jira": "creating_jira",
                ":creating": "creating",
                ":jira_request_id": jira_request_id,
                ":event_id": event_id or "unknown-event",
                ":now": now_iso,
                ":support_pending": "pending",
                ":assistance_pending_confirmation": "pending_confirmation",
                ":awaiting_details": "awaiting_details"
            }
        )

        log_json({
            "level": "INFO",
            "message": "support_jira_creation_lock_acquired",
            "session_id": session_id,
            "jira_request_id": jira_request_id
        })
        return True

    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log_json({
                "level": "INFO",
                "message": "support_jira_creation_lock_conflict",
                "session_id": session_id,
                "jira_request_id": jira_request_id
            })
            return False

        raise


def handle_support_create_jira(session_item, body, session_id, now_iso):
    result = base_interactive_result(session_item)
    intent_name = (
        session_item.get("support_lex_intent")
        or session_item.get("assistance_lex_intent")
        or session_item.get("lex_intent")
        or "ClaudeAssistance"
    )
    request_text = build_support_jira_request_text(session_item)
    jira_request_id = (
        session_item.get("jira_request_id")
        or make_jira_request_id(
            session_id,
            session_item.get("last_event_id") or body.get("event_id"),
            intent_name,
            request_text
        )
    )
    jira_requested_at = (
        session_item.get("jira_requested_at")
        or session_item.get("support_requested_at")
        or now_iso
    )

    result.update({
        "lex_intent": intent_name,
        "lex_state": "InProgress",
        "lex_slots": session_item.get("support_lex_slots") or session_item.get("lex_slots", {}),
        "response_source": "jira",
        "next_action": NEXT_ACTION_CREATE_JIRA_TICKET,
        "jira_status": "creating",
        "jira_intent_name": intent_name,
        "jira_request_text": request_text,
        "jira_request_id": jira_request_id,
        "jira_requested_at": jira_requested_at,
        "jira_confirmed_at": now_iso,
        "jira_create_started_at": now_iso,
        "support_options_status": "creating_jira",
        "support_original_text": session_item.get("support_original_text"),
        "support_raw_text": session_item.get("support_raw_text"),
        "support_lex_intent": session_item.get("support_lex_intent"),
        "support_lex_state": session_item.get("support_lex_state"),
        "support_lex_slots": session_item.get("support_lex_slots"),
        "support_lex_reply": session_item.get("support_lex_reply"),
        "support_claude_reply": session_item.get("support_claude_reply"),
        "support_claude_error": session_item.get("support_claude_error"),
        "support_requested_at": session_item.get("support_requested_at") or jira_requested_at,
    })

    if session_item.get("jira_status") in {"creating", "created"}:
        return existing_jira_state_result(session_item, result)

    if not (
        has_pending_final_support_options(session_item)
        or has_pending_assistance_confirmation(session_item)
        or has_pending_assistance_details(session_item)
    ):
        result.update({
            "lex_state": "Ignored",
            "response_source": "interactive_stale",
            "next_action": None,
            "jira_status": None,
            "reply": "That Jira ticket action is no longer active. Please send a new message."
        })
        return result

    if not acquire_support_jira_creation_lock(
        session_id,
        jira_request_id,
        body.get("event_id"),
        now_iso
    ):
        latest_session = get_session_item(session_id)
        if latest_session.get("jira_status") in {"creating", "created"}:
            return existing_jira_state_result(latest_session, result)

        result.update({
            "lex_state": "Ignored",
            "response_source": "interactive_stale",
            "next_action": None,
            "jira_status": None,
            "reply": "That Jira ticket action is no longer active. Please send a new message."
        })
        return result

    locked_session = {
        **session_item,
        "jira_request_id": jira_request_id,
        "jira_requested_at": jira_requested_at,
        "jira_confirmed_at": now_iso,
        "jira_create_started_at": now_iso,
        "jira_intent_name": intent_name,
        "jira_request_text": request_text,
        "last_raw_user_text": support_raw_text(session_item)
    }
    jira_result = invoke_create_jira_ticket(
        build_jira_payload(
            locked_session,
            body,
            session_id,
            body.get("action_value") or body.get("text") or "create_jira_ticket",
            support_raw_text(session_item)
        )
    )

    if jira_result.get("ok"):
        ticket_key = jira_result.get("ticket_key")
        ticket_url = jira_result.get("ticket_url")
        jira_created_at = to_iso(datetime.now(timezone.utc))
        result.update({
            "lex_state": "Fulfilled",
            "next_action": None,
            "jira_status": "created",
            "jira_created_at": jira_created_at,
            "jira_ticket_key": ticket_key,
            "jira_ticket_url": ticket_url,
            "last_jira_ticket_key": ticket_key,
            "last_jira_ticket_url": ticket_url,
            "last_jira_created_at": jira_created_at,
            "rovo_status": "pending" if ENABLE_ROVO_ENRICHMENT else None,
            "rovo_should_invoke": ENABLE_ROVO_ENRICHMENT,
            "support_options_status": "jira_created",
            "support_resolved_at": jira_created_at,
            "reply": jira_ticket_reply(ticket_key, ticket_url),
        })
        return result

    result.update({
        "lex_state": "Failed",
        "next_action": None,
        "jira_status": "create_failed",
        "jira_error": jira_result.get("error", "unknown_jira_error"),
        "jira_error_code": jira_result.get("error_code", "jira_create_failed"),
        "jira_error_status": jira_result.get("status"),
        "support_options_status": "jira_create_failed",
        "support_resolved_at": now_iso,
        "reply": jira_failure_reply(jira_result),
    })
    return result


def get_live_agent_config():
    if not live_agent_config_table:
        return None

    try:
        response = live_agent_config_table.get_item(
            Key={"intentName": LIVE_AGENT_CONFIG_INTENT}
        )
        return response.get("Item")

    except ClientError as e:
        log_json({
            "level": "WARN",
            "message": "live_agent_config_lookup_failed",
            "intent_name": LIVE_AGENT_CONFIG_INTENT,
            "error": str(e),
        })
        return None


def parse_config_parameters(config):
    parameters = (config or {}).get("parameters")
    if isinstance(parameters, dict):
        return parameters

    if isinstance(parameters, str) and parameters.strip():
        try:
            parsed = json.loads(parameters)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            log_json({
                "level": "WARN",
                "message": "live_agent_config_parameters_invalid",
                "intent_name": LIVE_AGENT_CONFIG_INTENT,
            })

    return {}


def compact_session_messages(session_item, limit=40):
    messages = session_item.get("session_messages") or []
    return messages[-limit:]


def build_live_agent_payload(session_item, body, session_id, now_iso):
    config = get_live_agent_config()
    config_parameters = parse_config_parameters(config)
    original_text = support_original_text(session_item) or session_item.get("last_user_text") or body.get("text") or ""
    raw_text = support_raw_text(session_item) or session_item.get("last_raw_user_text") or original_text
    summary_result = build_request_conversation_summary({
        **session_item,
        "session_id": session_id,
    })
    conversation_text = request_conversation_text(session_item)
    session_root_ts = session_item.get("session_root_ts") or session_item.get("thread_ts") or body.get("thread_ts")
    conversation_metadata = session_item.get("conversation_metadata") or {}
    slack_thread_ts = None if is_one_to_one_dm_conversation(conversation_metadata) else (
        session_item.get("thread_ts") or body.get("thread_ts") or session_root_ts
    )
    slack_channel = body.get("channel") or session_item.get("channel") or ""
    slack_user = body.get("user") or session_item.get("user") or ""

    payload = {
        "type": "live_agent_handoff",
        "source": "slack",
        "session_id": session_id,
        "session_root_ts": session_root_ts,
        "slack_channel": slack_channel,
        "slack_thread_ts": slack_thread_ts or "",
        "slack_user": slack_user,
        "intent_name": LIVE_AGENT_CONFIG_INTENT,
        "configIntent": LIVE_AGENT_CONFIG_INTENT,
        "requested_at": now_iso,
        "title": (config or {}).get("title") or "Live agent support request",
        "description": original_text,
        "raw_text": raw_text,
        "conversation_summary": summary_result.get("text"),
        "conversation_text": conversation_text,
        "conversation_summary_model_id": summary_result.get("model_id"),
        "conversation_summary_usage": summary_result.get("usage", {}),
        "conversation_summary_error": summary_result.get("error"),
        "conversation_summary_fallback_used": summary_result.get("fallback_used", False),
        "requestType": (config or {}).get("requestType") or LIVE_AGENT_DEFAULT_REQUEST_TYPE,
        "branching": (config or {}).get("branching") or LIVE_AGENT_DEFAULT_BRANCHING,
        "assignment": {
            "assignee": config_parameters.get("assignee"),
            "projectParams": config_parameters.get("projectParams") or config_parameters.get("assignee"),
        },
        "businessNotification": {
            "description": (config or {}).get("description"),
            "additionsDetails": [],
            "slackMessage": [],
        },
        "slack": {
            "channelId": slack_channel,
            "threadTs": slack_thread_ts,
            "userId": slack_user,
            "eventTs": body.get("ts"),
            "channelType": body.get("channel_type") or session_item.get("channel_type"),
            "conversationType": session_item.get("conversation_type"),
            "conversationMetadata": session_item.get("conversation_metadata") or {},
        },
        "user": slack_user,
        "email": session_item.get("email") or session_item.get("user_email"),
        "atlassianAccountId": session_item.get("atlassianAccountId") or session_item.get("atlassian_account_id"),
        "conversation": compact_session_messages(session_item),
        "context": {
            "last_bot_reply": session_item.get("last_bot_reply"),
            "support_lex_reply": session_item.get("support_lex_reply"),
            "support_claude_reply": session_item.get("support_claude_reply"),
            "support_claude_error": session_item.get("support_claude_error"),
            "lex_intent": session_item.get("lex_intent"),
            "lex_state": session_item.get("lex_state"),
            "response_source": session_item.get("response_source"),
            "conversation_summary": summary_result.get("text"),
            "conversation_text": conversation_text,
            "conversation_summary_model_id": summary_result.get("model_id"),
            "conversation_summary_fallback_used": summary_result.get("fallback_used", False),
        },
        "config": config,
    }

    return payload


def invoke_live_agent_webhook(payload):
    if not LIVE_AGENT_WEBHOOK_URL:
        return {
            "ok": False,
            "error": "missing_live_agent_webhook_url",
            "error_code": "missing_live_agent_webhook_url",
        }

    request = urllib.request.Request(
        LIVE_AGENT_WEBHOOK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=LIVE_AGENT_WEBHOOK_TIMEOUT_SECONDS) as response:
            response_text = response.read().decode("utf-8")
            if response.status < 200 or response.status >= 300:
                return {
                    "ok": False,
                    "error": f"Live agent webhook returned HTTP {response.status}",
                    "error_code": "live_agent_webhook_failed",
                    "status": response.status,
                }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "live_agent_webhook_failed",
        }

    parsed_response = None
    if response_text.strip():
        try:
            parsed_response = json.loads(response_text)
        except ValueError:
            parsed_response = {"message": response_text.strip()}

    return {
        "ok": True,
        "status": response.status,
        "response": parsed_response,
    }


def invoke_live_agent_function(payload):
    if not LIVE_AGENT_FUNCTION:
        return {
            "ok": False,
            "error": "missing_live_agent_function",
            "error_code": "missing_live_agent_function",
        }

    try:
        response = lambda_client.invoke(
            FunctionName=LIVE_AGENT_FUNCTION,
            InvocationType="RequestResponse",
            Payload=json.dumps(with_tenant_token(payload)).encode("utf-8")
        )

    except ClientError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "live_agent_lambda_invoke_failed",
        }

    raw_payload = response.get("Payload").read().decode("utf-8") if response.get("Payload") else ""
    parsed_payload = {}
    if raw_payload:
        try:
            parsed_payload = json.loads(raw_payload)
        except ValueError:
            parsed_payload = {"message": raw_payload}

    if response.get("FunctionError"):
        return {
            "ok": False,
            "error": parsed_payload.get("error") or response.get("FunctionError"),
            "error_code": parsed_payload.get("error_code") or "live_agent_lambda_error",
            "response": parsed_payload,
        }

    return {
        "ok": parsed_payload.get("ok", True),
        "status_code": response.get("StatusCode"),
        "response": parsed_payload,
        "error": parsed_payload.get("error"),
        "error_code": parsed_payload.get("error_code"),
    }


def invoke_live_agent_handoff(payload):
    if LIVE_AGENT_FUNCTION:
        result = invoke_live_agent_function(payload)
        result["target"] = "lambda"
        return result

    if LIVE_AGENT_WEBHOOK_URL:
        result = invoke_live_agent_webhook(payload)
        result["target"] = "webhook"
        return result

    # No live-agent backend wired yet: log the request internally and acknowledge
    # gracefully rather than erroring. (Full build — persisted tickets + on-call
    # roster routing + in-Slack agent chat — is pending the agent roster.)
    log_json({
        "level": "INFO",
        "message": "live_agent_request_logged_internally",
        "session_id": (payload.get("session") or {}).get("session_id") or payload.get("session_id"),
        "user": payload.get("user"),
    })
    return {"ok": True, "target": "internal", "response": {"reply": LIVE_AGENT_DEFERRED_REPLY}}


def live_agent_reply(result):
    live_agent_ticket = live_agent_ticket_fields(result)
    if live_agent_ticket.get("ticket_key"):
        return live_agent_ticket_reply(
            live_agent_ticket.get("ticket_key"),
            live_agent_ticket.get("ticket_url"),
        )

    response = result.get("response") if isinstance(result, dict) else None
    if isinstance(response, dict):
        for key in ("reply", "message", "text"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return LIVE_AGENT_DEFERRED_REPLY if result.get("ok") else LIVE_AGENT_FAILED_REPLY


def live_agent_ticket_fields(result):
    response = result.get("response") if isinstance(result, dict) else None
    response = response if isinstance(response, dict) else {}
    issue = response.get("issue") if isinstance(response.get("issue"), dict) else {}
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    project = fields.get("project") if isinstance(fields.get("project"), dict) else {}
    issue_type = fields.get("issuetype") if isinstance(fields.get("issuetype"), dict) else {}

    ticket_key = (
        result.get("ticket_key")
        or result.get("issue_key")
        or response.get("ticket_key")
        or response.get("issue_key")
        or response.get("key")
        or issue.get("key")
    )
    ticket_url = (
        result.get("ticket_url")
        or result.get("issue_url")
        or response.get("ticket_url")
        or response.get("issue_url")
        or response.get("url")
        or issue.get("url")
        or issue.get("self")
    )

    return {
        "ticket_key": str(ticket_key).strip() if ticket_key else None,
        "ticket_url": str(ticket_url).strip() if ticket_url else None,
        "jira_project": str(
            result.get("live_agent_jira_project")
            or result.get("jira_project")
            or result.get("project_key")
            or response.get("live_agent_jira_project")
            or response.get("jira_project")
            or response.get("project_key")
            or project.get("key")
            or (str(ticket_key).split("-", 1)[0] if ticket_key and "-" in str(ticket_key) else "")
        ).strip() or None,
        "issue_type": str(
            result.get("live_agent_issue_type")
            or result.get("issue_type")
            or result.get("issueType")
            or response.get("live_agent_issue_type")
            or response.get("issue_type")
            or response.get("issueType")
            or issue_type.get("name")
            or ""
        ).strip() or None,
        "portal_request_type": str(
            result.get("live_agent_portal_request_type")
            or result.get("portal_request_type")
            or result.get("portalRequestType")
            or result.get("request_type")
            or result.get("requestType")
            or response.get("live_agent_portal_request_type")
            or response.get("portal_request_type")
            or response.get("portalRequestType")
            or response.get("request_type")
            or response.get("requestType")
            or ""
        ).strip() or None,
    }


def existing_live_agent_state_result(session_item, base_result):
    live_agent_status = session_item.get("live_agent_status")
    support_options_status = session_item.get("support_options_status")
    live_agent_ticket_key = session_item.get("live_agent_ticket_key") or session_item.get("last_live_agent_ticket_key")
    live_agent_ticket_url = session_item.get("live_agent_ticket_url") or session_item.get("last_live_agent_ticket_url")
    live_agent_jira_project = session_item.get("live_agent_jira_project")
    live_agent_issue_type = session_item.get("live_agent_issue_type")
    live_agent_portal_request_type = session_item.get("live_agent_portal_request_type")

    if live_agent_status in {"ticket_created", "waiting_for_customer", "resolved"} or live_agent_ticket_key:
        reply = existing_live_agent_ticket_reply(live_agent_ticket_key, live_agent_ticket_url)

        return {
            **base_result,
            "lex_state": "Fulfilled",
            "response_source": "live_agent",
            "next_action": None,
            "support_options_status": "live_agent_requested",
            "live_agent_status": live_agent_status,
            "live_agent_requested_at": session_item.get("live_agent_requested_at"),
            "live_agent_updated_at": session_item.get("live_agent_updated_at"),
            "live_agent_ticket_key": live_agent_ticket_key,
            "live_agent_ticket_url": live_agent_ticket_url,
            "live_agent_jira_project": live_agent_jira_project,
            "live_agent_issue_type": live_agent_issue_type,
            "live_agent_portal_request_type": live_agent_portal_request_type,
            "last_live_agent_ticket_key": live_agent_ticket_key,
            "last_live_agent_ticket_url": live_agent_ticket_url,
            "reply": reply,
        }

    if live_agent_status == "requested" or support_options_status == "live_agent_requested":
        return {
            **base_result,
            "lex_state": "Fulfilled",
            "response_source": "live_agent",
            "next_action": None,
            "support_options_status": "live_agent_requested",
            "live_agent_status": "requested",
            "live_agent_requested_at": session_item.get("live_agent_requested_at"),
            "reply": LIVE_AGENT_DEFERRED_REPLY,
        }

    if live_agent_status in {"creating", "in_progress"} or support_options_status == "live_agent_creating":
        return {
            **base_result,
            "lex_state": "InProgress",
            "response_source": "live_agent",
            "next_action": NEXT_ACTION_FINAL_SUPPORT_OPTIONS,
            "support_options_status": "live_agent_creating",
            "live_agent_status": "in_progress",
            "live_agent_requested_at": session_item.get("live_agent_requested_at"),
            "live_agent_updated_at": session_item.get("live_agent_updated_at"),
            "reply": "Live agent handoff is already in progress. Please wait a moment.",
        }

    return {
        **base_result,
        "response_source": "interactive_stale",
        "reply": "That live-agent action is no longer active. Please send a new message.",
    }


def acquire_live_agent_handoff_lock(session_id, event_id, now_iso):
    try:
        sessions_table.update_item(
            Key={
                "session_id": session_id
            },
            UpdateExpression="""
                SET
                    support_options_status = :support_creating,
                    live_agent_status = :live_agent_in_progress,
                    live_agent_requested_at = :now,
                    live_agent_updated_at = :now,
                    live_agent_request_event_id = :event_id,
                    updated_at = :now
                REMOVE
                    live_agent_error,
                    live_agent_error_code
            """,
            ConditionExpression="""
                next_action = :next_action
                AND support_options_status = :pending
                AND (
                    attribute_not_exists(live_agent_status)
                    OR live_agent_status IN (:failed, :cancelled)
                )
            """,
            ExpressionAttributeValues={
                ":support_creating": "live_agent_creating",
                ":live_agent_in_progress": "in_progress",
                ":next_action": NEXT_ACTION_FINAL_SUPPORT_OPTIONS,
                ":pending": "pending",
                ":failed": "failed",
                ":cancelled": "cancelled",
                ":event_id": event_id or "unknown-event",
                ":now": now_iso,
            }
        )

        log_json({
            "level": "INFO",
            "message": "live_agent_handoff_lock_acquired",
            "session_id": session_id
        })
        return True

    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log_json({
                "level": "INFO",
                "message": "live_agent_handoff_lock_conflict",
                "session_id": session_id
            })
            return False

        raise


def handle_interactive_action(session_item, body, session_id):
    action_id = body.get("action_id")
    now_iso = to_iso(datetime.now(timezone.utc))
    result = base_interactive_result(session_item)

    if action_id == ACTION_ID_CLOSE_AND_SUMMARIZE:
        if not ENABLE_CLOSE_SUMMARY:
            result.update({
                "lex_state": "Ignored",
                "response_source": "manual_close_summary_disabled",
                "next_action": None,
                "reply": "Conversation summarization is currently disabled.",
            })
            return result

        if session_item.get("conversation_status") == "summarizing" or session_item.get("summary_status") == "started":
            result.update({
                "response_source": "manual_close_summary_duplicate",
                "reply": "This session is already being closed and summarized.",
            })
            return result

        if session_item.get("conversation_status") == "closed" or session_item.get("summary_status") == "completed":
            result.update({
                "response_source": "manual_close_summary_duplicate",
                "reply": "This session has already been closed.",
            })
            return result

        if session_item.get("conversation_status") in {"failed"}:
            result.update({
                "response_source": "manual_close_summary_failed",
                "reply": "This session is in a failed state. Please send a new message to start again.",
            })
            return result

        result.update({
            "lex_state": "Fulfilled",
            "response_source": "manual_close_summary",
            "next_action": None,
            "reply": "Closed this IVY session and started the summary.",
            "manual_close_summary": True,
            "manual_close_timeout_token": session_item.get("timeout_token"),
        })
        return result

    if action_id == ACTION_ID_ESCALATE:
        payload = body.get("action_payload") or parse_action_value(body.get("action_value"))
        query = (payload.get("q") or session_item.get("last_user_text") or "").strip()
        try:
            requested_index = int(payload.get("level"))
        except (TypeError, ValueError):
            requested_index = None
        session_root_ts = payload.get("srt") or session_item.get("session_root_ts") or session_item.get("thread_ts")
        if not ENABLE_ESCALATION_LADDER or requested_index is None or not query:
            return result
        # Run ONE layer per click so each layer is a distinct, visible step. If the
        # layer produces no usable answer (e.g. the KB has no matching article), we
        # DON'T silently jump ahead — we say so and offer a button to the next layer.
        target_index = next_escalation_index(requested_index)
        if target_index is None:
            result.update({
                "lex_state": "Failed",
                "response_source": "escalation_exhausted",
                "reply": ESCALATION_EXHAUSTED_REPLY,
            })
            return result
        level = ESCALATION_LEVELS[target_index]
        reply_text, layer_ok = run_escalation_layer(level, query, session_id, body.get("channel"))
        answered = layer_ok and has_meaningful_bot_answer(reply_text)
        next_index = next_escalation_index(target_index + 1)
        if not answered:
            # Layer had nothing — make that explicit and steer to the next layer.
            reply_text = escalation_layer_no_answer_text(level)
        response_source = "escalation_" + level + ("" if answered else "_no_answer")
        result.update({
            "lex_intent": "Escalation_" + level,
            "lex_state": "Fulfilled",
            "response_source": response_source,
            "reply": reply_text,
            "blocks": escalation_blocks(reply_text, query, next_index, session_id, session_root_ts),
        })
        # When no further automated layer remains we show a "live agent" button; prime
        # the state its handler (has_pending_final_support_options) requires so it works.
        if next_index is None and ENABLE_LIVE_AGENT_ESCALATION:
            result.update({
                "next_action": NEXT_ACTION_FINAL_SUPPORT_OPTIONS,
                "support_options_status": "pending",
                "support_original_text": query,
                "support_raw_text": query,
            })
        # A real L3 (LLM) answer opens a running chat: the user can keep replying and
        # each message continues with the LLM (up to LLM_CHAT_MAX_MESSAGES turns).
        if answered and level == "llm" and ENABLE_LLM_CHAT:
            result.update({
                "llm_chat_status": "active",
                "llm_chat_count": 0,
                "llm_chat_history": trim_llm_chat_history("IVY: " + (reply_text or "")),
            })
        log_json({
            "level": "INFO",
            "message": "escalation_ladder_advanced",
            "session_id": session_id,
            "level": level,
            "answered": answered,
            "next_index": next_index,
            "offers_live_agent": next_index is None and ENABLE_LIVE_AGENT_ESCALATION,
        })
        return result

    if action_id in {ACTION_ID_ASSISTANCE_NO, ACTION_ID_ASSISTANCE_SOLVED}:
        if not (
            has_pending_assistance_confirmation(session_item)
            or has_pending_assistance_details(session_item)
        ):
            return result

        result.update({
            "lex_state": "Fulfilled",
            "response_source": "assistance_closed",
            "reply": LEX_ASSISTANCE_CLOSED_REPLY,
            "assistance_status": "closed",
            "assistance_closed_at": now_iso,
        })
        return result

    if action_id == ACTION_ID_MCP_ASSIST:
        if not (
            has_pending_assistance_confirmation(session_item)
            or session_item.get("next_action") in {NEXT_ACTION_CLAUDE_ASSISTANCE, NEXT_ACTION_MCP_ASSIST}
            or session_item.get("response_source") == "lex"
        ):
            return result

        request_id = session_item.get("mcp_request_id") or make_mcp_request_id(
            session_id,
            body.get("event_id"),
            action_id,
        )
        original_text = session_item.get("assistance_original_text") or session_item.get("last_user_text")
        raw_text = session_item.get("assistance_raw_text") or session_item.get("last_raw_user_text") or original_text
        lex_reply = session_item.get("assistance_lex_reply") or session_item.get("last_bot_reply")

        if not mcp_assist_available():
            result.update({
                "lex_state": "Failed",
                "response_source": "mcp_assist_unconfigured",
                "next_action": NEXT_ACTION_CLAUDE_ASSISTANCE,
                "reply": mcp_followup_reply_text(MCP_ASSIST_NOT_CONFIGURED_REPLY),
                "blocks": mcp_followup_blocks(
                    MCP_ASSIST_NOT_CONFIGURED_REPLY,
                    session_id,
                    session_item.get("session_root_ts") or session_item.get("thread_ts"),
                ),
                "workflow_state": "MCP_ERROR",
                "mcp_status": "ERROR",
                "mcp_request_id": request_id,
                "mcp_errors": [{"source": "mcp", "category": "missing_configuration"}],
                "mcp_requested_at": now_iso,
                "mcp_completed_at": now_iso,
                "support_original_text": original_text,
                "support_raw_text": raw_text,
                "support_lex_intent": session_item.get("assistance_lex_intent") or session_item.get("lex_intent"),
                "support_lex_state": session_item.get("assistance_lex_state") or session_item.get("lex_state"),
                "support_lex_slots": session_item.get("assistance_lex_slots") or session_item.get("lex_slots", {}),
                "support_lex_reply": lex_reply,
            })
            return result

        result.update({
            "lex_state": "InProgress",
            "response_source": "mcp_assist_details_requested",
            "next_action": NEXT_ACTION_MCP_ASSIST,
            "reply": MCP_ASSIST_DETAILS_PROMPT_TEXT,
            "workflow_state": "MCP_AWAITING_QUERY",
            "mcp_status": "AWAITING_QUERY",
            "mcp_request_id": request_id,
            "mcp_requested_at": now_iso,
            "mcp_should_invoke": False,
            "assistance_status": "awaiting_mcp_query",
            "assistance_original_text": original_text,
            "assistance_raw_text": raw_text,
            "assistance_lex_intent": session_item.get("assistance_lex_intent") or session_item.get("lex_intent"),
            "assistance_lex_state": session_item.get("assistance_lex_state") or session_item.get("lex_state"),
            "assistance_lex_slots": session_item.get("assistance_lex_slots") or session_item.get("lex_slots", {}),
            "assistance_lex_reply": lex_reply,
            "assistance_requested_at": session_item.get("assistance_requested_at") or now_iso,
            "support_original_text": original_text,
            "support_raw_text": raw_text,
            "support_lex_intent": session_item.get("assistance_lex_intent") or session_item.get("lex_intent"),
            "support_lex_state": session_item.get("assistance_lex_state") or session_item.get("lex_state"),
            "support_lex_slots": session_item.get("assistance_lex_slots") or session_item.get("lex_slots", {}),
            "support_lex_reply": lex_reply,
            "support_requested_at": now_iso,
        })
        return result

    if action_id == ACTION_ID_CLAUDE_ASSIST:
        if not has_terminal_mcp_state(session_item):
            return result

        original_text = (
            session_item.get("support_original_text")
            or session_item.get("assistance_original_text")
            or session_item.get("last_user_text")
        )
        raw_text = (
            session_item.get("support_raw_text")
            or session_item.get("assistance_raw_text")
            or session_item.get("last_raw_user_text")
            or original_text
        )
        result.update({
            "lex_state": "InProgress",
            "response_source": "assistance_details_requested",
            "next_action": NEXT_ACTION_CLAUDE_ASSISTANCE,
            "reply": LEX_ASSISTANCE_DETAILS_PROMPT_TEXT,
            "assistance_status": "awaiting_details",
            "assistance_original_text": original_text,
            "assistance_raw_text": raw_text,
            "assistance_lex_intent": session_item.get("assistance_lex_intent") or session_item.get("support_lex_intent") or session_item.get("lex_intent"),
            "assistance_lex_state": session_item.get("assistance_lex_state") or session_item.get("support_lex_state") or session_item.get("lex_state"),
            "assistance_lex_slots": session_item.get("assistance_lex_slots") or session_item.get("support_lex_slots") or session_item.get("lex_slots", {}),
            "assistance_lex_reply": (
                session_item.get("mcp_answer")
                or session_item.get("assistance_lex_reply")
                or session_item.get("support_lex_reply")
                or session_item.get("last_bot_reply")
            ),
            "assistance_requested_at": session_item.get("assistance_requested_at") or now_iso,
            "support_original_text": original_text,
            "support_raw_text": raw_text,
            "support_lex_intent": session_item.get("support_lex_intent") or session_item.get("assistance_lex_intent") or session_item.get("lex_intent"),
            "support_lex_state": session_item.get("support_lex_state") or session_item.get("assistance_lex_state") or session_item.get("lex_state"),
            "support_lex_slots": session_item.get("support_lex_slots") or session_item.get("assistance_lex_slots") or session_item.get("lex_slots", {}),
            "support_lex_reply": (
                session_item.get("mcp_answer")
                or session_item.get("support_lex_reply")
                or session_item.get("assistance_lex_reply")
                or session_item.get("last_bot_reply")
            ),
            "support_requested_at": now_iso,
            "workflow_state": session_item.get("workflow_state"),
            "mcp_status": session_item.get("mcp_status"),
            "mcp_request_id": session_item.get("mcp_request_id"),
            "mcp_answer": session_item.get("mcp_answer"),
            "mcp_confidence": session_item.get("mcp_confidence"),
            "mcp_confidence_reasons": session_item.get("mcp_confidence_reasons"),
            "mcp_sources_queried": session_item.get("mcp_sources_queried"),
            "mcp_sources_used": session_item.get("mcp_sources_used"),
            "mcp_citations": session_item.get("mcp_citations"),
            "mcp_errors": session_item.get("mcp_errors"),
            "mcp_requested_at": session_item.get("mcp_requested_at"),
            "mcp_started_at": session_item.get("mcp_started_at"),
            "mcp_completed_at": session_item.get("mcp_completed_at"),
            "mcp_latency_ms": session_item.get("mcp_latency_ms"),
            "claude_fallback_attempted": False,
        })
        return result

    if action_id in {ACTION_ID_ASSISTANCE_YES, ACTION_ID_ASSISTANCE_NEED_MORE_HELP}:
        if not (
            has_pending_assistance_confirmation(session_item)
            or session_item.get("session_state") == SESSION_STATE_WAITING_FOR_USER
            or session_item.get("conversation_status") == "active"
        ):
            return result

        result.update({
            "lex_state": "InProgress",
            "response_source": "assistance_details_requested",
            "next_action": NEXT_ACTION_CLAUDE_ASSISTANCE,
            "reply": LEX_ASSISTANCE_DETAILS_PROMPT_TEXT,
            "assistance_status": "awaiting_details",
            "assistance_original_text": session_item.get("assistance_original_text") or session_item.get("last_user_text"),
            "assistance_raw_text": session_item.get("assistance_raw_text") or session_item.get("last_raw_user_text"),
            "assistance_lex_intent": session_item.get("assistance_lex_intent") or session_item.get("lex_intent"),
            "assistance_lex_state": session_item.get("assistance_lex_state") or session_item.get("lex_state"),
            "assistance_lex_slots": session_item.get("assistance_lex_slots") or session_item.get("lex_slots", {}),
            "assistance_lex_reply": session_item.get("assistance_lex_reply") or session_item.get("last_bot_reply"),
            "assistance_requested_at": session_item.get("assistance_requested_at") or now_iso,
        })
        return result

    if action_id == ACTION_ID_ASSISTANCE_CREATE_JIRA_TICKET:
        if not ENABLE_CREATE_JIRA_TICKET:
            result.update({
                "lex_state": "Ignored",
                "response_source": "jira_disabled",
                "next_action": None,
                "jira_status": None,
                "reply": "Jira ticket creation is currently disabled."
            })
            return result

        if not (
            has_pending_assistance_confirmation(session_item)
            or has_pending_assistance_details(session_item)
        ):
            return result

        support_session = {
            **session_item,
            "support_original_text": session_item.get("assistance_original_text") or session_item.get("last_user_text"),
            "support_raw_text": session_item.get("assistance_raw_text") or session_item.get("last_raw_user_text"),
            "support_lex_intent": session_item.get("assistance_lex_intent") or session_item.get("lex_intent"),
            "support_lex_state": session_item.get("assistance_lex_state") or session_item.get("lex_state"),
            "support_lex_slots": session_item.get("assistance_lex_slots") or session_item.get("lex_slots", {}),
            "support_lex_reply": session_item.get("assistance_lex_reply") or session_item.get("last_bot_reply"),
            "support_requested_at": session_item.get("assistance_requested_at") or now_iso,
        }
        return handle_support_create_jira(support_session, body, session_id, now_iso)

    if action_id == ACTION_ID_LIVE_AGENT_SUPPORT:
        if session_item.get("live_agent_status") in {"creating", "in_progress", "requested", "waiting_for_customer", "resolved"}:
            return existing_live_agent_state_result(session_item, result)

        if not has_pending_final_support_options(session_item):
            return result

        if not acquire_live_agent_handoff_lock(
            session_id,
            body.get("event_id"),
            now_iso
        ):
            latest_session = get_session_item(session_id)
            return existing_live_agent_state_result(latest_session, result)

        locked_session = {
            **session_item,
            "support_options_status": "live_agent_creating",
            "live_agent_status": "in_progress",
            "live_agent_requested_at": now_iso,
            "live_agent_updated_at": now_iso,
        }
        live_agent_result = invoke_live_agent_handoff(
            build_live_agent_payload(locked_session, body, session_id, now_iso)
        )
        live_agent_ok = bool(live_agent_result.get("ok"))
        live_agent_ticket = live_agent_ticket_fields(live_agent_result)
        live_agent_ticket_key = live_agent_ticket.get("ticket_key")
        live_agent_ticket_url = live_agent_ticket.get("ticket_url")
        live_agent_jira_project = live_agent_ticket.get("jira_project")
        live_agent_issue_type = live_agent_ticket.get("issue_type")
        live_agent_portal_request_type = live_agent_ticket.get("portal_request_type")

        log_json({
            "level": "INFO" if live_agent_ok else "ERROR",
            "message": "live_agent_handoff_completed",
            "session_id": session_id,
            "target": live_agent_result.get("target"),
            "ok": live_agent_ok,
            "ticket_key": live_agent_ticket_key,
            "error_code": live_agent_result.get("error_code"),
        })

        result.update({
            "lex_state": "Fulfilled" if live_agent_ok else "Failed",
            "response_source": "live_agent",
            "next_action": None,
            "reply": live_agent_reply(live_agent_result),
            "support_options_status": "live_agent_requested" if live_agent_ok else "live_agent_failed",
            "support_resolved_at": now_iso if live_agent_ok else None,
            "live_agent_status": "requested" if live_agent_ok else "failed",
            "live_agent_requested_at": now_iso,
            "live_agent_updated_at": now_iso,
            "live_agent_ticket_key": live_agent_ticket_key if live_agent_ok else None,
            "live_agent_ticket_url": live_agent_ticket_url if live_agent_ok else None,
            "live_agent_jira_project": live_agent_jira_project if live_agent_ok else None,
            "live_agent_issue_type": live_agent_issue_type if live_agent_ok else None,
            "live_agent_portal_request_type": live_agent_portal_request_type if live_agent_ok else None,
            "last_live_agent_ticket_key": live_agent_ticket_key if live_agent_ok else None,
            "last_live_agent_ticket_url": live_agent_ticket_url if live_agent_ok else None,
            "live_agent_error": None if live_agent_ok else live_agent_result.get("error"),
            "live_agent_error_code": None if live_agent_ok else live_agent_result.get("error_code"),
        })
        return result

    if action_id == ACTION_ID_CREATE_JIRA_TICKET:
        if not ENABLE_CREATE_JIRA_TICKET:
            result.update({
                "lex_state": "Ignored",
                "response_source": "jira_disabled",
                "next_action": None,
                "jira_status": None,
                "reply": "Jira ticket creation is currently disabled."
            })
            return result

        return handle_support_create_jira(session_item, body, session_id, now_iso)

    result.update({
        "response_source": "interactive_unknown",
        "reply": "I could not recognize that action. Please send a new message."
    })
    return result


def handle_jira_confirmation(session_item, body, session_id, text, raw_text):
    decision = classify_jira_confirmation(text)
    now_iso = to_iso(datetime.now(timezone.utc))
    intent_name = (
        session_item.get("jira_intent_name")
        or session_item.get("lex_intent")
        or "JIRA_CONFIRMATION"
    )
    request_text = (
        session_item.get("jira_request_text")
        or session_item.get("last_user_text")
        or raw_text
        or text
    )
    jira_request_id = (
        session_item.get("jira_request_id")
        or make_jira_request_id(
            session_id,
            session_item.get("last_event_id"),
            intent_name,
            request_text
        )
    )
    jira_requested_at = session_item.get("jira_requested_at") or session_item.get("updated_at") or now_iso

    base_result = {
        "lex_intent": intent_name,
        "lex_state": "InProgress",
        "lex_slots": session_item.get("lex_slots", {}),
        "response_source": "jira_confirmation",
        "next_action": "O3_CreateJiraTicket",
        "jira_status": "pending_confirmation",
        "jira_intent_name": intent_name,
        "jira_request_text": request_text,
        "jira_request_id": jira_request_id,
        "jira_requested_at": jira_requested_at,
        "jira_confirmed_at": None,
        "jira_create_started_at": None,
        "jira_created_at": None,
        "jira_ticket_key": None,
        "jira_ticket_url": None,
        "jira_error": None,
        "jira_error_code": None,
        "jira_error_status": None,
        "last_jira_ticket_key": None,
        "last_jira_ticket_url": None,
        "last_jira_created_at": None,
        "rovo_status": None,
        "rovo_requested_at": None,
        "rovo_enriched_at": None,
        "rovo_error": None,
        "rovo_error_code": None,
        "rovo_should_invoke": False,
    }

    if session_item.get("jira_status") in {"creating", "created"}:
        return existing_jira_state_result(session_item, base_result)

    if decision == "no":
        return {
            **base_result,
            "lex_state": "Fulfilled",
            "response_source": "jira",
            "next_action": None,
            "jira_status": "cancelled",
            "reply": JIRA_CANCELLED_REPLY,
        }

    if decision != "yes":
        return {
            **base_result,
            "reply": JIRA_UNCLEAR_CONFIRMATION_REPLY,
        }

    if not ENABLE_CREATE_JIRA_TICKET:
        return {
            **base_result,
            "lex_state": "Ignored",
            "response_source": "jira_disabled",
            "next_action": None,
            "jira_status": None,
            "reply": "Jira ticket creation is currently disabled.",
        }

    if not acquire_jira_creation_lock(
        session_id,
        jira_request_id,
        body.get("event_id"),
        now_iso
    ):
        return existing_jira_state_result(get_session_item(session_id), base_result)

    locked_session = {
        **session_item,
        "jira_request_id": jira_request_id,
        "jira_requested_at": jira_requested_at,
        "jira_confirmed_at": now_iso,
        "jira_create_started_at": now_iso
    }
    jira_result = invoke_create_jira_ticket(
        build_jira_payload(locked_session, body, session_id, text, raw_text)
    )

    if jira_result.get("ok"):
        ticket_key = jira_result.get("ticket_key")
        ticket_url = jira_result.get("ticket_url")
        jira_created_at = to_iso(datetime.now(timezone.utc))
        return {
            **base_result,
            "lex_state": "Fulfilled",
            "response_source": "jira",
            "next_action": None,
            "jira_status": "created",
            "jira_confirmed_at": now_iso,
            "jira_create_started_at": now_iso,
            "jira_created_at": jira_created_at,
            "jira_ticket_key": ticket_key,
            "jira_ticket_url": ticket_url,
            "last_jira_ticket_key": ticket_key,
            "last_jira_ticket_url": ticket_url,
            "last_jira_created_at": jira_created_at,
            "rovo_status": "pending" if ENABLE_ROVO_ENRICHMENT else None,
            "rovo_should_invoke": ENABLE_ROVO_ENRICHMENT,
            "reply": jira_ticket_reply(ticket_key, ticket_url),
        }

    return {
        **base_result,
        "lex_state": "Failed",
        "response_source": "jira",
        "next_action": None,
        "jira_status": "create_failed",
        "jira_confirmed_at": now_iso,
        "jira_create_started_at": now_iso,
        "jira_error": jira_result.get("error", "unknown_jira_error"),
        "jira_error_code": jira_result.get("error_code", "jira_create_failed"),
        "jira_error_status": jira_result.get("status"),
        "reply": jira_failure_reply(jira_result),
    }


def get_lex_reply(messages):
    replies = []

    for message in messages or []:
        content = (message.get("content") or "").strip()

        if content:
            replies.append(content)

    if replies:
        return "\n".join(replies), False

    log_json({
        "level": "WARN",
        "message": "empty_lex_reply"
    })

    return EMPTY_LEX_REPLY, True


def truncate_text(value, limit):
    text = str(value or "")
    return text[:limit]


def live_agent_slack_comment_marker_id(event_id, ticket_key, user, text, channel, ts):
    if event_id:
        marker_key = str(event_id)
    else:
        raw = json.dumps(
            {
                "ticket_key": ticket_key or "",
                "user": user or "",
                "text": text or "",
                "channel": channel or "",
                "ts": ts or "",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        marker_key = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    return f"live_agent_slack_comment:{marker_key}"


def acquire_live_agent_slack_comment_marker(session_id, ticket_key, event_id, user, text, channel, ts, now_iso):
    marker_session_id = live_agent_slack_comment_marker_id(event_id, ticket_key, user, text, channel, ts)

    try:
        sessions_table.update_item(
            Key={"session_id": marker_session_id},
            UpdateExpression="""
                SET
                    record_type = :record_type,
                    target_session_id = :target_session_id,
                    ticket_key = :ticket_key,
                    event_id = :event_id,
                    slack_user = :slack_user,
                    slack_channel = :slack_channel,
                    slack_ts = :slack_ts,
                    #status = :posting,
                    created_at = if_not_exists(created_at, :now),
                    updated_at = :now,
                    #ttl = :ttl
            """,
            ConditionExpression=(
                Attr("session_id").not_exists()
                | Attr("status").eq("failed")
            ),
            ExpressionAttributeNames={
                "#status": "status",
                "#ttl": "ttl",
            },
            ExpressionAttributeValues={
                ":record_type": "live_agent_slack_comment_marker",
                ":target_session_id": session_id,
                ":ticket_key": ticket_key,
                ":event_id": event_id or "",
                ":slack_user": user or "",
                ":slack_channel": channel or "",
                ":slack_ts": ts or "",
                ":posting": "posting",
                ":now": now_iso,
                ":ttl": ttl_epoch(),
            },
        )

        return {
            "acquired": True,
            "session_id": marker_session_id,
        }

    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return {
                "acquired": False,
                "session_id": marker_session_id,
            }

        raise


def update_live_agent_slack_comment_marker(marker_session_id, status, now_iso, error=None):
    expression_values = {
        ":status": status,
        ":now": now_iso,
        ":ttl": ttl_epoch(),
    }
    update_expression = """
        SET
            #status = :status,
            updated_at = :now,
            #ttl = :ttl
    """
    expression_attribute_names = {
        "#status": "status",
        "#ttl": "ttl",
    }

    if error:
        update_expression += """,
            #error = :error
        """
        expression_attribute_names["#error"] = "error"
        expression_values[":error"] = str(error)

    sessions_table.update_item(
        Key={"session_id": marker_session_id},
        UpdateExpression=update_expression,
        ExpressionAttributeNames=expression_attribute_names,
        ExpressionAttributeValues=expression_values,
    )


def build_live_agent_slack_comment(user, text, channel, thread_ts, ts):
    slack_thread = thread_ts or ts or ""
    return (
        f"[From Slack] User {user} replied:\n\n"
        f"{text}\n\n"
        f"Slack channel: {channel}\n"
        f"Slack thread: {slack_thread}"
    )


def update_live_agent_user_reply_session(session_id, session_item, event_id, text, now_iso):
    previous_status = session_item.get("live_agent_status")
    next_status = "user_replied" if previous_status == "waiting_for_customer" else previous_status

    sessions_table.update_item(
        Key={"session_id": session_id},
        UpdateExpression="""
            SET
                live_agent_status = :live_agent_status,
                last_live_agent_user_reply = :reply,
                last_live_agent_user_reply_at = :now,
                last_live_agent_user_reply_event_id = :event_id,
                live_agent_updated_at = :now,
                updated_at = :now,
                #ttl = :ttl
        """,
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={
            ":live_agent_status": next_status,
            ":reply": truncate_text(text, 2000),
            ":now": now_iso,
            ":event_id": event_id or "",
            ":ttl": ttl_epoch(),
        },
    )


def update_live_agent_user_reply_pointer(ticket_key, text, now_iso):
    if not ticket_key:
        return

    sessions_table.update_item(
        Key={"session_id": f"live_agent_ticket:{ticket_key}"},
        UpdateExpression="""
            SET
                last_slack_user_reply = :reply,
                last_slack_user_reply_at = :now,
                updated_at = :now,
                #ttl = :ttl
        """,
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={
            ":reply": truncate_text(text, 2000),
            ":now": now_iso,
            ":ttl": ttl_epoch(),
        },
    )


def support_bridge_enabled():
    return LIVE_AGENT_SUPPORT_MODE == "slack_console" and bool(LIVE_AGENT_SUPPORT_CHANNEL_ID)


def requester_thread_ts(pointer):
    channel = text_or_empty(pointer.get("slack_channel"))
    if channel.startswith("D"):
        return None
    return text_or_empty(pointer.get("slack_thread_ts")) or None


def find_live_agent_support_bridge(channel, thread_ts):
    if not (support_bridge_enabled() and channel and thread_ts):
        return None

    filter_expression = (
        Attr("pointer_type").eq("live_agent_ticket")
        & Attr("support_channel").eq(channel)
        & Attr("support_thread_ts").eq(thread_ts)
        & Attr("bridge_status").eq("active")
    )
    scan_kwargs = {
        "FilterExpression": filter_expression,
        "Limit": 100,
    }

    while True:
        response = sessions_table.scan(**scan_kwargs)
        items = response.get("Items") or []
        if items:
            return items[0]

        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return None

        scan_kwargs["ExclusiveStartKey"] = last_key


def find_live_agent_support_bridge_for_event(channel, *ts_values):
    for ts_value in dict.fromkeys(text_or_empty(ts_value) for ts_value in ts_values):
        pointer = find_live_agent_support_bridge(channel, ts_value)
        if pointer:
            return pointer
    return None


def is_live_agent_support_channel_message(channel, is_interactive_action=False):
    return (
        support_bridge_enabled()
        and channel == LIVE_AGENT_SUPPORT_CHANNEL_ID
        and not is_interactive_action
    )


def is_live_agent_support_control_action(action_id):
    return action_id in {ACTION_ID_LIVE_AGENT_RESOLVE, ACTION_ID_LIVE_AGENT_CANCEL, ACTION_ID_LIVE_AGENT_REASSIGN}


def live_agent_support_control_event_type(action_id):
    if action_id == ACTION_ID_LIVE_AGENT_RESOLVE:
        return "live_agent_support_resolve"
    if action_id == ACTION_ID_LIVE_AGENT_CANCEL:
        return "live_agent_support_cancel"
    return "live_agent_support_reassign"


def invoke_live_agent_support_control(pointer, body, action_id):
    if not LIVE_AGENT_FUNCTION:
        raise ValueError("Missing LIVE_AGENT_FUNCTION for support control action")

    payload = {
        "source": "slack_support",
        "event_type": live_agent_support_control_event_type(action_id),
        "event_id": body.get("event_id"),
        "action": body.get("action_value"),
        "action_id": action_id,
        "action_user": body.get("user"),
        "action_ts": body.get("ts"),
        "message_ts": body.get("message_ts") or body.get("thread_ts") or body.get("ts"),
        "support_channel": body.get("channel"),
        "support_thread_ts": pointer.get("support_thread_ts"),
        "ticket_key": pointer.get("ticket_key"),
        "session_id": pointer.get("target_session_id"),
    }
    response = lambda_client.invoke(
        FunctionName=LIVE_AGENT_FUNCTION,
        InvocationType="RequestResponse",
        Payload=json.dumps(with_tenant_token(payload)).encode("utf-8"),
    )
    raw_payload = response.get("Payload").read().decode("utf-8") if response.get("Payload") else "{}"
    parsed = json.loads(raw_payload or "{}")
    if response.get("FunctionError"):
        raise RuntimeError(parsed.get("error") or response.get("FunctionError"))
    if isinstance(parsed, dict) and "body" in parsed:
        try:
            parsed_body = json.loads(parsed.get("body") or "{}")
        except ValueError:
            parsed_body = {"raw_body": parsed.get("body")}
        return {**parsed, "parsed_body": parsed_body}
    return parsed


def live_agent_bridge_marker_id(direction, pointer, body, text):
    source_id = body.get("event_id") or body.get("ts") or hashlib.sha256(
        "|".join([
            direction,
            text_or_empty(pointer.get("ticket_key")),
            text_or_empty(body.get("channel")),
            text_or_empty(body.get("user")),
            text_or_empty(text),
        ]).encode("utf-8")
    ).hexdigest()[:32]
    return f"live_agent_bridge:{direction}:{source_id}"


def acquire_live_agent_bridge_marker(direction, pointer, body, text, now_iso):
    marker_session_id = live_agent_bridge_marker_id(direction, pointer, body, text)
    try:
        sessions_table.put_item(
            Item={
                "session_id": marker_session_id,
                "record_type": "live_agent_bridge_marker",
                "direction": direction,
                "ticket_key": pointer.get("ticket_key") or "",
                "source_event_id": body.get("event_id") or "",
                "source_channel": body.get("channel") or "",
                "source_ts": body.get("ts") or "",
                "created_at": now_iso,
                "ttl": ttl_epoch(),
            },
            ConditionExpression="attribute_not_exists(session_id)",
        )
        return {"acquired": True, "session_id": marker_session_id}
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return {"acquired": False, "session_id": marker_session_id}
        raise


def append_live_agent_bridge_message(pointer, sender, text, ts, now_iso):
    entries = [transcript_entry(sender, text, ts)]
    entries = [entry for entry in entries if entry]
    if not entries:
        return

    for session_id in dict.fromkeys([pointer.get("session_id"), pointer.get("target_session_id")]):
        if not session_id:
            continue
        sessions_table.update_item(
            Key={"session_id": session_id},
            UpdateExpression="""
                SET
                    last_live_agent_bridge_message = :text,
                    last_live_agent_bridge_message_at = :now,
                    live_agent_updated_at = :now,
                    updated_at = :now,
                    #ttl = :ttl,
                    session_messages = list_append(if_not_exists(session_messages, :empty_list), :entries)
            """,
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":text": truncate_text(text, 2000),
                ":now": now_iso,
                ":ttl": ttl_epoch(),
                ":empty_list": [],
                ":entries": entries,
            },
        )


def agent_reply_to_user_text(pointer, agent_user, text):
    agent_label = pointer.get("assignee_display_name") or "Support agent"
    return f"{agent_label}: {text}"


def agent_reply_to_jsm_comment(agent_user, text, support_channel, support_thread_ts):
    return (
        f"[From Slack] Agent {agent_user} replied:\n\n"
        f"{text}\n\n"
        f"Slack support channel: {support_channel}\n"
        f"Slack support thread: {support_thread_ts}"
    )


def handle_live_agent_support_thread_reply(pointer, body, text):
    now_iso = to_iso(datetime.now(timezone.utc))
    marker = acquire_live_agent_bridge_marker("agent_to_user", pointer, body, text, now_iso)
    if not marker.get("acquired"):
        log_json({
            "level": "INFO",
            "message": "live_agent_support_reply_duplicate",
            "ticket_key": pointer.get("ticket_key"),
            "marker_session_id": marker.get("session_id"),
        })
        return {"ok": True, "duplicate": True, "ticket_key": pointer.get("ticket_key")}

    requester_channel = text_or_empty(pointer.get("slack_channel"))
    if not requester_channel:
        return {"ok": False, "error": "missing_requester_channel", "ticket_key": pointer.get("ticket_key")}

    start_transition_result = transition_jira_issue_to_status(
        pointer.get("ticket_key"),
        LIVE_AGENT_START_STATUS_NAMES,
        reason="slack_agent_reply",
    )
    if (
        not start_transition_result.get("ok")
        and LIVE_AGENT_BLOCK_REPLY_ON_START_TRANSITION_FAILURE
    ):
        warning = live_agent_start_transition_failure_text(pointer.get("ticket_key"), start_transition_result)
        try:
            send_slack_message(
                body.get("channel") or pointer.get("support_channel"),
                warning,
                thread_ts=body.get("thread_ts") or pointer.get("support_thread_ts"),
            )
        except Exception as error:
            log_json({
                "level": "ERROR",
                "message": "live_agent_start_transition_warning_failed",
                "ticket_key": pointer.get("ticket_key"),
                "error": str(error),
            })
        log_json({
            "level": "ERROR",
            "message": "live_agent_support_reply_blocked_by_jira_transition",
            "ticket_key": pointer.get("ticket_key"),
            "error_code": start_transition_result.get("error_code"),
            "transition_result": start_transition_result,
        })
        return {
            "ok": False,
            "blocked": True,
            "ticket_key": pointer.get("ticket_key"),
            "error": start_transition_result.get("error"),
            "error_code": start_transition_result.get("error_code") or "jira_start_transition_failed",
            "transition_result": start_transition_result,
        }

    send_slack_message(
        requester_channel,
        agent_reply_to_user_text(pointer, body.get("user"), text),
        thread_ts=requester_thread_ts(pointer),
    )

    if LIVE_AGENT_SYNC_AGENT_REPLIES_TO_JSM and pointer.get("ticket_key"):
        try:
            add_jsm_request_comment(
                pointer["ticket_key"],
                agent_reply_to_jsm_comment(
                    body.get("user"),
                    text,
                    body.get("channel"),
                    body.get("thread_ts") or body.get("ts"),
                ),
                public=JSM_COMMENT_PUBLIC,
            )
        except Exception as error:
            log_json({
                "level": "ERROR",
                "message": "live_agent_support_reply_jsm_sync_failed",
                "ticket_key": pointer.get("ticket_key"),
                "error": str(error),
            })

    append_live_agent_bridge_message(pointer, "Agent", text, body.get("ts"), now_iso)
    log_json({
        "level": "INFO",
        "message": "live_agent_support_reply_forwarded",
        "ticket_key": pointer.get("ticket_key"),
        "requester_channel": requester_channel,
        "support_channel": body.get("channel"),
        "support_thread_ts": body.get("thread_ts"),
        "jira_start_transition": start_transition_result,
    })
    return {"ok": True, "ticket_key": pointer.get("ticket_key"), "jira_start_transition": start_transition_result}


def handle_live_agent_reply_submission(body):
    ticket_key = text_or_empty(body.get("ticket_key"))
    text = text_or_empty(body.get("text"))
    if not ticket_key:
        return {"ok": False, "error": "missing_ticket_key"}
    if not text:
        return {"ok": False, "error": "missing_reply_text", "ticket_key": ticket_key}

    pointer = get_session_item(f"live_agent_ticket:{ticket_key}")
    if not pointer:
        return {"ok": False, "error": "missing_live_agent_ticket_pointer", "ticket_key": ticket_key}
    if pointer.get("bridge_status") != "active":
        return {"ok": False, "error": "inactive_live_agent_bridge", "ticket_key": ticket_key}

    reply_body = {
        **body,
        "channel": body.get("channel") or pointer.get("support_channel"),
        "thread_ts": body.get("thread_ts") or pointer.get("support_thread_ts"),
        "ts": body.get("ts") or body.get("message_ts"),
    }
    return handle_live_agent_support_thread_reply(pointer, reply_body, text)


def handle_live_agent_user_reply(session_id, session_item, body, channel, user, text, thread_ts, ts, processing_message=None):
    ticket_key = session_item.get("live_agent_ticket_key") or session_item.get("last_live_agent_ticket_key")
    event_id = body.get("event_id")
    now_iso = to_iso(datetime.now(timezone.utc))
    marker = acquire_live_agent_slack_comment_marker(
        session_id,
        ticket_key,
        event_id,
        user,
        text,
        channel,
        ts,
        now_iso,
    )

    if not marker.get("acquired"):
        log_json({
            "level": "INFO",
            "message": "live_agent_user_reply_duplicate",
            "session_id": session_id,
            "ticket_key": ticket_key,
            "event_id": event_id,
            "marker_session_id": marker.get("session_id"),
        })
        return {
            "ok": True,
            "duplicate": True,
            "ticket_key": ticket_key,
            "marker_session_id": marker.get("session_id"),
        }

    comment_body = build_live_agent_slack_comment(user, text, channel, thread_ts, ts)

    try:
        support_channel = text_or_empty(session_item.get("support_channel"))
        support_thread_ts = text_or_empty(session_item.get("support_thread_ts"))
        if support_bridge_enabled() and support_channel and support_thread_ts and session_item.get("bridge_status") == "active":
            try:
                send_slack_message(
                    support_channel,
                    f"Requester <@{user}> replied:\n\n{text}",
                    thread_ts=support_thread_ts,
                )
            except Exception as error:
                log_json({
                    "level": "ERROR",
                    "message": "live_agent_user_reply_support_thread_forward_failed",
                    "session_id": session_id,
                    "ticket_key": ticket_key,
                    "error": str(error),
                })
        add_jsm_request_comment(ticket_key, comment_body, public=JSM_COMMENT_PUBLIC)
        update_live_agent_user_reply_session(session_id, session_item, event_id, text, now_iso)
        update_live_agent_user_reply_pointer(ticket_key, text, now_iso)
        append_live_agent_bridge_message(
            {
                "session_id": session_id,
                "target_session_id": session_id,
            },
            "User",
            text,
            ts,
            now_iso,
        )
        update_live_agent_slack_comment_marker(marker["session_id"], "posted", now_iso)
    except Exception as e:
        update_live_agent_slack_comment_marker(marker["session_id"], "failed", now_iso, error=e)
        raise

    ack = f"Sent your reply to support on {ticket_key}."
    if processing_message:
        update_processing_message(processing_message, ack)
    else:
        send_slack_message(channel, ack, thread_ts=thread_ts)

    log_json({
        "level": "INFO",
        "message": "live_agent_user_reply_forwarded",
        "session_id": session_id,
        "ticket_key": ticket_key,
        "event_id": event_id,
        "marker_session_id": marker.get("session_id"),
    })

    return {
        "ok": True,
        "ticket_key": ticket_key,
        "marker_session_id": marker.get("session_id"),
    }


def feedback_stars(rating):
    value = max(1, min(5, int(rating or 1)))
    return "★" * value + "☆" * (5 - value)


def feedback_comment_text(session_id, rating, feedback_text, user, channel):
    parts = [
        "IVY user feedback",
        f"Rating: {feedback_stars(rating)} ({rating}/5)",
    ]
    if feedback_text:
        parts.extend(["", "Feedback:", feedback_text])
    parts.extend([
        "",
        f"Slack user: {user or '-'}",
        f"Slack channel: {channel or '-'}",
        f"Session ID: {session_id or '-'}",
    ])
    return "\n".join(parts)


def feedback_comment_is_public(metadata):
    if not isinstance(metadata, dict):
        return True

    value = metadata.get("comment_public")
    if isinstance(value, bool):
        return value
    if value is None:
        return True
    return str(value).strip().lower() not in {"false", "0", "no", "private", "internal"}


def update_feedback_session(session_id, rating, feedback_text, user, user_name, channel, ticket_key, now_iso, jira_comment_result=None, sharepoint_result=None):
    if not session_id:
        return

    sharepoint_status = (sharepoint_result or {}).get("status") or (
        "posted" if (sharepoint_result or {}).get("ok") else "failed"
    )
    sessions_table.update_item(
        Key={"session_id": session_id},
        UpdateExpression="""
            SET
                feedback_rating = :rating,
                feedback_stars = :stars,
                feedback_text = :feedback_text,
                feedback_user = :user,
                feedback_user_name = :user_name,
                feedback_channel = :channel,
                feedback_submitted_at = :now,
                feedback_jira_ticket_key = :ticket_key,
                feedback_jira_comment_status = :comment_status,
                feedback_sharepoint_sync_status = :sharepoint_status,
                feedback_sharepoint_item_id = :sharepoint_item_id,
                feedback_sharepoint_error = :sharepoint_error,
                feedback_sharepoint_synced_at = :sharepoint_synced_at,
                updated_at = :now,
                #ttl = :ttl
        """,
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={
            ":rating": int(rating),
            ":stars": feedback_stars(rating),
            ":feedback_text": feedback_text or "",
            ":user": user or "",
            ":user_name": user_name or "",
            ":channel": channel or "",
            ":now": now_iso,
            ":ticket_key": ticket_key or "",
            ":comment_status": "posted" if (jira_comment_result or {}).get("ok") else "not_posted",
            ":sharepoint_status": sharepoint_status,
            ":sharepoint_item_id": (sharepoint_result or {}).get("item_id") or "",
            ":sharepoint_error": (sharepoint_result or {}).get("error") or (sharepoint_result or {}).get("reason") or "",
            ":sharepoint_synced_at": now_iso if (sharepoint_result or {}).get("ok") else "",
            ":ttl": ttl_epoch(),
        },
    )


def handle_feedback_submission(body):
    metadata = body.get("feedback_metadata") or {}
    session_id = text_or_empty(metadata.get("session_id"))
    rating = int(metadata.get("rating") or body.get("feedback_rating") or 0)
    feedback_text = text_or_empty(body.get("feedback_text"))
    user = text_or_empty(body.get("user"))
    user_name = text_or_empty(
        body.get("user_name")
        or metadata.get("user_name")
        or metadata.get("username")
        or metadata.get("name")
    )
    channel = text_or_empty(body.get("channel"))
    now_iso = to_iso(datetime.now(timezone.utc))
    session_item = get_session_item(session_id) if session_id else {}
    ticket_key = (
        text_or_empty(metadata.get("summary_jira_ticket_key"))
        or text_or_empty((session_item or {}).get("summary_jira_ticket_key"))
    )

    comment_result = {"ok": False, "skipped": True, "reason": "missing_ticket_key"}
    if ticket_key:
        comment_body = feedback_comment_text(session_id, rating, feedback_text, user, channel)
        try:
            public_comment = feedback_comment_is_public(metadata)
            if public_comment:
                add_jira_issue_comment(ticket_key, comment_body)
            else:
                add_jsm_request_comment(ticket_key, comment_body, public=False)
            comment_result = {"ok": True, "ticket_key": ticket_key, "public": public_comment}
        except Exception as error:
            comment_result = {
                "ok": False,
                "ticket_key": ticket_key,
                "error": str(error),
            }

    sharepoint_result = sync_feedback_to_sharepoint(
        session_id,
        rating,
        feedback_text,
        user,
        user_name,
        channel,
        ticket_key,
        now_iso,
        metadata,
        comment_result,
    )
    if not sharepoint_result.get("ok") and not sharepoint_result.get("skipped"):
        log_json({
            "level": "ERROR",
            "message": "feedback_sharepoint_sync_failed",
            "session_id": session_id,
            "ticket_key": ticket_key,
            "status": sharepoint_result.get("status"),
            "error": sharepoint_result.get("error"),
            "error_code": sharepoint_result.get("error_code"),
        })

    update_feedback_session(
        session_id,
        rating,
        feedback_text,
        user,
        user_name,
        channel,
        ticket_key,
        now_iso,
        jira_comment_result=comment_result,
        sharepoint_result=sharepoint_result,
    )

    try:
        send_slack_message(channel, "Thanks for the feedback.")
    except Exception as error:
        log_json({
            "level": "WARN",
            "message": "feedback_ack_failed",
            "session_id": session_id,
            "error": str(error),
        })

    log_json({
        "level": "INFO" if comment_result.get("ok") else "ERROR",
        "message": "feedback_submission_processed",
        "session_id": session_id,
        "rating": rating,
        "ticket_key": ticket_key,
        "jira_comment_ok": comment_result.get("ok"),
        "sharepoint_sync_status": sharepoint_result.get("status"),
        "error": comment_result.get("error"),
    })
    return {
        "ok": bool(comment_result.get("ok")),
        "session_id": session_id,
        "ticket_key": ticket_key,
        "comment_result": comment_result,
        "sharepoint_result": sharepoint_result,
    }


def process_record(record):
    body = json.loads(record["body"])

    event_id = body.get("event_id")
    text = (body.get("text") or "").strip()
    raw_text = body.get("raw_text", text)
    image_files = body.get("files") or []
    has_image = bool(body.get("has_image") or image_files)
    user = body.get("user", "unknown-user")
    channel = body.get("channel")
    ts = body.get("ts")
    thread_ts = body.get("thread_ts")
    event_type = body.get("event_type")
    channel_type = body.get("channel_type")
    routing_reason = body.get("routing_reason")
    action_id = body.get("action_id")
    action_value = body.get("action_value")
    if event_type == "feedback_submission":
        handle_feedback_submission(body)
        return

    if event_type == "live_agent_reply_submission":
        result = handle_live_agent_reply_submission(body)
        log_json({
            "level": "INFO" if result.get("ok") else "ERROR",
            "message": "live_agent_reply_submission_processed",
            "event_id": event_id,
            "ticket_key": body.get("ticket_key"),
            "ok": result.get("ok"),
            "error": result.get("error"),
        })
        return

    is_interactive_action = event_type == "interactive_action"
    action_payload = parse_action_value(action_value)
    body["action_payload"] = action_payload
    if is_interactive_action and action_payload.get("action"):
        action_value = action_payload["action"]
        body["action_value"] = action_value
        text = action_value
        raw_text = action_value

    conversation_metadata = fetch_conversation_metadata(channel, body.get("conversation_type") or channel_type)
    conversation_type = conversation_metadata.get("conversation_type") or channel_type
    dm_like_conversation = is_dm_like_conversation(conversation_metadata)
    one_to_one_dm = is_one_to_one_dm_conversation(conversation_metadata)
    session_identity = resolve_session_identity(body, channel, user)
    session_id = session_identity["session_id"]
    session_root_ts = session_identity.get("session_root_ts")
    session_thread_ts = slack_thread_ts_for_conversation(session_root_ts, conversation_metadata)

    log_json({
        "level": "INFO",
        "message": "worker_processing_started",
        "event_id": event_id,
        "channel": channel,
        "event_type": event_type,
        "channel_type": channel_type,
        "routing_reason": routing_reason,
        "conversation_type": conversation_type,
        "dm_like_conversation": dm_like_conversation,
        "one_to_one_dm": one_to_one_dm,
        "user": user,
        "text": text,
        "thread_ts": session_thread_ts,
        "session_id": session_id,
        "session_id_version": session_identity.get("session_id_version"),
        "session_scope": session_identity.get("session_scope"),
        "image_file_count": len(image_files),
        "action_id": action_id
    })

    is_bot_message = bool(
        body.get("bot_id")
        or body.get("bot_user_id")
        or body.get("subtype") in {"bot_message", "message_changed", "message_deleted"}
    )
    support_bridge = find_live_agent_support_bridge_for_event(
        channel,
        body.get("thread_ts"),
        body.get("message_ts"),
        body.get("ts"),
    )
    if support_bridge and is_interactive_action and is_live_agent_support_control_action(action_id):
        maybe_send_ephemeral(
            channel,
            user,
            support_control_processing_text(action_id) or "Processing...",
            body.get("thread_ts") or body.get("message_ts") or body.get("ts"),
        )
        control_result = invoke_live_agent_support_control(support_bridge, body, action_id)
        log_json({
            "level": "INFO",
            "message": "live_agent_support_control_forwarded",
            "event_id": event_id,
            "ticket_key": support_bridge.get("ticket_key"),
            "action_id": action_id,
            "result": control_result,
        })
        return

    if support_bridge and not is_interactive_action:
        if is_bot_message or not text:
            log_json({
                "level": "INFO",
                "message": "live_agent_support_thread_message_ignored",
                "event_id": event_id,
                "ticket_key": support_bridge.get("ticket_key"),
                "is_bot_message": is_bot_message,
                "has_text": bool(text),
            })
            return
        handle_live_agent_support_thread_reply(support_bridge, body, text)
        return

    if is_live_agent_support_channel_message(channel, is_interactive_action):
        log_json({
            "level": "WARN",
            "message": "live_agent_support_thread_bridge_not_found",
            "event_id": event_id,
            "channel": channel,
            "thread_ts": body.get("thread_ts"),
            "message_ts": body.get("message_ts"),
            "ts": body.get("ts"),
            "has_text": bool(text),
            "is_bot_message": is_bot_message,
        })
        return

    existing_session = get_session_item(session_id)
    if (
        one_to_one_dm
        and not existing_session
        and not session_identity.get("explicit")
        and not body.get("thread_ts")
    ):
        active_dm_session = get_active_dm_session(channel, user, text)
        if active_dm_session:
            session_id = active_dm_session["session_id"]
            session_root_ts = active_dm_session.get("session_root_ts") or root_ts_from_session_id(session_id)
            session_thread_ts = slack_thread_ts_for_conversation(session_root_ts, conversation_metadata)
            session_identity = {
                "session_id": session_id,
                "session_root_ts": session_root_ts,
                "session_id_version": active_dm_session.get("session_id_version") or "v2_issue_thread",
                "session_scope": active_dm_session.get("session_scope") or "support_issue",
                "legacy": not str(session_id).startswith("issue:"),
                "explicit": False,
            }
            existing_session = active_dm_session

    legacy_session_id = f"{channel}:{user}"
    if (
        not existing_session
        and is_interactive_action
        and not session_identity.get("explicit")
        and not body.get("thread_ts")
        and legacy_session_id != session_id
    ):
        legacy_session = get_session_item(legacy_session_id)
        if legacy_session:
            session_id = legacy_session_id
            session_root_ts = legacy_session.get("session_root_ts") or legacy_session.get("thread_ts")
            session_thread_ts = slack_thread_ts_for_conversation(session_root_ts, conversation_metadata)
            session_identity = {
                "session_id": session_id,
                "session_root_ts": session_root_ts,
                "session_id_version": legacy_session.get("session_id_version") or "legacy",
                "session_scope": legacy_session.get("session_scope") or "legacy_user_channel",
                "legacy": True,
                "explicit": False,
            }
            existing_session = legacy_session

    reset_closed_session = (
        not is_interactive_action
        and session_identity.get("legacy")
        and existing_session.get("conversation_status") in {"closed", "failed"}
    )
    if reset_closed_session:
        lex_session_id = f"{session_id}:{event_id or int(time.time())}"
        log_json({
            "level": "INFO",
            "message": "closed_session_reset_for_new_conversation",
            "event_id": event_id,
            "session_id": session_id,
            "previous_conversation_status": existing_session.get("conversation_status"),
            "lex_session_id": lex_session_id,
        })
        existing_session = {}
    else:
        lex_session_id = existing_session.get("lex_session_id") or session_id

    if existing_session and session_is_terminal(existing_session) and not reset_closed_session:
        reply = terminal_session_reply(existing_session)
        if one_to_one_dm:
            delete_active_dm_session(channel, user)

        send_slack_message(
            channel,
            reply,
            thread_ts=slack_thread_ts_for_conversation(
                session_root_ts or existing_session.get("session_root_ts") or existing_session.get("thread_ts"),
                conversation_metadata
            )
        )
        log_json({
            "level": "INFO",
            "message": "terminal_session_not_reopened",
            "event_id": event_id,
            "session_id": session_id,
            "conversation_status": existing_session.get("conversation_status"),
            "jira_status": existing_session.get("jira_status"),
            "live_agent_status": existing_session.get("live_agent_status")
        })
        return

    if existing_session and not is_interactive_action and is_live_agent_dm_session_pending_or_active(existing_session):
        if is_bot_message or not text:
            log_json({
                "level": "INFO",
                "message": "active_live_agent_non_user_message_ignored",
                "event_id": event_id,
                "session_id": session_id,
                "ticket_key": existing_session.get("live_agent_ticket_key") or existing_session.get("last_live_agent_ticket_key"),
                "is_bot_message": is_bot_message,
                "has_text": bool(text),
            })
            return

        if not is_active_live_agent_session(existing_session):
            reply = "Your live-agent ticket is still being linked. Please try again in a moment."
            send_slack_message(channel, reply, thread_ts=thread_ts or session_thread_ts)
            log_json({
                "level": "INFO",
                "message": "live_agent_pending_ticket_reply_deferred",
                "event_id": event_id,
                "session_id": session_id,
                "live_agent_status": existing_session.get("live_agent_status"),
                "support_options_status": existing_session.get("support_options_status"),
            })
            return

        processing_message = post_processing_message(
            channel,
            "Sending your reply to support...",
            thread_ts=thread_ts or session_thread_ts,
        )
        handle_live_agent_user_reply(
            session_id,
            existing_session,
            body,
            channel,
            user,
            text,
            thread_ts or session_thread_ts,
            ts,
            processing_message=processing_message,
        )
        return

    response_source = "lex"
    claude_fallback_attempted = False
    claude_fallback_error = None
    claude_model_id = None
    next_action = None
    jira_status = None
    jira_intent_name = None
    jira_request_text = None
    jira_request_id = None
    jira_requested_at = None
    jira_confirmed_at = None
    jira_create_started_at = None
    jira_created_at = None
    jira_ticket_key = None
    jira_ticket_url = None
    jira_error = None
    jira_error_code = None
    jira_error_status = None
    last_jira_ticket_key = None
    last_jira_ticket_url = None
    last_jira_created_at = None
    rovo_status = None
    rovo_requested_at = None
    rovo_enriched_at = None
    rovo_error = None
    rovo_error_code = None
    rovo_should_invoke = False
    image_status = None
    image_requested_at = None
    image_analyzed_at = None
    image_error = None
    image_error_code = None
    image_summary = None
    image_resolution_source = None
    image_files_value = image_files if has_image else None
    image_match_issue_id = None
    image_match_score = None
    image_match_expected_lex_intent = None
    image_match_actual_lex_intent = None
    image_match_fallback_reason = None
    assistance_status = None
    assistance_original_text = None
    assistance_raw_text = None
    assistance_lex_intent = None
    assistance_lex_state = None
    assistance_lex_slots = None
    assistance_lex_reply = None
    assistance_requested_at = None
    assistance_closed_at = None
    assistance_resolved_at = None
    support_options_status = None
    support_original_text_value = None
    support_raw_text_value = None
    support_lex_intent = None
    support_lex_state = None
    support_lex_slots = None
    support_lex_reply = None
    support_claude_reply = None
    support_claude_error = None
    support_requested_at = None
    support_resolved_at = None
    live_agent_status = None
    live_agent_requested_at = None
    live_agent_updated_at = None
    live_agent_ticket_key = None
    live_agent_ticket_url = None
    live_agent_jira_project = None
    live_agent_issue_type = None
    live_agent_portal_request_type = None
    last_live_agent_ticket_key = None
    last_live_agent_ticket_url = None
    live_agent_error = None
    live_agent_error_code = None
    slack_blocks = None
    llm_chat_status = None
    llm_chat_count = None
    llm_chat_history = None
    llm_chat_handled = False
    jira_confirmation_handled = False
    interactive_action_handled = False
    assistance_details_handled = False
    manual_close_summary = False
    manual_close_timeout_token = None
    processing_message = None
    workflow_state = None
    state_version = None
    mcp_status = None
    mcp_request_id = None
    mcp_attempt_count = None
    mcp_answer = None
    mcp_confidence = None
    mcp_confidence_reasons = None
    mcp_sources_queried = None
    mcp_sources_used = None
    mcp_citations = None
    mcp_errors = None
    mcp_requested_at = None
    mcp_started_at = None
    mcp_completed_at = None
    mcp_latency_ms = None
    mcp_should_invoke = False

    if is_interactive_action:
        processing_text = interactive_processing_text(action_id)
        if processing_text:
            maybe_send_ephemeral(
                channel,
                user,
                processing_text,
                session_thread_ts or body.get("thread_ts") or body.get("message_ts") or body.get("ts"),
            )
    elif has_image:
        processing_message = post_processing_message(
            channel,
            "Analyzing image...",
            thread_ts=session_thread_ts,
        )
    elif text:
        processing_message = post_processing_message(
            channel,
            "IVY is checking...",
            thread_ts=session_thread_ts,
        )

    if is_interactive_action:
        interactive_action_handled = True
        interactive_result = handle_interactive_action(
            existing_session,
            body,
            session_id
        )

        lex_intent = interactive_result["lex_intent"]
        lex_state = interactive_result["lex_state"]
        lex_slots = interactive_result["lex_slots"]
        lex_reply = interactive_result["reply"]
        slack_blocks = interactive_result.get("blocks")
        lex_reply_empty = False
        lex_session_attributes = {}
        response_source = interactive_result["response_source"]
        claude_fallback_attempted = interactive_result.get("claude_fallback_attempted", False)
        claude_fallback_error = interactive_result.get("claude_fallback_error")
        claude_model_id = interactive_result.get("claude_model_id")
        next_action = interactive_result.get("next_action")
        jira_status = interactive_result.get("jira_status")
        jira_intent_name = interactive_result.get("jira_intent_name")
        jira_request_text = interactive_result.get("jira_request_text")
        jira_request_id = interactive_result.get("jira_request_id")
        jira_requested_at = interactive_result.get("jira_requested_at")
        jira_confirmed_at = interactive_result.get("jira_confirmed_at")
        jira_create_started_at = interactive_result.get("jira_create_started_at")
        jira_created_at = interactive_result.get("jira_created_at")
        jira_ticket_key = interactive_result.get("jira_ticket_key")
        jira_ticket_url = interactive_result.get("jira_ticket_url")
        jira_error = interactive_result.get("jira_error")
        jira_error_code = interactive_result.get("jira_error_code")
        jira_error_status = interactive_result.get("jira_error_status")
        last_jira_ticket_key = interactive_result.get("last_jira_ticket_key")
        last_jira_ticket_url = interactive_result.get("last_jira_ticket_url")
        last_jira_created_at = interactive_result.get("last_jira_created_at")
        rovo_status = interactive_result.get("rovo_status")
        rovo_requested_at = interactive_result.get("rovo_requested_at")
        rovo_enriched_at = interactive_result.get("rovo_enriched_at")
        rovo_error = interactive_result.get("rovo_error")
        rovo_error_code = interactive_result.get("rovo_error_code")
        rovo_should_invoke = interactive_result.get("rovo_should_invoke", False)
        assistance_status = interactive_result.get("assistance_status")
        assistance_original_text = interactive_result.get("assistance_original_text")
        assistance_raw_text = interactive_result.get("assistance_raw_text")
        assistance_lex_intent = interactive_result.get("assistance_lex_intent")
        assistance_lex_state = interactive_result.get("assistance_lex_state")
        assistance_lex_slots = interactive_result.get("assistance_lex_slots")
        assistance_lex_reply = interactive_result.get("assistance_lex_reply")
        assistance_requested_at = interactive_result.get("assistance_requested_at")
        assistance_closed_at = interactive_result.get("assistance_closed_at")
        assistance_resolved_at = interactive_result.get("assistance_resolved_at")
        support_options_status = interactive_result.get("support_options_status")
        support_original_text_value = interactive_result.get("support_original_text")
        support_raw_text_value = interactive_result.get("support_raw_text")
        support_lex_intent = interactive_result.get("support_lex_intent")
        support_lex_state = interactive_result.get("support_lex_state")
        support_lex_slots = interactive_result.get("support_lex_slots")
        support_lex_reply = interactive_result.get("support_lex_reply")
        support_claude_reply = interactive_result.get("support_claude_reply")
        support_claude_error = interactive_result.get("support_claude_error")
        support_requested_at = interactive_result.get("support_requested_at")
        support_resolved_at = interactive_result.get("support_resolved_at")
        live_agent_status = interactive_result.get("live_agent_status")
        live_agent_requested_at = interactive_result.get("live_agent_requested_at")
        live_agent_updated_at = interactive_result.get("live_agent_updated_at")
        live_agent_ticket_key = interactive_result.get("live_agent_ticket_key")
        live_agent_ticket_url = interactive_result.get("live_agent_ticket_url")
        live_agent_jira_project = interactive_result.get("live_agent_jira_project")
        live_agent_issue_type = interactive_result.get("live_agent_issue_type")
        live_agent_portal_request_type = interactive_result.get("live_agent_portal_request_type")
        last_live_agent_ticket_key = interactive_result.get("last_live_agent_ticket_key")
        last_live_agent_ticket_url = interactive_result.get("last_live_agent_ticket_url")
        live_agent_error = interactive_result.get("live_agent_error")
        live_agent_error_code = interactive_result.get("live_agent_error_code")
        llm_chat_status = interactive_result.get("llm_chat_status")
        llm_chat_count = interactive_result.get("llm_chat_count")
        llm_chat_history = interactive_result.get("llm_chat_history")
        manual_close_summary = interactive_result.get("manual_close_summary", False)
        manual_close_timeout_token = interactive_result.get("manual_close_timeout_token")
        workflow_state = interactive_result.get("workflow_state")
        state_version = interactive_result.get("state_version")
        mcp_status = interactive_result.get("mcp_status")
        mcp_request_id = interactive_result.get("mcp_request_id")
        mcp_attempt_count = interactive_result.get("mcp_attempt_count")
        mcp_answer = interactive_result.get("mcp_answer")
        mcp_confidence = interactive_result.get("mcp_confidence")
        mcp_confidence_reasons = interactive_result.get("mcp_confidence_reasons")
        mcp_sources_queried = interactive_result.get("mcp_sources_queried")
        mcp_sources_used = interactive_result.get("mcp_sources_used")
        mcp_citations = interactive_result.get("mcp_citations")
        mcp_errors = interactive_result.get("mcp_errors")
        mcp_requested_at = interactive_result.get("mcp_requested_at")
        mcp_started_at = interactive_result.get("mcp_started_at")
        mcp_completed_at = interactive_result.get("mcp_completed_at")
        mcp_latency_ms = interactive_result.get("mcp_latency_ms")
        mcp_should_invoke = interactive_result.get("mcp_should_invoke", False)

        log_json({
            "level": "INFO",
            "message": "interactive_action_handled",
            "event_id": event_id,
            "session_id": session_id,
            "action_id": action_id,
            "action_value": action_value,
            "response_source": response_source,
            "next_action": next_action,
            "support_options_status": support_options_status,
            "jira_status": jira_status
        })

        if manual_close_summary:
            closed_at = datetime.now(timezone.utc).replace(microsecond=0)
            processing_message = post_processing_message(
                channel,
                "Closing and summarizing this session...",
                thread_ts=session_thread_ts,
            )

            try:
                mark_session_summarizing(
                    session_id,
                    manual_close_timeout_token,
                    closed_at
                )

            except ClientError as e:
                if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    lex_reply = "That action is no longer active. Please send a new message."
                    if not update_processing_message(processing_message, lex_reply):
                        send_slack_message(channel, lex_reply, thread_ts=session_thread_ts)
                    log_json({
                        "level": "INFO",
                        "message": "manual_close_summary_ignored",
                        "event_id": event_id,
                        "session_id": session_id,
                        "reason": "stale_or_closed_session"
                    })
                    return

                raise

            delete_timeout_schedule(session_id, "prompt")
            delete_timeout_schedule(session_id, "close")

            summarizer_result = invoke_summarizer({
                "session_id": session_id,
                "timeout_token": manual_close_timeout_token,
                "closed_at": to_iso(closed_at),
                "reason": "manual_close_summary",
                "conversation_type": conversation_type,
                "processing_channel": (processing_message or {}).get("channel"),
                "processing_ts": (processing_message or {}).get("ts"),
            })

            log_json({
                "level": "INFO" if summarizer_result.get("ok") else "ERROR",
                "message": "manual_close_summary_summarizer_invoked",
                "event_id": event_id,
                "session_id": session_id,
                "summarizer_function": SUMMARIZER_FUNCTION_NAME,
                "ok": summarizer_result.get("ok"),
                "error": summarizer_result.get("error"),
                "error_code": summarizer_result.get("error_code")
            })

            if not summarizer_result.get("ok"):
                failure_text = "I could not complete the summary/save step. This session has not been fully closed."
                if not update_processing_message(processing_message, failure_text):
                    send_slack_message(
                        channel,
                        failure_text,
                        thread_ts=session_thread_ts
                    )

            log_json({
                "level": "INFO",
                "message": "manual_close_summary_completed",
                "event_id": event_id,
                "session_id": session_id,
                "summarizer_ok": summarizer_result.get("ok"),
                "error_code": summarizer_result.get("error_code")
            })
            return

    elif has_image and not is_interactive_action:
        image_requested_at = to_iso(datetime.now(timezone.utc))
        image_result = invoke_image_analysis(
            build_image_payload(body, session_id, text, raw_text, image_files, session_root_ts, session_thread_ts)
        )
        image_analyzed_at = to_iso(datetime.now(timezone.utc))
        image_status = "analysis_completed" if image_result.get("ok") else "failed"
        image_error = image_result.get("error")
        image_error_code = image_result.get("error_code")
        image_summary = (
            image_result.get("summary")
            or image_result.get("message")
            or image_result.get("reply")
        )
        image_query = image_issue_text(text, image_result)

        if ENABLE_ESCALATION_LADDER and image_result.get("ok") and image_query:
            # Ladder path: send the screenshot's OCR text to Lex FIRST (L1), then
            # let the downstream ladder block attach the escalate button so the
            # user can advance L2 KB -> L3 LLM -> L4 ticket. This makes an image
            # follow the same escalation ladder as a typed message.
            text = image_query
            raw_text = image_query
            update_processing_message(processing_message, "Checking IVY routing...")
            lex_bot_id, lex_alias_id, lex_locale_id = current_lex_config()
            lex_response = lex.recognize_text(
                botId=lex_bot_id,
                botAliasId=lex_alias_id,
                localeId=lex_locale_id,
                sessionId=lex_session_id,
                text=image_query,
            )
            lex_state_obj = lex_response.get("sessionState", {})
            intent = lex_state_obj.get("intent", {})
            lex_session_attributes = lex_state_obj.get("sessionAttributes", {}) or {}
            lex_intent = intent.get("name", "UNKNOWN")
            lex_state = intent.get("state", "UNKNOWN")
            lex_slots = simplify_slots(intent.get("slots", {}))
            lex_reply, lex_reply_empty = get_lex_reply(lex_response.get("messages", []))
            response_source = "lex"
            image_status = "completed"
            image_resolution_source = "lex_ladder"
            log_json({
                "level": "INFO",
                "message": "image_routed_to_lex_ladder",
                "event_id": event_id,
                "session_id": session_id,
                "lex_intent": lex_intent,
                "lex_state": lex_state,
            })

        elif image_result.get("ok") and image_query:
            # Direct path: OCR-extracted text -> LLM (Mistral by default, Gemini
            # if IMAGE_LLM_PROVIDER=gemini). No screenshot vector match, no Lex,
            # no Bedrock KB.
            llm_result = invoke_image_llm(image_query)
            llm_source = llm_result.get("source", IMAGE_LLM_PROVIDER)
            if llm_result.get("ok"):
                lex_intent = "ImageLLMFallback"
                lex_state = "Fulfilled"
                lex_slots = {}
                lex_session_attributes = {}
                lex_reply = llm_result["reply"]
                lex_reply_empty = False
                image_status = "completed"
                image_resolution_source = llm_source
                response_source = f"image_{llm_source}"
                image_summary = llm_result.get("summary") or image_summary
            else:
                lex_intent = "ImageLLMFallback"
                lex_state = "Failed"
                lex_slots = {}
                lex_session_attributes = {}
                image_status = "failed"
                image_resolution_source = "unresolved"
                response_source = "image"
                image_error = llm_result.get("error")
                image_error_code = llm_result.get("error_code")
                lex_reply = image_reply_from_result({
                    "ok": False,
                    "error": image_error,
                    "error_code": image_error_code,
                })
                lex_reply_empty = False

        else:
            lex_intent = "ImageRek"
            lex_state = "Failed"
            lex_slots = {}
            lex_session_attributes = {}
            lex_reply = image_reply_from_result(image_result)
            lex_reply_empty = False
            response_source = "image"
            image_resolution_source = "image_analysis"

        log_json({
            "level": "INFO" if image_status == "completed" else "WARN",
            "message": "image_flow_completed",
            "event_id": event_id,
            "session_id": session_id,
            "image_status": image_status,
            "image_resolution_source": image_resolution_source,
            "image_file_count": len(image_files),
            "error_code": image_error_code
        })

    elif has_pending_mcp_query(existing_session) and text:
        assistance_details_handled = True
        now_mcp = to_iso(datetime.now(timezone.utc))
        request_id = make_mcp_request_id(session_id, event_id, ACTION_ID_MCP_ASSIST)
        mcp_query_text = text
        mcp_query_raw_text = raw_text
        original_text = existing_session.get("assistance_original_text") or existing_session.get("last_user_text")

        lex_intent = existing_session.get("assistance_lex_intent") or existing_session.get("lex_intent") or "McpAssist"
        lex_state = "InProgress"
        lex_slots = existing_session.get("assistance_lex_slots") or existing_session.get("lex_slots", {})
        lex_session_attributes = {}
        lex_reply = MCP_ASSIST_PROGRESS_REPLY
        lex_reply_empty = False
        response_source = "mcp_assist_queued"
        next_action = NEXT_ACTION_MCP_ASSIST
        workflow_state = "MCP_QUEUED"
        mcp_status = "QUEUED"
        mcp_request_id = request_id
        mcp_attempt_count = int(existing_session.get("mcp_attempt_count") or 0) + 1
        mcp_requested_at = now_mcp
        mcp_should_invoke = True
        assistance_status = "mcp_queued"
        assistance_original_text = original_text
        assistance_raw_text = existing_session.get("assistance_raw_text") or existing_session.get("last_raw_user_text") or original_text
        assistance_lex_intent = existing_session.get("assistance_lex_intent") or existing_session.get("lex_intent")
        assistance_lex_state = existing_session.get("assistance_lex_state") or existing_session.get("lex_state")
        assistance_lex_slots = existing_session.get("assistance_lex_slots") or existing_session.get("lex_slots", {})
        assistance_lex_reply = existing_session.get("assistance_lex_reply") or existing_session.get("last_bot_reply")
        assistance_requested_at = existing_session.get("assistance_requested_at") or now_mcp
        support_original_text_value = mcp_query_text
        support_raw_text_value = mcp_query_raw_text
        support_lex_intent = assistance_lex_intent
        support_lex_state = assistance_lex_state
        support_lex_slots = assistance_lex_slots
        support_lex_reply = assistance_lex_reply
        support_requested_at = now_mcp

        log_json({
            "level": "INFO",
            "message": "mcp_query_received",
            "event_id": event_id,
            "session_id": session_id,
            "mcp_request_id": mcp_request_id,
            "query": mcp_query_text,
        })

    elif has_pending_assistance_details(existing_session) and text:
        assistance_details_handled = True
        claude_fallback_attempted = True

        details_session = {
            **existing_session,
            "session_id": session_id,
            "session_root_ts": session_root_ts,
            "thread_ts": session_thread_ts,
            "assistance_followup_text": text,
            "assistance_followup_raw_text": raw_text,
            "assistance_followup_at": to_iso(datetime.now(timezone.utc)),
        }

        if ENABLE_CLAUDE_FALLBACK:
            update_processing_message(processing_message, "Thinking through this issue...")
            claude_result = invoke_claude_fallback(
                build_assistance_claude_payload(
                    details_session,
                    body,
                    session_id,
                    text,
                    raw_text
                )
            )
        else:
            claude_result = disabled_claude_fallback_result(
                details_session.get("assistance_lex_reply")
                or details_session.get("support_lex_reply")
                or details_session.get("last_bot_reply")
            )

        final_support_result = build_final_support_result(
            details_session,
            claude_result,
            to_iso(datetime.now(timezone.utc))
        )

        lex_intent = final_support_result["lex_intent"]
        lex_state = final_support_result["lex_state"]
        lex_slots = final_support_result["lex_slots"]
        lex_reply = final_support_result["reply"]
        slack_blocks = final_support_result.get("blocks")
        lex_reply_empty = False
        lex_session_attributes = {}
        response_source = final_support_result["response_source"]
        claude_fallback_error = final_support_result.get("claude_fallback_error")
        claude_model_id = final_support_result.get("claude_model_id")
        next_action = final_support_result.get("next_action")
        assistance_status = final_support_result.get("assistance_status")
        assistance_resolved_at = final_support_result.get("assistance_resolved_at")
        support_options_status = final_support_result.get("support_options_status")
        support_original_text_value = final_support_result.get("support_original_text")
        support_raw_text_value = final_support_result.get("support_raw_text")
        support_lex_intent = final_support_result.get("support_lex_intent")
        support_lex_state = final_support_result.get("support_lex_state")
        support_lex_slots = final_support_result.get("support_lex_slots")
        support_lex_reply = final_support_result.get("support_lex_reply")
        support_claude_reply = final_support_result.get("support_claude_reply")
        support_claude_error = final_support_result.get("support_claude_error")
        support_requested_at = final_support_result.get("support_requested_at")

        log_json({
            "level": "INFO" if response_source == "claude" else "WARN",
            "message": "assistance_details_claude_completed",
            "event_id": event_id,
            "session_id": session_id,
            "lex_intent": lex_intent,
            "response_source": response_source,
            "model_id": claude_model_id,
            "error": claude_fallback_error
        })

    elif has_jira_confirmation_state(existing_session, text):
        jira_confirmation_handled = True
        if classify_jira_confirmation(text) == "yes":
            update_processing_message(processing_message, "Creating Jira ticket...")
        confirmation_result = handle_jira_confirmation(
            existing_session,
            body,
            session_id,
            text,
            raw_text
        )

        lex_intent = confirmation_result["lex_intent"]
        lex_state = confirmation_result["lex_state"]
        lex_slots = confirmation_result["lex_slots"]
        lex_reply = confirmation_result["reply"]
        lex_reply_empty = False
        lex_session_attributes = {}
        response_source = confirmation_result["response_source"]
        next_action = confirmation_result["next_action"]
        jira_status = confirmation_result["jira_status"]
        jira_intent_name = confirmation_result["jira_intent_name"]
        jira_request_text = confirmation_result["jira_request_text"]
        jira_request_id = confirmation_result["jira_request_id"]
        jira_requested_at = confirmation_result["jira_requested_at"]
        jira_confirmed_at = confirmation_result["jira_confirmed_at"]
        jira_create_started_at = confirmation_result["jira_create_started_at"]
        jira_created_at = confirmation_result["jira_created_at"]
        jira_ticket_key = confirmation_result["jira_ticket_key"]
        jira_ticket_url = confirmation_result["jira_ticket_url"]
        jira_error = confirmation_result["jira_error"]
        jira_error_code = confirmation_result["jira_error_code"]
        jira_error_status = confirmation_result["jira_error_status"]
        last_jira_ticket_key = confirmation_result["last_jira_ticket_key"]
        last_jira_ticket_url = confirmation_result["last_jira_ticket_url"]
        last_jira_created_at = confirmation_result["last_jira_created_at"]
        rovo_status = confirmation_result["rovo_status"]
        rovo_requested_at = confirmation_result["rovo_requested_at"]
        rovo_enriched_at = confirmation_result["rovo_enriched_at"]
        rovo_error = confirmation_result["rovo_error"]
        rovo_error_code = confirmation_result["rovo_error_code"]
        rovo_should_invoke = confirmation_result["rovo_should_invoke"]

        log_json({
            "level": "INFO",
            "message": "jira_confirmation_handled",
            "event_id": event_id,
            "session_id": session_id,
            "decision": classify_jira_confirmation(text),
            "jira_status": jira_status,
            "jira_request_id": jira_request_id,
            "jira_ticket_key": jira_ticket_key,
            "jira_error_code": jira_error_code
        })

    elif has_llm_chat_active(existing_session) and text:
        # L3 chat continuation: the user reached the LLM and keeps replying. Each turn
        # goes straight to the LLM (with conversation context) instead of back to Lex,
        # up to LLM_CHAT_MAX_MESSAGES turns. A live agent stays one tap away.
        llm_chat_handled = True
        prior_count = int(existing_session.get("llm_chat_count") or 0)
        original_issue = (
            existing_session.get("support_original_text")
            or existing_session.get("last_user_text")
            or ""
        )
        prior_history = existing_session.get("llm_chat_history") or ""
        lex_intent = "LlmChat"
        lex_state = "Fulfilled"
        lex_slots = {}
        lex_session_attributes = {}
        lex_reply_empty = False
        # Keep the live-agent button primed and the session in a details-collecting state.
        next_action = NEXT_ACTION_FINAL_SUPPORT_OPTIONS
        support_options_status = "pending"
        support_original_text_value = original_issue

        if prior_count >= LLM_CHAT_MAX_MESSAGES:
            # Hit the message cap — stop chatting and steer to a human.
            lex_reply = LLM_CHAT_LIMIT_REPLY
            response_source = "llm_chat_limit"
            slack_blocks = escalation_blocks(lex_reply, original_issue, None, session_id, session_root_ts)
            llm_chat_status = "capped"
            llm_chat_count = prior_count
            llm_chat_history = prior_history
            log_json({
                "level": "INFO",
                "message": "llm_chat_limit_reached",
                "event_id": event_id,
                "session_id": session_id,
                "count": prior_count,
            })
        else:
            update_processing_message(processing_message, "Thinking through this...")
            chat_result = escalation_llm_answer(
                build_llm_chat_prompt(original_issue, prior_history, text),
                session_id,
                channel,
            )
            reply = (chat_result.get("reply") or "").strip() or ESCALATION_EXHAUSTED_REPLY
            lex_reply = reply
            claude_fallback_attempted = True
            response_source = "llm_chat"
            slack_blocks = escalation_blocks(reply, original_issue, None, session_id, session_root_ts)
            llm_chat_count = prior_count + 1
            llm_chat_status = "active"
            llm_chat_history = trim_llm_chat_history(
                prior_history + f"\nUser: {text}\nIVY: {reply}"
            )
            log_json({
                "level": "INFO" if chat_result.get("ok") else "WARN",
                "message": "llm_chat_turn",
                "event_id": event_id,
                "session_id": session_id,
                "count": llm_chat_count,
                "ok": chat_result.get("ok"),
            })

    elif text:
        update_processing_message(processing_message, "Checking IVY routing...")
        lex_bot_id, lex_alias_id, lex_locale_id = current_lex_config()
        response = lex.recognize_text(
            botId=lex_bot_id,
            botAliasId=lex_alias_id,
            localeId=lex_locale_id,
            sessionId=lex_session_id,
            text=text
        )

        session_state = response.get("sessionState", {})
        intent = session_state.get("intent", {})
        lex_session_attributes = session_state.get("sessionAttributes", {}) or {}

        lex_intent = intent.get("name", "UNKNOWN")
        lex_state = intent.get("state", "UNKNOWN")
        lex_slots = simplify_slots(intent.get("slots", {}))
        lex_reply, lex_reply_empty = get_lex_reply(response.get("messages", []))
    else:
        lex_intent = "EMPTY_MESSAGE"
        lex_state = "Ignored"
        lex_slots = {}
        lex_session_attributes = {}
        lex_reply = EMPTY_USER_TEXT_REPLY
        lex_reply_empty = False

    if lex_session_attributes.get("response_source") == "router":
        response_source = "router"
        next_action = lex_session_attributes.get("next_action") or None
        jira_status = lex_session_attributes.get("jira_status") or None
        jira_intent_name = lex_session_attributes.get("jira_intent_name") or lex_intent
        jira_request_text = lex_session_attributes.get("jira_request_text") or text

        log_json({
            "level": "INFO",
            "message": "worker_router_metadata_received",
            "event_id": event_id,
            "session_id": session_id,
            "lex_intent": lex_intent,
            "next_action": next_action,
            "jira_status": jira_status
        })

    should_try_fallback = (
        not interactive_action_handled
        and not assistance_details_handled
        and not jira_confirmation_handled
        and response_source != "router"
        # With the escalation ladder on, KB/LLM are reached by the user clicking
        # "escalate", not by silent auto-fallback — so suppress the auto path.
        and not ENABLE_ESCALATION_LADDER
        and should_use_claude_fallback(text, lex_intent, lex_state, lex_reply_empty)
    )

    if (
        should_try_fallback
        and ENABLE_BEDROCK_KB_ASSIST
    ):
        update_processing_message(processing_message, "Searching knowledge base...")
        kb_result = invoke_bedrock_knowledge_base(text)
        if kb_result.get("ok"):
            lex_intent = BEDROCK_KB_INTENT_NAME
            lex_state = "Fulfilled"
            lex_slots = {}
            lex_reply = kb_result.get("reply") or ""
            lex_reply_empty = not bool(lex_reply.strip())
            response_source = "bedrock_knowledge_base"
            should_try_fallback = False

            log_json({
                "level": "INFO",
                "message": "bedrock_kb_assist_answered",
                "event_id": event_id,
                "session_id": session_id,
                "intent": lex_intent,
                "citation_count": len(kb_result.get("citations") or []),
            })
        else:
            log_json({
                "level": "INFO",
                "message": "bedrock_kb_assist_no_answer",
                "event_id": event_id,
                "session_id": session_id,
                "error_code": kb_result.get("error_code"),
                "error": kb_result.get("error"),
            })

    if (
        AUTO_CLAUDE_FALLBACK_ENABLED
        and should_try_fallback
    ):
        update_processing_message(processing_message, "Thinking through this issue...")
        claude_fallback_attempted = True
        original_lex_reply = lex_reply
        claude_payload = {
            "event_id": event_id,
            "session_id": session_id,
            "channel": channel,
            "channel_type": channel_type,
            "routing_reason": routing_reason,
            "user": user,
            "text": text,
            "raw_text": raw_text,
            "lex": {
                "intent": lex_intent,
                "state": lex_state,
                "slots": lex_slots,
                "reply": lex_reply
            },
            "session": {
                "conversation_status": get_conversation_status(lex_state)
            }
        }
        if ENABLE_CLAUDE_FALLBACK:
            claude_result = invoke_claude_fallback(claude_payload)
        else:
            claude_result = disabled_claude_fallback_result(original_lex_reply)
        final_support_session = {
            **existing_session,
            "session_id": session_id,
            "session_root_ts": session_root_ts,
            "thread_ts": session_thread_ts,
            "assistance_original_text": text,
            "assistance_raw_text": raw_text,
            "assistance_lex_intent": lex_intent,
            "assistance_lex_state": lex_state,
            "assistance_lex_slots": lex_slots,
            "assistance_lex_reply": original_lex_reply,
            "lex_intent": lex_intent,
            "lex_state": lex_state,
            "lex_slots": lex_slots,
            "last_user_text": text,
            "last_raw_user_text": raw_text,
        }
        final_support_result = build_final_support_result(
            final_support_session,
            claude_result,
            to_iso(datetime.now(timezone.utc))
        )

        lex_state = final_support_result["lex_state"]
        lex_reply = final_support_result["reply"]
        slack_blocks = final_support_result.get("blocks")
        response_source = final_support_result["response_source"]
        claude_fallback_error = final_support_result.get("claude_fallback_error")
        claude_model_id = final_support_result.get("claude_model_id")
        next_action = final_support_result.get("next_action")
        assistance_resolved_at = final_support_result.get("assistance_resolved_at")
        support_options_status = final_support_result.get("support_options_status")
        support_original_text_value = final_support_result.get("support_original_text")
        support_raw_text_value = final_support_result.get("support_raw_text")
        support_lex_intent = final_support_result.get("support_lex_intent")
        support_lex_state = final_support_result.get("support_lex_state")
        support_lex_slots = final_support_result.get("support_lex_slots")
        support_lex_reply = final_support_result.get("support_lex_reply")
        support_claude_reply = final_support_result.get("support_claude_reply")
        support_claude_error = final_support_result.get("support_claude_error")
        support_requested_at = final_support_result.get("support_requested_at")

        log_json({
            "level": "INFO" if response_source == "claude" else "WARN",
            "message": "claude_fallback_completed_with_final_options",
            "event_id": event_id,
            "session_id": session_id,
            "lex_intent": lex_intent,
            "lex_state": lex_state,
            "response_source": response_source,
            "model_id": claude_model_id,
            "error": claude_fallback_error
        })

    ladder_active = (
        ENABLE_ESCALATION_LADDER
        and not interactive_action_handled
        and not assistance_details_handled
        and not jira_confirmation_handled
        and response_source == "lex"
        and text
        and lex_state != "Ignored"
        and not next_action
        and not jira_status
        and has_meaningful_user_issue(text)
    )
    if ladder_active:
        # Lex's empty-reply placeholder (EMPTY_LEX_REPLY) reads as a real sentence, so
        # gate on lex_reply_empty too — otherwise "I could not generate a response..."
        # leaks out as if Lex had answered, instead of offering the knowledge base.
        if has_meaningful_bot_answer(lex_reply) and not lex_reply_empty:
            # L1 (Lex) gave a real answer — show it, escalate button -> next layer.
            answer = lex_reply
            next_index = next_escalation_index(1)
            answered_level = "lex"
        else:
            next_index = next_escalation_index(1)
            if next_index is not None and ESCALATION_LEVELS[next_index] == "kb":
                # L1 had no usable answer AND a knowledge base exists — keep L1 and L2
                # separate: DON'T auto-run the KB. Show a short prompt with the
                # "Explore Knowledge base" button so the user opts into L2.
                answer = LEX_NO_ANSWER_EXPLORE_KB_TEXT
                answered_level = "lex_no_answer"
            else:
                # No KB layer available — auto-advance in the BACKGROUND (no click)
                # through the next available layers until one answers.
                answered_index, answer = escalate_autoadvance(1, text, session_id, channel)
                next_index = next_escalation_index((answered_index + 1) if answered_index is not None else len(ESCALATION_LEVELS))
                answered_level = ESCALATION_LEVELS[answered_index] if answered_index is not None else "exhausted"
        lex_reply = answer
        slack_blocks = escalation_blocks(answer, text, next_index, session_id, session_root_ts)
        # If auto-advance already exhausted the automated layers, the block shows a
        # live-agent button; prime the state its handler requires. (The handler falls
        # back to last_user_text for ticket context, so we only set these two.)
        if next_index is None and ENABLE_LIVE_AGENT_ESCALATION:
            next_action = NEXT_ACTION_FINAL_SUPPORT_OPTIONS
            support_options_status = "pending"
        # If auto-advance landed on L3 (LLM), open a running chat so the user can keep
        # replying to the LLM directly.
        if answered_level == "llm" and ENABLE_LLM_CHAT:
            llm_chat_status = "active"
            llm_chat_count = 0
            llm_chat_history = trim_llm_chat_history("IVY: " + (answer or ""))
            support_original_text_value = support_original_text_value or text
        log_json({
            "level": "INFO",
            "message": "escalation_ladder_lex_answer",
            "event_id": event_id,
            "session_id": session_id,
            "answered_level": answered_level,
            "next_index": next_index,
        })
    elif (
        not interactive_action_handled
        and not assistance_details_handled
        and not jira_confirmation_handled
        and response_source == "lex"
        and text
        and lex_state != "Ignored"
        and not next_action
        and not jira_status
        and has_meaningful_user_issue(text)
        and has_meaningful_bot_answer(lex_reply)
    ):
        original_lex_reply = lex_reply
        lex_reply = assistance_reply_text(original_lex_reply)
        slack_blocks = assistance_blocks(original_lex_reply, session_id, session_root_ts)
        next_action = NEXT_ACTION_CLAUDE_ASSISTANCE
        assistance_status = "pending_confirmation"
        assistance_original_text = text
        assistance_raw_text = raw_text
        assistance_lex_intent = lex_intent
        assistance_lex_state = lex_state
        assistance_lex_slots = lex_slots
        assistance_lex_reply = original_lex_reply

        log_json({
            "level": "INFO",
            "message": "lex_assistance_prompt_added",
            "event_id": event_id,
            "session_id": session_id,
            "lex_intent": lex_intent,
            "lex_state": lex_state
        })

    conversation_status = get_conversation_status(lex_state)
    if response_source in {"claude", "claude_failed"}:
        conversation_status = "active"
    if jira_status in {"pending_confirmation", "creating"}:
        conversation_status = "active"
    if assistance_status in {"pending_confirmation", "awaiting_details", "awaiting_mcp_query"}:
        conversation_status = "active"
    if support_options_status in {"pending", "creating_jira", "live_agent_creating", "live_agent_requested"}:
        conversation_status = "active"
    if live_agent_status in ACTIVE_LIVE_AGENT_STATUSES:
        conversation_status = "active"

    offer_close_summary = (
        not is_interactive_action
        and should_offer_close_summary(
            response_source,
            jira_status,
            assistance_status,
            support_options_status,
            existing_session,
            text,
            lex_reply,
        )
    )
    if offer_close_summary:
        conversation_status = "active"

    if conversation_status == "failed":
        session_state = SESSION_STATE_FAILED
    elif offer_close_summary:
        session_state = SESSION_STATE_WAITING_FOR_USER
    elif assistance_status == "pending_confirmation":
        session_state = SESSION_STATE_WAITING_FOR_USER
    elif assistance_status in {"awaiting_details", "awaiting_mcp_query"} or support_options_status in {"pending", "creating_jira", "live_agent_creating"}:
        session_state = SESSION_STATE_COLLECTING_DETAILS
    elif conversation_status == "closed":
        session_state = SESSION_STATE_CLOSED
    else:
        session_state = SESSION_STATE_OPEN

    activity_at_dt = datetime.now(timezone.utc).replace(microsecond=0)
    updated_at = to_iso(activity_at_dt)

    if jira_status == "pending_confirmation":
        jira_request_id = jira_request_id or make_jira_request_id(
            session_id,
            event_id,
            jira_intent_name or lex_intent,
            jira_request_text or text
        )
        jira_requested_at = jira_requested_at or updated_at

    if assistance_status in {"pending_confirmation", "awaiting_details", "awaiting_mcp_query"}:
        assistance_requested_at = assistance_requested_at or updated_at

    if support_options_status == "pending":
        support_requested_at = support_requested_at or updated_at

    if live_agent_status in {"requested", "in_progress", "waiting_for_customer", "resolved"}:
        live_agent_requested_at = live_agent_requested_at or updated_at
        live_agent_updated_at = live_agent_updated_at or updated_at

    if rovo_should_invoke and rovo_status == "pending":
        rovo_requested_at = rovo_requested_at or updated_at

    transcript_append = build_transcript_append(text, lex_reply, ts)

    timeout_state = None
    if conversation_status == "active":
        timeout_due_at_dt = activity_at_dt + timedelta(seconds=INACTIVITY_TIMEOUT_SECONDS)
        timeout_due_at = to_iso(timeout_due_at_dt)
        timeout_state = {
            "timeout_due_at_dt": timeout_due_at_dt,
            "timeout_due_at": timeout_due_at,
            "timeout_token": timeout_token(session_id, event_id, updated_at),
            "timeout_schedule_name": timeout_schedule_name(session_id, "prompt")
        }

    # The ladder's own blocks already include a "Resolved" (close) button, so don't
    # append the close-summary actions on top — that was producing duplicate rows.
    if offer_close_summary and not ladder_active:
        slack_blocks = add_close_summary_actions(slack_blocks, lex_reply, session_id, session_root_ts)

    created_at_expression = (
        "created_at = :created_at"
        if reset_closed_session
        else "created_at = if_not_exists(created_at, :created_at)"
    )

    update_expression = f"""
        SET
            #channel = :channel,
            #user = :user,
            last_event_id = :event_id,
            last_user_text = :last_user_text,
            last_raw_user_text = :last_raw_user_text,
            last_bot_reply = :last_bot_reply,
            thread_ts = :thread_ts,
            session_root_ts = :session_root_ts,
            session_id_version = :session_id_version,
            session_scope = :session_scope,
            response_source = :response_source,
            claude_fallback_attempted = :claude_fallback_attempted,
            last_ts = :last_ts,
            last_activity_at = :last_activity_at,
            event_type = :event_type,
            channel_type = :channel_type,
            routing_reason = :routing_reason,
            conversation_type = :conversation_type,
            conversation_metadata = :conversation_metadata,
            lex_session_id = :lex_session_id,
            lex_intent = :lex_intent,
            lex_state = :lex_state,
            lex_slots = :lex_slots,
            conversation_status = :conversation_status,
            session_state = :session_state,
            timeout_status = :timeout_status,
            slack_team_id = :slack_team_id,
            {created_at_expression},
            updated_at = :updated_at,
            #ttl = :ttl
    """

    expression_attribute_values = {
        ":channel": channel,
        # Stamped on every session so anything that acts on a session LATER
        # can work out which workspace it belongs to. The timeout handler
        # wakes from EventBridge hours after the fact with no live payload to
        # carry a token, so the session record is the only durable place that
        # information can come from. Empty string rather than None because
        # DynamoDB rejects a null here and the readers treat "" as absent.
        ":slack_team_id": (body.get("slack_tenant") or {}).get("team_id") or "",
        ":user": user,
        ":event_id": event_id,
        ":last_user_text": text,
        ":last_raw_user_text": raw_text,
        ":last_bot_reply": lex_reply,
        ":thread_ts": session_thread_ts,
        ":session_root_ts": session_root_ts,
        ":session_id_version": session_identity.get("session_id_version"),
        ":session_scope": session_identity.get("session_scope"),
        ":response_source": response_source,
        ":claude_fallback_attempted": claude_fallback_attempted,
        ":last_ts": ts,
        ":last_activity_at": updated_at,
        ":event_type": event_type,
        ":channel_type": channel_type,
        ":routing_reason": routing_reason,
        ":conversation_type": conversation_type,
        ":conversation_metadata": conversation_metadata,
        ":lex_session_id": lex_session_id,
        ":lex_intent": lex_intent,
        ":lex_state": lex_state,
        ":lex_slots": lex_slots,
        ":conversation_status": conversation_status,
        ":session_state": session_state,
        ":timeout_status": "scheduled" if timeout_state else "inactive",
        ":created_at": updated_at,
        ":updated_at": updated_at,
        ":ttl": ttl_epoch(),
        ":one": 1
    }

    remove_attributes = []
    if reset_closed_session:
        remove_attributes.extend([
            "manual_closed_at",
            "manual_close_reason",
            "summary_status",
            "summary_started_at",
            "summary_completed_at",
            "summary_failed_at",
            "summary_error",
            "summary_error_code",
            "conversation_summary",
            "summary_webhook_sent",
            "summary_webhook_error",
            "summary_audit_s3_key",
            "summary_audit_error",
        ])

    if timeout_state:
        update_expression += """
            ,
            timeout_due_at = :timeout_due_at,
            timeout_token = :timeout_token,
            timeout_schedule_name = :timeout_schedule_name
        """
        expression_attribute_values.update({
            ":timeout_due_at": timeout_state["timeout_due_at"],
            ":timeout_token": timeout_state["timeout_token"],
            ":timeout_schedule_name": timeout_state["timeout_schedule_name"]
        })
        remove_attributes.extend([
            "timeout_prompt_started_at",
            "timeout_prompted_at",
            "timeout_close_due_at",
            "timeout_closing_started_at",
            "timeout_closed_at"
        ])
    else:
        remove_attributes.extend([
            "timeout_due_at",
            "timeout_token",
            "timeout_schedule_name",
            "timeout_prompt_started_at",
            "timeout_prompted_at",
            "timeout_close_due_at",
            "timeout_closing_started_at",
            "timeout_closed_at"
        ])

    if next_action:
        update_expression += """
            ,
            next_action = :next_action
        """
        expression_attribute_values.update({
            ":next_action": next_action
        })
    else:
        remove_attributes.append("next_action")

    if jira_status:
        update_expression += """
            ,
            jira_status = :jira_status
        """
        expression_attribute_values[":jira_status"] = jira_status
    else:
        remove_attributes.append("jira_status")

    if jira_intent_name:
        update_expression += """
            ,
            jira_intent_name = :jira_intent_name
        """
        expression_attribute_values[":jira_intent_name"] = jira_intent_name
    else:
        remove_attributes.append("jira_intent_name")

    if jira_request_text:
        update_expression += """
            ,
            jira_request_text = :jira_request_text
        """
        expression_attribute_values[":jira_request_text"] = jira_request_text
    else:
        remove_attributes.append("jira_request_text")

    if jira_request_id:
        update_expression += """
            ,
            jira_request_id = :jira_request_id
        """
        expression_attribute_values[":jira_request_id"] = jira_request_id
    else:
        remove_attributes.append("jira_request_id")

    if jira_requested_at:
        update_expression += """
            ,
            jira_requested_at = :jira_requested_at
        """
        expression_attribute_values[":jira_requested_at"] = jira_requested_at
    else:
        remove_attributes.append("jira_requested_at")

    if jira_confirmed_at:
        update_expression += """
            ,
            jira_confirmed_at = :jira_confirmed_at
        """
        expression_attribute_values[":jira_confirmed_at"] = jira_confirmed_at
    else:
        remove_attributes.append("jira_confirmed_at")

    if jira_create_started_at:
        update_expression += """
            ,
            jira_create_started_at = :jira_create_started_at
        """
        expression_attribute_values[":jira_create_started_at"] = jira_create_started_at
    else:
        remove_attributes.append("jira_create_started_at")

    if jira_created_at:
        update_expression += """
            ,
            jira_created_at = :jira_created_at
        """
        expression_attribute_values[":jira_created_at"] = jira_created_at
    else:
        remove_attributes.append("jira_created_at")

    if jira_ticket_key:
        update_expression += """
            ,
            jira_ticket_key = :jira_ticket_key
        """
        expression_attribute_values[":jira_ticket_key"] = jira_ticket_key
    else:
        remove_attributes.append("jira_ticket_key")

    if jira_ticket_url:
        update_expression += """
            ,
            jira_ticket_url = :jira_ticket_url
        """
        expression_attribute_values[":jira_ticket_url"] = jira_ticket_url
    else:
        remove_attributes.append("jira_ticket_url")

    if jira_error:
        update_expression += """
            ,
            jira_error = :jira_error
        """
        expression_attribute_values[":jira_error"] = jira_error
    else:
        remove_attributes.append("jira_error")

    if jira_error_code:
        update_expression += """
            ,
            jira_error_code = :jira_error_code
        """
        expression_attribute_values[":jira_error_code"] = jira_error_code
    else:
        remove_attributes.append("jira_error_code")

    if jira_error_status:
        update_expression += """
            ,
            jira_error_status = :jira_error_status
        """
        expression_attribute_values[":jira_error_status"] = jira_error_status
    else:
        remove_attributes.append("jira_error_status")

    if last_jira_ticket_key:
        update_expression += """
            ,
            last_jira_ticket_key = :last_jira_ticket_key
        """
        expression_attribute_values[":last_jira_ticket_key"] = last_jira_ticket_key

    if last_jira_ticket_url:
        update_expression += """
            ,
            last_jira_ticket_url = :last_jira_ticket_url
        """
        expression_attribute_values[":last_jira_ticket_url"] = last_jira_ticket_url

    if last_jira_created_at:
        update_expression += """
            ,
            last_jira_created_at = :last_jira_created_at
        """
        expression_attribute_values[":last_jira_created_at"] = last_jira_created_at

    if rovo_status:
        update_expression += """
            ,
            rovo_status = :rovo_status
        """
        expression_attribute_values[":rovo_status"] = rovo_status

    if rovo_requested_at:
        update_expression += """
            ,
            rovo_requested_at = :rovo_requested_at
        """
        expression_attribute_values[":rovo_requested_at"] = rovo_requested_at

    if rovo_enriched_at:
        update_expression += """
            ,
            rovo_enriched_at = :rovo_enriched_at
        """
        expression_attribute_values[":rovo_enriched_at"] = rovo_enriched_at

    if rovo_error:
        update_expression += """
            ,
            rovo_error = :rovo_error
        """
        expression_attribute_values[":rovo_error"] = rovo_error

    if rovo_error_code:
        update_expression += """
            ,
            rovo_error_code = :rovo_error_code
        """
        expression_attribute_values[":rovo_error_code"] = rovo_error_code

    if rovo_status == "pending":
        remove_attributes.extend([
            "rovo_enriched_at",
            "rovo_error",
            "rovo_error_code",
            "rovo_summary",
            "rovo_comment_id"
        ])

    image_attributes = {
        "image_status": image_status,
        "image_requested_at": image_requested_at,
        "image_analyzed_at": image_analyzed_at,
        "image_error": image_error,
        "image_error_code": image_error_code,
        "image_summary": image_summary,
        "image_resolution_source": image_resolution_source,
        "image_files": image_files_value,
    }

    for attribute_name, attribute_value in image_attributes.items():
        if attribute_value is not None:
            value_name = f":{attribute_name}"
            update_expression += f"""
            ,
            {attribute_name} = {value_name}
        """
            expression_attribute_values[value_name] = attribute_value
        else:
            remove_attributes.append(attribute_name)

    if claude_fallback_error:
        update_expression += """
            ,
            claude_fallback_error = :claude_fallback_error
        """
        expression_attribute_values[":claude_fallback_error"] = claude_fallback_error
    else:
        remove_attributes.append("claude_fallback_error")

    if claude_model_id:
        update_expression += """
            ,
            claude_model_id = :claude_model_id
        """
        expression_attribute_values[":claude_model_id"] = claude_model_id
    else:
        remove_attributes.append("claude_model_id")

    support_flow_attributes = {
        "assistance_status": assistance_status,
        "assistance_original_text": assistance_original_text,
        "assistance_raw_text": assistance_raw_text,
        "assistance_lex_intent": assistance_lex_intent,
        "assistance_lex_state": assistance_lex_state,
        "assistance_lex_slots": assistance_lex_slots,
        "assistance_lex_reply": assistance_lex_reply,
        "assistance_requested_at": assistance_requested_at,
        "assistance_closed_at": assistance_closed_at,
        "assistance_resolved_at": assistance_resolved_at,
        "support_options_status": support_options_status,
        "support_original_text": support_original_text_value,
        "support_raw_text": support_raw_text_value,
        "support_lex_intent": support_lex_intent,
        "support_lex_state": support_lex_state,
        "support_lex_slots": support_lex_slots,
        "support_lex_reply": support_lex_reply,
        "support_claude_reply": support_claude_reply,
        "support_claude_error": support_claude_error,
        "support_requested_at": support_requested_at,
        "support_resolved_at": support_resolved_at,
        "live_agent_status": live_agent_status,
        "live_agent_requested_at": live_agent_requested_at,
        "live_agent_updated_at": live_agent_updated_at,
        "live_agent_error": live_agent_error,
        "live_agent_error_code": live_agent_error_code,
        "llm_chat_status": llm_chat_status,
        "llm_chat_count": llm_chat_count,
        "llm_chat_history": llm_chat_history,
    }

    for attribute_name, attribute_value in support_flow_attributes.items():
        if attribute_value is not None:
            value_name = f":{attribute_name}"
            update_expression += f"""
            ,
            {attribute_name} = {value_name}
        """
            expression_attribute_values[value_name] = attribute_value
        else:
            remove_attributes.append(attribute_name)

    mcp_flow_attributes = {
        "workflow_state": workflow_state,
        "state_version": state_version,
        "mcp_status": mcp_status,
        "mcp_request_id": mcp_request_id,
        "mcp_attempt_count": mcp_attempt_count,
        "mcp_answer": mcp_answer,
        "mcp_confidence": mcp_confidence,
        "mcp_confidence_reasons": mcp_confidence_reasons,
        "mcp_sources_queried": mcp_sources_queried,
        "mcp_sources_used": mcp_sources_used,
        "mcp_citations": mcp_citations,
        "mcp_errors": mcp_errors,
        "mcp_requested_at": mcp_requested_at,
        "mcp_started_at": mcp_started_at,
        "mcp_completed_at": mcp_completed_at,
        "mcp_latency_ms": mcp_latency_ms,
        "last_action_id": action_id if is_interactive_action else None,
        "last_slack_event_id": event_id if is_interactive_action else None,
        "last_updated_at": updated_at,
    }

    for attribute_name, attribute_value in mcp_flow_attributes.items():
        if attribute_value is None:
            continue

        value_name = f":{attribute_name}"
        update_expression += f"""
            ,
            {attribute_name} = {value_name}
        """
        expression_attribute_values[value_name] = attribute_value

    live_agent_ticket_attributes = {
        "live_agent_ticket_key": live_agent_ticket_key,
        "live_agent_ticket_url": live_agent_ticket_url,
        "live_agent_jira_project": live_agent_jira_project,
        "live_agent_issue_type": live_agent_issue_type,
        "live_agent_portal_request_type": live_agent_portal_request_type,
        "last_live_agent_ticket_key": last_live_agent_ticket_key,
        "last_live_agent_ticket_url": last_live_agent_ticket_url,
    }

    for attribute_name, attribute_value in live_agent_ticket_attributes.items():
        if attribute_value is None:
            continue

        value_name = f":{attribute_name}"
        update_expression += f"""
            ,
            {attribute_name} = {value_name}
        """
        expression_attribute_values[value_name] = attribute_value

    if transcript_append:
        update_expression += """
            ,
            session_messages = list_append(if_not_exists(session_messages, :empty_list), :transcript_append)
        """
        expression_attribute_values[":empty_list"] = []
        expression_attribute_values[":transcript_append"] = transcript_append

    remove_attributes = list(dict.fromkeys(remove_attributes))

    if remove_attributes:
        update_expression += " REMOVE " + ", ".join(remove_attributes)

    update_expression += """
        ADD
            message_count :one
    """

    try:
        sessions_table.update_item(
            Key={
                "session_id": session_id
            },
            UpdateExpression=update_expression,
            ExpressionAttributeNames={
                "#channel": "channel",
                "#user": "user",
                "#ttl": "ttl"
            },
            ExpressionAttributeValues=expression_attribute_values
        )
    except ClientError as e:
        error = e.response.get("Error", {})
        if (
            error.get("Code") != "ValidationException"
            or "Expression size has exceeded" not in error.get("Message", "")
        ):
            raise

        log_json({
            "level": "WARN",
            "message": "session_full_update_too_large_compacting",
            "event_id": event_id,
            "session_id": session_id,
            "jira_request_id": jira_request_id,
            "jira_status": jira_status,
            "update_expression_length": len(update_expression),
        })

        compact_update_expression = """
            SET
                #channel = :channel,
                #user = :user,
                last_event_id = :event_id,
                last_user_text = :last_user_text,
                last_raw_user_text = :last_raw_user_text,
                last_bot_reply = :last_bot_reply,
                thread_ts = :thread_ts,
                session_root_ts = :session_root_ts,
                response_source = :response_source,
                last_ts = :last_ts,
                last_activity_at = :last_activity_at,
                event_type = :event_type,
                channel_type = :channel_type,
                routing_reason = :routing_reason,
                conversation_type = :conversation_type,
                lex_session_id = :lex_session_id,
                lex_intent = :lex_intent,
                lex_state = :lex_state,
                lex_slots = :lex_slots,
                conversation_status = :conversation_status,
                session_state = :session_state,
                timeout_status = :timeout_status,
                updated_at = :updated_at,
                #ttl = :ttl
        """
        compact_values = {
            key: expression_attribute_values[key]
            for key in (
                ":channel",
                ":user",
                ":event_id",
                ":last_user_text",
                ":last_raw_user_text",
                ":last_bot_reply",
                ":thread_ts",
                ":session_root_ts",
                ":response_source",
                ":last_ts",
                ":last_activity_at",
                ":event_type",
                ":channel_type",
                ":routing_reason",
                ":conversation_type",
                ":lex_session_id",
                ":lex_intent",
                ":lex_state",
                ":lex_slots",
                ":conversation_status",
                ":session_state",
                ":timeout_status",
                ":updated_at",
                ":ttl",
                ":one",
            )
        }

        compact_optional_attributes = {
            "next_action": next_action,
            "assistance_status": assistance_status,
            "assistance_original_text": assistance_original_text,
            "assistance_raw_text": assistance_raw_text,
            "assistance_lex_intent": assistance_lex_intent,
            "assistance_lex_state": assistance_lex_state,
            "assistance_lex_slots": assistance_lex_slots,
            "assistance_lex_reply": assistance_lex_reply,
            "assistance_requested_at": assistance_requested_at,
            "assistance_closed_at": assistance_closed_at,
            "assistance_resolved_at": assistance_resolved_at,
            "support_options_status": support_options_status,
            "support_original_text": support_original_text_value,
            "support_raw_text": support_raw_text_value,
            "support_lex_intent": support_lex_intent,
            "support_lex_state": support_lex_state,
            "support_lex_slots": support_lex_slots,
            "support_lex_reply": support_lex_reply,
            "support_claude_reply": support_claude_reply,
            "support_claude_error": support_claude_error,
            "support_requested_at": support_requested_at,
            "support_resolved_at": support_resolved_at,
            "jira_status": jira_status,
            "jira_intent_name": jira_intent_name,
            "jira_request_text": jira_request_text,
            "jira_request_id": jira_request_id,
            "jira_requested_at": jira_requested_at,
            "jira_confirmed_at": jira_confirmed_at,
            "jira_create_started_at": jira_create_started_at,
            "jira_created_at": jira_created_at,
            "jira_ticket_key": jira_ticket_key,
            "jira_ticket_url": jira_ticket_url,
            "jira_error": jira_error,
            "jira_error_code": jira_error_code,
            "jira_error_status": jira_error_status,
            "last_jira_ticket_key": last_jira_ticket_key,
            "last_jira_ticket_url": last_jira_ticket_url,
            "last_jira_created_at": last_jira_created_at,
            "rovo_status": rovo_status,
            "rovo_requested_at": rovo_requested_at,
            "support_options_status": support_options_status,
            "support_resolved_at": support_resolved_at,
            "llm_chat_status": llm_chat_status,
            "llm_chat_count": llm_chat_count,
            "llm_chat_history": llm_chat_history,
            "live_agent_status": live_agent_status,
            "live_agent_requested_at": live_agent_requested_at,
            "live_agent_updated_at": live_agent_updated_at,
            "live_agent_ticket_key": live_agent_ticket_key,
            "live_agent_ticket_url": live_agent_ticket_url,
            "live_agent_jira_project": live_agent_jira_project,
            "live_agent_issue_type": live_agent_issue_type,
            "live_agent_portal_request_type": live_agent_portal_request_type,
            "last_live_agent_ticket_key": last_live_agent_ticket_key,
            "last_live_agent_ticket_url": last_live_agent_ticket_url,
            "live_agent_error": live_agent_error,
            "live_agent_error_code": live_agent_error_code,
            "workflow_state": workflow_state,
            "state_version": state_version,
            "mcp_status": mcp_status,
            "mcp_request_id": mcp_request_id,
            "mcp_attempt_count": mcp_attempt_count,
            "mcp_answer": mcp_answer,
            "mcp_confidence": mcp_confidence,
            "mcp_confidence_reasons": mcp_confidence_reasons,
            "mcp_sources_queried": mcp_sources_queried,
            "mcp_sources_used": mcp_sources_used,
            "mcp_citations": mcp_citations,
            "mcp_errors": mcp_errors,
            "mcp_requested_at": mcp_requested_at,
            "mcp_started_at": mcp_started_at,
            "mcp_completed_at": mcp_completed_at,
            "mcp_latency_ms": mcp_latency_ms,
            "last_action_id": action_id if is_interactive_action else None,
            "last_slack_event_id": event_id if is_interactive_action else None,
            "last_updated_at": updated_at,
        }

        compact_remove_attributes = []
        for attribute_name, attribute_value in compact_optional_attributes.items():
            if attribute_value is None:
                compact_remove_attributes.append(attribute_name)
                continue

            value_name = f":compact_{attribute_name}"
            compact_update_expression += f", {attribute_name} = {value_name}"
            compact_values[value_name] = attribute_value

        if timeout_state:
            compact_update_expression += """
                ,
                timeout_due_at = :timeout_due_at,
                timeout_token = :timeout_token,
                timeout_schedule_name = :timeout_schedule_name
            """
            compact_values[":timeout_due_at"] = timeout_state["timeout_due_at"]
            compact_values[":timeout_token"] = timeout_state["timeout_token"]
            compact_values[":timeout_schedule_name"] = timeout_state["timeout_schedule_name"]

        if transcript_append:
            compact_update_expression += """
                ,
                session_messages = list_append(if_not_exists(session_messages, :empty_list), :transcript_append)
            """
            compact_values[":empty_list"] = []
            compact_values[":transcript_append"] = transcript_append

        compact_remove_attributes = [
            attribute_name
            for attribute_name in dict.fromkeys(compact_remove_attributes)
            if attribute_name not in {
                "last_jira_ticket_key",
                "last_jira_ticket_url",
                "last_jira_created_at",
                "live_agent_ticket_key",
                "live_agent_ticket_url",
                "live_agent_jira_project",
                "live_agent_issue_type",
                "live_agent_portal_request_type",
                "live_agent_updated_at",
                "last_live_agent_ticket_key",
                "last_live_agent_ticket_url",
                "assistance_status",
                "assistance_original_text",
                "assistance_raw_text",
                "assistance_lex_intent",
                "assistance_lex_state",
                "assistance_lex_slots",
                "assistance_lex_reply",
                "assistance_requested_at",
                "assistance_closed_at",
                "assistance_resolved_at",
                "support_options_status",
                "support_original_text",
                "support_raw_text",
                "support_lex_intent",
                "support_lex_state",
                "support_lex_slots",
                "support_lex_reply",
                "support_claude_reply",
                "support_claude_error",
                "support_requested_at",
                "support_resolved_at",
                "workflow_state",
                "state_version",
                "mcp_status",
                "mcp_request_id",
                "mcp_attempt_count",
                "mcp_answer",
                "mcp_confidence",
                "mcp_confidence_reasons",
                "mcp_sources_queried",
                "mcp_sources_used",
                "mcp_citations",
                "mcp_errors",
                "mcp_requested_at",
                "mcp_started_at",
                "mcp_completed_at",
                "mcp_latency_ms",
                "last_action_id",
                "last_slack_event_id",
                "last_updated_at",
            }
        ]
        if compact_remove_attributes:
            compact_update_expression += " REMOVE " + ", ".join(compact_remove_attributes)

        compact_update_expression += " ADD message_count :one"

        sessions_table.update_item(
            Key={
                "session_id": session_id
            },
            UpdateExpression=compact_update_expression,
            ExpressionAttributeNames={
                "#channel": "channel",
                "#user": "user",
                "#ttl": "ttl"
            },
            ExpressionAttributeValues=compact_values
        )

    if one_to_one_dm and not session_identity.get("legacy"):
        if not is_interactive_action and conversation_status == "active":
            supersede_other_dm_sessions(channel, user, session_id, updated_at)

        updated_session_state = {
            **existing_session,
            "conversation_status": conversation_status,
            "summary_status": None,
            "jira_status": jira_status,
            "jira_ticket_key": jira_ticket_key,
            "last_jira_ticket_key": last_jira_ticket_key,
            "jira_created_at": jira_created_at,
            "next_action": next_action,
            "assistance_status": assistance_status,
            "support_options_status": support_options_status,
            "live_agent_status": live_agent_status,
        }
        if conversation_status == "active" and session_waits_for_dm_text(updated_session_state, text):
            upsert_active_dm_session(channel, user, session_id, session_root_ts, updated_at)
        else:
            delete_active_dm_session(channel, user)

    if mcp_should_invoke:
        mcp_payload = build_mcp_assist_payload(
            {
                **existing_session,
                "assistance_original_text": assistance_original_text,
                "assistance_raw_text": assistance_raw_text,
                "assistance_lex_intent": assistance_lex_intent,
                "assistance_lex_state": assistance_lex_state,
                "assistance_lex_slots": assistance_lex_slots,
                "assistance_lex_reply": assistance_lex_reply,
                "mcp_query_text": support_original_text_value,
                "last_user_text": support_original_text_value or assistance_original_text or existing_session.get("last_user_text"),
                "last_raw_user_text": support_raw_text_value or assistance_raw_text or existing_session.get("last_raw_user_text"),
                "last_bot_reply": assistance_lex_reply or existing_session.get("last_bot_reply"),
                "session_messages": existing_session.get("session_messages") or [],
            },
            body,
            session_id,
            mcp_request_id,
            channel,
            session_thread_ts,
            user,
        )
        mcp_invoke_result = invoke_mcp_assist(mcp_payload)
        if mcp_invoke_result.get("ok"):
            log_json({
                "level": "INFO",
                "message": "mcp_assist_invoked",
                "event_id": event_id,
                "session_id": session_id,
                "mcp_request_id": mcp_request_id,
                "target": mcp_invoke_result.get("target"),
            })
        else:
            mark_mcp_invoke_failed(
                session_id,
                mcp_request_id,
                mcp_invoke_result.get("error"),
                mcp_invoke_result.get("error_code"),
            )
            response_source = "mcp_assist_failed"
            workflow_state = "MCP_ERROR"
            mcp_status = "ERROR"
            mcp_errors = [{
                "source": "mcp",
                "category": mcp_invoke_result.get("error_code") or "mcp_assist_enqueue_failed",
            }]
            next_action = NEXT_ACTION_CLAUDE_ASSISTANCE
            lex_reply = mcp_followup_reply_text(MCP_ASSIST_FAILED_REPLY)
            slack_blocks = mcp_followup_blocks(
                MCP_ASSIST_FAILED_REPLY,
                session_id,
                session_root_ts,
            )
            log_json({
                "level": "ERROR",
                "message": "mcp_assist_invoke_failed",
                "event_id": event_id,
                "session_id": session_id,
                "mcp_request_id": mcp_request_id,
                "error": mcp_invoke_result.get("error"),
                "error_code": mcp_invoke_result.get("error_code"),
            })

    if rovo_should_invoke:
        rovo_payload = build_rovo_payload(
            body,
            session_id,
            lex_intent,
            lex_state,
            lex_slots,
            jira_request_id,
            jira_requested_at,
            jira_created_at,
            jira_ticket_key,
            jira_ticket_url,
            jira_request_text,
            raw_text,
            support_original_text_value,
            support_raw_text_value,
            support_lex_reply,
            support_claude_reply,
            support_claude_error,
        )
        rovo_result = invoke_rovo_enrichment(rovo_payload)

        if rovo_result.get("ok"):
            log_json({
                "level": "INFO",
                "message": "rovo_enrichment_invoked",
                "event_id": event_id,
                "session_id": session_id,
                "jira_request_id": jira_request_id,
                "ticket_key": jira_ticket_key,
                "skipped": rovo_result.get("skipped", False)
            })

        else:
            mark_rovo_invoke_failed(
                session_id,
                rovo_result.get("error", "unknown_rovo_invoke_error"),
                rovo_result.get("error_code", "rovo_lambda_invoke_failed")
            )
            rovo_status = "failed"
            rovo_error = rovo_result.get("error", "unknown_rovo_invoke_error")
            rovo_error_code = rovo_result.get("error_code", "rovo_lambda_invoke_failed")

            log_json({
                "level": "ERROR",
                "message": "rovo_enrichment_invoke_failed",
                "event_id": event_id,
                "session_id": session_id,
                "jira_request_id": jira_request_id,
                "ticket_key": jira_ticket_key,
                "error": rovo_error,
                "error_code": rovo_error_code
            })

    if timeout_state:
        refresh_timeout_schedule(session_id, timeout_state)
    else:
        delete_timeout_schedule(session_id, "prompt")
        delete_timeout_schedule(session_id, "close")

    slack_response = update_processing_message(processing_message, lex_reply, slack_blocks)
    if not slack_response:
        slack_response = send_slack_message(channel, lex_reply, slack_blocks, thread_ts=session_thread_ts)
    if rovo_should_invoke and jira_ticket_key:
        store_rovo_slack_message_target(
            session_id,
            slack_response.get("ts"),
            lex_reply,
        )

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
        "action_id": action_id,
        "lex_intent": lex_intent,
        "lex_state": lex_state,
        "lex_slots": lex_slots,
        "response_source": response_source,
        "claude_fallback_attempted": claude_fallback_attempted,
        "next_action": next_action,
        "assistance_status": assistance_status,
        "support_options_status": support_options_status,
        "live_agent_status": live_agent_status,
        "live_agent_error_code": live_agent_error_code,
        "jira_status": jira_status,
        "jira_intent_name": jira_intent_name,
        "jira_request_id": jira_request_id,
        "jira_ticket_key": jira_ticket_key,
        "jira_ticket_url": jira_ticket_url,
        "jira_error": jira_error,
        "jira_error_code": jira_error_code,
        "rovo_status": rovo_status,
        "rovo_error": rovo_error,
        "rovo_error_code": rovo_error_code,
        "workflow_state": workflow_state,
        "mcp_status": mcp_status,
        "mcp_request_id": mcp_request_id,
        "mcp_error_count": len(mcp_errors or []),
        "image_status": image_status,
        "image_resolution_source": image_resolution_source,
        "image_match_issue_id": image_match_issue_id,
        "image_match_score": image_match_score,
        "image_match_expected_lex_intent": image_match_expected_lex_intent,
        "image_match_actual_lex_intent": image_match_actual_lex_intent,
        "image_match_fallback_reason": image_match_fallback_reason,
        "image_error_code": image_error_code,
        "timeout_status": "scheduled" if timeout_state else "inactive",
        "timeout_due_at": timeout_state["timeout_due_at"] if timeout_state else None,
        "reply_sent": True,
        "slack_ts": slack_response.get("ts")
    })


def lambda_handler(event, context):
    batch_item_failures = []

    for record in event["Records"]:
        token_ctx = None
        try:
            # Resolve the tenant before process_record runs, since its first
            # Slack call (fetch_conversation_metadata) needs the right token
            # already in context. Reset in `finally` so a tenant can never
            # bleed into the next record of the same batch.
            tenant = resolve_tenant(
                json.loads(record["body"]).get("slack_tenant")
            )

            if tenant and tenant.get("status", "active") != "active":
                # An expired or suspended plan is not a transient fault —
                # returning it to the queue would just retry until the
                # redrive policy gives up. Log it and acknowledge.
                log_json({
                    "level": "WARNING",
                    "message": "tenant_inactive_message_dropped",
                    "tenant_id": tenant.get("tenant_id"),
                    "status": tenant.get("status"),
                })
                continue

            token_ctx = _tenant_ctx.set(tenant)
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

        finally:
            # Always clear, including on the error path: a leaked context
            # would hand the next record in this batch the previous
            # tenant's bot token.
            if token_ctx is not None:
                _tenant_ctx.reset(token_ctx)

    return {
        "batchItemFailures": batch_item_failures
    }
