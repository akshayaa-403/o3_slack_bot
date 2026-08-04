"""
Admin console API — the ten routes the web portal calls.

Deployment shape
----------------
API Gateway **HTTP API** (payload format 2.0) with a **JWT authorizer** pointed
at the Cognito user pool, in front of this single Lambda. The authorizer does
signature/issuer/audience validation, so this function trusts
``requestContext.authorizer.jwt.claims`` and never parses a token itself.

CloudFront serves ``/api/*`` from the API Gateway origin, so the browser sees
one origin and there is no CORS to configure. Nothing here emits CORS headers.

Authorisation model
-------------------
Two roles, taken from custom claims set on the Cognito user:

  ``custom:role``     ``platform_admin`` sees every tenant; anything else is a
                      BU admin scoped to their own.
  ``custom:tenants``  comma-separated tenant ids a BU admin may touch. ``*``
                      means all (only honoured for platform admins).

Every route that names a tenant goes through ``_authorise_tenant``. A BU admin
asking for someone else's tenant gets 404, not 403 — a 403 would confirm the
tenant exists.

Secrets
-------
Connector credentials are written to Secrets Manager under
``ivvy-bot/{tenant_id}/connectors`` and are **never** read back out to the
browser. No route returns a credential, and nothing logs one.

Environment
-----------
  TENANT_TABLE            DynamoDB tenant registry            (required)
  INTENT_TABLE            DynamoDB drafted-intent table       (optional)
  SECRET_PREFIX           Secrets Manager prefix              default 'ivvy-bot'
  PROVISION_STATE_MACHINE Step Functions ARN to provision     (optional)
  METRIC_NAMESPACE        CloudWatch namespace for EMF        default 'IvvY/Bot'
  ALLOWED_JIRA_SUFFIXES   comma list, default '.atlassian.net'
  VALIDATE_TIMEOUT        seconds for outbound validation     default 6
"""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TENANT_TABLE = os.environ.get("TENANT_TABLE", "ivvy-bot-tenants")
INTENT_TABLE = os.environ.get("INTENT_TABLE", "")
SECRET_PREFIX = os.environ.get("SECRET_PREFIX", "ivvy-bot")
PROVISION_STATE_MACHINE = os.environ.get("PROVISION_STATE_MACHINE", "")
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "IvvY/Bot")
VALIDATE_TIMEOUT = int(os.environ.get("VALIDATE_TIMEOUT", "6"))
ALLOWED_JIRA_SUFFIXES = tuple(
    s.strip() for s in os.environ.get("ALLOWED_JIRA_SUFFIXES", ".atlassian.net").split(",") if s.strip()
)

_ddb = boto3.resource("dynamodb")
_secrets = boto3.client("secretsmanager")
_cloudwatch = boto3.client("cloudwatch")
_sfn = boto3.client("stepfunctions") if PROVISION_STATE_MACHINE else None

TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
SPACE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")

# Connector ids the console can send. Anything else is rejected rather than
# stored, so a typo cannot create an unroutable tenant.
KNOWN_CONNECTORS = {
    "slack", "teams",
    "jira", "servicenow", "freshservice", "topdesk", "teamdynamix",
    "sharepoint", "confluence", "chathistory",
    "simpplr", "workvivo", "unily",
}
KNOWN_SURFACES = {"collaboration", "itsm", "knowledge", "intranet"}

# Credential field names accepted from the setup flow. Everything else in the
# payload is dropped before it reaches Secrets Manager.
CREDENTIAL_FIELDS = {
    "jira_site", "jira_token", "jira_project",
    "servicenow_instance",
    "freshservice_domain", "freshservice_key",
    "topdesk_url", "topdesk_token",
    "tdx_url", "tdx_token",
    "sharepoint_site",
    "confluence_site", "confluence_token", "confluence_space",
    "workvivo_token",
}
# Never echoed back, never logged.
SENSITIVE_FIELDS = {
    "jira_token", "freshservice_key", "topdesk_token", "tdx_token",
    "confluence_token", "workvivo_token",
}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #

