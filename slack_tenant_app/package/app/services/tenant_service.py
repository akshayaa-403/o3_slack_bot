"""
Tenant resolution: deliberately separate from authentication.

Slack OAuth answers "who is this". This module answers "which tenant
does that map to, creating one if needed" — kept apart so tenant
creation stays a single, idempotent, testable code path.

Creating a tenant spans several fallible operations (write the row,
provision a bucket, seed intents). They are run as a saga so that a
failure part-way leaves nothing behind: see app/services/onboarding.py
for why rollback is preferred over resume.
"""
import logging
import uuid

from app.db import WorkspaceAlreadyClaimed, store
from app.services import provisioning
from app.services.onboarding import OnboardingError, Saga

logger = logging.getLogger(__name__)


def get_or_create_tenant(slack_team_id: str, team_name: str, user_email: str, bot_access_token: str) -> dict:
    existing = store.get_tenant_by_slack_team(slack_team_id)
    if existing:
        return existing, False

    # Reserve the workspace before creating anything. The lookup above is not
    # enough on its own: two installs of the same workspace arriving together
    # can both see "no tenant" and both proceed. The claim is a conditional
    # write, so exactly one of them wins and the loser adopts the winner's
    # tenant rather than creating a duplicate.
    tenant_id = str(uuid.uuid4())
    try:
        store.claim_workspace(slack_team_id, tenant_id)
    except WorkspaceAlreadyClaimed as claimed:
        logger.info(
            "Workspace %s already onboarded as tenant %s; reusing it",
            slack_team_id, claimed.tenant_id,
        )
        existing = store.get_tenant(claimed.tenant_id) if claimed.tenant_id else None
        if existing:
            return existing, False
        # Claimed but the tenant row is gone — a previous rollback that could
        # not clean up. Release the stale claim so this install can proceed.
        store.release_workspace(slack_team_id)
        store.claim_workspace(slack_team_id, tenant_id)

    saga = Saga()
    tenant = None
    try:
        # Step 1 — the tenant row. Its compensation deletes the row, which
        # is what stops a failed attempt from leaving a record that makes
        # the lookup above short-circuit on every future try.
        tenant = saga.run(
            "create_tenant",
            lambda: store.create_tenant(
                slack_team_id=slack_team_id,
                name=team_name,
                email=user_email,
                bot_access_token=bot_access_token,
                tenant_id=tenant_id,
            ),
            compensate=lambda: store.delete_tenant(tenant_id),
        )

        # Step 2 — bucket + seeded intents. Provisioning is idempotent, so a
        # retry after a partial failure re-runs it safely.
        saga.run(
            "provision_tenant",
            lambda: provisioning.provision_tenant(tenant["tenant_id"]),
            compensate=lambda: store.delete_all_intents(tenant["tenant_id"]),
        )
    except Exception as exc:
        leftovers = saga.rollback()
        # Release the claim last, after the rest of the rollback. Holding it
        # until then keeps a concurrent install from racing into a
        # half-dismantled tenant; dropping it at all is what stops a failed
        # attempt from locking this workspace out of ever installing again.
        try:
            store.release_workspace(slack_team_id)
        except Exception:
            leftovers.append("workspace_claim")
            logger.error(
                "Could not release workspace claim for %s — it will block "
                "reinstall until cleared manually", slack_team_id,
            )
        if leftovers:
            logger.error(
                "Onboarding rollback incomplete for team %s; needs manual "
                "cleanup of: %s", slack_team_id, ", ".join(leftovers),
            )
        raise OnboardingError("create_tenant", exc) from exc

    return tenant, True
