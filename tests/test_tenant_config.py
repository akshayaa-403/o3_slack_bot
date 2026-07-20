"""Tests for the multi-tenant control plane (tenant/ package)."""

import os

import pytest

os.environ.setdefault("TENANT_CONFIG_BACKEND", "local")

from tenant import schema  # noqa: E402
from tenant.config_store import (  # noqa: E402
    LocalConfigStore,
    SecretResolver,
    load_config,
    require_active_config,
    reset_for_tests,
    resolve_from_slack_event,
    resolve_tenant,
    TenantDisabledError,
    TenantNotFoundError,
)


@pytest.fixture(autouse=True)
def fresh_store():
    """Give every test a clean local store built from the seed file."""
    reset_for_tests(LocalConfigStore())
    yield
    reset_for_tests(None)


def _minimal_config(tenant_id="tester", **overrides):
    doc = {
        "tenant_id": tenant_id,
        "display_name": tenant_id.title(),
        "enabled": True,
        "identity": {"slack_team_ids": [f"T_{tenant_id.upper()}"]},
    }
    doc.update(overrides)
    return doc


# --- resolution --------------------------------------------------------------
def test_resolve_known_slack_workspace():
    assert resolve_tenant("slack", "T_INNOVYQ01") == "innovyq"
    assert resolve_tenant("slack", "T_ACME0002") == "acme-robotics"


def test_resolve_unknown_workspace_returns_none():
    assert resolve_tenant("slack", "T_NOPE") is None
    assert resolve_tenant("slack", "") is None
    assert resolve_tenant("", "T_INNOVYQ01") is None


def test_resolve_teams_platform():
    assert resolve_tenant("teams", "aad-innovyq-0000-1111-2222") == "innovyq"


def test_resolve_web_platform():
    assert resolve_tenant("web", "web/acme-robotics/portal_key") == "acme-robotics"


def test_resolve_from_slack_event_team_id():
    body = {"team_id": "T_INNOVYQ01", "event": {"type": "message"}}
    assert resolve_from_slack_event(body) == "innovyq"


def test_resolve_from_slack_event_nested_team_fallback():
    body = {"event": {"type": "message", "team": "T_ACME0002"}}
    assert resolve_from_slack_event(body) == "acme-robotics"


def test_resolve_from_slack_event_unknown_returns_none():
    body = {"team_id": "T_STRANGER", "authorizations": [{"enterprise_id": None}]}
    assert resolve_from_slack_event(body) is None


# --- load + kill switch ------------------------------------------------------
def test_load_known_tenant():
    config = load_config("innovyq")
    assert config.tenant_id == "innovyq"
    assert config.display_name == "InnovyQ"
    assert config.is_enabled is True


def test_load_unknown_tenant_raises():
    with pytest.raises(TenantNotFoundError):
        load_config("ghost")


def test_require_active_config_ok_when_enabled():
    config = require_active_config("innovyq")
    assert config.is_enabled is True


def test_require_active_config_raises_when_disabled():
    store = LocalConfigStore()
    store.put_config(_minimal_config("dark", enabled=False))
    reset_for_tests(store)
    with pytest.raises(TenantDisabledError) as exc:
        require_active_config("dark")
    assert exc.value.tenant_id == "dark"


def test_config_is_cached_within_ttl():
    first = load_config("innovyq")
    second = load_config("innovyq")
    assert first is second  # served from the in-process cache


# --- divergence (the whole point) --------------------------------------------
def test_two_tenants_diverge_on_config():
    innovyq = load_config("innovyq")
    acme = load_config("acme-robotics")

    # Different intent sets / Lex bots.
    assert innovyq.intent_set_ref != acme.intent_set_ref
    assert innovyq.lex()["bot_id"] != acme.lex()["bot_id"]

    # Different feature posture.
    assert innovyq.feature("create_jira_ticket") is True
    assert acme.feature("create_jira_ticket") is False
    assert innovyq.feature("image_analysis") is False
    assert acme.feature("image_analysis") is True

    # Different limits and copy.
    assert innovyq.limit("inactivity_timeout_seconds") == 120
    assert acme.limit("inactivity_timeout_seconds") == 300
    assert innovyq.copy_text("empty_user_text_reply") != acme.copy_text(
        "empty_user_text_reply"
    )


def test_feature_defaults_to_false():
    config = load_config("innovyq")
    assert config.feature("some_feature_not_set") is False


def test_limit_falls_back_to_default():
    store = LocalConfigStore()
    store.put_config(_minimal_config("nolimits"))
    reset_for_tests(store)
    config = load_config("nolimits")
    assert config.limit("session_ttl_seconds") == schema.DEFAULT_LIMITS[
        "session_ttl_seconds"
    ]


# --- schema validation -------------------------------------------------------
def test_missing_required_field_rejected():
    with pytest.raises(schema.TenantConfigError):
        schema.validate_tenant_config({"tenant_id": "x", "display_name": "X"})


def test_tenant_without_identity_handle_rejected():
    bad = {
        "tenant_id": "x",
        "display_name": "X",
        "enabled": True,
        "identity": {"slack_team_ids": []},
    }
    with pytest.raises(schema.TenantConfigError):
        schema.validate_tenant_config(bad)


def test_uppercase_tenant_id_rejected():
    with pytest.raises(schema.TenantConfigError):
        schema.validate_tenant_config(_minimal_config("BadId"))


def test_inline_secret_in_integration_rejected():
    bad = _minimal_config(
        "leaky",
        integrations={"jira": {"enabled": True, "api_token": "SECRETVALUE"}},
    )
    with pytest.raises(schema.TenantConfigError) as exc:
        schema.validate_tenant_config(bad)
    assert "secret_ref" in str(exc.value)


def test_non_bool_feature_rejected():
    bad = _minimal_config("weird", features={"claude_fallback": "yes"})
    with pytest.raises(schema.TenantConfigError):
        schema.validate_tenant_config(bad)


def test_identity_collision_rejected():
    store = LocalConfigStore()
    # Claims T_INNOVYQ01, already owned by the seeded innovyq tenant.
    colliding = _minimal_config("impostor")
    colliding["identity"]["slack_team_ids"] = ["T_INNOVYQ01"]
    with pytest.raises(ValueError):
        store.put_config(colliding)


# --- secrets are references, never values ------------------------------------
def test_secret_resolver_local_never_returns_a_real_secret():
    resolver = SecretResolver("local")
    resolved = resolver.resolve("tenant/innovyq/atlassian")
    assert resolved == "<unresolved-secret:tenant/innovyq/atlassian>"
    assert "SECRET" not in resolved.upper() or "unresolved" in resolved
