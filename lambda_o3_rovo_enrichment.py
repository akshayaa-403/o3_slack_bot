import base64
import html
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import boto3

AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
JIRA_SECRET_ID = os.environ.get("JIRA_SECRET_ID")
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "o3_slack_sessions")


def env_int(name, default, minimum=None, maximum=None):
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default

    if minimum is not None:
        value = max(minimum, value)

    if maximum is not None:
        value = min(maximum, value)

    return value


ROVO_MODE = os.environ.get("ROVO_MODE", "confluence").strip().lower() or "confluence"
ROVO_COMMENT_PREFIX = os.environ.get("ROVO_COMMENT_PREFIX", "Project IVY Rovo enrichment")
ROVO_TIMEOUT_SECONDS = env_int("ROVO_TIMEOUT_SECONDS", 15, 1, 60)
CONFLUENCE_RESULT_LIMIT = env_int("CONFLUENCE_RESULT_LIMIT", 3, 0, 5)
CONFLUENCE_EXCERPT_LIMIT = env_int("CONFLUENCE_EXCERPT_LIMIT", 600, 120, 2000)
CONFLUENCE_SPACE_KEYS = [
    space_key.strip()
    for space_key in os.environ.get("CONFLUENCE_SPACE_KEYS", "").split(",")
    if space_key.strip()
]
CONFLUENCE_SITE_URL = os.environ.get("CONFLUENCE_SITE_URL")
ROVO_CLAUDE_SUMMARY_ENABLED = (
    os.environ.get("ROVO_CLAUDE_SUMMARY_ENABLED", "false").lower() == "true"
)
ROVO_CLAUDE_SUMMARY_FUNCTION = os.environ.get("ROVO_CLAUDE_SUMMARY_FUNCTION")

secretsmanager = boto3.client("secretsmanager", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
lambda_client = boto3.client("lambda", region_name=AWS_REGION)
sessions_table = dynamodb.Table(DYNAMODB_TABLE)
_jira_secret_cache = None


STOP_WORDS = {
    "about",
    "after",
    "again",
    "already",
    "also",
    "and",
    "any",
    "are",
    "because",
    "been",
    "between",
    "but",
    "can",
    "cannot",
    "could",
    "does",
    "for",
    "from",
    "get",
    "getting",
    "have",
    "help",
    "how",
    "into",
    "issue",
    "kindly",
    "login",
    "need",
    "not",
    "please",
    "request",
    "should",
    "that",
    "the",
    "this",
    "unable",
    "user",
    "what",
    "when",
    "where",
    "with",
    "would",
    "you",
}


def to_iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def log_json(data):
    print(json.dumps(data, default=str))


def require_env(name, value):
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")

    return value


def text_or_empty(value):
    if value is None:
        return ""

    return str(value).strip()


def compact_whitespace(value):
    return re.sub(r"\s+", " ", text_or_empty(value)).strip()


def shorten(value, limit):
    text = compact_whitespace(value)

    if len(text) <= limit:
        return text

    return text[: max(0, limit - 3)].rstrip() + "..."


def humanize_intent(intent_name):
    value = text_or_empty(intent_name)

    if not value:
        return "Support request"

    value = re.sub(r"[_-]+", " ", value)
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    value = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", value)
    return compact_whitespace(value).title()


def get_jira_secret():
    global _jira_secret_cache

    if _jira_secret_cache:
        return _jira_secret_cache

    secret_id = require_env("JIRA_SECRET_ID", JIRA_SECRET_ID)
    response = secretsmanager.get_secret_value(SecretId=secret_id)
    secret_string = response.get("SecretString")

    if not secret_string:
        raise ValueError("Jira secret must be stored as a JSON SecretString")

    secret = json.loads(secret_string)
    for key in ("site_url", "email", "api_token"):
        if not secret.get(key):
            raise ValueError(f"Jira secret is missing required key: {key}")

    secret["site_url"] = secret["site_url"].rstrip("/")
    _jira_secret_cache = secret
    return secret


def jira_auth_header(secret):
    raw_token = f"{secret['email']}:{secret['api_token']}".encode("utf-8")
    encoded = base64.b64encode(raw_token).decode("utf-8")
    return f"Basic {encoded}"


def confluence_base_url(secret):
    base_url = (
        text_or_empty(CONFLUENCE_SITE_URL)
        or text_or_empty(secret.get("confluence_site_url"))
        or text_or_empty(secret["site_url"])
    ).rstrip("/")

    if base_url.endswith("/wiki"):
        return base_url

    return f"{base_url}/wiki"


def atlassian_json_request(secret, url, method="GET", payload=None):
    data = None
    headers = {
        "Accept": "application/json",
        "Authorization": jira_auth_header(secret),
    }

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )

    with urllib.request.urlopen(request, timeout=ROVO_TIMEOUT_SECONDS) as response:
        body = response.read().decode("utf-8")

    if not body:
        return {}

    return json.loads(body)


