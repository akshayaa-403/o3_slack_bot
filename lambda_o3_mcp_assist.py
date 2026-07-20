import difflib
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
secretsmanager = boto3.client("secretsmanager", region_name=AWS_REGION)
bedrock_runtime = boto3.client("bedrock-runtime", region_name=AWS_REGION)

DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "o3_slack_sessions")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
MCP_OVERALL_TIMEOUT_MS = int(os.environ.get("MCP_OVERALL_TIMEOUT_MS", "8000"))
MCP_PROVIDER_TIMEOUT_MS = int(os.environ.get("MCP_PROVIDER_TIMEOUT_MS", "2500"))
MCP_MIN_CONFIDENCE_SCORE = float(os.environ.get("MCP_MIN_CONFIDENCE_SCORE", "0.82"))
MCP_REQUIRE_CITATIONS = os.environ.get("MCP_REQUIRE_CITATIONS", "true").lower() == "true"
MCP_MAX_RESULTS_PER_PROVIDER = int(os.environ.get("MCP_MAX_RESULTS_PER_PROVIDER", "5"))
MCP_SYNTHESIS_MODE = os.environ.get("MCP_SYNTHESIS_MODE", "deterministic").strip().lower()

# Search-stage limits are intentionally separate from full-document fetch limits.
MCP_SEARCH_CANDIDATE_LIMIT = int(os.environ.get("MCP_SEARCH_CANDIDATE_LIMIT", "12"))
MCP_FETCH_TOP_K = int(os.environ.get("MCP_FETCH_TOP_K", "3"))
MCP_EXACT_MATCH_FETCH_TOP_K = int(os.environ.get("MCP_EXACT_MATCH_FETCH_TOP_K", "1"))
MCP_EXACT_TITLE_SCORE = float(os.environ.get("MCP_EXACT_TITLE_SCORE", "0.96"))
MCP_EXACT_TITLE_MARGIN = float(os.environ.get("MCP_EXACT_TITLE_MARGIN", "0.15"))
MCP_TOOL_CACHE_TTL_SECONDS = int(os.environ.get("MCP_TOOL_CACHE_TTL_SECONDS", "900"))

# Bedrock is used only for grounded answer synthesis. Retrieval remains deterministic.
MCP_BEDROCK_MODEL_ID = os.environ.get("MCP_BEDROCK_MODEL_ID", "").strip()
MCP_LLM_CONTEXT_DOCS = int(os.environ.get("MCP_LLM_CONTEXT_DOCS", "3"))
MCP_LLM_MAX_CHARS_PER_DOC = int(os.environ.get("MCP_LLM_MAX_CHARS_PER_DOC", "6000"))
MCP_LLM_MAX_TOKENS = int(os.environ.get("MCP_LLM_MAX_TOKENS", "800"))

MCP_ATLASSIAN_ENABLED = os.environ.get("MCP_ATLASSIAN_ENABLED", "false").lower() == "true"
MCP_ATLASSIAN_SERVER_URL = os.environ.get("MCP_ATLASSIAN_SERVER_URL", "").strip()
MCP_ATLASSIAN_CLOUD_ID = os.environ.get("MCP_ATLASSIAN_CLOUD_ID", "").strip()
MCP_ATLASSIAN_ALLOWED_SPACES = os.environ.get("MCP_ATLASSIAN_ALLOWED_SPACES", "")
MCP_ATLASSIAN_ALLOWED_PROJECTS = os.environ.get("MCP_ATLASSIAN_ALLOWED_PROJECTS", "")
MCP_ATLASSIAN_ALLOWED_TOOLS = os.environ.get(
    "MCP_ATLASSIAN_ALLOWED_TOOLS",
    "searchConfluence,searchConfluenceUsingCql,searchAtlassian,"
    "getConfluenceContent,getConfluencePage,fetchAtlassian",
)
MCP_ATLASSIAN_AUTH_SECRET_ID = os.environ.get("MCP_ATLASSIAN_AUTH_SECRET_ID", "").strip()

MCP_SHAREPOINT_ENABLED = os.environ.get("MCP_SHAREPOINT_ENABLED", "false").lower() == "true"
MCP_SHAREPOINT_SERVER_URL = os.environ.get("MCP_SHAREPOINT_SERVER_URL", "").strip()
MCP_SHAREPOINT_ALLOWED_SITES = os.environ.get("MCP_SHAREPOINT_ALLOWED_SITES", "")
MCP_SHAREPOINT_ALLOWED_DRIVES = os.environ.get("MCP_SHAREPOINT_ALLOWED_DRIVES", "")
MCP_SHAREPOINT_ALLOWED_LISTS = os.environ.get("MCP_SHAREPOINT_ALLOWED_LISTS", "")
MCP_SHAREPOINT_ALLOWED_TOOLS = os.environ.get("MCP_SHAREPOINT_ALLOWED_TOOLS", "search,fetch")
MCP_SHAREPOINT_AUTH_SECRET_ID = os.environ.get("MCP_SHAREPOINT_AUTH_SECRET_ID", "").strip()
MCP_SHAREPOINT_TOKEN_SCOPE = os.environ.get(
    "MCP_SHAREPOINT_TOKEN_SCOPE",
    "https://agent365.svc.cloud.microsoft/.default",
).strip()
MS_GRAPH_TENANT_ID = os.environ.get("MS_GRAPH_TENANT_ID", "").strip()
MS_GRAPH_CLIENT_ID = os.environ.get("MS_GRAPH_CLIENT_ID", "").strip()
MS_GRAPH_CLIENT_SECRET_ID = os.environ.get("MS_GRAPH_CLIENT_SECRET_ID", "").strip()

MCP_SLACK_ENABLED = os.environ.get("MCP_SLACK_ENABLED", "false").lower() == "true"
MCP_SLACK_SERVER_URL = os.environ.get("MCP_SLACK_SERVER_URL", "").strip()
MCP_SLACK_ALLOWED_CHANNEL_IDS = os.environ.get("MCP_SLACK_ALLOWED_CHANNEL_IDS", "")
MCP_SLACK_ALLOWED_TOOLS = os.environ.get("MCP_SLACK_ALLOWED_TOOLS", "search,fetch")
MCP_SLACK_AUTH_SECRET_ID = os.environ.get("MCP_SLACK_AUTH_SECRET_ID", "").strip()

STATUS_ANSWER = "ANSWER"
STATUS_NO_ANSWER = "NO_ANSWER"
STATUS_AUTH_REQUIRED = "AUTH_REQUIRED"
STATUS_PARTIAL = "PARTIAL"
STATUS_ERROR = "ERROR"

sessions_table = dynamodb.Table(DYNAMODB_TABLE)

# Warm Lambda execution environments reuse this metadata cache.
_MCP_TOOL_CACHE = {}
_AUTH_TOKEN_CACHE = {}


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def log_json(data):
    print(json.dumps(data, default=str))


def text_or_empty(value):
    if value is None:
        return ""
    return str(value).strip()


def csv_set(value):
    return {item.strip() for item in str(value or "").split(",") if item.strip()}


def csv_list(value):
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def safe_error(error):
    value = str(error or "")
    value = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value, flags=re.I)
    value = re.sub(r"Basic\s+[A-Za-z0-9+/=-]+", "Basic [REDACTED]", value, flags=re.I)
    return value[:500]


def get_secret_token(secret_id):
    if not secret_id:
        return ""
    response = secretsmanager.get_secret_value(SecretId=secret_id)
    secret = response.get("SecretString") or ""
    try:
        parsed = json.loads(secret)
        for key in ("access_token", "token", "bearer_token", "api_token"):
            if parsed.get(key):
                return str(parsed[key]).strip()
    except ValueError:
        pass
    return secret.strip()


def get_secret_string(secret_id):
    if not secret_id:
        return ""
    response = secretsmanager.get_secret_value(SecretId=secret_id)
    return (response.get("SecretString") or "").strip()