def _json_default(o):
    if isinstance(o, Decimal):
        return int(o) if o % 1 == 0 else float(o)
    raise TypeError(f"not JSON serialisable: {type(o)}")


def _reply(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body, default=_json_default),
    }


def derive_tenant_id(company_name):
    """Same cascade the console previews, re-derived here as the authority."""
    slug = re.sub(r"[^a-z0-9]+", "-", (company_name or "").lower())
    return slug.strip("-")[:32]


def _claims(event):
    try:
        return event["requestContext"]["authorizer"]["jwt"]["claims"] or {}
    except (KeyError, TypeError):
        raise ApiError(401, "Not signed in")


def _caller(event):
    c = _claims(event)
    role = c.get("custom:role", "bu_admin")
    raw = c.get("custom:tenants", "")

    # Claim values reach DynamoDB keys and CloudWatch dimensions, so they are
    # filtered to the id shape here rather than trusted downstream. A wildcard
    # is dropped on purpose: breadth comes from the role, never from this list,
    # so a "*" in a BU admin's claim grants nothing.
    tenants = []
    for part in raw.split(","):
        candidate = part.strip()
        if candidate and TENANT_ID_RE.match(candidate):
            tenants.append(candidate)
        elif candidate and candidate != "*":
            logger.warning("ignoring malformed tenant claim %r", candidate[:40])

    return {
        "email": c.get("email", ""),
        "role": role,
        "is_platform_admin": role == "platform_admin",
        "tenants": tenants,
    }


def _may_see(caller, tenant_id):
    if caller["is_platform_admin"]:
        return True
    return tenant_id in caller["tenants"]


def _authorise_tenant(caller, tenant_id):
    """404 rather than 403, so the response cannot confirm a tenant exists."""
    if not TENANT_ID_RE.match(tenant_id or "") or not _may_see(caller, tenant_id):
        raise ApiError(404, "Tenant not found")