def adf_text(text, marks=None):
    node = {
        "type": "text",
        "text": text_or_empty(text) or "-"
    }

    if marks:
        node["marks"] = marks

    return node


def adf_link(text, url):
    return adf_text(
        text_or_empty(text) or text_or_empty(url) or "-",
        marks=[
            {
                "type": "link",
                "attrs": {
                    "href": url
                }
            }
        ] if text_or_empty(url) else None
    )


def adf_inline_text(text):
    lines = text_or_empty(text).splitlines() or ["-"]
    content = []

    for index, line in enumerate(lines):
        if index:
            content.append({"type": "hardBreak"})
        content.append(adf_text(line or " "))

    return content


def adf_paragraph(text):
    return {
        "type": "paragraph",
        "content": adf_inline_text(text)
    }


def adf_paragraph_nodes(nodes):
    return {
        "type": "paragraph",
        "content": nodes or [adf_text("-")]
    }


def adf_heading(text, level=3):
    return {
        "type": "heading",
        "attrs": {
            "level": level
        },
        "content": [
            adf_text(text)
        ]
    }


def adf_bullet_item_from_text(text):
    return {
        "type": "listItem",
        "content": [
            adf_paragraph(text)
        ]
    }


def adf_bullet_item_from_nodes(nodes):
    return {
        "type": "listItem",
        "content": [
            adf_paragraph_nodes(nodes)
        ]
    }


def adf_bullet_list(items):
    return {
        "type": "bulletList",
        "content": items or [adf_bullet_item_from_text("-")]
    }


def extract_request_context(event):
    lex = event.get("lex", {}) or {}
    request = event.get("request", {}) or {}
    slack = event.get("slack", {}) or {}

    original_text = (
        text_or_empty(request.get("original_text"))
        or text_or_empty(event.get("text"))
        or text_or_empty(event.get("raw_text"))
    )
    raw_text = text_or_empty(request.get("raw_text")) or text_or_empty(event.get("raw_text")) or original_text
    user_followup = text_or_empty(request.get("user_followup"))
    jira_request_text = text_or_empty(request.get("jira_request_text")) or text_or_empty(event.get("text"))

    return {
        "original_text": original_text,
        "raw_text": raw_text,
        "user_followup": user_followup,
        "jira_request_text": jira_request_text,
        "lex_intent": text_or_empty(lex.get("intent")),
        "slack_user": text_or_empty(event.get("user")) or text_or_empty(slack.get("user")),
        "slack_channel": text_or_empty(event.get("channel")) or text_or_empty(slack.get("channel")),
    }


def tokenize_query(*values):
    tokens = []

    for value in values:
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]{1,}", text_or_empty(value)):
            normalized = token.lower().strip("._+-")

            if len(normalized) < 3 or normalized in STOP_WORDS:
                continue

            if normalized not in tokens:
                tokens.append(normalized)

    return tokens


def cql_quote(value):
    sanitized = re.sub(r"[^A-Za-z0-9\s._+-]", " ", text_or_empty(value))
    sanitized = compact_whitespace(sanitized)[:180]
    sanitized = sanitized.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{sanitized}"'


