"""
Data access layer.

Every query is tenant-scoped by design: callers pass tenant_id explicitly
and table helpers key off it, so there is no path that accidentally reads
across tenants.

In AWS mode this talks to real DynamoDB tables (tenants, intents,
conversations). In local/dev mode it uses in-memory dicts so the whole
onboarding flow is runnable without any cloud setup.
"""
import time
import uuid
import threading
from decimal import Decimal

from app import crypto
from app.config import Config

_lock = threading.Lock()

# DynamoDB's boto3 serializer rejects native Python floats outright
# ("Float types are not supported. Use Decimal types instead.") — every
# write site below that stores a time.time() value needs this, not just
# one of them, so it's a shared helper rather than repeating
# Decimal(str(...)) at each call site.
def _now():
    return Decimal(str(time.time()))


class WorkspaceAlreadyClaimed(Exception):
    """Raised when a Slack workspace already belongs to a tenant.

    Carries the winning tenant_id so the caller can adopt it instead of
    treating a lost race as a failure — a second install of the same
    workspace should land on the existing tenant, not error.
    """

    def __init__(self, slack_team_id, tenant_id=None):
        super().__init__(
            f"Slack workspace {slack_team_id} is already claimed by tenant {tenant_id}"
        )
        self.slack_team_id = slack_team_id
        self.tenant_id = tenant_id


class InMemoryStore:
    def __init__(self):
        self.tenants = {}          # tenant_id -> tenant dict
        self.slack_team_index = {}  # slack_team_id -> tenant_id
        self.intents = {}          # tenant_id -> {intent_id: intent dict}
        self.conversations = {}    # tenant_id -> [ {question, answer, ts} ]

    # --- tenants ---
    def get_tenant_by_slack_team(self, slack_team_id):
        tid = self.slack_team_index.get(slack_team_id)
        return self.tenants.get(tid) if tid else None

    def claim_workspace(self, slack_team_id, tenant_id):
        # The lock is what makes this the same atomic check-and-set the
        # DynamoDB conditional write provides.
        with _lock:
            existing = self.slack_team_index.get(slack_team_id)
            if existing and existing != tenant_id:
                raise WorkspaceAlreadyClaimed(slack_team_id, existing)
            self.slack_team_index[slack_team_id] = tenant_id

    def release_workspace(self, slack_team_id):
        with _lock:
            self.slack_team_index.pop(slack_team_id, None)

    def create_tenant(self, slack_team_id, name, email, bot_access_token=None,
                      tenant_id=None):
        with _lock:
            tenant_id = tenant_id or str(uuid.uuid4())
            tenant = {
                "tenant_id": tenant_id,
                "slack_team_id": slack_team_id,
                "name": name,
                "email": email,
                "status": "active",
                "created_at": time.time(),
                "connected_services": {},
                "bot_access_token": bot_access_token,
            }
            self.tenants[tenant_id] = tenant
            self.slack_team_index[slack_team_id] = tenant_id
            self.intents[tenant_id] = {}
            self.conversations[tenant_id] = []
            return tenant

    def get_tenant(self, tenant_id):
        return self.tenants.get(tenant_id)

    def get_bot_access_token(self, tenant_id):
        return (self.tenants.get(tenant_id) or {}).get("bot_access_token")

    def set_tenant_status(self, tenant_id, status):
        tenant = self.tenants.get(tenant_id)
        if tenant is not None:
            tenant["status"] = status

    def set_answering_config(self, tenant_id, answering):
        tenant = self.tenants.get(tenant_id)
        if tenant is not None:
            tenant["answering"] = answering

    def get_answering_config(self, tenant_id):
        return (self.tenants.get(tenant_id) or {}).get("answering") or {}

    def delete_tenant(self, tenant_id):
        with _lock:
            tenant = self.tenants.pop(tenant_id, None)
            if tenant:
                # Drop the secondary index entry too, or a retry of the same
                # workspace would resolve to a tenant row that no longer exists.
                self.slack_team_index.pop(tenant.get("slack_team_id"), None)
            self.intents.pop(tenant_id, None)
            self.conversations.pop(tenant_id, None)

    def delete_all_intents(self, tenant_id):
        self.intents[tenant_id] = {}

    def mark_service_connected(self, tenant_id, service_name, credentials=None):
        tenant = self.tenants[tenant_id]
        tenant["connected_services"][service_name] = {
            "connected_at": time.time(),
            "credentials": credentials or {},
            "intents_added": [],
        }

    def record_sync_result(self, tenant_id, service_name, intents_added):
        tenant = self.tenants.get(tenant_id)
        service = (tenant or {}).get("connected_services", {}).get(service_name)
        if service is not None:
            service["intents_added"] = intents_added

    def get_service_credentials(self, tenant_id, service_name):
        tenant = self.tenants.get(tenant_id) or {}
        service = tenant.get("connected_services", {}).get(service_name) or {}
        return service.get("credentials") or {}

    # --- per-product state (Jira/Confluence under one Atlassian sign-in) ---
    # A product's own on/off state has to be independent of the account
    # connection itself: signing out of Atlassian drops both, but disabling
    # just Confluence must leave Jira, and the Atlassian token, untouched.
    def set_product_state(self, tenant_id, service_name, product_id, state, error=None):
        tenant = self.tenants.get(tenant_id)
        service = (tenant or {}).get("connected_services", {}).get(service_name)
        if service is None:
            return
        service.setdefault("products", {})[product_id] = {"state": state, "error": error}

    def get_product_state(self, tenant_id, service_name, product_id):
        tenant = self.tenants.get(tenant_id) or {}
        service = tenant.get("connected_services", {}).get(service_name) or {}
        return service.get("products", {}).get(product_id, {}).get("state", "off")

    def get_product_states(self, tenant_id, service_name):
        tenant = self.tenants.get(tenant_id) or {}
        service = tenant.get("connected_services", {}).get(service_name) or {}
        return service.get("products", {})

    def clear_product_intents(self, tenant_id, product_id):
        remaining = {
            k: v for k, v in self.intents.get(tenant_id, {}).items()
            if v.get("product") != product_id
        }
        self.intents[tenant_id] = remaining

    # --- intents ---
    def put_intents(self, tenant_id, intents: dict):
        self.intents.setdefault(tenant_id, {}).update(intents)

    def get_intents(self, tenant_id):
        return self.intents.get(tenant_id, {})

    # --- conversations ---
    def log_conversation(self, tenant_id, question, answer):
        self.conversations.setdefault(tenant_id, []).append(
            {"question": question, "answer": answer, "ts": time.time()}
        )

    def get_conversations(self, tenant_id):
        return self.conversations.get(tenant_id, [])


