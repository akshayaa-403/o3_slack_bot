"""Tenant resolution + config store.

Two backends, one interface:

  * LocalConfigStore  - reads a seed JSON file. Zero AWS. Used for the demo and
                        tests, and fine for a single pilot tenant.
  * DynamoConfigStore - reads DynamoDB. The production backend. Pool compute,
                        per-tenant data. boto3 is imported lazily so the local
                        path never needs it.

Selected by env var TENANT_CONFIG_BACKEND (default: "local").

Resolution model:

    (platform, workspace_id) -> tenant_id -> config

  platform    : "slack" | "teams" | "web"
  workspace_id: Slack team_id (T...), Teams AAD tenant id, or a web api-key id
  tenant_id   : our stable internal id ("innovyq")

Why a reverse index and not "scan for the team_id"? Because resolution runs on
every single message. It must be one O(1) keyed lookup, not a table scan.

Caching: configs are cached in-process with a short TTL. Lambda reuses warm
containers, so this collapses ~every-message DynamoDB reads down to one read
per tenant per TTL window. A control-plane update is visible within one TTL.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional, Tuple

from tenant.schema import TenantConfig, validate_tenant_config


# --- errors ------------------------------------------------------------------
class TenantNotFoundError(Exception):
    """The (platform, workspace_id) or tenant_id did not resolve to a tenant."""


class TenantDisabledError(Exception):
    """The tenant exists but its kill switch (`enabled`) is off."""

    def __init__(self, tenant_id: str):
        super().__init__(f"tenant {tenant_id!r} is disabled")
        self.tenant_id = tenant_id


# --- config cache (TTL) ------------------------------------------------------
CONFIG_CACHE_TTL_SECONDS = int(os.environ.get("TENANT_CONFIG_CACHE_TTL_SECONDS", "60"))


class _TtlCache:
    """Tiny in-process TTL cache. Not thread-heavy; Lambda is single-request."""

    def __init__(self, ttl_seconds: int):
        self._ttl = ttl_seconds
        self._data: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.time() >= expires_at:
            self._data.pop(key, None)
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        self._data[key] = (time.time() + self._ttl, value)

    def clear(self) -> None:
        self._data.clear()


# --- secret refs -------------------------------------------------------------
class SecretResolver:
    """Resolves a *_secret_ref (a name, not a secret) to its value.

    In production this calls Secrets Manager. Locally it returns an opaque,
    non-sensitive placeholder so nothing real is ever printed or logged. The
    control plane deliberately never stores raw secrets (see schema.py).
    """

    def __init__(self, backend: str = "local"):
        self._backend = backend
        self._sm = None

    def resolve(self, secret_ref: str) -> str:
        if not secret_ref:
            return ""
        if self._backend != "aws":
            # Local/dev: never fabricate or fetch a real secret.
            return f"<unresolved-secret:{secret_ref}>"
        if self._sm is None:
            import boto3  # lazy: only when actually talking to AWS

            self._sm = boto3.client("secretsmanager")
        response = self._sm.get_secret_value(SecretId=secret_ref)
        return response.get("SecretString", "")


# --- base store --------------------------------------------------------------
class ConfigStore:
    """Interface every backend implements."""

    def resolve_tenant(self, platform: str, workspace_id: str) -> Optional[str]:
        raise NotImplementedError

    def get_config(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def put_config(self, doc: Dict[str, Any]) -> None:
        raise NotImplementedError

    # shared helpers ----------------------------------------------------------
    @staticmethod
    def _index_keys(doc: Dict[str, Any]):
        """Yield ('slack', 'T123'), ('teams', 'aad...'), ('web', 'ref') keys."""
        identity = doc.get("identity", {}) or {}
        for team_id in identity.get("slack_team_ids", []) or []:
            yield ("slack", team_id)
        for aad in identity.get("teams_aad_tenant_ids", []) or []:
            yield ("teams", aad)
        for key_ref in identity.get("web_api_key_refs", []) or []:
            yield ("web", key_ref)


# --- local backend -----------------------------------------------------------
DEFAULT_SEED_PATH = os.path.join(os.path.dirname(__file__), "seed_tenants.json")


class LocalConfigStore(ConfigStore):
    """Seed-file backed store. Builds the reverse index in memory on load."""

    def __init__(self, seed_path: str = DEFAULT_SEED_PATH):
        self._seed_path = seed_path
        self._by_id: Dict[str, Dict[str, Any]] = {}
        self._index: Dict[Tuple[str, str], str] = {}
        self._load()

    def _load(self) -> None:
        with open(self._seed_path, "r", encoding="utf-8") as handle:
            tenants = json.load(handle)
        if isinstance(tenants, dict):
            tenants = tenants.get("tenants", [])
        for doc in tenants:
            validate_tenant_config(doc)  # fail loud on a bad seed
            self.put_config(doc)

    def resolve_tenant(self, platform: str, workspace_id: str) -> Optional[str]:
        if not platform or not workspace_id:
            return None
        return self._index.get((platform, workspace_id))

    def get_config(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        doc = self._by_id.get(tenant_id)
        return dict(doc) if doc is not None else None

    def put_config(self, doc: Dict[str, Any]) -> None:
        validate_tenant_config(doc)
        tenant_id = doc["tenant_id"]
        self._by_id[tenant_id] = dict(doc)
        # Rebuild this tenant's index entries.
        self._index = {
            key: tid for key, tid in self._index.items() if tid != tenant_id
        }
        for key in self._index_keys(doc):
            existing = self._index.get(key)
            if existing and existing != tenant_id:
                raise ValueError(
                    f"identity {key} is claimed by both {existing!r} and {tenant_id!r}"
                )
            self._index[key] = tenant_id


# --- dynamodb backend --------------------------------------------------------
class DynamoConfigStore(ConfigStore):
    """Production backend. Two tables:

        config table : PK tenant_id            -> the config document
        index table  : PK 'platform#workspace' -> { tenant_id }

    Writing a tenant writes the config item plus one index item per identity
    handle, so resolution stays a single GetItem.
    """

    def __init__(
        self,
        config_table: Optional[str] = None,
        index_table: Optional[str] = None,
    ):
        import boto3  # lazy: only the prod path needs it

        self._ddb = boto3.resource("dynamodb")
        self._config_table_name = config_table or os.environ.get(
            "TENANT_CONFIG_TABLE", "o3_tenant_config"
        )
        self._index_table_name = index_table or os.environ.get(
            "TENANT_INDEX_TABLE", "o3_tenant_index"
        )
        self._config_table = self._ddb.Table(self._config_table_name)
        self._index_table = self._ddb.Table(self._index_table_name)

    @staticmethod
    def _lookup_key(platform: str, workspace_id: str) -> str:
        return f"{platform}#{workspace_id}"

    def resolve_tenant(self, platform: str, workspace_id: str) -> Optional[str]:
        if not platform or not workspace_id:
            return None
        response = self._index_table.get_item(
            Key={"lookup_key": self._lookup_key(platform, workspace_id)}
        )
        item = response.get("Item")
        return item.get("tenant_id") if item else None

    def get_config(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        response = self._config_table.get_item(Key={"tenant_id": tenant_id})
        item = response.get("Item")
        return item.get("config") if item else None

    def put_config(self, doc: Dict[str, Any]) -> None:
        validate_tenant_config(doc)
        tenant_id = doc["tenant_id"]
        self._config_table.put_item(
            Item={"tenant_id": tenant_id, "config": doc}
        )
        for platform, workspace_id in self._index_keys(doc):
            self._index_table.put_item(
                Item={
                    "lookup_key": self._lookup_key(platform, workspace_id),
                    "tenant_id": tenant_id,
                }
            )


# --- module-level singletons + public API -----------------------------------
_store: Optional[ConfigStore] = None
_config_cache = _TtlCache(CONFIG_CACHE_TTL_SECONDS)
_secret_resolver: Optional[SecretResolver] = None


def get_store() -> ConfigStore:
    """Return the process-wide store, constructed from TENANT_CONFIG_BACKEND."""
    global _store
    if _store is None:
        backend = os.environ.get("TENANT_CONFIG_BACKEND", "local").lower()
        if backend == "dynamodb":
            _store = DynamoConfigStore()
        else:
            _store = LocalConfigStore()
    return _store


def get_secret_resolver() -> SecretResolver:
    global _secret_resolver
    if _secret_resolver is None:
        backend = "aws" if os.environ.get("TENANT_CONFIG_BACKEND", "local").lower() == "dynamodb" else "local"
        _secret_resolver = SecretResolver(backend)
    return _secret_resolver


def reset_for_tests(store: Optional[ConfigStore] = None) -> None:
    """Swap in a store and clear caches. Test seam only."""
    global _store
    _store = store
    _config_cache.clear()


def resolve_tenant(platform: str, workspace_id: str) -> Optional[str]:
    """(platform, workspace_id) -> tenant_id, or None if unknown."""
    return get_store().resolve_tenant(platform, workspace_id)


def resolve_from_slack_event(body: Dict[str, Any]) -> Optional[str]:
    """Resolve a tenant straight from a Slack Events API request body.

    Slack puts the workspace at body['team_id']; enterprise-grid installs also
    carry an enterprise id in authorizations[].enterprise_id. We try the team
    first, then fall back to the enterprise.
    """
    team_id = body.get("team_id")
    if not team_id:
        # Some payload shapes nest it under the event.
        team_id = (body.get("event", {}) or {}).get("team")
    if team_id:
        tenant_id = resolve_tenant("slack", team_id)
        if tenant_id:
            return tenant_id

    authorizations = body.get("authorizations") or []
    for auth in authorizations:
        enterprise_id = auth.get("enterprise_id")
        if enterprise_id:
            tenant_id = resolve_tenant("slack", enterprise_id)
            if tenant_id:
                return tenant_id
    return None


def load_config(tenant_id: str, *, use_cache: bool = True) -> TenantConfig:
    """tenant_id -> TenantConfig. Raises TenantNotFoundError if unknown.

    Does NOT enforce the kill switch — callers decide what to do with a disabled
    tenant (see require_active_config for the enforcing variant).
    """
    if not tenant_id:
        raise TenantNotFoundError("empty tenant_id")

    if use_cache:
        cached = _config_cache.get(tenant_id)
        if cached is not None:
            return cached

    doc = get_store().get_config(tenant_id)
    if doc is None:
        raise TenantNotFoundError(f"no config for tenant_id {tenant_id!r}")

    config = TenantConfig(doc)
    if use_cache:
        _config_cache.put(tenant_id, config)
    return config


def require_active_config(tenant_id: str) -> TenantConfig:
    """Load config and enforce the kill switch.

    Raises TenantNotFoundError (unknown) or TenantDisabledError (kill switch
    off). Use this at the top of the worker so a disabled tenant is refused
    before any model calls, ticket creation, or spend.
    """
    config = load_config(tenant_id)
    if not config.is_enabled:
        raise TenantDisabledError(config.tenant_id)
    return config