def microsoft_client_secret():
    secret = get_secret_string(MS_GRAPH_CLIENT_SECRET_ID)
    if not secret:
        return ""
    try:
        parsed = json.loads(secret)
        for key in ("client_secret", "secret", "value"):
            if parsed.get(key):
                return str(parsed[key]).strip()
    except ValueError:
        pass
    return secret


def microsoft_sharepoint_mcp_token():
    if not (MS_GRAPH_TENANT_ID and MS_GRAPH_CLIENT_ID and MS_GRAPH_CLIENT_SECRET_ID and MCP_SHAREPOINT_TOKEN_SCOPE):
        return ""

    cache_key = ("sharepoint", MS_GRAPH_TENANT_ID, MS_GRAPH_CLIENT_ID, MCP_SHAREPOINT_TOKEN_SCOPE)
    cached = _AUTH_TOKEN_CACHE.get(cache_key)
    now = time.time()
    if cached and cached.get("expires_at", 0) > now + 60:
        return cached.get("access_token", "")

    client_secret = microsoft_client_secret()
    if not client_secret:
        return ""

    token_url = f"https://login.microsoftonline.com/{urllib.parse.quote(MS_GRAPH_TENANT_ID, safe='')}/oauth2/v2.0/token"
    form = urllib.parse.urlencode({
        "client_id": MS_GRAPH_CLIENT_ID,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
        "scope": MCP_SHAREPOINT_TOKEN_SCOPE,
    }).encode("utf-8")
    request = urllib.request.Request(
        token_url,
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))

    access_token = text_or_empty(payload.get("access_token"))
    if access_token:
        _AUTH_TOKEN_CACHE[cache_key] = {
            "access_token": access_token,
            "expires_at": now + int(payload.get("expires_in") or 3600),
        }
    return access_token


def provider_bearer_token(provider):
    if provider.name == "sharepoint":
        token = microsoft_sharepoint_mcp_token()
        if token:
            return token
    return get_secret_token(provider.auth_secret_id)


def slack_api(method, payload):
    if not SLACK_BOT_TOKEN:
        raise ValueError("Missing SLACK_BOT_TOKEN")
    request = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Slack API {method} failed: {result.get('error')}")
    return result


def slack_mrkdwn(text, limit=2900):
    value = text_or_empty(text)
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def action_button_value(action, session_id=None, session_root_ts=None):
    return json.dumps(
        {
            "action": action,
            "session_id": session_id,
            "session_root_ts": session_root_ts,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def mcp_followup_blocks(reply, session_id, session_root_ts, citations=None):
    text = text_or_empty(reply)
    citation_lines = []
    for index, citation in enumerate(citations or [], start=1):
        if not isinstance(citation, dict):
            continue
        title = text_or_empty(citation.get("title")) or text_or_empty(citation.get("source")) or f"Source {index}"
        url = text_or_empty(citation.get("url"))
        if url:
            citation_lines.append(f"{index}. <{url}|{title}>")
        else:
            citation_lines.append(f"{index}. {title}")
    if citation_lines:
        text = f"{text}\n\n*Sources:*\n" + "\n".join(citation_lines)

    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": slack_mrkdwn(text)},
        },
        {
            "type": "actions",
            "block_id": "ivy_mcp_followup_actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Resolved"},
                    "action_id": "ivy_close_and_summarize",
                    "value": action_button_value("close_and_summarize", session_id, session_root_ts),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "I need more help"},
                    "action_id": "o3_request_claude_assist",
                    "value": action_button_value("claude_assist", session_id, session_root_ts),
                },
            ],
        },
    ]


class ProviderConfig:
    def __init__(self, name, enabled, server_url, allowed_tools, auth_secret_id, policy):
        self.name = name
        self.enabled = enabled
        self.server_url = server_url.rstrip("/")
        self.allowed_tools = allowed_tools
        self.auth_secret_id = auth_secret_id
        self.policy = policy


def provider_configs():
    return [
        ProviderConfig(
            "atlassian",
            MCP_ATLASSIAN_ENABLED,
            MCP_ATLASSIAN_SERVER_URL,
            csv_list(MCP_ATLASSIAN_ALLOWED_TOOLS),
            MCP_ATLASSIAN_AUTH_SECRET_ID,
            {
                "spaces": csv_set(MCP_ATLASSIAN_ALLOWED_SPACES),
                "projects": csv_set(MCP_ATLASSIAN_ALLOWED_PROJECTS),
            },
        ),
        ProviderConfig(
            "sharepoint",
            MCP_SHAREPOINT_ENABLED,
            MCP_SHAREPOINT_SERVER_URL,
            csv_list(MCP_SHAREPOINT_ALLOWED_TOOLS),
            MCP_SHAREPOINT_AUTH_SECRET_ID,
            {
                "sites": csv_set(MCP_SHAREPOINT_ALLOWED_SITES),
                "drives": csv_set(MCP_SHAREPOINT_ALLOWED_DRIVES),
                "lists": csv_set(MCP_SHAREPOINT_ALLOWED_LISTS),
            },
        ),
        ProviderConfig(
            "slack",
            MCP_SLACK_ENABLED,
            MCP_SLACK_SERVER_URL,
            csv_list(MCP_SLACK_ALLOWED_TOOLS),
            MCP_SLACK_AUTH_SECRET_ID,
            {
                "channels": csv_set(MCP_SLACK_ALLOWED_CHANNEL_IDS),
            },
        ),
    ]


def enabled_providers():
    return [provider for provider in provider_configs() if provider.enabled]


def route_provider_names(question):
    """Fast deterministic routing; ambiguous queries still fan out safely."""
    value = text_or_empty(question).casefold()
    selected = set()

    if re.search(r"\b[A-Z][A-Z0-9]+-\d+\b", text_or_empty(question)) or any(
        token in value for token in ("jira", "ticket", "issue", "incident", "epic", "story")
    ):
        selected.add("atlassian")
    if any(token in value for token in ("confluence", "knowledge base", "kb article", "policy", "procedure", "guide")):
        selected.add("atlassian")
    if any(token in value for token in ("sharepoint", "onedrive", "document library", "microsoft 365")):
        selected.add("sharepoint")
    if any(token in value for token in ("slack", "channel", "thread", "message", "conversation")):
        selected.add("slack")
    return selected


def providers_for_request(request_context):
    providers = enabled_providers()
    explicit = set(request_context.get("target_sources") or [])
    routed = explicit or route_provider_names(request_context.get("question") or "")
    if not routed:
        return providers
    return [provider for provider in providers if provider.name in routed]


def validate_provider(provider):
    if not provider.server_url:
        return "missing_server_url"
    if not provider.allowed_tools:
        return "missing_allowed_tools"
    for tool in provider.allowed_tools:
        lowered = tool.lower()
        if any(marker in lowered for marker in ("create", "update", "delete", "write", "admin", "transition")):
            return "write_capable_tool_blocked"
    if provider.name == "slack" and not provider.policy.get("channels"):
        return "missing_slack_channel_allowlist"
    return None


def select_tool(provider, *markers):
    for tool in provider.allowed_tools:
        lowered = tool.lower()
        if all(marker in lowered for marker in markers):
            return tool
    return provider.allowed_tools[0] if provider.allowed_tools else ""


def json_safe_policy(policy):
    safe = {}
    for key, value in (policy or {}).items():
        if isinstance(value, set):
            safe[key] = sorted(value)
        else:
            safe[key] = value
    return safe


def mcp_tool_call(provider, tool_name, arguments):
    token = provider_bearer_token(provider)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    payload = {
        "jsonrpc": "2.0",
        "id": f"ivy-{int(time.time() * 1000)}",
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }
    request = urllib.request.Request(
        provider.server_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=MCP_PROVIDER_TIMEOUT_MS / 1000) as response:
        raw = response.read().decode("utf-8").strip()
    if not raw:
        return {}
    if raw.startswith("data:"):
        data_lines = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]
        raw = data_lines[-1] if data_lines else "{}"
    parsed = json.loads(raw)
    if parsed.get("error"):
        raise RuntimeError(parsed["error"])
    return parsed.get("result") or parsed


