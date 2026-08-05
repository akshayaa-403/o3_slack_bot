from flask import Flask, request, redirect, render_template, jsonify
from app.config import Config
from app.auth import slack_oauth
from app.services import tenant_service, qa_service
from app.services.connectors.sharepoint import register_connectors
from app.db import store

app = Flask(__name__)
app.secret_key = Config.SECRET_KEY

CONNECTORS = register_connectors()


# ---------------------------------------------------------------------------
# Step 1-2: Login page with a single "Continue with Slack" button
# ---------------------------------------------------------------------------
@app.route("/login")
def login():
    return render_template("login.html", authorize_url=slack_oauth.build_authorize_url())


# ---------------------------------------------------------------------------
# Step 3-4: Slack OAuth callback -> authenticate, resolve/create tenant,
# provision infra (bucket + seed intents) for new tenants.
# ---------------------------------------------------------------------------
@app.route("/auth/slack/callback")
def slack_callback():
    code = request.args.get("code")
    if not code:
        return "Missing OAuth code", 400

    identity = slack_oauth.exchange_code_for_token(code)
    tenant, is_new = tenant_service.get_or_create_tenant(
        slack_team_id=identity["slack_team_id"],
        team_name=identity["team_name"],
        user_email=identity["user_email"],
        bot_access_token=identity["bot_access_token"],
    )

    return redirect(f"/dashboard?tenant_id={tenant['tenant_id']}&new={is_new}")


@app.route("/dashboard")
def dashboard():
    tenant_id = request.args.get("tenant_id")
    tenant = store.get_tenant(tenant_id)
    if not tenant:
        return "Unknown tenant", 404

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

    return render_template("dashboard.html", tenant=tenant, connectors=connectors)


# ---------------------------------------------------------------------------
# Step 5: Optional connector tiles (Atlassian, SharePoint)
# ---------------------------------------------------------------------------
@app.route("/connectors/<service>/connect")
def connector_connect(service):
    tenant_id = request.args.get("tenant_id")
    connector = CONNECTORS.get(service)
    if not connector or not tenant_id:
        return "Unknown connector or missing tenant_id", 400
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
    if not connector or not tenant_id or not store.get_tenant(tenant_id):
        return "Unknown connector, product, or tenant_id", 400
    if service not in store.get_tenant(tenant_id).get("connected_services", {}):
        return "Service is not connected", 400

    connector.sync_product(tenant_id, product_id)
    return redirect(f"/dashboard?tenant_id={tenant_id}")


@app.route("/connectors/<service>/products/<product_id>/disable", methods=["POST"])
def product_disable(service, product_id):
    tenant_id = request.args.get("tenant_id")
    connector = CONNECTORS.get(service)
    if not connector or not tenant_id or not store.get_tenant(tenant_id):
        return "Unknown connector, product, or tenant_id", 400

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
    if not store.get_tenant(tenant_id):
        return jsonify({"error": "unknown tenant"}), 404

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