def build_search_query(context):
    intent_name = context["lex_intent"]
    keyword_sources = [
        context["original_text"],
        context["user_followup"],
        humanize_intent(intent_name),
    ]

    tokens = tokenize_query(*keyword_sources)

    if not tokens:
        return ""

    return " ".join(tokens[:16])


def build_cql(search_query):
    clauses = [
        "type = page",
        f"text ~ {cql_quote(search_query)}",
    ]

    if CONFLUENCE_SPACE_KEYS:
        quoted_spaces = ", ".join(cql_quote(space_key) for space_key in CONFLUENCE_SPACE_KEYS)
        clauses.append(f"space in ({quoted_spaces})")

    return " AND ".join(clauses) + " ORDER BY lastmodified DESC"


def strip_html(value):
    without_scripts = re.sub(
        r"(?is)<(script|style).*?>.*?</\1>",
        " ",
        text_or_empty(value),
    )
    without_tags = re.sub(r"(?s)<[^>]+>", " ", without_scripts)
    return compact_whitespace(html.unescape(without_tags))


def page_url(secret, page):
    links = page.get("_links", {}) or {}
    webui = text_or_empty(links.get("webui"))
    base = text_or_empty(links.get("base"))

    if base and webui:
        return f"{base.rstrip('/')}/{webui.lstrip('/')}"

    if webui.startswith("http"):
        return webui

    site_url = secret["site_url"].rstrip("/")

    if webui.startswith("/wiki/"):
        return f"{site_url}{webui}"

    if webui:
        return f"{confluence_base_url(secret)}{webui if webui.startswith('/') else '/' + webui}"

    page_id = text_or_empty(page.get("id"))
    if page_id:
        return f"{confluence_base_url(secret)}/pages/{page_id}"

    return ""


def normalize_confluence_page(secret, page):
    body_view = (((page.get("body") or {}).get("view") or {}).get("value")) or ""
    excerpt = strip_html(page.get("excerpt"))
    body_snippet = strip_html(body_view)
    snippet = excerpt or body_snippet

    return {
        "id": text_or_empty(page.get("id")),
        "title": text_or_empty(page.get("title")) or "Untitled Confluence page",
        "url": page_url(secret, page),
        "excerpt": shorten(snippet, CONFLUENCE_EXCERPT_LIMIT),
        "body_snippet": shorten(body_snippet, CONFLUENCE_EXCERPT_LIMIT),
        "space_key": text_or_empty(((page.get("space") or {}).get("key"))),
    }


def search_confluence_pages(secret, search_query):
    if not search_query or CONFLUENCE_RESULT_LIMIT <= 0:
        return []

    query_params = urllib.parse.urlencode({
        "cql": build_cql(search_query),
        "limit": str(CONFLUENCE_RESULT_LIMIT),
        "expand": "body.view,space,version",
    })
    url = f"{confluence_base_url(secret)}/rest/api/content/search?{query_params}"
    response = atlassian_json_request(secret, url)
    pages = []
    seen = set()

    for page in response.get("results", []) or []:
        normalized = normalize_confluence_page(secret, page)
        dedupe_key = normalized["id"] or normalized["url"] or normalized["title"]

        if dedupe_key in seen:
            continue

        seen.add(dedupe_key)
        pages.append(normalized)

    return pages[:CONFLUENCE_RESULT_LIMIT]


def confluence_error_code(status):
    if status in {401, 403}:
        return "rovo_confluence_auth_or_permission_error"

    if status == 404:
        return "rovo_confluence_not_found"

    if status == 429:
        return "rovo_confluence_rate_limited"

    if status >= 500:
        return "rovo_confluence_service_error"

    return "rovo_confluence_http_error"


