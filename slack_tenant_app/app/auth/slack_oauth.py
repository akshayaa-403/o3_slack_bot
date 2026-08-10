"""
Slack OAuth: single entry point for both authentication and bot install.

Deliberately has no "enter your workspace name" step: the flow installs
into whichever workspace the user is already authenticated against. A
free-text workspace field would let someone aim the install at a
workspace they hold no rights in (e.g. targeting production while only
authorized for a sandbox), so Slack's own session is the source of truth
for which workspace this is.

In dev/mock mode (no real Slack app configured) this simulates the token
exchange so the rest of the flow can be exercised end to end without a
live Slack app.
"""
import secrets
from urllib.parse import urlencode

import requests
from app.config import Config

MOCK_CLIENT_ID = "dev-client-id"


def is_mock_mode() -> bool:
    return Config.SLACK_CLIENT_ID == MOCK_CLIENT_ID


def generate_state() -> str:
    """CSRF token for the authorize round trip.

    Slack's install-flow guidance requires `state`, and Marketplace review
    checks for it: without it an attacker can hand a victim a crafted
    callback URL and graft their own workspace install onto the victim's
    session.
    """
    return secrets.token_urlsafe(32)


def build_authorize_url(state: str | None = None):
    params = {
        "client_id": Config.SLACK_CLIENT_ID,
        "scope": Config.SLACK_BOT_SCOPES,
        "user_scope": Config.SLACK_USER_SCOPES,
        "redirect_uri": Config.SLACK_REDIRECT_URI,
    }
    if state:
        params["state"] = state
    # urlencode so scope's commas and the redirect URI's :// are escaped
    # properly rather than pasted raw into the query string.
    return "https://slack.com/oauth/v2/authorize?" + urlencode(params)


def exchange_code_for_token(code: str) -> dict:
    """Exchanges the OAuth `code` for workspace + user identity info.

    Returns a normalized dict: slack_team_id, team_name, user_email, bot_access_token.
    """
    if is_mock_mode():
        # Mock mode: no real Slack credentials configured.
        return {
            "slack_team_id": f"T-MOCK-{code[:8]}",
            "team_name": "Acme Corp (mock workspace)",
            "user_email": "admin@acme-corp.example",
            "bot_access_token": f"xoxb-mock-{code[:8]}",
            "installer_user_id": "U-MOCK-INSTALLER",
        }

    resp = requests.post(
        "https://slack.com/api/oauth.v2.access",
        data={
            "client_id": Config.SLACK_CLIENT_ID,
            "client_secret": Config.SLACK_CLIENT_SECRET,
            "code": code,
            "redirect_uri": Config.SLACK_REDIRECT_URI,
        },
        timeout=10,
    )
    payload = resp.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Slack OAuth failed: {payload.get('error')}")

    bot_token = payload["access_token"]
    authed_user = payload.get("authed_user") or {}

    return {
        "slack_team_id": payload["team"]["id"],
        "team_name": payload["team"]["name"],
        "user_email": _lookup_user_email(bot_token, authed_user.get("id")),
        "bot_access_token": bot_token,
        # Who to DM the setup card to. The installing admin is the right
        # recipient: they are the person who just chose to add IvvY, and the
        # only one who can complete the connector setup.
        "installer_user_id": authed_user.get("id"),
    }


def _lookup_user_email(bot_token: str, user_id: str | None) -> str:
    """Resolve the installing admin's email.

    oauth.v2.access does NOT return an email in `authed_user` — it only
    carries the user id (and a user token when user_scope was granted), so
    reading `authed_user["email"]` always fell through to the default.
    users.info returns the email when the bot holds users:read.email.

    Email is non-essential to onboarding (the tenant is keyed on team id),
    so a lookup failure degrades to a placeholder rather than failing the
    whole install.
    """
    if not user_id:
        return "unknown@workspace"

    try:
        resp = requests.get(
            "https://slack.com/api/users.info",
            headers={"Authorization": f"Bearer {bot_token}"},
            params={"user": user_id},
            timeout=10,
        )
        payload = resp.json()
    except requests.RequestException:
        return "unknown@workspace"

    if not payload.get("ok"):
        return "unknown@workspace"

    profile = (payload.get("user") or {}).get("profile") or {}
    return profile.get("email") or "unknown@workspace"
