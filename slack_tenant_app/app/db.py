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

from app.config import Config

_lock = threading.Lock()

# DynamoDB's boto3 serializer rejects native Python floats outright
# ("Float types are not supported. Use Decimal types instead.") — every
# write site below that stores a time.time() value needs this, not just
# one of them, so it's a shared helper rather than repeating
# Decimal(str(...)) at each call site.
def _now():
    return Decimal(str(time.time()))


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

    def create_tenant(self, slack_team_id, name, email):
        with _lock:
            tenant_id = str(uuid.uuid4())
            tenant = {
                "tenant_id": tenant_id,
                "slack_team_id": slack_team_id,
                "name": name,
                "email": email,
                "status": "active",
                "created_at": time.time(),
                "connected_services": {},
            }
            self.tenants[tenant_id] = tenant
            self.slack_team_index[slack_team_id] = tenant_id
            self.intents[tenant_id] = {}
            self.conversations[tenant_id] = []
            return tenant

    def get_tenant(self, tenant_id):
        return self.tenants.get(tenant_id)

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

    def get_tenant_by_slack_team(self, slack_team_id):
        resp = self.tenants_table.query(
            IndexName="slack_team_id-index",
            KeyConditionExpression="slack_team_id = :t",
            ExpressionAttributeValues={":t": slack_team_id},
        )
        items = resp.get("Items", [])
        return items[0] if items else None

    def create_tenant(self, slack_team_id, name, email):
        tenant_id = str(uuid.uuid4())
        tenant = {
            "tenant_id": tenant_id,
            "slack_team_id": slack_team_id,
            "name": name,
            "email": email,
            "status": "active",
            "created_at": _now(),
            "connected_services": {},
        }
        self.tenants_table.put_item(Item=tenant)
        return tenant

    def get_tenant(self, tenant_id):
        resp = self.tenants_table.get_item(Key={"tenant_id": tenant_id})
        return resp.get("Item")

    def mark_service_connected(self, tenant_id, service_name, credentials=None):
        # TODO(security): the architecture doc requires connector OAuth
        # tokens to be encrypted at rest (KMS), not stored as a plain
        # DynamoDB attribute. This mirrors InMemoryStore's shape so the two
        # stores stay interchangeable, but the real-AWS path needs a KMS
        # encrypt/decrypt wrapper around `credentials` before this ships —
        # tracked here rather than silently shipped as plaintext.
        self.tenants_table.update_item(
            Key={"tenant_id": tenant_id},
            UpdateExpression="SET connected_services.#s = :v",
            ExpressionAttributeNames={"#s": service_name},
            ExpressionAttributeValues={":v": {
                "connected_at": _now(),
                "credentials": credentials or {},
                "intents_added": [],
            }},
        )

    def get_service_credentials(self, tenant_id, service_name):
        tenant = self.get_tenant(tenant_id) or {}
        service = tenant.get("connected_services", {}).get(service_name) or {}
        return service.get("credentials") or {}

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
