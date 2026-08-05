"""
Tenant resolution: deliberately separate from authentication.

Slack OAuth answers "who is this". This module answers "which tenant
does that map to, creating one if needed" — kept apart so tenant
creation stays a single, idempotent, testable code path.
"""
from app.db import store
from app.services import provisioning


def get_or_create_tenant(slack_team_id: str, team_name: str, user_email: str, bot_access_token: str) -> dict:
    existing = store.get_tenant_by_slack_team(slack_team_id)
    if existing:
        return existing, False

    tenant = store.create_tenant(slack_team_id=slack_team_id, name=team_name, email=user_email)
    tenant["bot_access_token"] = bot_access_token  # in production: encrypt before persisting

    # New tenant -> kick off infra provisioning (S3 buckets, seed intents).
    # Modeled as a discrete, idempotent step, not inline in the OAuth callback,
    # so it can be retried/queued in production without blocking the redirect.
    provisioning.provision_tenant(tenant["tenant_id"])

    return tenant, True