def invoke_claude_summary(event, matches):
    if not (matches and ROVO_CLAUDE_SUMMARY_ENABLED and ROVO_CLAUDE_SUMMARY_FUNCTION):
        return None

    payload = {
        "task": "rovo_support_summary",
        "event_id": event.get("event_id"),
        "session_id": event.get("session_id"),
        "jira_request_id": event.get("jira_request_id"),
        "ticket_key": event.get("ticket_key"),
        "text": "\n".join([
            "Create a concise Jira support-agent summary using only the Confluence KB data below.",
            "Do not add troubleshooting steps, assumptions, or context that is not present in the KB snippets.",
            "",
            "Confluence KB matches:",
            json.dumps(matches, ensure_ascii=True, default=str),
        ]),
        "kb_matches": matches,
    }

    try:
        response = lambda_client.invoke(
            FunctionName=ROVO_CLAUDE_SUMMARY_FUNCTION,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8"),
        )
        response_payload = json.loads(response["Payload"].read().decode("utf-8") or "{}")
        summary = (
            text_or_empty(response_payload.get("summary"))
            or text_or_empty(response_payload.get("reply"))
            or text_or_empty(response_payload.get("text"))
        )

        if summary:
            return shorten(summary, 1200)

        log_json({
            "level": "WARN",
            "message": "rovo_claude_summary_empty",
            "jira_request_id": event.get("jira_request_id"),
            "ticket_key": event.get("ticket_key"),
            "response": response_payload,
        })

    except Exception as e:
        log_json({
            "level": "WARN",
            "message": "rovo_claude_summary_failed",
            "jira_request_id": event.get("jira_request_id"),
            "ticket_key": event.get("ticket_key"),
            "error": str(e),
        })

    return None


def build_enrichment(event):
    secret = get_jira_secret()
    context = extract_request_context(event)
    search_query = build_search_query(context)
    confluence_error = None
    confluence_error_code_value = None
    matches = []

    if ROVO_MODE == "confluence":
        try:
            matches = search_confluence_pages(secret, search_query)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            confluence_error_code_value = confluence_error_code(e.code)
            confluence_error = f"Confluence returned HTTP {e.code} during KB search."
            log_json({
                "level": "WARN",
                "message": "rovo_confluence_search_http_error",
                "jira_request_id": event.get("jira_request_id"),
                "ticket_key": event.get("ticket_key"),
                "status": e.code,
                "body": body,
            })
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            confluence_error_code_value = "rovo_confluence_network_error"
            confluence_error = "Confluence did not respond during KB search."
            log_json({
                "level": "WARN",
                "message": "rovo_confluence_search_network_error",
                "jira_request_id": event.get("jira_request_id"),
                "ticket_key": event.get("ticket_key"),
                "error": str(e),
            })
        except Exception as e:
            confluence_error_code_value = "rovo_confluence_search_failed"
            confluence_error = "Confluence KB search failed."
            log_json({
                "level": "WARN",
                "message": "rovo_confluence_search_failed",
                "jira_request_id": event.get("jira_request_id"),
                "ticket_key": event.get("ticket_key"),
                "error": str(e),
            })

    claude_summary = invoke_claude_summary(event, matches)

    if matches:
        match_status = "matched"
        message = (
            f"Found {len(matches)} matching Confluence KB article"
            f"{'' if len(matches) == 1 else 's'}."
        )
    elif confluence_error:
        match_status = "search_failed"
        message = f"Confluence KB search failed: {confluence_error}"
    else:
        match_status = "no_match"
        message = "No matching Confluence KB articles found for this request."

    if claude_summary:
        summary = claude_summary
    else:
        summary = f"{ROVO_COMMENT_PREFIX}: {message}"

    return {
        "context": context,
        "search_query": search_query,
        "kb_matches": matches,
        "confluence_error": confluence_error,
        "confluence_error_code": confluence_error_code_value,
        "match_status": match_status,
        "message": message,
        "summary": summary,
        "claude_summary": claude_summary,
    }