def mcp_post(provider, method, params=None, session_id=None):
    token = provider_bearer_token(provider)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
        "User-Agent": "Project-IVY-MCP/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    payload = {
        "jsonrpc": "2.0",
        "id": f"ivy-{int(time.time() * 1000)}",
        "method": method,
        "params": params or {},
    }
    request = urllib.request.Request(
        provider.server_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=MCP_PROVIDER_TIMEOUT_MS / 1000) as response:
        raw = response.read().decode("utf-8").strip()
        returned_session_id = response.headers.get("Mcp-Session-Id") or session_id

    parsed = parse_mcp_response(raw)
    if parsed.get("error"):
        raise RuntimeError(parsed["error"])
    return returned_session_id, parsed.get("result") or parsed



def mcp_notify(provider, method, params=None, session_id=None):
    """Send an MCP JSON-RPC notification (no request id and no response required)."""
    token = provider_bearer_token(provider)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
        "User-Agent": "Project-IVY-MCP/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
    }
    request = urllib.request.Request(
        provider.server_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=MCP_PROVIDER_TIMEOUT_MS / 1000):
        return None


def initialize_mcp(provider):
    """Initialize one MCP session and return its session id."""
    session_id, _ = mcp_post(
        provider,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "project-ivy", "version": "2.0.0"},
        },
    )
    # Some Streamable HTTP servers are sessionless, so a missing id is valid.
    try:
        mcp_notify(provider, "notifications/initialized", {}, session_id)
    except Exception as error:
        log_json({
            "level": "WARN",
            "message": "mcp_initialized_notification_failed",
            "provider": provider.name,
            "error": safe_error(error),
        })
    return session_id


def mcp_tool_catalog(provider, session_id):
    """Discover tools once per warm Lambda container and cache only metadata."""
    cache_key = (provider.name, provider.server_url, tuple(provider.allowed_tools))
    cached = _MCP_TOOL_CACHE.get(cache_key)
    now = time.time()
    if cached and cached["expires_at"] > now:
        return cached["tools"]

    _, result = mcp_post(provider, "tools/list", {}, session_id)
    payload = extract_mcp_text_json(result)
    tools = payload.get("tools") if isinstance(payload, dict) else None
    if not isinstance(tools, list):
        tools = []

    # Application allowlist is the final policy boundary.
    allowed = set(provider.allowed_tools or [])
    if allowed:
        tools = [tool for tool in tools if text_or_empty(tool.get("name")) in allowed]

    _MCP_TOOL_CACHE[cache_key] = {
        "expires_at": now + MCP_TOOL_CACHE_TTL_SECONDS,
        "tools": tools,
    }
    return tools


def tool_by_preference(tools, preferred_names):
    by_name = {
        text_or_empty(tool.get("name")): tool
        for tool in tools
        if isinstance(tool, dict) and text_or_empty(tool.get("name"))
    }
    for name in preferred_names:
        if name in by_name:
            return by_name[name]
    return None


def tool_properties(tool):
    schema = (tool or {}).get("inputSchema") or (tool or {}).get("input_schema") or {}
    properties = schema.get("properties") if isinstance(schema, dict) else {}
    return properties if isinstance(properties, dict) else {}


def parse_mcp_response(raw):
    if not raw:
        return {}
    if raw.startswith("event:") or raw.startswith("data:"):
        data_lines = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]
        raw = data_lines[-1] if data_lines else "{}"
    return json.loads(raw)


def extract_mcp_text_json(result):
    if not isinstance(result, dict):
        return {}
    content = result.get("content") or []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            text = item.get("text") or ""
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                return {"text": text}
    return result


def result_items(result):
    """Return the first likely list of business records from nested MCP output."""
    result = extract_mcp_text_json(result) if isinstance(result, dict) else result
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if not isinstance(result, dict):
        return []

    preferred_keys = (
        "results", "items", "documents", "issues", "messages",
        "pages", "values", "entities",
    )
    for key in preferred_keys:
        value = result.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    for key in ("data", "result", "response", "payload"):
        value = result.get(key)
        nested = result_items(value)
        if nested:
            return nested
    return []


def first_value(data, *keys):
    if not isinstance(data, dict):
        return ""
    for key in keys:
        value = data.get(key)
        if value is not None and not isinstance(value, (dict, list)) and str(value).strip():
            return str(value).strip()
    return ""


def deep_first_value(data, *keys, max_depth=4):
    """Find a scalar field in nested provider responses without binding to one schema."""
    if max_depth < 0 or not isinstance(data, dict):
        return ""
    direct = first_value(data, *keys)
    if direct:
        return direct
    for value in data.values():
        if isinstance(value, dict):
            found = deep_first_value(value, *keys, max_depth=max_depth - 1)
            if found:
                return found
    return ""


SEARCH_STOP_WORDS = {
    "a", "an", "and", "the", "of", "to", "in", "on", "for", "is", "are",
}


def search_tokens(text):
    value = unicodedata.normalize("NFKC", text_or_empty(text))
    value = re.sub(r"^\s*\d+\s*[\-_.:]+\s*", "", value)
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    value = re.sub(r"[_/\\\-]+", " ", value)
    value = value.casefold()
    return [
        token for token in re.findall(r"[a-z0-9]+", value)
        if token not in SEARCH_STOP_WORDS
    ]


def normalize_search_text(text):
    return " ".join(search_tokens(text))


def tokenize(text):
    return {token for token in search_tokens(text) if len(token) > 2}


def cql_escape(value):
    return text_or_empty(value).replace("\\", "\\\\").replace('"', '\\"')


def confluence_cql(question, space_key, exact_phrase=False):
    phrase = cql_escape(text_or_empty(question) if exact_phrase else normalize_search_text(question))
    if exact_phrase:
        search_value = f'\\"{phrase}\\"'
    else:
        search_value = phrase
    return (
        f'space = "{cql_escape(space_key)}" AND type = page AND '
        f'(title ~ "{search_value}" OR text ~ "{search_value}")'
    )


def normalize_atlassian_candidate(provider, item, space_key, provider_rank=0):
    if not isinstance(item, dict):
        return {}
    page_id = deep_first_value(item, "pageId", "contentId", "id")
    title = deep_first_value(item, "title", "name", "summary")
    snippet = deep_first_value(item, "excerpt", "snippet", "text", "description")
    url = deep_first_value(item, "webUrl", "url", "permalink", "webui", "self")
    candidate_space = deep_first_value(item, "spaceKey", "space") or space_key
    return {
        "provider": provider.name,
        "id": page_id,
        "title": title,
        "url": url,
        "snippet": snippet,
        "space": candidate_space,
        "project": "",
        "updated_at": deep_first_value(item, "updatedAt", "updated_at", "lastModifiedDateTime"),
        "provider_rank": provider_rank,
        "raw": item,
    }


def score_search_candidate(question, candidate):
    query_normalized = normalize_search_text(question)
    title_normalized = normalize_search_text(candidate.get("title"))
    snippet_normalized = normalize_search_text(candidate.get("snippet"))
    query_tokens = set(search_tokens(question))
    title_tokens = set(search_tokens(candidate.get("title")))
    snippet_tokens = set(search_tokens(candidate.get("snippet")))

    if query_normalized and query_normalized == title_normalized:
        return 0.99

    title_coverage = len(query_tokens & title_tokens) / max(1, len(query_tokens))
    title_precision = len(query_tokens & title_tokens) / max(1, len(title_tokens))
    snippet_coverage = len(query_tokens & snippet_tokens) / max(1, len(query_tokens))
    sequence_similarity = difflib.SequenceMatcher(
        None, query_normalized, title_normalized
    ).ratio() if title_normalized else 0.0
    phrase_bonus = 0.10 if query_normalized and query_normalized in title_normalized else 0.0
    provider_order_bonus = max(0.0, 0.10 - (candidate.get("provider_rank", 0) * 0.01))

    return min(
        0.98,
        (0.48 * title_coverage)
        + (0.17 * title_precision)
        + (0.15 * sequence_similarity)
        + (0.10 * snippet_coverage)
        + phrase_bonus
        + provider_order_bonus,
    )


