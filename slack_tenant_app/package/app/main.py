import logging
import secrets
from datetime import timedelta

from flask import Flask, request, redirect, render_template, jsonify, session
from app.config import Config
from app.auth import slack_oauth
from app.services import tenant_service, qa_service, onboarding_card
from app.services.onboarding import OnboardingError
from app.services.connectors.sharepoint import register_connectors
from app.db import store

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = Config.SECRET_KEY

# The session cookie carries the OAuth `state` CSRF token across the Slack
# round trip, so it needs protecting in its own right.
#   Secure   — never send it over plain HTTP.
#   HttpOnly — no script access; nothing client-side reads it.
#   SameSite=Lax — the Slack callback is a cross-site top-level GET, which
#     Lax still permits. Strict would drop the cookie on exactly that
#     redirect and break every install. Set explicitly rather than leaning
#     on the browser default.
# Secure is conditional: it would stop the cookie working on plain-HTTP
# localhost during local dev.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=Config.SLACK_REDIRECT_URI.startswith("https://"),
    # Set explicitly rather than inheriting Flask's silent 31-day default:
    # this is how long a signed-in admin stays signed in, which is a real
    # security decision, not an implementation detail.
    PERMANENT_SESSION_LIFETIME=timedelta(days=Config.SESSION_LIFETIME_DAYS),
)

CONNECTORS = register_connectors()


# ---------------------------------------------------------------------------
# Step 1-2: Login page with a single "Continue with Slack" button
# ---------------------------------------------------------------------------
SLACK_STATE_KEY = "slack_oauth_state"
# Set once the Slack round trip succeeds; lets a returning user land on their
# dashboard without re-authorizing. Cleared by /logout.
TENANT_SESSION_KEY = "tenant_id"

# A tenant whose plan lapsed (or who was suspended) must be turned away with
# something meaningful rather than silently failing or 500ing. Anything other
# than "active" is treated as blocked: a status we don't recognise is a
# problem, so failing closed is the safe reading.
ACTIVE_STATUS = "active"


def resolve_active_tenant(tenant_id):
    """Look up a tenant and enforce its status.

    Returns (tenant, None) when the caller may proceed, or (None, response)
    with a ready-to-return error. Centralised so a new tenant-scoped route
    cannot forget the status check — the gate lives in one place instead of
    being re-implemented per route.
    """
    tenant = store.get_tenant(tenant_id) if tenant_id else None
    if not tenant:
        return None, ("Unknown tenant", 404)

    status = tenant.get("status", ACTIVE_STATUS)
    if status != ACTIVE_STATUS:
        # 403, not 402/404: the tenant exists and is authenticated, they are
        # simply not permitted right now.
        return None, (
            render_template("inactive.html", tenant=tenant, status=status,
                            contact_email=Config.SUPPORT_EMAIL),
            403,
        )
    return tenant, None


# ---------------------------------------------------------------------------
# Public legal/support pages. The Slack Marketplace requires a reachable
# privacy policy and a support contact that works without creating an
# account, so these are unauthenticated by design.
# ---------------------------------------------------------------------------
@app.route("/privacy")
def privacy():
    return render_template(
        "privacy.html",
        contact_email=Config.SUPPORT_EMAIL,
        last_updated=Config.POLICY_LAST_UPDATED,
    )


@app.route("/support")
def support():
    return render_template("support.html", contact_email=Config.SUPPORT_EMAIL)


@app.route("/login")
def login():
    # Already signed in? Go straight to the dashboard rather than sending the
    # user back through Slack's authorize screen. Re-consenting on every visit
    # is the thing this avoids: the install already happened, and the session
    # cookie is proof of it.
    tenant_id = session.get(TENANT_SESSION_KEY)
    if tenant_id and store.get_tenant(tenant_id):
        return redirect(f"/dashboard?tenant_id={tenant_id}")

    state = slack_oauth.generate_state()
    session[SLACK_STATE_KEY] = state
    return render_template(
        "login.html", authorize_url=slack_oauth.build_authorize_url(state)
    )


@app.route("/logout")
def logout():
    """Clear the local session.

    Deliberately *only* local: this does not revoke the Slack install or
    delete the tenant, because signing out of a dashboard should not
    uninstall the bot for the whole workspace. Uninstalling is a separate,
    much more destructive action that belongs in Slack's own app settings.
    """
    session.clear()
    return redirect("/login")


