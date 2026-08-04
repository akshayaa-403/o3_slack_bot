"""
kb-connect OAuth exchange — Slack and Atlassian.

Deployment shape
----------------
API Gateway HTTP API (no authorizer — the OAuth `code` itself is the proof of
consent) in front of this Lambda. CloudFront serves ``/api/*`` from that API,
same pattern as ``lambda_o3_admin_api``, so the browser sees one origin and
there is no CORS to configure.

Routes handled
--------------
``POST /sources/slackhistory/connect``  body: ``{"code": "...", "redirect_uri": "..."}``
``POST /sources/atlassian/connect``     body: ``{"code": "...", "redirect_uri": "..."}``

What it does
------------
Exchanges the OAuth ``code`` kb-connect's popup got back from the provider
for a real access token, using that provider's client id/secret. The
resulting token is written to Secrets Manager under
``ivvy-bot/kb-connect/{source}`` and never returned to the browser — the
response is just ``{"connected": true}``.

Atlassian's token additionally requires one follow-up call
(``GET /oauth/token/accessible-resources``) to learn the cloud id the token
is scoped to — every subsequent Jira/Confluence API call needs that id in
its URL, so it's stored alongside the token rather than looked up again
later.

Environment
-----------
  SLACK_CLIENT_ID           Slack app client id                (required for Slack)
  SLACK_CLIENT_SECRET       Slack app client secret             (required for Slack)
  ATLASSIAN_CLIENT_ID       Atlassian OAuth 2.0 (3LO) client id (required for Atlassian)
  ATLASSIAN_CLIENT_SECRET   Atlassian OAuth 2.0 (3LO) secret    (required for Atlassian)
  SECRET_PREFIX             Secrets Manager prefix              default 'ivvy-bot'
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SLACK_CLIENT_ID = os.environ.get("SLACK_CLIENT_ID", "")
SLACK_CLIENT_SECRET = os.environ.get("SLACK_CLIENT_SECRET", "")
ATLASSIAN_CLIENT_ID = os.environ.get("ATLASSIAN_CLIENT_ID", "")
ATLASSIAN_CLIENT_SECRET = os.environ.get("ATLASSIAN_CLIENT_SECRET", "")
SECRET_PREFIX = os.environ.get("SECRET_PREFIX", "ivvy-bot")

_secrets = boto3.client("secretsmanager")


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _post_form(url, fields):
    payload = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _post_json(url, payload, headers=None):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _store_secret(name, value):
    try:
        _secrets.create_secret(Name=name, SecretString=json.dumps(value))
    except ClientError as err:
        if err.response["Error"]["Code"] != "ResourceExistsException":
            raise
        _secrets.put_secret_value(SecretId=name, SecretString=json.dumps(value))


def _connect_slack(code, redirect_uri):
    if not SLACK_CLIENT_ID or not SLACK_CLIENT_SECRET:
        return _response(500, {"message": "Slack app credentials are not configured"})

    try:
        resp = _post_form("https://slack.com/api/oauth.v2.access", {
            "client_id": SLACK_CLIENT_ID,
            "client_secret": SLACK_CLIENT_SECRET,
            "code": code,
            "redirect_uri": redirect_uri,
        })
    except urllib.error.URLError as err:
        logger.error("Slack oauth.v2.access request failed: %s", err)
        return _response(502, {"message": "Could not reach Slack"})

    if not resp.get("ok"):
        logger.warning("Slack oauth.v2.access rejected the code: %s", resp.get("error"))
        return _response(400, {"message": f"Slack rejected the sign-in: {resp.get('error', 'unknown_error')}"})

    _store_secret(f"{SECRET_PREFIX}/kb-connect/slack", {
        "access_token": resp.get("access_token"),
        "team_id": (resp.get("team") or {}).get("id"),
        "bot_user_id": resp.get("bot_user_id"),
        "scope": resp.get("scope"),
    })
    return _response(200, {"connected": True, "source": "slackhistory"})


def _connect_atlassian(code, redirect_uri):
    if not ATLASSIAN_CLIENT_ID or not ATLASSIAN_CLIENT_SECRET:
        return _response(500, {"message": "Atlassian app credentials are not configured"})

    try:
        token_resp = _post_json("https://auth.atlassian.com/oauth/token", {
            "grant_type": "authorization_code",
            "client_id": ATLASSIAN_CLIENT_ID,
            "client_secret": ATLASSIAN_CLIENT_SECRET,
            "code": code,
            "redirect_uri": redirect_uri,
        })
    except urllib.error.HTTPError as err:
        body = err.read().decode(errors="replace")
        logger.warning("Atlassian token exchange rejected the code: %s", body)
        return _response(400, {"message": "Atlassian rejected the sign-in. Try again."})
    except urllib.error.URLError as err:
        logger.error("Atlassian token exchange request failed: %s", err)
        return _response(502, {"message": "Could not reach Atlassian"})

    access_token = token_resp.get("access_token")
    if not access_token:
        logger.warning("Atlassian token response had no access_token: %s", token_resp)
        return _response(400, {"message": "Atlassian did not return an access token"})

    try:
        resources = _get_json(
            "https://api.atlassian.com/oauth/token/accessible-resources",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
    except (urllib.error.URLError, urllib.error.HTTPError) as err:
        logger.error("accessible-resources lookup failed: %s", err)
        return _response(502, {"message": "Signed in, but could not look up your Atlassian site"})

    if not resources:
        return _response(400, {"message": "That Atlassian account has no accessible Jira/Confluence site"})

    site = resources[0]  # first site the token was granted against

    _store_secret(f"{SECRET_PREFIX}/kb-connect/atlassian", {
        "access_token": access_token,
        "refresh_token": token_resp.get("refresh_token"),
        "expires_in": token_resp.get("expires_in"),
        "scope": token_resp.get("scope"),
        "cloud_id": site.get("id"),
        "site_url": site.get("url"),
    })
    return _response(200, {"connected": True, "source": "atlassian"})


ROUTES = {
    "/sources/slackhistory/connect": _connect_slack,
    "/sources/atlassian/connect": _connect_atlassian,
}


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "POST")
    path = event.get("rawPath", "")

    handler = next((fn for route, fn in ROUTES.items() if path.endswith(route)), None)
    if method != "POST" or handler is None:
        return _response(404, {"message": "Not found"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"message": "Invalid JSON body"})

    code = body.get("code")
    redirect_uri = body.get("redirect_uri")
    if not code or not redirect_uri:
        return _response(400, {"message": "code and redirect_uri are required"})

    return handler(code, redirect_uri)