def rank_search_candidates(question, candidates):
    ranked = []
    for candidate in candidates:
        if not candidate.get("id") and not candidate.get("url"):
            continue
        score = score_search_candidate(question, candidate)
        ranked.append({**candidate, "retrieval_score": score})
    return sorted(ranked, key=lambda item: item.get("retrieval_score", 0), reverse=True)


def deduplicate_candidates(candidates):
    output = []
    seen = set()
    for candidate in candidates:
        key = (
            candidate.get("id")
            or candidate.get("url")
            or normalize_search_text(candidate.get("title"))
        )
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(candidate)
    return output


def fetch_limit_for_ranked_candidates(ranked):
    if not ranked:
        return 0
    top_score = ranked[0].get("retrieval_score", 0)
    second_score = ranked[1].get("retrieval_score", 0) if len(ranked) > 1 else 0
    if top_score >= MCP_EXACT_TITLE_SCORE and (top_score - second_score) >= MCP_EXACT_TITLE_MARGIN:
        return min(MCP_EXACT_MATCH_FETCH_TOP_K, len(ranked))
    return min(MCP_FETCH_TOP_K, len(ranked))


def normalize_search_result(provider, item):
    if not isinstance(item, dict):
        return {}
    return {
        "provider": provider.name,
        "id": first_value(item, "id", "key", "pageId", "issueKey", "ts", "url"),
        "title": first_value(item, "title", "name", "summary", "key", "text"),
        "url": first_value(item, "url", "webUrl", "permalink", "self"),
        "snippet": first_value(item, "snippet", "excerpt", "text", "summary", "description"),
        "space": first_value(item, "space", "spaceKey"),
        "project": first_value(item, "project", "projectKey"),
        "site": first_value(item, "siteId", "site"),
        "drive": first_value(item, "driveId", "drive"),
        "list": first_value(item, "listId", "list"),
        "channel": first_value(item, "channel", "channel_id", "channelId"),
        "updated_at": first_value(item, "updated_at", "updatedAt", "lastModifiedDateTime"),
        "raw": item,
    }


def allowed_result(provider, result):
    if provider.name == "atlassian":
        spaces = provider.policy.get("spaces") or set()
        projects = provider.policy.get("projects") or set()
        space = result.get("space")
        project = result.get("project")
        if spaces and space and space not in spaces:
            return False
        if projects and project and project not in projects:
            return False
    if provider.name == "sharepoint":
        sites = provider.policy.get("sites") or set()
        drives = provider.policy.get("drives") or set()
        lists = provider.policy.get("lists") or set()
        if sites and result.get("site") and result["site"] not in sites:
            return False
        if drives and result.get("drive") and result["drive"] not in drives:
            return False
        if lists and result.get("list") and result["list"] not in lists:
            return False
    if provider.name == "slack":
        channels = provider.policy.get("channels") or set()
        if result.get("channel") not in channels:
            return False
    return True


def normalize_evidence(provider, result, fetched):
    fetched_map = fetched if isinstance(fetched, dict) else {}
    text = first_value(
        fetched_map,
        "text",
        "content",
        "body",
        "description",
        "summary",
        "answer",
    ) or result.get("snippet")
    url = first_value(fetched_map, "url", "webUrl", "permalink", "self") or result.get("url")
    title = first_value(fetched_map, "title", "name", "summary", "key") or result.get("title")
    return {
        "provider": provider.name,
        "title": title,
        "url": url,
        "text": text,
        "updated_at": first_value(fetched_map, "updated_at", "updatedAt", "lastModifiedDateTime") or result.get("updated_at"),
        "locator": first_value(fetched_map, "locator", "heading", "section") or title,
        "authority": evidence_authority(provider.name, result, fetched_map),
        "raw_result_id": result.get("id"),
    }


def build_search_arguments(tool, question, space_key, exact_phrase):
    properties = tool_properties(tool)
    tool_name = text_or_empty((tool or {}).get("name"))
    cql = confluence_cql(question, space_key, exact_phrase=exact_phrase)
    natural_query = f'Confluence pages in space {space_key}: {question}'
    arguments = {}

    # Use only fields advertised by tools/list. If a server omits a schema,
    # fall back to the conventional arguments for the selected tool.
    if "cloudId" in properties:
        arguments["cloudId"] = MCP_ATLASSIAN_CLOUD_ID
    if "cql" in properties:
        arguments["cql"] = cql
    elif "query" in properties:
        arguments["query"] = natural_query if tool_name == "searchAtlassian" else cql
    elif not properties:
        arguments.update({"cloudId": MCP_ATLASSIAN_CLOUD_ID, "cql": cql})

    for key in ("limit", "maxResults", "max_results"):
        if key in properties:
            arguments[key] = MCP_SEARCH_CANDIDATE_LIMIT
            break
    if "spaceKey" in properties:
        arguments["spaceKey"] = space_key
    if "contentType" in properties:
        arguments["contentType"] = "page"
    return arguments


def build_fetch_arguments(tool, candidate):
    properties = tool_properties(tool)
    arguments = {}
    candidate_id = candidate.get("id")
    candidate_url = candidate.get("url")
    raw = candidate.get("raw")

    if "cloudId" in properties:
        arguments["cloudId"] = MCP_ATLASSIAN_CLOUD_ID
    for key in ("pageId", "contentId", "id"):
        if key in properties and candidate_id:
            arguments[key] = candidate_id
            break
    if "url" in properties and candidate_url:
        arguments["url"] = candidate_url
    if "contentType" in properties:
        arguments["contentType"] = "page"
    if "contentFormat" in properties:
        arguments["contentFormat"] = "markdown"
    if "format" in properties:
        arguments["format"] = "markdown"
    if "result" in properties:
        arguments["result"] = raw

    if not properties:
        arguments = {
            "cloudId": MCP_ATLASSIAN_CLOUD_ID,
            "pageId": candidate_id,
            "contentType": "page",
            "contentFormat": "markdown",
        }
    return {key: value for key, value in arguments.items() if value not in (None, "")}


def tool_named(tools, *names):
    return tool_by_preference(tools, names)


def sharepoint_search_arguments(tool, question, provider):
    properties = tool_properties(tool)
    arguments = {}
    for key in ("searchQuery", "query", "searchText", "search"):
        if key in properties:
            arguments[key] = question
            break
    if not arguments:
        arguments["searchQuery"] = question

    sites = sorted(provider.policy.get("sites") or [])
    if len(sites) == 1 and "siteId" in properties:
        arguments["siteId"] = sites[0]
    for key in ("limit", "top", "maxResults", "max_results"):
        if key in properties:
            arguments[key] = min(MCP_SEARCH_CANDIDATE_LIMIT, 20)
            break
    return arguments


def normalize_sharepoint_candidate(provider, item, provider_rank=0):
    if not isinstance(item, dict):
        return {}
    raw = item
    file_id = deep_first_value(raw, "fileId", "fileOrFolderId", "driveItemId", "itemId", "id", max_depth=6)
    drive_id = deep_first_value(raw, "documentLibraryId", "driveId", "parentDriveId", max_depth=6)
    site_id = deep_first_value(raw, "siteId", "site", "sharepointSiteId", max_depth=6)
    list_id = deep_first_value(raw, "listId", "sharepointListId", max_depth=6)
    title = deep_first_value(raw, "name", "title", "displayName", "summary", max_depth=6)
    url = deep_first_value(raw, "webUrl", "url", "fileOrFolderUrl", "shareUrl", "permalink", max_depth=6)
    snippet = deep_first_value(raw, "snippet", "excerpt", "description", "text", "summary", max_depth=6)
    return {
        "provider": provider.name,
        "id": file_id,
        "title": title,
        "url": url,
        "snippet": snippet,
        "site": site_id,
        "drive": drive_id,
        "list": list_id,
        "updated_at": deep_first_value(raw, "lastModifiedDateTime", "updatedAt", "updated_at", max_depth=6),
        "provider_rank": provider_rank,
        "raw": raw,
    }