def add_metadata_section(content, event, enrichment):
    context = enrichment["context"]

    content.extend([
        adf_heading("Trace Metadata", 3),
        adf_paragraph(f"Project IVY request ID: {text_or_empty(event.get('jira_request_id')) or '-'}"),
        adf_paragraph(f"Jira ticket: {text_or_empty(event.get('ticket_key')) or '-'}"),
        adf_paragraph(f"Session ID: {text_or_empty(event.get('session_id')) or '-'}"),
        adf_paragraph(f"Slack user/channel: {context['slack_user'] or '-'} / {context['slack_channel'] or '-'}"),
    ])


def add_kb_section(content, enrichment):
    matches = enrichment["kb_matches"]
    content.append(adf_heading("Matched KB Articles", 3))

    if enrichment["confluence_error"]:
        content.append(adf_paragraph(enrichment["message"]))

        if enrichment["confluence_error_code"]:
            content.append(adf_paragraph(f"Error code: {enrichment['confluence_error_code']}"))

        return

    if not matches:
        content.append(adf_paragraph(enrichment["message"]))
        return

    article_items = []
    for match in matches:
        nodes = [adf_link(match["title"], match.get("url"))]

        if match.get("space_key"):
            nodes.append(adf_text(f" ({match['space_key']})"))

        article_items.append(adf_bullet_item_from_nodes(nodes))

    content.append(adf_bullet_list(article_items))
    content.append(adf_heading("Relevant Excerpts", 3))

    for match in matches:
        title = match["title"]
        excerpt = match.get("excerpt") or match.get("body_snippet") or "-"
        content.append(adf_paragraph(f"{title}:\n{excerpt}"))


def build_comment_payload(event, enrichment):
    content = [
        adf_heading(ROVO_COMMENT_PREFIX, 2),
        adf_paragraph(f"Mode: {ROVO_MODE}"),
    ]

    add_metadata_section(content, event, enrichment)

    if enrichment.get("claude_summary"):
        content.extend([
            adf_heading("Confluence KB Summary", 3),
            adf_paragraph(enrichment["claude_summary"]),
        ])

    add_kb_section(content, enrichment)

    return {
        "body": {
            "version": 1,
            "type": "doc",
            "content": content
        }
    }


def add_jira_comment(ticket_key, comment_payload):
    secret = get_jira_secret()
    quoted_ticket_key = urllib.parse.quote(ticket_key, safe="")
    url = f"{secret['site_url']}/rest/api/3/issue/{quoted_ticket_key}/comment"
    return atlassian_json_request(secret, url, method="POST", payload=comment_payload)


def update_rovo_session(event, status, enriched_at, **fields):
    session_id = text_or_empty(event.get("session_id"))
    if not session_id:
        log_json({
            "level": "WARN",
            "message": "rovo_session_update_skipped",
            "reason": "missing_session_id",
            "jira_request_id": event.get("jira_request_id"),
            "ticket_key": event.get("ticket_key")
        })
        return

    expression_values = {
        ":status": status,
        ":enriched_at": enriched_at,
        ":updated_at": enriched_at,
    }
    update_expression = """
        SET
            rovo_status = :status,
            rovo_enriched_at = :enriched_at,
            updated_at = :updated_at
    """

    optional_fields = {
        "rovo_error": fields.get("error"),
        "rovo_error_code": fields.get("error_code"),
        "rovo_summary": fields.get("summary"),
        "rovo_comment_id": fields.get("comment_id"),
    }

    for field_name, value in optional_fields.items():
        if value:
            placeholder = f":{field_name}"
            update_expression += f", {field_name} = {placeholder}"
            expression_values[placeholder] = value

    sessions_table.update_item(
        Key={
            "session_id": session_id
        },
        UpdateExpression=update_expression,
        ExpressionAttributeValues=expression_values
    )


def http_error_code(status):
    if status in {401, 403}:
        return "rovo_jira_auth_or_permission_error"

    if status == 404:
        return "rovo_jira_ticket_not_found"

    if status == 429:
        return "rovo_jira_rate_limited"

    if status >= 500:
        return "rovo_jira_service_error"

    return "rovo_jira_http_error"


