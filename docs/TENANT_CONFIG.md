# Tenant Config — the multi-tenant control plane

This is the contract that turns the IvvY bot from *"a bot for InnovyQ"* into a
product that serves many customers **from one deployment**. It is the socket the
other workstreams plug into:

- **Intents** produce an *intent set* per customer → referenced here by
  `intents.intent_set_ref` (plus that customer's own Lex bot).
- **MCP / connectors** enable *tools* per customer → declared under
  `integrations`, with credentials stored as **references**, never inline.
- **The runtime worker** reads feature flags, limits, and copy from here instead
  of from the ~150 global environment variables it reads today.

> One rule above all: **we never fork the codebase per customer.** Per-tenant
> differences live in config that is loaded at request time.

---

## The model

```
incoming request
   → resolve_tenant(platform, workspace_id)   # who is this for?
   → load_config(tenant_id)                    # what are their settings?
   → behave according to that config           # one code path, N tenants
```

| Term          | Meaning                                             | Example                         |
|---------------|-----------------------------------------------------|---------------------------------|
| `platform`    | Where the message came from                         | `slack` / `teams` / `web`       |
| `workspace_id`| The platform's id for the customer's space          | Slack `team_id` = `T_INNOVYQ01` |
| `tenant_id`   | Our stable internal id (lowercase, no spaces)       | `innovyq`                       |

Resolution is a single O(1) keyed lookup (it runs on **every** message), backed
by a reverse index `(platform, workspace_id) → tenant_id`.

---

## Quick start

```bash
# Runnable end-to-end demo (zero AWS — uses the local seed file):
python -m tenant.demo

# Tests:
python -m pytest tests/test_tenant_config.py -q
```

```python
from tenant import resolve_from_slack_event, require_active_config

tenant_id = resolve_from_slack_event(slack_body)     # "innovyq" | None
if not tenant_id:
    ...  # → onboarding, do not serve
config = require_active_config(tenant_id)             # raises if kill switch off

config.lex()                       # {'bot_id', 'bot_alias_id', 'locale_id'}
config.intent_set_ref              # "innovyq-it-v3"
config.feature("create_jira_ticket")     # bool
config.integration("jira")               # {'atlassian_domain', 'secret_ref', ...}
config.limit("inactivity_timeout_seconds")
config.copy_text("empty_user_text_reply")
```

---

## The schema

A tenant config is one JSON document (one DynamoDB item in prod). Full field
reference — see `tenant/schema.py` for the authoritative validation.

```jsonc
{
  "tenant_id": "innovyq",              // lowercase, no spaces; used in keys/ARNs
  "display_name": "InnovyQ",
  "enabled": true,                     // KILL SWITCH — false = refuse before processing

  "identity": {                        // how requests resolve to this tenant
    "slack_team_ids": ["T_INNOVYQ01"],
    "teams_aad_tenant_ids": ["aad-..."],
    "web_api_key_refs": []             // references, not the keys themselves
  },

  "intents": {                         // ← the INTENTS workstream plugs in here
    "lex_bot_id": "LEXBOT_INNOVYQ",
    "lex_bot_alias_id": "TSTALIASID",
    "locale_id": "en_US",
    "intent_set_ref": "innovyq-it-v3", // pointer to the generated intent set
    "intent_count": 333
  },

  "knowledge_base": {                  // per-tenant RAG
    "enabled": true,
    "bedrock_kb_id": "KB_INNOVYQ_IT",
    "model_arn": "arn:aws:bedrock:...:foundation-model/..."
  },

  "integrations": {                    // ← the MCP / CONNECTORS workstream plugs in here
    "slack": {
      "enabled": true,
      "bot_user_id": "U_IVVY_INNOVYQ",
      "bot_token_secret_ref": "tenant/innovyq/slack/bot_token",   // a NAME
      "signing_secret_ref": "tenant/innovyq/slack/signing_secret"
    },
    "jira": {
      "enabled": true,
      "atlassian_domain": "innovyq.atlassian.net",
      "project_key": "SUP",
      "secret_ref": "tenant/innovyq/atlassian"                    // a NAME
    }
  },

  "features": {                        // opt-in flags; default OFF if omitted
    "claude_fallback": true,
    "create_jira_ticket": true,
    "live_agent": true,
    "image_analysis": false,
    "bedrock_kb_assist": true,
    "feedback_rating": true
  },

  "limits": {                          // guardrail hooks; sensible defaults if omitted
    "inactivity_timeout_seconds": 120,
    "session_ttl_seconds": 86400,
    "max_daily_requests": 5000
  },

  "copy": {                            // per-tenant, brandable user-facing text
    "empty_user_text_reply": "Hi, I'm IvvY. How can I help?",
    "claude_failure_reply": "..."
  },

  "metadata": { "schema_version": 1, "onboarded_at": "2026-05-01" }
}
```

### Two hard rules the schema enforces at write time

1. **No secrets in config.** Any `secret` / `token` / `api_token` / `password` /
   `client_secret` key inside an integration is rejected. Store a `*_secret_ref`
   (a Secrets Manager name); the store resolves it at load time. Nothing
   sensitive is ever printed or committed.
2. **Every tenant must have at least one identity handle**, or it could never be
   resolved. Duplicate handles across tenants are rejected (no ambiguous routing).

---

## The kill switch (`enabled`)

This is the deterministic "observer/guardrail" — **not** an LLM. A disabled
tenant is refused by `require_active_config()` *before* any model call, ticket
creation, or spend. Flip `enabled: false` in the control plane and the tenant
stops being served within one cache TTL.

---

## Storage

| Backend               | When                         | Notes                                  |
|-----------------------|------------------------------|----------------------------------------|
| `local` (seed JSON)   | demo, tests, single pilot    | zero AWS; `tenant/seed_tenants.json`   |
| `dynamodb`            | production                   | pool compute, per-tenant data          |

Select with `TENANT_CONFIG_BACKEND=local|dynamodb`.

**DynamoDB layout (prod):**

- `o3_tenant_config` — PK `tenant_id` → the config document.
- `o3_tenant_index`  — PK `lookup_key` = `"{platform}#{workspace_id}"` → `{tenant_id}`.

Writing a tenant writes the config item **plus** one index item per identity
handle, so resolution stays a single `GetItem`.

**Caching:** configs are cached in-process with a short TTL
(`TENANT_CONFIG_CACHE_TTL_SECONDS`, default 60s). Warm Lambda containers reuse
the cache, so we don't read DynamoDB on every message; a control-plane change is
visible within one TTL.

---

## How this replaces the ~150 env vars

The worker (`lambda_o3_slack_worker.py`) currently reads its entire behaviour
from module-level `os.environ` at import time. That is the single-tenant
bottleneck. The migration is a **mechanical mapping**, not a rewrite:

| Today (global env var)              | Tomorrow (per-tenant config)                    |
|-------------------------------------|-------------------------------------------------|
| `BOT_ID`, `BOT_ALIAS_ID`            | `config.lex()`                                  |
| `ENABLE_CREATE_JIRA_TICKET`         | `config.feature("create_jira_ticket")`          |
| `ATLASSIAN_DOMAIN`                  | `config.integration("jira")["atlassian_domain"]`|
| `ATLASSIAN_API_TOKEN`               | resolve `config.integration("jira")["secret_ref"]` via Secrets Manager |
| `INACTIVITY_TIMEOUT_SECONDS`        | `config.limit("inactivity_timeout_seconds")`    |
| `ENABLE_BEDROCK_KB_ASSIST`          | `config.feature("bedrock_kb_assist")`           |
| `EMPTY_USER_TEXT_REPLY`             | `config.copy_text("empty_user_text_reply")`     |

Migration path (incremental, no big-bang):

1. Worker resolves tenant at the top of the message loop (from `slack_tenant`,
   which the handler now attaches to every SQS message) and calls
   `require_active_config(tenant_id)`.
2. Replace each global-env read with the matching `config.*` accessor, one group
   at a time. Until a value is migrated, fall back to the existing env default
   so behaviour is unchanged for the InnovyQ pilot.
3. Once all reads are migrated, the env vars become the *default tenant* only.

---

## Adding a tenant

- **Local:** add an object to `tenant/seed_tenants.json` (validated on load).
- **Prod:** the onboarding flow calls `store.put_config(doc)` after
  `validate_tenant_config(doc)`; that writes the config item + index entries and
  stores the customer's secrets in Secrets Manager under the `*_secret_ref`
  names. No deploy required to onboard a customer.