def sharepoint_fetch_arguments(tool, candidate):
    properties = tool_properties(tool)
    tool_name = text_or_empty((tool or {}).get("name"))
    file_id = candidate.get("id")
    drive_id = candidate.get("drive")
    url = candidate.get("url")
    arguments = {}

    if tool_name == "getFileOrFolderMetadataByUrl" or "fileOrFolderUrl" in properties:
        if url:
            arguments["fileOrFolderUrl"] = url
        return arguments

    for key in ("fileId", "fileOrFolderId", "driveItemId", "itemId", "id"):
        if key in properties and file_id:
            arguments[key] = file_id
            break
    if not any(key in arguments for key in ("fileId", "fileOrFolderId", "driveItemId", "itemId", "id")) and file_id:
        arguments["fileId" if tool_name == "readSmallTextFile" else "fileOrFolderId"] = file_id

    for key in ("documentLibraryId", "driveId"):
        if key in properties and drive_id:
            arguments[key] = drive_id
            break
    if not any(key in arguments for key in ("documentLibraryId", "driveId")) and drive_id:
        arguments["documentLibraryId"] = drive_id
    return arguments


def sharepoint_text_from_payload(payload):
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    text = deep_first_value(
        payload,
        "contentText",
        "text",
        "content",
        "body",
        "description",
        "summary",
        max_depth=8,
    )
    if text:
        return text
    if payload:
        return json.dumps(payload, default=str)
    return ""


def sharepoint_evidence(provider, request_context):
    question = text_or_empty(request_context.get("question"))
    if not question:
        return [], {"source": provider.name, "category": "missing_question"}

    session_id = initialize_mcp(provider)
    tools = mcp_tool_catalog(provider, session_id)
    search_tool = tool_named(tools, "findFileOrFolder")
    read_tool = tool_named(tools, "readSmallTextFile")
    metadata_tool = tool_named(tools, "getFileOrFolderMetadata")
    metadata_by_url_tool = tool_named(tools, "getFileOrFolderMetadataByUrl")
    fetch_tool = read_tool or metadata_tool or metadata_by_url_tool
    if not search_tool:
        return [], {"source": provider.name, "category": "missing_sharepoint_search_tool"}

    search_started = time.time()
    search_payload = call_session_tool(
        provider,
        session_id,
        search_tool,
        sharepoint_search_arguments(search_tool, question, provider),
    )
    candidates = []
    for index, item in enumerate(result_items(search_payload)[:MCP_SEARCH_CANDIDATE_LIMIT]):
        candidate = normalize_sharepoint_candidate(provider, item, index)
        if candidate and allowed_result(provider, candidate):
            candidates.append(candidate)

    ranked_candidates = rank_search_candidates(question, deduplicate_candidates(candidates))
    selected = ranked_candidates[:fetch_limit_for_ranked_candidates(ranked_candidates)]

    evidence = []
    fetch_started = time.time()
    for candidate in selected:
        fetched = {}
        if fetch_tool:
            fetch_args = sharepoint_fetch_arguments(fetch_tool, candidate)
            if fetch_args:
                fetched = call_session_tool(provider, session_id, fetch_tool, fetch_args)
        fetched_map = fetched if isinstance(fetched, dict) else {}
        text = sharepoint_text_from_payload(fetched) or candidate.get("snippet")
        url = (
            deep_first_value(fetched_map, "webUrl", "url", "fileOrFolderUrl", "shareUrl", "permalink", max_depth=6)
            or candidate.get("url")
        )
        title = (
            deep_first_value(fetched_map, "name", "title", "displayName", "summary", max_depth=6)
            or candidate.get("title")
        )
        evidence.append({
            "provider": provider.name,
            "title": title,
            "url": url,
            "text": text,
            "updated_at": (
                deep_first_value(fetched_map, "lastModifiedDateTime", "updatedAt", "updated_at", max_depth=6)
                or candidate.get("updated_at")
            ),
            "locator": title,
            "authority": "official_support_document",
            "raw_result_id": candidate.get("id"),
            "retrieval_score": candidate.get("retrieval_score", 0),
        })

    log_json({
        "level": "INFO",
        "message": "sharepoint_search_pipeline",
        "query": question,
        "candidate_count": len(ranked_candidates),
        "fetched_count": len(evidence),
        "search_tool": search_tool.get("name") if search_tool else "",
        "fetch_tool": fetch_tool.get("name") if fetch_tool else "",
        "top_candidate_title": ranked_candidates[0].get("title") if ranked_candidates else "",
        "top_candidate_score": round(ranked_candidates[0].get("retrieval_score", 0), 3) if ranked_candidates else 0,
        "search_ms": int((fetch_started - search_started) * 1000),
        "fetch_ms": int((time.time() - fetch_started) * 1000),
    })

    if not ranked_candidates:
        return [], {"source": provider.name, "category": "search_empty"}
    if not evidence:
        return [], {"source": provider.name, "category": "content_fetch_failed"}
    return evidence, None


def call_session_tool(provider, session_id, tool, arguments):
    _, result = mcp_post(
        provider,
        "tools/call",
        {"name": tool["name"], "arguments": arguments},
        session_id,
    )
    if isinstance(result, dict) and result.get("isError"):
        payload = extract_mcp_text_json(result)
        message = payload.get("text") if isinstance(payload, dict) else ""
        raise RuntimeError(message or f"MCP tool {tool.get('name')} returned isError")
    return extract_mcp_text_json(result)


def legacy_atlassian_confluence_evidence(provider, request_context, session_id, tools):
    spaces_tool = tool_by_preference(tools, ("getConfluenceSpaces",))
    pages_tool = tool_by_preference(tools, ("getPagesInConfluenceSpace",))
    fetch_tool = tool_by_preference(tools, ("getConfluencePage", "getConfluenceContent"))
    if not spaces_tool or not pages_tool or not fetch_tool:
        return [], {"source": provider.name, "category": "missing_confluence_search_tool"}

    evidence = []
    question = request_context.get("question") or ""
    query_tokens = tokenize(question)
    started = time.time()
    for space_key in sorted(provider.policy.get("spaces") or []):
        spaces_payload = call_session_tool(
            provider,
            session_id,
            spaces_tool,
            {
                "cloudId": MCP_ATLASSIAN_CLOUD_ID,
                "keys": [space_key],
                "limit": 10,
            },
        )
        for space in result_items(spaces_payload):
            space_id = text_or_empty(space.get("id"))
            if not space_id:
                continue
            pages_payload = call_session_tool(
                provider,
                session_id,
                pages_tool,
                {
                    "cloudId": MCP_ATLASSIAN_CLOUD_ID,
                    "spaceId": space_id,
                    "limit": MCP_MAX_RESULTS_PER_PROVIDER,
                    "status": "current",
                    "contentFormat": "markdown",
                },
            )
            base_url = text_or_empty((pages_payload.get("_links") or {}).get("base"))
            for page in result_items(pages_payload):
                title = text_or_empty(page.get("title"))
                if query_tokens and not (query_tokens & tokenize(title)):
                    continue
                page_id = text_or_empty(page.get("id"))
                if not page_id:
                    continue
                page_payload = call_session_tool(
                    provider,
                    session_id,
                    fetch_tool,
                    {
                        "cloudId": MCP_ATLASSIAN_CLOUD_ID,
                        "pageId": page_id,
                        "contentType": "page",
                        "contentFormat": "markdown",
                    },
                )
                body = deep_first_value(page_payload, "body", "text", "content", "markdown", max_depth=6)
                webui = (
                    deep_first_value(page_payload, "url", "webUrl", "permalink", "webui", "self", max_depth=6)
                    or text_or_empty((page.get("_links") or {}).get("webui"))
                )
                if webui.startswith("http"):
                    url = webui
                elif base_url and webui:
                    url = f"{base_url}{webui}"
                elif base_url:
                    url = f"{base_url}/spaces/{urllib.parse.quote(space_key)}/pages/{urllib.parse.quote(page_id)}"
                else:
                    url = ""
                evidence.append({
                    "provider": provider.name,
                    "title": title or deep_first_value(page_payload, "title", "name", "summary"),
                    "url": url,
                    "text": body or json.dumps(page_payload, default=str),
                    "updated_at": deep_first_value(page_payload, "updatedAt", "updated_at", "version"),
                    "locator": title,
                    "authority": "official_support_document",
                    "raw_result_id": page_id,
                })

    log_json({
        "level": "INFO",
        "message": "atlassian_legacy_space_pipeline",
        "query": question,
        "evidence_count": len(evidence),
        "latency_ms": int((time.time() - started) * 1000),
    })
    return evidence, None


