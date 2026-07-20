"""Tenant config schema — THE CONTRACT.

This is the shape of one customer's configuration. It is the single interface
that every other workstream plugs into:

  * The intent workstream produces an *intent set* for a tenant; that set is
    referenced here by `intents.intent_set_ref` (+ the tenant's own Lex bot).
  * The MCP / connectors workstream enables *tools* for a tenant; those live
    under `integrations`, with credentials stored as *references* (never inline).
  * The runtime worker reads feature flags, limits, and copy from here instead
    of from ~150 global environment variables.

Design rules baked into this schema:

  1. SECRETS ARE NEVER STORED HERE. Only *references* to Secrets Manager
     (e.g. "tenant/innovyq/atlassian"). The store resolves refs at load time.
  2. `enabled` is a hard kill switch. A disabled tenant is refused before any
     processing — this is the deterministic "observer/guardrail", not an LLM.
  3. Everything is JSON-serialisable so a config is one DynamoDB item / one
     document, and diffs are reviewable.

The stored form is a plain dict (JSON / DynamoDB item). `TenantConfig` is a thin
typed accessor over that dict so callers get ergonomics + safe defaults without
the whole codebase having to adopt a new object model.
"""

from __future__ import annotations

from typing import Any, Dict, List

SCHEMA_VERSION = 1

# Feature flags default to False (off) unless the tenant opts in. Keeping the
# canonical list here means "what can a tenant turn on?" has one answer.
KNOWN_FEATURES = (
    "claude_fallback",
    "auto_claude_fallback",
    "create_jira_ticket",
    "live_agent",
    "image_analysis",
    "bedrock_kb_assist",
    "feedback_rating",
    "feedback_form",
    "close_summary",
    "rovo_enrichment",
)

# Limits default to these if a tenant omits them. These mirror the current
# global env defaults in lambda_o3_slack_worker.py so behaviour is unchanged
# for a tenant that specifies nothing.
DEFAULT_LIMITS = {
    "inactivity_timeout_seconds": 120,
    "session_ttl_seconds": 86400,
    "max_daily_requests": 5000,  # guardrail hook; 0 or missing => unlimited
}

REQUIRED_TOP_LEVEL = ("tenant_id", "display_name", "enabled", "identity")


class TenantConfigError(ValueError):
    """Raised when a tenant config document fails validation."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TenantConfigError(message)


def validate_tenant_config(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a raw tenant config dict, returning it unchanged if valid.

    Raises TenantConfigError with a specific message on the first problem, so a
    bad onboarding submission fails loudly at write time rather than silently at
    request time.
    """
    _require(isinstance(doc, dict), "tenant config must be an object")

    for key in REQUIRED_TOP_LEVEL:
        _require(key in doc, f"missing required field: {key!r}")

    tenant_id = doc.get("tenant_id")
    _require(
        isinstance(tenant_id, str) and tenant_id.strip() != "",
        "tenant_id must be a non-empty string",
    )
    _require(
        tenant_id == tenant_id.lower() and " " not in tenant_id,
        "tenant_id must be lowercase with no spaces (it is used in keys/ARNs)",
    )

    _require(
        isinstance(doc.get("display_name"), str) and doc["display_name"].strip() != "",
        "display_name must be a non-empty string",
    )
    _require(isinstance(doc.get("enabled"), bool), "enabled must be a boolean")

    identity = doc.get("identity")
    _require(isinstance(identity, dict), "identity must be an object")
    slack_ids = identity.get("slack_team_ids", []) or []
    teams_ids = identity.get("teams_aad_tenant_ids", []) or []
    web_keys = identity.get("web_api_key_refs", []) or []
    _require(
        isinstance(slack_ids, list)
        and isinstance(teams_ids, list)
        and isinstance(web_keys, list),
        "identity.slack_team_ids / teams_aad_tenant_ids / web_api_key_refs must be lists",
    )
    _require(
        len(slack_ids) + len(teams_ids) + len(web_keys) > 0,
        f"tenant {tenant_id!r} has no identity handles; it can never be resolved",
    )

    features = doc.get("features", {}) or {}
    _require(isinstance(features, dict), "features must be an object")
    for name, value in features.items():
        _require(
            isinstance(value, bool),
            f"feature {name!r} must be a boolean, got {type(value).__name__}",
        )

    limits = doc.get("limits", {}) or {}
    _require(isinstance(limits, dict), "limits must be an object")
    for name, value in limits.items():
        _require(
            isinstance(value, (int, float)) and not isinstance(value, bool),
            f"limit {name!r} must be a number",
        )

    integrations = doc.get("integrations", {}) or {}
    _require(isinstance(integrations, dict), "integrations must be an object")
    for name, spec in integrations.items():
        _require(isinstance(spec, dict), f"integration {name!r} must be an object")
        # Guard against the classic mistake: a raw secret pasted into config.
        for forbidden in ("secret", "token", "api_token", "password", "client_secret"):
            _require(
                forbidden not in spec,
                f"integration {name!r} contains an inline {forbidden!r}; "
                f"store a *_secret_ref pointing at Secrets Manager instead",
            )

    return doc


class TenantConfig:
    """Thin typed accessor over a validated tenant config dict."""

    def __init__(self, doc: Dict[str, Any]):
        self._doc = validate_tenant_config(doc)

    # --- identity / lifecycle -------------------------------------------------
    @property
    def tenant_id(self) -> str:
        return self._doc["tenant_id"]

    @property
    def display_name(self) -> str:
        return self._doc["display_name"]

    @property
    def is_enabled(self) -> bool:
        """The kill switch. False => refuse the request before processing."""
        return bool(self._doc.get("enabled"))

    @property
    def identity(self) -> Dict[str, List[str]]:
        return self._doc.get("identity", {})

    # --- intents (the intent workstream's plug point) -------------------------
    @property
    def intent_set_ref(self) -> str:
        return self._doc.get("intents", {}).get("intent_set_ref", "")

    def lex(self) -> Dict[str, str]:
        """Per-tenant Lex bot coordinates used by the worker to route intents."""
        intents = self._doc.get("intents", {})
        return {
            "bot_id": intents.get("lex_bot_id", ""),
            "bot_alias_id": intents.get("lex_bot_alias_id", ""),
            "locale_id": intents.get("locale_id", "en_US"),
        }

    # --- knowledge base / RAG -------------------------------------------------
    def knowledge_base(self) -> Dict[str, Any]:
        return self._doc.get("knowledge_base", {})

    # --- integrations (the MCP / connectors plug point) -----------------------
    def integration(self, name: str) -> Dict[str, Any]:
        """Return the config block for an integration (e.g. 'jira', 'slack').

        Values include *_secret_ref pointers, never raw secrets.
        """
        return self._doc.get("integrations", {}).get(name, {})

    def integration_enabled(self, name: str) -> bool:
        return bool(self.integration(name).get("enabled", False))

    # --- feature flags --------------------------------------------------------
    def feature(self, name: str, default: bool = False) -> bool:
        return bool(self._doc.get("features", {}).get(name, default))

    # --- limits / guardrails --------------------------------------------------
    def limit(self, name: str) -> Any:
        limits = {**DEFAULT_LIMITS, **(self._doc.get("limits", {}) or {})}
        return limits.get(name)

    # --- user-facing copy -----------------------------------------------------
    def copy_text(self, name: str, default: str = "") -> str:
        return self._doc.get("copy", {}).get(name, default)

    # --- serialisation --------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return dict(self._doc)

    def __repr__(self) -> str:
        state = "enabled" if self.is_enabled else "DISABLED"
        return f"<TenantConfig {self.tenant_id!r} ({state})>"
