"""Multi-tenant control plane for the IvvY bot.

This package is the *control plane*: it turns the single-tenant bot into a
product that can serve many customers from one deployment, without forking the
codebase per customer.

The core idea:

    incoming request
        -> resolve_tenant(platform, workspace_id)   # who is this for?
        -> load_config(tenant_id)                    # what are their settings?
        -> behave according to that config           # one code path, N tenants

Today the worker (lambda_o3_slack_worker.py) reads ~150 global os.environ
values at import time. Those are exactly the settings that must become
*per-tenant*. This package is where a tenant's version of those settings lives,
keyed by tenant_id, loaded at request time instead of baked in at deploy time.

Public surface:

    from tenant import resolve_tenant, load_config, TenantConfig

    tenant_id = resolve_tenant("slack", team_id)     # -> "innovyq" | None
    config = load_config(tenant_id)                  # -> TenantConfig
    if not config.is_enabled:
        ...  # kill switch: refuse politely, do not process
"""

from tenant.schema import TenantConfig, validate_tenant_config, SCHEMA_VERSION
from tenant.config_store import (
    resolve_tenant,
    resolve_from_slack_event,
    load_config,
    get_store,
    TenantNotFoundError,
    TenantDisabledError,
)

__all__ = [
    "TenantConfig",
    "validate_tenant_config",
    "SCHEMA_VERSION",
    "resolve_tenant",
    "resolve_from_slack_event",
    "load_config",
    "get_store",
    "TenantNotFoundError",
    "TenantDisabledError",
]