def atlassian_confluence_evidence(provider, request_context):
    """Search metadata first, rank locally, then fetch only the best pages."""
    if not MCP_ATLASSIAN_CLOUD_ID:
        return [], {"source": provider.name, "category": "missing_cloud_id"}

    question = text_or_empty(request_context.get("question"))
    if not question:
        return [], {"source": provider.name, "category": "missing_question"}

    session_id = initialize_mcp(provider)
    tools = mcp_tool_catalog(provider, session_id)
    search_tool = tool_by_preference(
        tools,
        ("searchConfluence", "searchConfluenceUsingCql", "searchAtlassian"),
    )
    fetch_tool = tool_by_preference(
        tools,
        ("getConfluenceContent", "getConfluencePage", "fetchAtlassian"),
    )
    if not search_tool:
        return legacy_atlassian_confluence_evidence(provider, request_context, session_id, tools)
    if not fetch_tool:
        return [], {"source": provider.name, "category": "missing_confluence_fetch_tool"}

    search_supports_cql = "cql" in tool_properties(search_tool) or "confluence" in search_tool["name"].lower()
    candidates = []
    search_started = time.time()

    for space_key in sorted(provider.policy.get("spaces") or []):
        # Exact phrase first. A broad fallback catches imported prefixes,
        # paraphrases, and body-only questions such as battery disposal.
        passes = (True, False) if search_supports_cql else (False,)
        for exact_phrase in passes:
            arguments = build_search_arguments(search_tool, question, space_key, exact_phrase)
            search_payload = call_session_tool(provider, session_id, search_tool, arguments)
            items = result_items(search_payload)
            for index, item in enumerate(items[:MCP_SEARCH_CANDIDATE_LIMIT]):
                candidate = normalize_atlassian_candidate(provider, item, space_key, index)
                if candidate and allowed_result(provider, candidate):
                    candidates.append(candidate)

            current_ranked = rank_search_candidates(question, deduplicate_candidates(candidates))
            if current_ranked and current_ranked[0].get("retrieval_score", 0) >= MCP_EXACT_TITLE_SCORE:
                break

    ranked_candidates = rank_search_candidates(question, deduplicate_candidates(candidates))
    fetch_limit = fetch_limit_for_ranked_candidates(ranked_candidates)
    selected = ranked_candidates[:fetch_limit]

    evidence = []
    fetch_started = time.time()
    for candidate in selected:
        page_payload = call_session_tool(
            provider,
            session_id,
            fetch_tool,
            build_fetch_arguments(fetch_tool, candidate),
        )
        body = deep_first_value(page_payload, "body", "text", "content", "markdown", max_depth=6)
        title = deep_first_value(page_payload, "title", "name", "summary") or candidate.get("title")
        url = deep_first_value(page_payload, "webUrl", "url", "permalink", "webui", "self", max_depth=6) or candidate.get("url")
        if url and not url.startswith("http"):
            url = ""
        evidence.append({
            "provider": provider.name,
            "title": title,
            "url": url,
            "text": body or candidate.get("snippet") or json.dumps(page_payload, default=str),
            "updated_at": deep_first_value(page_payload, "updatedAt", "updated_at", "version"),
            "locator": title,
            "authority": "official_support_document",
            "raw_result_id": candidate.get("id"),
            "retrieval_score": candidate.get("retrieval_score", 0),
        })

    log_json({
        "level": "INFO",
        "message": "atlassian_search_pipeline",
        "query": question,
        "normalized_query": normalize_search_text(question),
        "candidate_count": len(ranked_candidates),
        "fetched_count": len(evidence),
        "top_candidate_id": ranked_candidates[0].get("id") if ranked_candidates else "",
        "top_candidate_title": ranked_candidates[0].get("title") if ranked_candidates else "",
        "top_candidate_score": round(ranked_candidates[0].get("retrieval_score", 0), 3) if ranked_candidates else 0,
        "search_ms": int((fetch_started - search_started) * 1000),
        "fetch_ms": int((time.time() - fetch_started) * 1000),
    })

    if not ranked_candidates:
        return [], {"source": provider.name, "category": "search_empty"}
    if not evidence:
        return [], {"source": provider.name, "category": "content_fetch_failed"}
    return evidence, None


def evidence_authority(provider_name, result, fetched):
    if provider_name == "atlassian":
        if result.get("space"):
            return "official_support_document"
        return "resolved_jira_record" if "resolved" in json.dumps(fetched).lower() else "jira_record"
    if provider_name == "sharepoint":
        return "official_support_document"
    if provider_name == "slack":
        return "confirmed_resolved_slack_thread" if "resolved" in json.dumps(fetched).lower() else "slack_thread"
    return "retrieved_document"


def search_provider(provider, request_context):
    validation_error = validate_provider(provider)
    if validation_error:
        return [], {"source": provider.name, "category": validation_error}

    if provider.name == "atlassian":
        try:
            return atlassian_confluence_evidence(provider, request_context)
        except urllib.error.HTTPError as error:
            category = "authentication_failed" if error.code in {401, 403} else "rate_limited" if error.code == 429 else "unavailable"
            return [], {"source": provider.name, "category": category, "status": error.code, "message": safe_error(error)}
        except urllib.error.URLError as error:
            return [], {"source": provider.name, "category": "timeout", "message": safe_error(error)}
        except Exception as error:
            return [], {"source": provider.name, "category": "malformed_response", "message": safe_error(error)}

    if provider.name == "sharepoint":
        try:
            return sharepoint_evidence(provider, request_context)
        except urllib.error.HTTPError as error:
            category = "authentication_failed" if error.code in {401, 403} else "rate_limited" if error.code == 429 else "unavailable"
            return [], {"source": provider.name, "category": category, "status": error.code, "message": safe_error(error)}
        except urllib.error.URLError as error:
            return [], {"source": provider.name, "category": "timeout", "message": safe_error(error)}
        except Exception as error:
            return [], {"source": provider.name, "category": "malformed_response", "message": safe_error(error)}

    question = request_context.get("question") or ""
    search_tool = select_tool(provider, "search")
    fetch_tool = select_tool(provider, "fetch")
    search_args = {
        "query": question,
        "max_results": MCP_MAX_RESULTS_PER_PROVIDER,
        "policy": json_safe_policy(provider.policy),
    }
    if provider.name == "slack":
        search_args["channel_ids"] = sorted(provider.policy.get("channels") or [])

    try:
        search_result = mcp_tool_call(provider, search_tool, search_args)
        normalized_results = [
            normalize_search_result(provider, item)
            for item in result_items(search_result)
        ]
        allowed_results = [
            item for item in normalized_results
            if item and allowed_result(provider, item)
        ][:MCP_MAX_RESULTS_PER_PROVIDER]

        evidence = []
        for result in allowed_results:
            fetch_args = {
                "id": result.get("id"),
                "url": result.get("url"),
                "result": result.get("raw"),
            }
            fetched = mcp_tool_call(provider, fetch_tool, fetch_args) if fetch_tool else result.get("raw")
            evidence_item = normalize_evidence(provider, result, fetched)
            if evidence_item.get("text") and valid_url(evidence_item.get("url")):
                evidence.append(evidence_item)

        return evidence, None

    except urllib.error.HTTPError as error:
        category = "authentication_failed" if error.code in {401, 403} else "rate_limited" if error.code == 429 else "unavailable"
        return [], {"source": provider.name, "category": category, "status": error.code, "message": safe_error(error)}
    except urllib.error.URLError as error:
        return [], {"source": provider.name, "category": "timeout", "message": safe_error(error)}
    except Exception as error:
        return [], {"source": provider.name, "category": "malformed_response", "message": safe_error(error)}