def http_error_message(status):
    if status in {401, 403}:
        return "Jira rejected the enrichment comment request. Check comment permissions."

    if status == 404:
        return "Jira ticket was not found for enrichment."

    if status == 429:
        return "Jira rate limited the enrichment comment request."

    if status >= 500:
        return "Jira returned a temporary service error while adding enrichment."

    return f"Jira returned HTTP {status} while adding enrichment."


def failure_response(event, error, error_code):
    enriched_at = to_iso(datetime.now(timezone.utc))

    try:
        update_rovo_session(
            event,
            "failed",
            enriched_at,
            error=str(error),
            error_code=error_code
        )

    except Exception as update_error:
        log_json({
            "level": "ERROR",
            "message": "rovo_failure_session_update_failed",
            "jira_request_id": event.get("jira_request_id"),
            "ticket_key": event.get("ticket_key"),
            "error": str(update_error),
            "original_error": str(error),
            "original_error_code": error_code
        })

    return {
        "ok": False,
        "rovo_status": "failed",
        "error": str(error),
        "error_code": error_code
    }


def lambda_handler(event, context):
    log_json({
        "level": "INFO",
        "message": "rovo_enrichment_received",
        "jira_request_id": event.get("jira_request_id"),
        "ticket_key": event.get("ticket_key"),
        "session_id": event.get("session_id"),
        "mode": ROVO_MODE
    })

    try:
        ticket_key = text_or_empty(event.get("ticket_key"))
        if not ticket_key:
            raise ValueError("Missing required event field: ticket_key")

        enrichment = build_enrichment(event)
        comment_payload = build_comment_payload(event, enrichment)
        comment_response = add_jira_comment(ticket_key, comment_payload)
        enriched_at = to_iso(datetime.now(timezone.utc))
        comment_id = comment_response.get("id")

        update_rovo_session(
            event,
            "completed",
            enriched_at,
            summary=enrichment["summary"],
            comment_id=comment_id,
            error=enrichment.get("confluence_error"),
            error_code=enrichment.get("confluence_error_code"),
        )

        log_json({
            "level": "INFO",
            "message": "rovo_enrichment_completed",
            "jira_request_id": event.get("jira_request_id"),
            "ticket_key": ticket_key,
            "session_id": event.get("session_id"),
            "comment_id": comment_id,
            "kb_match_count": len(enrichment["kb_matches"]),
            "match_status": enrichment.get("match_status"),
            "confluence_error_code": enrichment.get("confluence_error_code"),
        })

        return {
            "ok": True,
            "rovo_status": "completed",
            "enrichment_summary": enrichment["summary"],
            "message": enrichment["message"],
            "match_status": enrichment["match_status"],
            "kb_matches": enrichment["kb_matches"],
            "comment_id": comment_id,
            "confluence_error_code": enrichment.get("confluence_error_code"),
        }

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        error = http_error_message(e.code)

        log_json({
            "level": "ERROR",
            "message": "rovo_enrichment_http_error",
            "jira_request_id": event.get("jira_request_id"),
            "ticket_key": event.get("ticket_key"),
            "session_id": event.get("session_id"),
            "status": e.code,
            "body": body
        })

        return failure_response(event, error, http_error_code(e.code))

    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        log_json({
            "level": "ERROR",
            "message": "rovo_enrichment_network_error",
            "jira_request_id": event.get("jira_request_id"),
            "ticket_key": event.get("ticket_key"),
            "session_id": event.get("session_id"),
            "error": str(e)
        })

        return failure_response(event, "Jira did not respond to the enrichment request.", "rovo_jira_network_error")

    except Exception as e:
        log_json({
            "level": "ERROR",
            "message": "rovo_enrichment_failed",
            "jira_request_id": event.get("jira_request_id"),
            "ticket_key": event.get("ticket_key"),
            "session_id": event.get("session_id"),
            "error": str(e)
        })

        return failure_response(event, e, "rovo_enrichment_failed")