def _body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        from base64 import b64decode
        raw = b64decode(raw).decode("utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise ApiError(400, "Body must be JSON")
    if not isinstance(parsed, dict):
        raise ApiError(400, "Body must be a JSON object")
    return parsed


def _tenants_table():
    return _ddb.Table(TENANT_TABLE)


def _public_tenant(item):
    """Only fields the console renders. Credentials never appear here."""
    return {
        "tenant_id": item.get("tenant_id"),
        "display_name": item.get("display_name"),
        "bot_display_name": item.get("bot_display_name", "IvvY"),
        "status": item.get("status", "provisioning"),
        "created_at": item.get("created_at"),
        "home_surface": item.get("home_surface", "collaboration"),
        "connectors": item.get("connectors", []),
        "jsm_project_key": item.get("jsm_project_key"),
        "approver_email": item.get("approver_email"),
        "intents_live": int(item.get("intents_live", 0) or 0),
        "intents_pending": int(item.get("intents_pending", 0) or 0),
    }


# --------------------------------------------------------------------------- #
#  tenants
# --------------------------------------------------------------------------- #

def list_tenants(event, caller):
    table = _tenants_table()
    items = []
    kwargs = {}
    # The registry is small (one row per customer), so a scan is appropriate
    # and cheaper than maintaining an index purely for this screen.
    while True:
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    visible = [i for i in items if _may_see(caller, i.get("tenant_id", ""))]
    visible.sort(key=lambda i: i.get("created_at", ""), reverse=True)
    return _reply(200, [_public_tenant(i) for i in visible])


def get_tenant(event, caller, tenant_id):
    _authorise_tenant(caller, tenant_id)
    item = _tenants_table().get_item(Key={"tenant_id": tenant_id}).get("Item")
    if not item:
        raise ApiError(404, "Tenant not found")
    return _reply(200, _public_tenant(item))


def patch_tenant(event, caller, tenant_id):
    _authorise_tenant(caller, tenant_id)
    body = _body(event)
    status = body.get("status")
    if status not in {"active", "suspended"}:
        raise ApiError(400, "status must be active or suspended")

    try:
        updated = _tenants_table().update_item(
            Key={"tenant_id": tenant_id},
            UpdateExpression="SET #s = :s, status_changed_at = :t, status_changed_by = :w",
            ConditionExpression="attribute_exists(tenant_id)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": status,
                ":t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ":w": caller["email"],
            },
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ApiError(404, "Tenant not found")
        raise

    logger.info("tenant %s set to %s by %s", tenant_id, status, caller["email"])
    return _reply(200, _public_tenant(updated))


def delete_tenant(event, caller, tenant_id):
    """Remove a tenant and everything keyed to it.

    Ordering matters: dependents first, registry row last. If a step fails
    part-way the tenant is still listed, so the mess is visible and the call can
    be retried. Deleting the row first would orphan credentials and drafts with
    nothing left pointing at them.
    """
    if not caller["is_platform_admin"]:
        raise ApiError(403, "Only a platform admin can delete a tenant")

    _authorise_tenant(caller, tenant_id)

    item = _tenants_table().get_item(Key={"tenant_id": tenant_id}).get("Item")
    if not item:
        raise ApiError(404, "Tenant not found")

    # Deleting a bot that is still answering people should not be one click.
    # Pausing first is a deliberate second step, and it is reversible.
    if item.get("status") != "suspended":
        raise ApiError(409, "Pause this bot before deleting it")

    removed_drafts = _delete_tenant_intents(tenant_id)
    secret_state = _schedule_credential_deletion(tenant_id)

    _tenants_table().delete_item(
        Key={"tenant_id": tenant_id},
        ConditionExpression="attribute_exists(tenant_id)",
    )

    logger.info(
        "deleted tenant %s by %s (%d drafts removed, credentials %s)",
        tenant_id, caller["email"], removed_drafts, secret_state,
    )
    return _reply(200, {
        "deleted": tenant_id,
        "drafts_removed": removed_drafts,
        "credentials": secret_state,
    })


def _delete_tenant_intents(tenant_id):
    """Drafted intents are keyed by tenant, so one query finds them all."""
    if not INTENT_TABLE:
        return 0

    table = _ddb.Table(INTENT_TABLE)
    removed = 0
    kwargs = {
        "KeyConditionExpression": "tenant_id = :t",
        "ExpressionAttributeValues": {":t": tenant_id},
        "ProjectionExpression": "tenant_id, intent_key",
    }
    while True:
        page = table.query(**kwargs)
        for row in page.get("Items", []):
            table.delete_item(
                Key={"tenant_id": row["tenant_id"], "intent_key": row["intent_key"]}
            )
            removed += 1
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return removed


def _schedule_credential_deletion(tenant_id):
    """Schedule rather than erase. A deletion made by mistake is recoverable for
    the recovery window, and leaving a live customer's API tokens behind for a
    tenant that no longer exists is not acceptable either.

    Note this works despite the explicit Deny on GetSecretValue — the API can
    destroy credentials it was never able to read.
    """
    name = f"{SECRET_PREFIX}/{tenant_id}/connectors"
    try:
        _secrets.delete_secret(SecretId=name, RecoveryWindowInDays=7)
        return "scheduled for deletion in 7 days"
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            return "none were stored"
        raise


def _clean_credentials(raw):
    """Keep only known fields, and only non-empty strings."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, value in raw.items():
        if key in CREDENTIAL_FIELDS and isinstance(value, str) and value.strip():
            out[key] = value.strip()
    return out


def create_tenant(event, caller):
    # Standing up a new tenant provisions IAM and secrets, so it stays a
    # platform-admin action.
    if not caller["is_platform_admin"]:
        raise ApiError(403, "Only a platform admin can add a tenant")

    body = _body(event)
    company = (body.get("company_name") or "").strip()
    if not company:
        raise ApiError(400, "A company name is required")

    tenant_id = derive_tenant_id(company)
    if not TENANT_ID_RE.match(tenant_id):
        raise ApiError(400, "That company name has no letters or digits to build an id from")

    connectors = body.get("connectors") or []
    if not isinstance(connectors, list) or not connectors:
        raise ApiError(400, "Pick at least one system to connect")
    unknown = sorted(set(connectors) - KNOWN_CONNECTORS)
    if unknown:
        raise ApiError(400, f"Unrecognised connectors: {', '.join(unknown)}")

    surface = body.get("home_surface", "collaboration")
    if surface not in KNOWN_SURFACES:
        raise ApiError(400, "home_surface is not one of the known groups")

    creds = _clean_credentials(body.get("credentials"))

    project_key = creds.get("jira_project", "")
    if project_key and not PROJECT_KEY_RE.match(project_key):
        raise ApiError(400, "Project keys are uppercase letters and digits, like HRSD")

    space_key = creds.get("confluence_space", "")
    if space_key and not SPACE_KEY_RE.match(space_key):
        raise ApiError(400, "Space keys are uppercase letters and digits, like HRSD")

    now = datetime.now(timezone.utc)
    item = {
        "tenant_id": tenant_id,
        "display_name": company,
        "bot_display_name": (body.get("bot_name") or "IvvY").strip(),
        "status": "provisioning",
        "created_at": now.date().isoformat(),
        "created_by": caller["email"],
        "home_surface": surface,
        "connectors": connectors,
        "jsm_project_key": project_key or None,
        "approver_email": (body.get("approver_email") or caller["email"]) or None,
        "intents_live": 0,
        "intents_pending": 0,
        "guardrail_id": f"gr-{tenant_id}-001",
    }

    # Claim the id first. The condition is what makes two people setting up the
    # same company at once safe.
    try:
        _tenants_table().put_item(
            Item=item, ConditionExpression="attribute_not_exists(tenant_id)"
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ApiError(409, "A tenant with that name already exists")
        raise

    # Then the credentials, into this tenant's own namespace.
    if creds:
        _store_credentials(tenant_id, creds)

    _start_provisioning(tenant_id, item)

    logger.info(
        "created tenant %s (%d connectors) by %s", tenant_id, len(connectors), caller["email"]
    )
    return _reply(201, {"tenant_id": tenant_id, "status": "provisioning"})


def _store_credentials(tenant_id, creds):
    name = f"{SECRET_PREFIX}/{tenant_id}/connectors"
    payload = json.dumps(creds)
    try:
        _secrets.create_secret(
            Name=name,
            SecretString=payload,
            Description=f"Connector credentials for {tenant_id}",
            Tags=[{"Key": "tenant_id", "Value": tenant_id}],
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceExistsException":
            raise
        _secrets.put_secret_value(SecretId=name, SecretString=payload)
    # Deliberately logs the field names only, never the values.
    logger.info("stored %d credential fields for %s", len(creds), tenant_id)


def _start_provisioning(tenant_id, item):
    """Hand off to Step Functions: IAM role, S3 prefix, guardrail, then the
    ticket and document import. Without a state machine configured the tenant
    stays 'provisioning' and the console keeps saying so, which is honest."""
    if not _sfn:
        logger.warning("no PROVISION_STATE_MACHINE set; %s left provisioning", tenant_id)
        return
    _sfn.start_execution(
        stateMachineArn=PROVISION_STATE_MACHINE,
        name=f"provision-{tenant_id}-{int(time.time())}",
        input=json.dumps({
            "tenant_id": tenant_id,
            "connectors": item["connectors"],
            "home_surface": item["home_surface"],
            "approver_email": item["approver_email"],
            "jsm_project_key": item["jsm_project_key"],
        }),
    )


# --------------------------------------------------------------------------- #
#  metrics
# --------------------------------------------------------------------------- #
#
#  Read from CloudWatch **metrics** rather than Logs Insights: a metric query
#  is synchronous and returns in tens of milliseconds, where Insights needs
#  start-then-poll and would make the dashboard wait seconds.
#
#  This expects the worker to emit EMF with dimension TenantId and these
#  metric names. Until it does, GetMetricData returns empty and every route
#  below reports has_data: false — which is what the console renders as
#  "no answers yet" instead of inventing zeroes.
#
LADDER_METRICS = {
    "l1": "AnsweredByIntent",
    "l2": "AnsweredByKnowledge",
    "l3": "AnsweredByModel",
    "l4": "HandedToPerson",
}
UNRESOLVED_METRIC = "UnresolvedTickets"


def _metric_query(qid, name, tenant_id, period, stat="Sum"):
    dims = [{"Name": "TenantId", "Value": tenant_id}] if tenant_id else []
    return {
        "Id": qid,
        "MetricStat": {
            "Metric": {
                "Namespace": METRIC_NAMESPACE,
                "MetricName": name,
                "Dimensions": dims,
            },
            "Period": period,
            "Stat": stat,
        },
        "ReturnData": True,
    }


def _fetch_metrics(tenant_id, days):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    day = 86400

    queries = [
        _metric_query(f"m_{key}", name, tenant_id, day)
        for key, name in LADDER_METRICS.items()
    ]
    queries.append(_metric_query("m_unres", UNRESOLVED_METRIC, tenant_id, day, stat="Maximum"))

    result = _cloudwatch.get_metric_data(
        MetricDataQueries=queries,
        StartTime=start,
        EndTime=end,
        ScanBy="TimestampAscending",
    )

    series = {r["Id"]: [float(v) for v in r.get("Values", [])] for r in result.get("MetricDataResults", [])}

    ladder = {key: int(sum(series.get(f"m_{key}", []))) for key in LADDER_METRICS}
    total = sum(ladder.values())

    if total == 0:
        return {"window_days": days, "has_data": False}

    # Daily deflection: everything the bot closed itself over everything asked.
    per_day = {key: series.get(f"m_{key}", []) for key in LADDER_METRICS}
    length = max((len(v) for v in per_day.values()), default=0)

    def at(key, i):
        vals = per_day[key]
        return vals[i] if i < len(vals) else 0.0

    daily = []
    for i in range(length):
        asked = sum(at(k, i) for k in LADDER_METRICS)
        if asked <= 0:
            continue
        answered = asked - at("l4", i)
        daily.append(round(answered / asked * 100, 1))

    if not daily:
        daily = [round((total - ladder["l4"]) / total * 100, 1)]

    unresolved = series.get("m_unres", [])
    unresolved_now = int(unresolved[-1]) if unresolved else 0
    unresolved_then = int(unresolved[0]) if unresolved else 0

    return {
        "window_days": days,
        "has_data": True,
        "deflection": {
            "rate": daily[-1],
            "delta": round(daily[-1] - daily[0], 1),
            "series": daily,
        },
        "ladder": {**ladder, "total": total},
        "unresolved": {
            "count": unresolved_now,
            "delta": unresolved_now - unresolved_then,
            "series": [int(v) for v in unresolved] or [unresolved_now],
        },
    }


def _window_days(event):
    raw = (event.get("queryStringParameters") or {}).get("days", "7")
    try:
        days = int(raw)
    except (TypeError, ValueError):
        raise ApiError(400, "days must be a number")
    if days not in (7, 14, 30):
        raise ApiError(400, "days must be 7, 14 or 30")
    return days


def tenant_metrics(event, caller, tenant_id):
    _authorise_tenant(caller, tenant_id)
    if not _tenants_table().get_item(Key={"tenant_id": tenant_id}).get("Item"):
        raise ApiError(404, "Tenant not found")
    return _reply(200, _fetch_metrics(tenant_id, _window_days(event)))


def fleet_metrics(event, caller):
    days = _window_days(event)
    # A platform admin sees the whole namespace in one query. A BU admin only
    # ever has their own tenants, so sum those explicitly.
    if caller["is_platform_admin"]:
        return _reply(200, _fetch_metrics(None, days))

    combined = None
    for tid in caller["tenants"]:
        part = _fetch_metrics(tid, days)
        if not part.get("has_data"):
            continue
        if combined is None:
            combined = part
            continue
        for key in LADDER_METRICS:
            combined["ladder"][key] += part["ladder"][key]
        combined["ladder"]["total"] += part["ladder"]["total"]
        combined["unresolved"]["count"] += part["unresolved"]["count"]
        combined["unresolved"]["delta"] += part["unresolved"]["delta"]

    return _reply(200, combined or {"window_days": days, "has_data": False})


# --------------------------------------------------------------------------- #
#  drafted intents
# --------------------------------------------------------------------------- #

def list_intents(event, caller, tenant_id):
    _authorise_tenant(caller, tenant_id)
    if not INTENT_TABLE:
        return _reply(200, [])

    status = (event.get("queryStringParameters") or {}).get("status", "pending")
    if status not in {"pending", "approved", "rejected"}:
        raise ApiError(400, "status must be pending, approved or rejected")

    resp = _ddb.Table(INTENT_TABLE).query(
        KeyConditionExpression="tenant_id = :t",
        FilterExpression="#s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":t": tenant_id, ":s": status},
    )
    drafts = [
        {
            "key": i.get("intent_key"),
            "name": i.get("name"),
            "source_ticket": i.get("source_ticket"),
            "utterances": int(i.get("utterance_count", 0) or 0),
            "category": i.get("category"),
            "confidence": float(i.get("confidence", 0) or 0),
            "kind": i.get("kind", "text"),
        }
        for i in resp.get("Items", [])
    ]
    return _reply(200, drafts)


def decide_intent(event, caller, tenant_id, intent_key):
    _authorise_tenant(caller, tenant_id)
    if not INTENT_TABLE:
        raise ApiError(503, "Intent review is not configured yet")

    decision = _body(event).get("decision")
    if decision not in {"approve", "reject"}:
        raise ApiError(400, "decision must be approve or reject")

    new_status = "approved" if decision == "approve" else "rejected"
    try:
        _ddb.Table(INTENT_TABLE).update_item(
            Key={"tenant_id": tenant_id, "intent_key": intent_key},
            UpdateExpression="SET #s = :s, decided_by = :w, decided_at = :t",
            ConditionExpression="attribute_exists(intent_key) AND #s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": new_status,
                ":pending": "pending",
                ":w": caller["email"],
                ":t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Either gone, or somebody else already ruled on it.
            raise ApiError(409, "That answer has already been reviewed")
        raise

    _bump_pending_count(tenant_id, delta=-1, live_delta=1 if decision == "approve" else 0)
    logger.info("intent %s/%s %s by %s", tenant_id, intent_key, new_status, caller["email"])
    return _reply(200, {"status": new_status})


def _bump_pending_count(tenant_id, delta, live_delta=0):
    try:
        _tenants_table().update_item(
            Key={"tenant_id": tenant_id},
            UpdateExpression=(
                "SET intents_pending = if_not_exists(intents_pending, :z) + :d, "
                "intents_live = if_not_exists(intents_live, :z) + :l"
            ),
            ExpressionAttributeValues={":d": delta, ":l": live_delta, ":z": 0},
        )
    except ClientError:
        logger.exception("could not adjust counters for %s", tenant_id)


# --------------------------------------------------------------------------- #
#  connector validation
# --------------------------------------------------------------------------- #

class _RedirectBlocked(Exception):
    """A validation target tried to bounce us somewhere else."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Allowlisting the host is pointless if we then follow a redirect wherever
    it leads: a genuine *.atlassian.net site could answer 302 with
    http://169.254.169.254/ and we would dutifully fetch instance credentials.
    Refuse to follow rather than re-validating a moving target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _RedirectBlocked(newurl)


# Built once. urlopen() would apply the default redirect handler instead.
_opener = urllib.request.build_opener(_NoRedirects)


def _http_get(url, headers, timeout):
    req = urllib.request.Request(url, headers=headers, method="GET")
    with _opener.open(req, timeout=timeout) as resp:
        return resp.status, resp.read(8192)


def _basic(email, token):
    return "Basic " + b64encode(f"{email}:{token}".encode()).decode()


def _check_atlassian_host(raw_url):
    """Only allow the vendor's own domains. Without this the endpoint would
    happily fetch any URL a caller supplies, which is an SSRF hole.

    Rebuilds the URL from the parsed hostname rather than passing the caller's
    string through, so nothing after the host survives to be re-interpreted."""
    try:
        parts = urllib.parse.urlsplit(raw_url.strip())
    except ValueError:
        raise ApiError(400, "That address could not be parsed")

    if parts.scheme != "https" or not parts.hostname:
        raise ApiError(400, "The address must start with https://")

    # https://real.atlassian.net@evil.example.com parses with hostname
    # evil.example.com, so the suffix test below already catches it — but
    # credentials in a URL are never legitimate here, so refuse them outright.
    if parts.username or parts.password:
        raise ApiError(400, "Remove the username and password from the address")

    # Atlassian Cloud only answers on 443. A custom port means something else
    # is being pointed at, often on the internal network.
    try:
        port = parts.port
    except ValueError:
        raise ApiError(400, "That address has an invalid port")
    if port not in (None, 443):
        raise ApiError(400, "The address must not include a port")

    host = parts.hostname.rstrip(".").lower()
    if not host.endswith(ALLOWED_JIRA_SUFFIXES):
        raise ApiError(400, f"Only {' or '.join(ALLOWED_JIRA_SUFFIXES)} addresses are supported")

    return f"https://{host}"


def validate_jsm(event, caller):
    body = _body(event)
    base = _check_atlassian_host(body.get("base_url") or "")
    token = (body.get("api_token") or "").strip()
    email = (body.get("email") or caller["email"] or "").strip()

    if len(token) < 12:
        return _reply(200, {"ok": False, "error": "That token looks too short to be complete."})
    if not email:
        return _reply(200, {"ok": False, "error": "An account email is needed alongside the token."})

    try:
        status, raw = _http_get(
            f"{base}/rest/api/3/myself",
            {"Authorization": _basic(email, token), "Accept": "application/json"},
            VALIDATE_TIMEOUT,
        )
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return _reply(200, {"ok": False, "error": "Jira rejected those details. Check the email and token."})
        return _reply(200, {"ok": False, "error": f"Jira replied {exc.code}."})
    except _RedirectBlocked:
        return _reply(200, {"ok": False,
                            "error": "That address redirects elsewhere. Give us the Jira site itself."})
    except (urllib.error.URLError, TimeoutError):
        return _reply(200, {"ok": False, "error": "Could not reach that Jira site."})

    if status != 200:
        return _reply(200, {"ok": False, "error": f"Jira replied {status}."})

    account = ""
    try:
        account = json.loads(raw).get("emailAddress") or json.loads(raw).get("displayName") or ""
    except (json.JSONDecodeError, AttributeError):
        pass

    return _reply(200, {"ok": True, "account": account})


def validate_confluence(event, caller):
    body = _body(event)
    space = (body.get("space_key") or "").strip().upper()
    if not SPACE_KEY_RE.match(space):
        return _reply(200, {"ok": False, "error": "Space keys are uppercase letters and digits, like HRSD."})

    base_raw = body.get("base_url") or ""
    token = (body.get("api_token") or "").strip()
    email = (body.get("email") or caller["email"] or "").strip()

    # The console omits these when Confluence rides the Jira connection; the
    # key format check above is then all we can verify without the tenant's
    # stored secret.
    if not base_raw or not token:
        return _reply(200, {"ok": True, "checked": "format only"})

    base = _check_atlassian_host(base_raw)
    try:
        status, raw = _http_get(
            f"{base}/wiki/api/v2/spaces?keys={urllib.parse.quote(space)}&limit=1",
            {"Authorization": _basic(email, token), "Accept": "application/json"},
            VALIDATE_TIMEOUT,
        )
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return _reply(200, {"ok": False, "error": "Confluence rejected those details."})
        return _reply(200, {"ok": False, "error": f"Confluence replied {exc.code}."})
    except _RedirectBlocked:
        return _reply(200, {"ok": False,
                            "error": "That address redirects elsewhere. Give us the Confluence site itself."})
    except (urllib.error.URLError, TimeoutError):
        return _reply(200, {"ok": False, "error": "Could not reach that Confluence site."})

    if status != 200:
        return _reply(200, {"ok": False, "error": f"Confluence replied {status}."})

    try:
        found = len(json.loads(raw).get("results", []))
    except (json.JSONDecodeError, AttributeError):
        found = 0
    if not found:
        return _reply(200, {"ok": False, "error": f"No space called {space} is visible to that account."})
    return _reply(200, {"ok": True})


# --------------------------------------------------------------------------- #
#  router
# --------------------------------------------------------------------------- #

ROUTES = [
    ("GET",   re.compile(r"^/tenants$"),                                  lambda e, c, m: list_tenants(e, c)),
    ("POST",  re.compile(r"^/tenants$"),                                  lambda e, c, m: create_tenant(e, c)),
    ("GET",   re.compile(r"^/tenants/([^/]+)$"),                          lambda e, c, m: get_tenant(e, c, m[0])),
    ("PATCH", re.compile(r"^/tenants/([^/]+)$"),                          lambda e, c, m: patch_tenant(e, c, m[0])),
    ("DELETE", re.compile(r"^/tenants/([^/]+)$"),                         lambda e, c, m: delete_tenant(e, c, m[0])),
    ("GET",   re.compile(r"^/tenants/([^/]+)/metrics$"),                  lambda e, c, m: tenant_metrics(e, c, m[0])),
    ("GET",   re.compile(r"^/tenants/([^/]+)/intents$"),                  lambda e, c, m: list_intents(e, c, m[0])),
    ("POST",  re.compile(r"^/tenants/([^/]+)/intents/([^/]+)$"),          lambda e, c, m: decide_intent(e, c, m[0], m[1])),
    ("GET",   re.compile(r"^/metrics$"),                                  lambda e, c, m: fleet_metrics(e, c)),
    ("POST",  re.compile(r"^/validate/jsm$"),                             lambda e, c, m: validate_jsm(e, c)),
    ("POST",  re.compile(r"^/validate/confluence$"),                      lambda e, c, m: validate_confluence(e, c)),
]


def _route(method, path):
    for verb, pattern, handler in ROUTES:
        match = pattern.match(path)
        if match and verb == method:
            return handler, list(match.groups())
    return None, None


def _path_of(event):
    # CloudFront forwards /api/tenants; the stage may or may not strip it.
    path = event.get("rawPath") or event.get("path") or "/"
    for prefix in ("/api", "/prod", "/default"):
        if path.startswith(prefix + "/") or path == prefix:
            path = path[len(prefix):] or "/"
    return path.rstrip("/") or "/"


def lambda_handler(event, context):
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    ).upper()
    path = _path_of(event)

    try:
        caller = _caller(event)
        handler, groups = _route(method, path)
        if not handler:
            raise ApiError(404, "No such route")
        return handler(event, caller, groups)

    except ApiError as err:
        if err.status >= 500:
            logger.error("%s %s -> %s %s", method, path, err.status, err.message)
        else:
            logger.info("%s %s -> %s %s", method, path, err.status, err.message)
        return _reply(err.status, {"message": err.message})

    except ClientError as err:
        logger.exception("AWS call failed on %s %s", method, path)
        code = err.response.get("Error", {}).get("Code", "")
        if code in ("AccessDeniedException", "AccessDenied"):
            return _reply(500, {"message": "The console is missing an AWS permission for that."})
        return _reply(500, {"message": "Something went wrong at our end."})

    except Exception:
        # Never leak a stack trace to the browser.
        logger.exception("unhandled error on %s %s", method, path)
        return _reply(500, {"message": "Something went wrong at our end."})
