"""
The Block Kit card IvvY DMs the installing admin right after install.

Why a card at all: without one, installing from the Marketplace drops the
admin on a web page and leaves nothing behind in Slack. The card is what
makes the app feel installed — it lands in their DMs, says what IvvY can
already do, and points at the browser for the parts that need OAuth.

Why the buttons are plain URL links: a Slack app has exactly one
interactivity Request URL, and this workspace's is already pointed at the
production bot. `url`-type buttons are handled by Slack itself and never
call back to us, so this card works with no interactivity endpoint and no
change to the existing app's configuration.

Setup deliberately happens in the browser rather than in Slack. Connecting
Jira or Confluence is a full OAuth consent flow with a redirect, which
cannot be completed inside a Slack modal — and the browser is where the
tenant dashboard, per-product toggles and sign-out already live.
"""
import logging

import requests

logger = logging.getLogger(__name__)

SLACK_POST_MESSAGE = "https://slack.com/api/chat.postMessage"


def build_welcome_blocks(team_name: str, dashboard_url: str) -> list:
    """The post-install card. Leads with what already works, so the setup
    link reads as an upgrade rather than a prerequisite."""
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "IvvY is ready \U0001f44b", "emoji": True},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"Thanks for adding me to *{team_name}*. You can ask me IT and "
                    "HR questions right here in Slack — just send me a message or "
                    "mention me in a channel.\n\n"
                    "I already answer from IvvY's general knowledge base. Connect "
                    "your own tools and I'll learn from those too."
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Connect your knowledge sources*\n"
                    "Jira, Confluence, SharePoint or G Suite. Setup happens in your "
                    "browser because each one needs you to sign in and approve access."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Set up my sources", "emoji": True},
                    # A url button is resolved by Slack itself, so no
                    # interactivity Request URL is involved.
                    "url": dashboard_url,
                    "action_id": "open_setup_dashboard",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Not now", "emoji": True},
                    "url": f"{dashboard_url}&skip=1",
                    "action_id": "skip_setup",
                },
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        ":bulb: You don't have to connect anything — try asking me "
                        "_\"how do I reset my password?\"_ right now. "
                        "IvvY uses AI, so double-check anything important."
                    ),
                }
            ],
        },
    ]


def send_welcome_dm(bot_token: str, user_id: str, team_name: str, dashboard_url: str) -> bool:
    """DM the card to the installing admin.

    Best-effort by design: onboarding has already succeeded by the time this
    runs, so a Slack hiccup here must not fail the install or send the user
    to an error page. Returns whether it posted, and logs why if not.
    """
    if not bot_token or not user_id:
        logger.info("Skipping welcome DM: no bot token or installer user id")
        return False

    try:
        resp = requests.post(
            SLACK_POST_MESSAGE,
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={
                # Slack opens the IM conversation implicitly when a user id
                # is used as the channel.
                "channel": user_id,
                # Fallback for notifications and screen readers, which do not
                # render blocks.
                "text": f"IvvY is ready. Set up your knowledge sources: {dashboard_url}",
                "blocks": build_welcome_blocks(team_name, dashboard_url),
            },
            timeout=10,
        )
        payload = resp.json()
    except requests.RequestException as exc:
        logger.warning("Welcome DM failed to send: %s", exc.__class__.__name__)
        return False

    if not payload.get("ok"):
        # Most likely causes: the bot lacks chat:write, or the installing
        # user cannot be DM'd. Neither is worth failing the install over.
        logger.warning("Welcome DM rejected by Slack: %s", payload.get("error"))
        return False

    return True