# ---------------------------------------------------------------------------
# Step 3-4: Slack OAuth callback -> authenticate, resolve/create tenant,
# provision infra (bucket + seed intents) for new tenants.
# ---------------------------------------------------------------------------
@app.route("/auth/slack/callback")
def slack_callback():
    code = request.args.get("code")
    if not code:
        return "Missing OAuth code", 400

    # CSRF check: the state we handed to Slack in /login must come back
    # unchanged. Enforced only for a real Slack app — mock mode is driven by
    # hitting this callback directly (?code=demo123) with no prior /login,
    # so there is no state to compare and requiring one would break the
    # credential-free demo path on purpose.
    if not slack_oauth.is_mock_mode():
        expected = session.pop(SLACK_STATE_KEY, None)
        received = request.args.get("state")
        if not expected or not received or not secrets.compare_digest(expected, received):
            return "Invalid OAuth state", 400

    identity = slack_oauth.exchange_code_for_token(code)
    try:
        tenant, is_new = tenant_service.get_or_create_tenant(
            slack_team_id=identity["slack_team_id"],
            team_name=identity["team_name"],
            user_email=identity["user_email"],
            bot_access_token=identity["bot_access_token"],
        )
    except OnboardingError:
        # Everything the failed attempt created has been rolled back, so
        # "try again" genuinely starts clean rather than colliding with a
        # half-built tenant. Nothing internal is surfaced to the user.
        logger.exception("Onboarding failed for team %s", identity.get("slack_team_id"))
        return render_template("onboarding_failed.html",
                               contact_email=Config.SUPPORT_EMAIL), 503

    # Remember the tenant so a later visit to /login skips the Slack round
    # trip entirely — permission is asked for once, not on every return.
    session[TENANT_SESSION_KEY] = tenant["tenant_id"]
    session.permanent = True

    dashboard_url = (
        f"{Config.PUBLIC_BASE_URL}/dashboard?tenant_id={tenant['tenant_id']}"
    )

    # Only on a genuinely new install. Someone revisiting /login should not
    # get the welcome card again — the tenant already exists and they have
    # seen it once.
    if is_new:
        onboarding_card.send_welcome_dm(
            bot_token=identity.get("bot_access_token"),
            user_id=identity.get("installer_user_id"),
            team_name=identity.get("team_name") or "your workspace",
            dashboard_url=dashboard_url,
        )

    return redirect(f"/dashboard?tenant_id={tenant['tenant_id']}&new={is_new}")


@app.route("/dashboard")
def dashboard():
    tenant_id = request.args.get("tenant_id")
    tenant, error = resolve_active_tenant(tenant_id)
    if error:
        return error

    # Built from the connector registry itself, not a separate hardcoded
    # list — a connector that isn't registered never appears here, which is
    # the server-side half of "unbuilt connectors stay hidden" (the other
    # half is that /connectors/<service>/connect 400s for anything not in
    # CONNECTORS, see below).
    intents = store.get_intents(tenant_id)

    connectors = []
    for c in CONNECTORS.values():
        entry = {
            "id": c.name,
            "name": c.display_name or c.name,
            "kind": c.kind,
            "color": c.color,
            "logo": c.logo,
            "blurb": c.blurb,
            "feeds": c.feeds,
        }
        products = getattr(c, "PRODUCTS", None)
        if products:
            # Sub-tiles (Jira/Confluence under one Atlassian sign-in): count
            # how many synced intents belong to each, straight from the
            # tenant's own intents rather than a separate tracked total —
            # one source of truth, no second place that can drift from it.
            # `state` (off/indexed/failed) drives which button the tile
            # shows, same three states kb-connect's actionFor() switches on.
            product_states = store.get_product_states(tenant_id, c.name)
            entry["products"] = [
                {
                    **p,
                    "count": sum(1 for i in intents.values() if i.get("product") == p["id"]),
                    "state": product_states.get(p["id"], {}).get("state", "off"),
                    "error": product_states.get(p["id"], {}).get("error"),
                }
                for p in products
            ]
        connectors.append(entry)

    # `slack://` opens the desktop client straight to this workspace. The
    # team id is what makes it land in the right one for people signed in to
    # several. Browsers that cannot handle the scheme fall back to app.slack.com.
    slack_team_id = tenant.get("slack_team_id") or ""
    slack_workspace_url = (
        f"slack://open?team={slack_team_id}" if slack_team_id
        else "https://app.slack.com/client"
    )

    return render_template(
        "dashboard.html",
        tenant=tenant,
        connectors=connectors,
        slack_workspace_url=slack_workspace_url,
        slack_web_fallback_url=(
            f"https://app.slack.com/client/{slack_team_id}" if slack_team_id
            else "https://app.slack.com/client"
        ),
    )


