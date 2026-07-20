"""End-to-end demo: one code path, many tenants, off one deployment.

Run it:

    python -m tenant.demo          (from the repo root)
    python tenant/demo.py          (also works; bootstraps sys.path)

What it shows:

  * Two Slack messages arrive from two *different workspaces*.
  * The SAME handle_message() function processes both.
  * It resolves each to a tenant, loads that tenant's config, and the behaviour
    diverges - different Lex bot, feature flags, integrations, copy, limits -
    without a line of tenant-specific code.
  * An unknown workspace is routed to onboarding, not served.
  * Flipping a tenant's `enabled` kill switch refuses it before any processing.

This is the control-plane pattern that replaces the ~150 global env vars in
lambda_o3_slack_worker.py with per-tenant config loaded at request time.
"""

from __future__ import annotations

import os
import sys

# Allow `python tenant/demo.py` (not just `-m tenant.demo`) by ensuring the
# repo root is importable.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Force the local (zero-AWS) backend for the demo regardless of environment.
os.environ.setdefault("TENANT_CONFIG_BACKEND", "local")

from tenant.config_store import (  # noqa: E402
    resolve_from_slack_event,
    require_active_config,
    load_config,
    get_store,
    reset_for_tests,
    LocalConfigStore,
    TenantNotFoundError,
    TenantDisabledError,
)


def slack_event(team_id: str, text: str, user: str = "U_REQUESTER") -> dict:
    """A minimal but realistic Slack Events API request body."""
    return {
        "type": "event_callback",
        "team_id": team_id,
        "api_app_id": "A0IVVY",
        "event_id": f"Ev_{team_id}_{abs(hash(text)) % 100000}",
        "event": {
            "type": "message",
            "channel_type": "im",
            "user": user,
            "text": text,
            "channel": "D_DM_CHANNEL",
            "team": team_id,
        },
        "authorizations": [{"enterprise_id": None, "team_id": team_id}],
    }


# --- the ONE code path -------------------------------------------------------
def handle_message(platform: str, body: dict, text: str) -> dict:
    """Tenant-agnostic message handler. Identical for every customer.

    Returns a 'decision' dict describing what the worker would do. In the real
    worker this is where Lex is invoked, flags gate features, secrets are
    fetched by ref, and copy is chosen - all read from `config`.
    """
    tenant_id = resolve_from_slack_event(body) if platform == "slack" else None

    if not tenant_id:
        return {
            "outcome": "route_to_onboarding",
            "reason": "no tenant matched this workspace",
            "workspace": body.get("team_id"),
        }

    try:
        config = require_active_config(tenant_id)
    except TenantDisabledError as exc:
        return {
            "outcome": "refused_disabled",
            "tenant_id": exc.tenant_id,
            "reason": "kill switch is off (enabled=false)",
        }
    except TenantNotFoundError as exc:
        return {"outcome": "error_no_config", "reason": str(exc)}

    lex = config.lex()
    decision = {
        "outcome": "handled",
        "tenant_id": config.tenant_id,
        "display_name": config.display_name,
        "lex_bot_id": lex["bot_id"],
        "intent_set_ref": config.intent_set_ref,
        "greeting": config.copy_text("empty_user_text_reply"),
        "inactivity_timeout_s": config.limit("inactivity_timeout_seconds"),
        "can_create_jira": config.feature("create_jira_ticket")
        and config.integration_enabled("jira"),
        "can_live_agent": config.feature("live_agent"),
        "analyzes_images": config.feature("image_analysis"),
        "kb_enabled": config.knowledge_base().get("enabled", False),
    }

    # Example of a feature gate + secret-by-reference (no secret is ever shown).
    if decision["can_create_jira"]:
        jira = config.integration("jira")
        decision["jira_domain"] = jira.get("atlassian_domain")
        decision["jira_secret_ref"] = jira.get("secret_ref")  # a NAME, not a secret

    return decision


# --- pretty printing ---------------------------------------------------------
def _line(char: str = "-", width: int = 74) -> str:
    return char * width


def _print_decision(title: str, decision: dict) -> None:
    print(f"\n{title}")
    print(_line())
    width = max(len(k) for k in decision) + 2
    for key, value in decision.items():
        print(f"  {key.ljust(width)}: {value}")


def main() -> None:
    print(_line("="))
    print("  IvvY multi-tenant control plane - end-to-end demo")
    print("  One deployment. One handle_message(). Behaviour from config only.")
    print(_line("="))

    store = get_store()
    tenant_ids = sorted(
        {store.resolve_tenant("slack", "T_INNOVYQ01"), store.resolve_tenant("slack", "T_ACME0002")}
    )
    print(f"\nLoaded tenants from config store: {tenant_ids}")
    print("Backend:", os.environ.get("TENANT_CONFIG_BACKEND"))

    # 1) Message from InnovyQ's workspace.
    d1 = handle_message(
        "slack",
        slack_event("T_INNOVYQ01", "I need access to AWS"),
        "I need access to AWS",
    )
    _print_decision("[1] Slack message from workspace T_INNOVYQ01", d1)

    # 2) Same code, message from Acme's workspace -> different behaviour.
    d2 = handle_message(
        "slack",
        slack_event("T_ACME0002", "My VPN won't connect"),
        "My VPN won't connect",
    )
    _print_decision("[2] Slack message from workspace T_ACME0002", d2)

    # 3) Unknown workspace -> onboarding, not served.
    d3 = handle_message(
        "slack",
        slack_event("T_STRANGER99", "hello?"),
        "hello?",
    )
    _print_decision("[3] Slack message from an UNKNOWN workspace", d3)

    # 4) Kill switch: disable Acme in the control plane, re-run the same message.
    acme_doc = load_config("acme-robotics").to_dict()
    acme_doc["enabled"] = False
    # Rebuild a store with Acme disabled (simulates a control-plane update).
    disabled_store = LocalConfigStore()
    disabled_store.put_config(acme_doc)
    reset_for_tests(disabled_store)
    d4 = handle_message(
        "slack",
        slack_event("T_ACME0002", "My VPN won't connect"),
        "My VPN won't connect",
    )
    _print_decision("[4] Same Acme message AFTER flipping the kill switch off", d4)
    reset_for_tests(None)  # restore default store

    print("\n" + _line("="))
    print("  Takeaways")
    print(_line("="))
    print("  * [1] vs [2]: different Lex bot, intent set, flags, copy, limits -")
    print("        zero tenant-specific code. All of it came from config.")
    print("  * [2]: Acme has create_jira_ticket OFF -> no Jira path offered.")
    print("         Acme has image_analysis ON -> screenshots analysed.")
    print("  * [3]: an unknown workspace is onboarded, never silently served.")
    print("  * [4]: the kill switch refuses a tenant before any spend/model call.")
    print("  * No secret was printed - only *_secret_ref names.")
    print(_line("="))


if __name__ == "__main__":
    main()
