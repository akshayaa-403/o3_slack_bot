"""
Slack OAuth: single entry point for both authentication and bot install.

In dev/mock mode (no real Slack app configured) this simulates the token
exchange so the rest of the flow can be exercised end to end without a
live Slack app.
"""
import requests
from app.config import Config


def build_authorize_url():
    return (
        "https://slack.com/oauth/v2/authorize"
        f"?client_id={Config.SLACK_CLIENT_ID}"
        f"&scope={Config.SLACK_BOT_SCOPES}"
        f"&user_scope={Config.SLACK_USER_SCOPES}"
        f"&redirect_uri={Config.SLACK_REDIRECT_URI}"
    )


def exchange_code_for_token(code: str) -> dict:
    """Exchanges the OAuth `code` for workspace + user identity info.

    Returns a normalized dict: slack_team_id, team_name, user_email, bot_access_token.
    """
    if Config.SLACK_CLIENT_ID == "dev-client-id":
        # Mock mode: no real Slack credentials configured.
        return {
            "slack_team_id": f"T-MOCK-{code[:8]}",
            "team_name": "Acme Corp (mock workspace)",
            "user_email": "admin@acme-corp.example",
            "bot_access_token": f"xoxb-mock-{code[:8]}",
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

    return {
        "slack_team_id": payload["team"]["id"],
        "team_name": payload["team"]["name"],
        "user_email": payload.get("authed_user", {}).get("email", "unknown@workspace"),
        "bot_access_token": payload["access_token"],
    }