def valid_url(url):
    parsed = urllib.parse.urlparse(text_or_empty(url))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def score_evidence(question, evidence):
    question_tokens = tokenize(question)
    title_tokens = tokenize(evidence.get("title", ""))
    body_tokens = tokenize(evidence.get("text", ""))
    title_overlap = len(question_tokens & title_tokens) / max(1, len(question_tokens))
    body_overlap = len(question_tokens & body_tokens) / max(1, len(question_tokens))
    retrieval_score = float(evidence.get("retrieval_score") or 0)
    authority_bonus = {
        "official_support_document": 0.10,
        "resolved_jira_record": 0.08,
        "confirmed_resolved_slack_thread": 0.06,
        "jira_record": 0.03,
        "slack_thread": -0.08,
    }.get(evidence.get("authority"), 0.02)
    score = max(
        retrieval_score,
        (0.25 * title_overlap) + (0.65 * body_overlap) + authority_bonus,
    )
    if evidence.get("authority") == "official_support_document" and title_overlap > 0 and body_overlap >= 0.50:
        score = max(score, 0.90)
    return min(0.99, score)


def ranked_evidence(question, evidence_items):
    ranked = []
    for evidence in evidence_items:
        score = score_evidence(question, evidence)
        ranked.append({**evidence, "score": score})
    return sorted(ranked, key=lambda item: item.get("score", 0), reverse=True)


def excerpt(text, limit=1200):
    clean = re.sub(r"\s+", " ", text_or_empty(text))
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def evidence_citation(evidence):
    return {
        "source": evidence.get("provider"),
        "title": evidence.get("title"),
        "url": evidence.get("url"),
        "locator": evidence.get("locator"),
        "updated_at": evidence.get("updated_at"),
        "authority": evidence.get("authority"),
    }


def deterministic_answer_candidate(evidence_items):
    best = evidence_items[0]
    return (
        f"I found this in {best.get('title') or best.get('provider')}:\n\n"
        f"{excerpt(best.get('text'))}"
    ), [0]


def bedrock_answer_candidate(question, evidence_items):
    if not MCP_BEDROCK_MODEL_ID:
        raise ValueError("MCP_BEDROCK_MODEL_ID is required when MCP_SYNTHESIS_MODE=bedrock")

    selected = evidence_items[:MCP_LLM_CONTEXT_DOCS]
    source_blocks = []
    for index, evidence in enumerate(selected, start=1):
        source_blocks.append(
            f"[SOURCE {index}]\n"
            f"Title: {evidence.get('title') or 'Untitled'}\n"
            f"URL: {evidence.get('url') or 'Unavailable'}\n"
            f"Content:\n{excerpt(evidence.get('text'), MCP_LLM_MAX_CHARS_PER_DOC)}"
        )

    response = bedrock_runtime.converse(
        modelId=MCP_BEDROCK_MODEL_ID,
        system=[{
            "text": (
                "You are a grounded company knowledge assistant. Answer only from the supplied "
                "SOURCE blocks. Do not use outside knowledge. Cite every factual paragraph with "
                "one or more source markers such as [1] or [1][2]. If the sources do not contain "
                "enough information to answer, output exactly INSUFFICIENT_EVIDENCE."
            )
        }],
        messages=[{
            "role": "user",
            "content": [{
                "text": f"Question:\n{question}\n\n" + "\n\n".join(source_blocks)
            }],
        }],
        inferenceConfig={
            "maxTokens": MCP_LLM_MAX_TOKENS,
            "temperature": 0.0,
            "topP": 0.9,
        },
    )
    content = (((response.get("output") or {}).get("message") or {}).get("content") or [])
    answer = "\n".join(
        text_or_empty(block.get("text"))
        for block in content
        if isinstance(block, dict) and block.get("text")
    ).strip()
    if not answer or answer == "INSUFFICIENT_EVIDENCE":
        return None, []

    referenced = []
    for marker in re.findall(r"\[(\d+)\]", answer):
        index = int(marker) - 1
        if 0 <= index < len(selected) and index not in referenced:
            referenced.append(index)
    if MCP_REQUIRE_CITATIONS and not referenced:
        return None, []
    return answer, referenced or [0]


def build_answer_candidate(question, evidence_items):
    if not evidence_items:
        return None
    best = evidence_items[0]
    score = best.get("score", 0)
    if score < MCP_MIN_CONFIDENCE_SCORE:
        return None
    if best.get("authority") == "slack_thread" and score < 0.92:
        return None

    usable = [item for item in evidence_items[:MCP_LLM_CONTEXT_DOCS] if item.get("text")]
    if MCP_REQUIRE_CITATIONS:
        usable = [item for item in usable if valid_url(item.get("url"))]
    if not usable:
        return None

    if MCP_SYNTHESIS_MODE == "bedrock":
        answer, referenced = bedrock_answer_candidate(question, usable)
        if not answer:
            return None
    else:
        answer, referenced = deterministic_answer_candidate(usable)

    cited = [usable[index] for index in referenced if 0 <= index < len(usable)]
    sources_used = list(dict.fromkeys(item.get("provider") for item in cited if item.get("provider")))
    return {
        "answer": answer,
        "confidence": {
            "score": round(score, 2),
            "band": "high",
            "reasons": confidence_reasons(best),
        },
        "citations": [evidence_citation(item) for item in cited],
        "sources_used": sources_used,
    }


def confidence_reasons(evidence):
    reasons = ["fetched_passage_directly_supports_answer"]
    authority = evidence.get("authority")
    if authority:
        reasons.append(authority)
    if valid_url(evidence.get("url")):
        reasons.append("valid_citation_available")
    return reasons


def retrieve_evidence(request_context):
    providers = providers_for_request(request_context)
    if not providers:
        return [], [{"source": "mcp", "category": "no_enabled_providers"}]

    evidence_items = []
    source_errors = []
    max_workers = min(len(providers), 4)
    deadline = time.time() + (MCP_OVERALL_TIMEOUT_MS / 1000)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(search_provider, provider, request_context): provider
            for provider in providers
        }
        processed_futures = set()
        try:
            completed = as_completed(futures, timeout=MCP_OVERALL_TIMEOUT_MS / 1000)
            for future in completed:
                processed_futures.add(future)
                provider = futures[future]
                if time.time() > deadline:
                    source_errors.append({"source": provider.name, "category": "timeout"})
                    continue
                try:
                    evidence, error = future.result(timeout=max(0.1, deadline - time.time()))
                    evidence_items.extend(evidence)
                    if error:
                        source_errors.append(error)
                except Exception as error:
                    source_errors.append({
                        "source": provider.name,
                        "category": "timeout",
                        "message": safe_error(error),
                    })
        except FuturesTimeoutError:
            completed_futures = {future for future in futures if future.done()}
            for future, provider in futures.items():
                if future in processed_futures:
                    continue
                if future in completed_futures:
                    try:
                        evidence, error = future.result(timeout=0)
                        evidence_items.extend(evidence)
                        if error:
                            source_errors.append(error)
                    except Exception as error:
                        source_errors.append({
                            "source": provider.name,
                            "category": "timeout",
                            "message": safe_error(error),
                        })
                    continue
                source_errors.append({"source": provider.name, "category": "timeout"})
    return evidence_items, source_errors