# ---------------------------------------------------------------------------
# Step 5: Optional connector tiles (Atlassian, SharePoint)
# ---------------------------------------------------------------------------
@app.route("/connectors/<service>/connect")
def connector_connect(service):
    tenant_id = request.args.get("tenant_id")
    connector = CONNECTORS.get(service)
    if not connector or not tenant_id:
        return "Unknown connector or missing tenant_id", 400
    _, error = resolve_active_tenant(tenant_id)
    if error:
        return error
    return redirect(connector.build_authorize_url(tenant_id))


@app.route("/connectors/<service>/callback")
def connector_callback(service):
    # In this mock flow, tenant_id is passed as `state`; in production
    # Slack/Atlassian/Microsoft return whatever you sent as `state` verbatim.
    tenant_id = request.args.get("state") or request.args.get("tenant_id")
    code = request.args.get("code", "mock-code")
    connector = CONNECTORS.get(service)
    if not connector or not tenant_id:
        return "Unknown connector or missing tenant_id", 400
    _, error = resolve_active_tenant(tenant_id)
    if error:
        return error

    summary = connector.handle_callback(tenant_id, code)
    store.record_sync_result(tenant_id, service, summary.get("intents_added", []))
    return redirect(f"/dashboard?tenant_id={tenant_id}&connected={service}")


# ---------------------------------------------------------------------------
# Sub-tile actions: Enable / Read again / Disconnect for one product
# (Jira, Confluence) under an already-connected account. Deliberately
# separate from /connectors/<service>/connect — these never touch the
# account's own OAuth token, so disabling Confluence can never accidentally
# sign the tenant out of Atlassian or drop Jira with it.
# ---------------------------------------------------------------------------
@app.route("/connectors/<service>/products/<product_id>/enable", methods=["POST"])
def product_enable(service, product_id):
    tenant_id = request.args.get("tenant_id")
    connector = CONNECTORS.get(service)
    if not connector or not tenant_id:
        return "Unknown connector, product, or tenant_id", 400
    tenant, error = resolve_active_tenant(tenant_id)
    if error:
        return error
    if service not in tenant.get("connected_services", {}):
        return "Service is not connected", 400

    connector.sync_product(tenant_id, product_id)
    return redirect(f"/dashboard?tenant_id={tenant_id}")


@app.route("/connectors/<service>/products/<product_id>/disable", methods=["POST"])
def product_disable(service, product_id):
    tenant_id = request.args.get("tenant_id")
    connector = CONNECTORS.get(service)
    if not connector or not tenant_id or not store.get_tenant(tenant_id):
        return "Unknown connector, product, or tenant_id", 400

    # Deliberately not status-gated. Disconnecting is how a tenant withdraws
    # our access to their data, so an expired plan must not trap them in a
    # state where we keep reading Jira/Confluence and they cannot stop it.
    connector.disable_product(tenant_id, product_id)
    return redirect(f"/dashboard?tenant_id={tenant_id}")


# ---------------------------------------------------------------------------
# Step 6: Tenant-scoped question answering (LLM never named to the client)
# ---------------------------------------------------------------------------
@app.route("/api/query", methods=["POST"])
def api_query():
    body = request.get_json(force=True)
    tenant_id = body.get("tenant_id")
    question = body.get("question")
    if not tenant_id or not question:
        return jsonify({"error": "tenant_id and question are required"}), 400
    # JSON callers get a JSON error, not the HTML inactive page.
    tenant = store.get_tenant(tenant_id)
    if not tenant:
        return jsonify({"error": "unknown tenant"}), 404
    if tenant.get("status", ACTIVE_STATUS) != ACTIVE_STATUS:
        return jsonify({
            "error": "tenant inactive",
            "status": tenant.get("status"),
            "message": "This workspace's IvvY plan is not active. Contact support to restore access.",
        }), 403

    answer = qa_service.answer_question(tenant_id, question)
    return jsonify({"answer": answer})  # no model/provider field, by design


# ---------------------------------------------------------------------------
# Periodic job endpoint: regenerate intents from conversation history
# (in production this is a scheduled worker, exposed here for demo purposes)
# ---------------------------------------------------------------------------
@app.route("/internal/regenerate-intents/<tenant_id>", methods=["POST"])
def regenerate_intents(tenant_id):
    if not store.get_tenant(tenant_id):
        return jsonify({"error": "unknown tenant"}), 404
    result = qa_service.regenerate_intents_from_conversations(tenant_id)
    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