class DynamoStore:
    """Thin wrapper around boto3 DynamoDB resource. Mirrors InMemoryStore's
    interface so the rest of the app is storage-agnostic."""

    def __init__(self):
        import boto3
        self.ddb = boto3.resource("dynamodb", region_name=Config.AWS_REGION)
        self.tenants_table = self.ddb.Table("tenants")
        self.intents_table = self.ddb.Table("intents")
        self.conversations_table = self.ddb.Table("conversations")
        # One row per Slack workspace, keyed by team id. Exists purely so a
        # conditional write can enforce "one tenant per workspace"; the GSI
        # on `tenants` cannot, being both eventually consistent and non-unique.
        self.workspace_registry_table = self.ddb.Table("tenant_workspace_registry")

    def get_tenant_by_slack_team(self, slack_team_id):
        # The registry is the authority, not the GSI. A global secondary
        # index is eventually consistent, so immediately after an install it
        # can still report "no such workspace" — which is exactly how a
        # workspace ends up with two tenant rows. The registry is a base
        # table, so a read after a successful claim always sees it.
        registry = self.workspace_registry_table.get_item(
            Key={"slack_team_id": slack_team_id},
            ConsistentRead=True,
        ).get("Item")
        if registry:
            return self.get_tenant(registry["tenant_id"])

        # Fall back to the GSI for tenants created before the registry
        # existed, and backfill them so the next lookup is consistent.
        resp = self.tenants_table.query(
            IndexName="slack_team_id-index",
            KeyConditionExpression="slack_team_id = :t",
            ExpressionAttributeValues={":t": slack_team_id},
        )
        items = resp.get("Items", [])
        if not items:
            return None

        legacy = items[0]
        try:
            self.claim_workspace(slack_team_id, legacy["tenant_id"])
        except WorkspaceAlreadyClaimed:
            pass  # another request backfilled it first; harmless
        return legacy

    def claim_workspace(self, slack_team_id, tenant_id):
        """Reserve a workspace for one tenant, or refuse.

        A conditional put is what makes this safe: DynamoDB evaluates the
        condition atomically, so if two installs for the same workspace race,
        exactly one write succeeds and the other raises. Checking-then-writing
        in application code cannot give that guarantee.
        """
        try:
            self.workspace_registry_table.put_item(
                Item={
                    "slack_team_id": slack_team_id,
                    "tenant_id": tenant_id,
                    "claimed_at": _now(),
                },
                ConditionExpression="attribute_not_exists(slack_team_id)",
            )
        except Exception as exc:
            if exc.__class__.__name__ == "ConditionalCheckFailedException":
                existing = self.workspace_registry_table.get_item(
                    Key={"slack_team_id": slack_team_id}, ConsistentRead=True,
                ).get("Item") or {}
                raise WorkspaceAlreadyClaimed(
                    slack_team_id, existing.get("tenant_id")
                ) from exc
            raise

    def release_workspace(self, slack_team_id):
        """Drop a claim. Used when onboarding rolls back, so a failed attempt
        does not permanently lock a workspace out of ever installing."""
        self.workspace_registry_table.delete_item(
            Key={"slack_team_id": slack_team_id}
        )

    def create_tenant(self, slack_team_id, name, email, bot_access_token=None,
                      tenant_id=None):
        # The caller may pass an id it has already reserved in the workspace
        # registry, so the claim and the row agree.
        tenant_id = tenant_id or str(uuid.uuid4())
        tenant = {
            "tenant_id": tenant_id,
            "slack_team_id": slack_team_id,
            "name": name,
            "email": email,
            "status": "active",
            "created_at": _now(),
            "connected_services": {},
            # The workspace bot token is what lets us post back into Slack
            # later, so it has to be persisted at creation — and encrypted
            # like any other OAuth token, not left as a plain attribute.
            "bot_access_token": crypto.encrypt_credentials(
                tenant_id, {"token": bot_access_token} if bot_access_token else {}
            ),
        }
        self.tenants_table.put_item(Item=tenant)
        return tenant

    def get_bot_access_token(self, tenant_id):
        tenant = self.get_tenant(tenant_id) or {}
        return crypto.decrypt_credentials(
            tenant_id, tenant.get("bot_access_token")
        ).get("token")

    def set_tenant_status(self, tenant_id, status):
        """Flip a tenant between active / expired / suspended.

        Billing and admin tooling live outside this app, so this is the seam
        they drive: the status gate reads what this writes.
        """
        self.tenants_table.update_item(
            Key={"tenant_id": tenant_id},
            UpdateExpression="SET #s = :v",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":v": status},
        )

    def set_answering_config(self, tenant_id, answering):
        """Record which Lex bot answers for this tenant.

        The worker reads this to route a message, so it is the seam between
        onboarding here and answering in the existing pipeline.
        """
        self.tenants_table.update_item(
            Key={"tenant_id": tenant_id},
            UpdateExpression="SET answering = :v",
            ExpressionAttributeValues={":v": answering},
        )

    def get_answering_config(self, tenant_id):
        return (self.get_tenant(tenant_id) or {}).get("answering") or {}

    def delete_tenant(self, tenant_id):
        """Remove a tenant row. Used to roll back a failed onboarding.

        Intentionally narrow: it deletes the tenant record only, and the
        onboarding saga separately undoes whatever else it created. That
        keeps this from becoming an account-deletion helper that a stray
        call could turn into data loss.
        """
        self.tenants_table.delete_item(Key={"tenant_id": tenant_id})

    def delete_all_intents(self, tenant_id):
        existing = self.get_intents(tenant_id)
        if not existing:
            return
        with self.intents_table.batch_writer() as batch:
            for intent_id in existing:
                batch.delete_item(
                    Key={"tenant_id": tenant_id, "intent_id": intent_id}
                )

    def get_tenant(self, tenant_id):
        resp = self.tenants_table.get_item(Key={"tenant_id": tenant_id})
        return resp.get("Item")

    def mark_service_connected(self, tenant_id, service_name, credentials=None):
        # Tokens are encrypted before they reach DynamoDB — see app/crypto.py.
        # Doing it here rather than in each connector means no connector can
        # forget to, and get_service_credentials is the matching decrypt.
        self.tenants_table.update_item(
            Key={"tenant_id": tenant_id},
            UpdateExpression="SET connected_services.#s = :v",
            ExpressionAttributeNames={"#s": service_name},
            ExpressionAttributeValues={":v": {
                "connected_at": _now(),
                "credentials": crypto.encrypt_credentials(tenant_id, credentials or {}),
                "intents_added": [],
            }},
        )

    def get_service_credentials(self, tenant_id, service_name):
        tenant = self.get_tenant(tenant_id) or {}
        service = tenant.get("connected_services", {}).get(service_name) or {}
        return crypto.decrypt_credentials(tenant_id, service.get("credentials"))

    def record_sync_result(self, tenant_id, service_name, intents_added):
        self.tenants_table.update_item(
            Key={"tenant_id": tenant_id},
            UpdateExpression="SET connected_services.#s.intents_added = :v",
            ExpressionAttributeNames={"#s": service_name},
            ExpressionAttributeValues={":v": intents_added},
        )

    def set_product_state(self, tenant_id, service_name, product_id, state, error=None):
        self.tenants_table.update_item(
            Key={"tenant_id": tenant_id},
            UpdateExpression="SET connected_services.#s.products.#p = :v",
            ExpressionAttributeNames={"#s": service_name, "#p": product_id},
            ExpressionAttributeValues={":v": {"state": state, "error": error}},
        )

    def get_product_state(self, tenant_id, service_name, product_id):
        tenant = self.get_tenant(tenant_id) or {}
        service = tenant.get("connected_services", {}).get(service_name) or {}
        return service.get("products", {}).get(product_id, {}).get("state", "off")

    def get_product_states(self, tenant_id, service_name):
        tenant = self.get_tenant(tenant_id) or {}
        service = tenant.get("connected_services", {}).get(service_name) or {}
        return service.get("products", {})

    def clear_product_intents(self, tenant_id, product_id):
        existing = self.get_intents(tenant_id)
        to_delete = [k for k, v in existing.items() if v.get("product") == product_id]
        with self.intents_table.batch_writer() as batch:
            for intent_id in to_delete:
                batch.delete_item(Key={"tenant_id": tenant_id, "intent_id": intent_id})

    def put_intents(self, tenant_id, intents: dict):
        with self.intents_table.batch_writer() as batch:
            for intent_id, body in intents.items():
                batch.put_item(Item={"tenant_id": tenant_id, "intent_id": intent_id, **body})

    def get_intents(self, tenant_id):
        resp = self.intents_table.query(
            KeyConditionExpression="tenant_id = :t",
            ExpressionAttributeValues={":t": tenant_id},
        )
        return {i["intent_id"]: i for i in resp.get("Items", [])}

    def log_conversation(self, tenant_id, question, answer):
        self.conversations_table.put_item(
            Item={"tenant_id": tenant_id, "timestamp": _now(), "question": question, "answer": answer}
        )

    def get_conversations(self, tenant_id):
        resp = self.conversations_table.query(
            KeyConditionExpression="tenant_id = :t",
            ExpressionAttributeValues={":t": tenant_id},
        )
        return resp.get("Items", [])


store = DynamoStore() if Config.USE_AWS else InMemoryStore()