def result_for_request(request_context):
    started = time.time()
    evidence_items, source_errors = retrieve_evidence(request_context)
    ranked = ranked_evidence(request_context.get("question") or "", evidence_items)
    candidate = build_answer_candidate(request_context.get("question") or "", ranked)
    sources_queried = [provider.name for provider in providers_for_request(request_context)]
    latency_ms = int((time.time() - started) * 1000)

    if candidate:
        status = STATUS_PARTIAL if source_errors else STATUS_ANSWER
        return {
            "schema_version": "1.0",
            "request_id": request_context.get("request_id"),
            "session_id": request_context.get("session_id"),
            "status": status,
            "answer": candidate["answer"],
            "confidence": candidate["confidence"],
            "citations": candidate["citations"],
            "sources_queried": sources_queried,
            "sources_used": candidate["sources_used"],
            "source_errors": source_errors,
            "latency_ms": latency_ms,
        }

    if source_errors and len(source_errors) == len(sources_queried or source_errors):
        status = STATUS_AUTH_REQUIRED if any(error.get("category") in {"authentication_failed", "authorization_denied"} for error in source_errors) else STATUS_ERROR
    elif source_errors:
        status = STATUS_PARTIAL
    else:
        status = STATUS_NO_ANSWER

    return {
        "schema_version": "1.0",
        "request_id": request_context.get("request_id"),
        "session_id": request_context.get("session_id"),
        "status": status,
        "answer": "",
        "confidence": {
            "score": ranked[0].get("score", 0) if ranked else 0,
            "band": "low",
            "reasons": ["no_fetched_evidence_met_answer_policy"],
        },
        "citations": [],
        "sources_queried": sources_queried,
        "sources_used": [],
        "source_errors": source_errors,
        "latency_ms": latency_ms,
    }


def user_message_for_result(result):
    status = result.get("status")
    if status in {STATUS_ANSWER, STATUS_PARTIAL} and result.get("answer"):
        return result["answer"]
    if status == STATUS_AUTH_REQUIRED:
        return "Company knowledge search failed because one or more approved sources need additional authorization."
    if status == STATUS_ERROR:
        return "Company knowledge search failed before I could retrieve a reliable answer."
    return "Company knowledge search failed to find a reliable answer in Jira, Confluence, SharePoint, or approved Slack sources."


def workflow_state_for_status(status):
    return {
        STATUS_ANSWER: "MCP_ANSWERED",
        STATUS_NO_ANSWER: "MCP_NO_ANSWER",
        STATUS_AUTH_REQUIRED: "MCP_AUTH_REQUIRED",
        STATUS_PARTIAL: "MCP_PARTIAL",
        STATUS_ERROR: "MCP_ERROR",
    }.get(status, "MCP_ERROR")


def mark_mcp_in_progress(session_id, request_id):
    now = utc_now_iso()
    try:
        sessions_table.update_item(
            Key={"session_id": session_id},
            UpdateExpression="""
                SET
                    workflow_state = :workflow_state,
                    mcp_status = :status,
                    mcp_started_at = :now,
                    updated_at = :now,
                    last_updated_at = :now
            """,
            ConditionExpression="mcp_request_id = :request_id AND workflow_state = :queued",
            ExpressionAttributeValues={
                ":workflow_state": "MCP_IN_PROGRESS",
                ":status": "IN_PROGRESS",
                ":now": now,
                ":request_id": request_id,
                ":queued": "MCP_QUEUED",
            },
        )
        return True
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def update_session_with_result(result):
    now = utc_now_iso()
    status = result.get("status") or STATUS_ERROR
    confidence = json.loads(
        json.dumps(result.get("confidence") or {}),
        parse_float=Decimal,
    )
    sessions_table.update_item(
        Key={"session_id": result.get("session_id")},
        UpdateExpression="""
            SET
                workflow_state = :workflow_state,
                mcp_status = :mcp_status,
                mcp_answer = :mcp_answer,
                mcp_confidence = :mcp_confidence,
                mcp_confidence_reasons = :mcp_confidence_reasons,
                mcp_sources_queried = :mcp_sources_queried,
                mcp_sources_used = :mcp_sources_used,
                mcp_citations = :mcp_citations,
                mcp_errors = :mcp_errors,
                mcp_completed_at = :now,
                mcp_latency_ms = :mcp_latency_ms,
                next_action = :next_action,
                response_source = :response_source,
                last_bot_reply = :last_bot_reply,
                updated_at = :now,
                last_updated_at = :now
        """,
        ConditionExpression="""
            mcp_request_id = :request_id
            AND workflow_state IN (:queued, :in_progress)
        """,
        ExpressionAttributeValues={
            ":workflow_state": workflow_state_for_status(status),
            ":mcp_status": status,
            ":mcp_answer": result.get("answer") or "",
            ":mcp_confidence": confidence,
            ":mcp_confidence_reasons": confidence.get("reasons") or [],
            ":mcp_sources_queried": result.get("sources_queried") or [],
            ":mcp_sources_used": result.get("sources_used") or [],
            ":mcp_citations": result.get("citations") or [],
            ":mcp_errors": result.get("source_errors") or [],
            ":now": now,
            ":mcp_latency_ms": int(result.get("latency_ms") or 0),
            ":next_action": "O3_ClaudeFurtherAssistance",
            ":response_source": "mcp_assist",
            ":last_bot_reply": user_message_for_result(result),
            ":request_id": result.get("request_id"),
            ":queued": "MCP_QUEUED",
            ":in_progress": "MCP_IN_PROGRESS",
        },
    )


def post_result_to_slack(request_context, result):
    slack = request_context.get("slack") or {}
    channel = slack.get("channel_id")
    thread_ts = slack.get("thread_ts")
    if not channel:
        return None
    text = user_message_for_result(result)
    payload = {
        "channel": channel,
        "text": text,
        "blocks": mcp_followup_blocks(
            text,
            request_context.get("session_id"),
            thread_ts,
            result.get("citations") or [],
        ),
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    return slack_api("chat.postMessage", payload)


def process_request(request_context):
    request_id = request_context.get("request_id")
    session_id = request_context.get("session_id")
    if not request_id or not session_id:
        raise ValueError("Missing request_id or session_id")

    if not mark_mcp_in_progress(session_id, request_id):
        log_json({
            "level": "INFO",
            "message": "mcp_request_ignored_non_queued_state",
            "session_id": session_id,
            "request_id": request_id,
        })
        return {"skipped": True, "reason": "not_queued"}

    result = result_for_request(request_context)
    update_session_with_result(result)
    slack_result = post_result_to_slack(request_context, result)

    log_json({
        "level": "INFO" if result.get("status") in {STATUS_ANSWER, STATUS_NO_ANSWER, STATUS_PARTIAL} else "WARN",
        "message": "mcp_assist_completed",
        "session_id": session_id,
        "request_id": request_id,
        "status": result.get("status"),
        "sources_queried": result.get("sources_queried"),
        "sources_used": result.get("sources_used"),
        "source_error_count": len(result.get("source_errors") or []),
        "latency_ms": result.get("latency_ms"),
        "slack_ts": (slack_result or {}).get("ts"),
        "synthesis_mode": MCP_SYNTHESIS_MODE,
    })
    return result


def event_records(event):
    records = event.get("Records") if isinstance(event, dict) else None
    if not records:
        return [event]
    values = []
    for record in records:
        body = record.get("body")
        if isinstance(body, str):
            values.append(json.loads(body))
        elif isinstance(body, dict):
            values.append(body)
    return values


def lambda_handler(event, context):
    results = []
    for request_context in event_records(event):
        results.append(process_request(request_context))
    return results[0] if len(results) == 1 else {"results": results}
