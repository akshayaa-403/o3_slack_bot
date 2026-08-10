# Proposal: multi-tenant resolution in `lambda_o3_slack_worker.py`

**Status: for review. Nothing has been applied to the worker.**

## Why

`slack_tenant_app` onboards a workspace and writes everything the worker needs
into the `tenants` table: `slack_team_id` (GSI), `status`, a KMS-encrypted
`bot_access_token`, and `answering = {lex_bot_id, lex_bot_alias_id,
lex_locale_id}`.

The handler already puts `slack_tenant: {team_id, enterprise_id}` on every SQS
message (`extract_slack_tenant`, handler:208 — four producers). The worker
never reads it: `grep 'slack_tenant\|team_id'` over 8,821 lines returns zero
matches. So the wire format is ready and only the consumer needs changing.

Today the worker serves exactly one workspace from three module-level env vars
(`SLACK_BOT_TOKEN`, `BOT_ID`, `BOT_ALIAS_ID`). A second workspace would be
answered with the first workspace's bot token.

## Scope

Small, because everything funnels through very few places:

| What | Where | Count |
|---|---|---|
| Slack token reads | `send_slack_message:419`, `slack_api:1043` | 2 |
| Lex invocations | `:7185` (image), `:7506` (text) | 2 |
| Slack call sites | all route through the two functions above | ~33, untouched |

## Design decisions

**1. Context object set per record, never per invocation.**

`lambda_handler:8799` loops over `event["Records"]`, and a single SQS batch can
carry messages from different workspaces. A module-level "current token" set
once per invocation would leak tenant A's token into tenant B's reply. The
context is therefore set at the top of `process_record` and always reset, even
on the error path.

A `contextvars.ContextVar` is used rather than a plain global: it is the
mechanism designed for exactly this, and it does not silently become wrong if
the worker is ever made concurrent.

**2. Fall back to the env vars when no tenant matches.**

Every existing message resolves to no tenant (nothing has onboarded through
`slack_tenant_app` yet), so the fallback path is what runs in production today.
This is deliberate: the change is a no-op for current traffic and can be
deployed and observed before any tenant depends on it.

**3. Resolution happens before the first Slack call.**

`fetch_conversation_metadata:6633` is the earliest Slack API call in
`process_record`, so resolution goes after the field unpacking that ends at
`:6607` and before that call.

**4. Inactive tenants are dropped, not retried.**

An expired plan is not a transient failure; returning it to the queue would
retry until the redrive policy gives up. It is logged and the record is
acknowledged.

**5. Token decryption reuses the same KMS encryption context as the writer.**

`slack_tenant_app` encrypts with `{"tenant_id": ..., "purpose":
"connector-oauth-token"}`. Decryption must present exactly that or KMS refuses,
which is what stops one tenant's ciphertext being used as another's.

## The change

### a. New module-level additions (near the existing env vars, ~line 43)

```python
import contextvars

# Set per SQS record in process_record. A ContextVar rather than a plain
# global because one SQS batch can mix tenants: leaking a token across
# records would post one workspace's reply with another's credentials.
_tenant_ctx = contextvars.ContextVar("tenant_ctx", default=None)

TENANTS_TABLE = os.environ.get("TENANTS_TABLE", "tenants")
TENANT_KMS_KEY_ID = os.environ.get("TENANT_KMS_KEY_ID", "")
MULTITENANT_ENABLED = os.environ.get("MULTITENANT_ENABLED", "false").lower() == "true"

_tenants_table = None
_kms_client = None


def _current_tenant():
    return _tenant_ctx.get()


def current_slack_token():
    """Bot token for the record being processed.

    Falls back to the single-workspace env var when no tenant resolved,
    which is every message today.
    """
    tenant = _current_tenant()
    if tenant and tenant.get("bot_token"):
        return tenant["bot_token"]
    return SLACK_BOT_TOKEN


def current_lex_config():
    """(bot_id, alias_id, locale_id) for the record being processed."""
    tenant = _current_tenant()
    answering = (tenant or {}).get("answering") or {}
    return (
        answering.get("lex_bot_id") or BOT_ID,
        answering.get("lex_bot_alias_id") or BOT_ALIAS_ID,
        answering.get("lex_locale_id") or LOCALE_ID,
    )
```

### b. Tenant resolution helper (new function)

```python
def resolve_tenant(slack_tenant):
    """Look up the tenant for an incoming message.

    Returns a dict with bot_token/answering/status, or None to mean "use the
    single-workspace env vars" — which is the current production path.

    Never raises: a lookup failure must not take down message processing,
    because falling back to existing behaviour is strictly better than
    dropping the message.
    """
    if not MULTITENANT_ENABLED:
        return None

    team_id = (slack_tenant or {}).get("team_id")
    if not team_id:
        return None

    try:
        global _tenants_table
        if _tenants_table is None:
            _tenants_table = boto3.resource(
                "dynamodb", region_name=AWS_REGION).Table(TENANTS_TABLE)

        resp = _tenants_table.query(
            IndexName="slack_team_id-index",
            KeyConditionExpression="slack_team_id = :t",
            ExpressionAttributeValues={":t": team_id},
        )
        items = resp.get("Items") or []
        if not items:
            return None

        tenant = items[0]
        return {
            "tenant_id": tenant.get("tenant_id"),
            "status": tenant.get("status", "active"),
            "answering": tenant.get("answering") or {},
            "bot_token": _decrypt_bot_token(tenant),
        }
    except Exception as exc:
        log_json({
            "level": "ERROR",
            "message": "tenant_resolution_failed",
            "team_id": team_id,
            "error": exc.__class__.__name__,
        })
        return None


def _decrypt_bot_token(tenant):
    """Decrypt the stored bot token, mirroring slack_tenant_app/app/crypto.py.

    The encryption context must match the writer's exactly or KMS refuses —
    that is the mechanism preventing one tenant's ciphertext being replayed
    as another's.
    """
    stored = tenant.get("bot_access_token")
    if not isinstance(stored, dict) or stored.get("__enc__") != "kms.v1":
        # Legacy plaintext, or absent.
        return stored if isinstance(stored, str) else None

    global _kms_client
    if _kms_client is None:
        _kms_client = boto3.client("kms", region_name=AWS_REGION)

    resp = _kms_client.decrypt(
        CiphertextBlob=base64.b64decode(stored["data"]),
        EncryptionContext={
            "tenant_id": tenant["tenant_id"],
            "purpose": "connector-oauth-token",
        },
    )
    return json.loads(resp["Plaintext"].decode("utf-8")).get("token")
```

### c. `send_slack_message` — one line (`:419`)

```diff
         headers={
             "Content-Type": "application/json",
-            "Authorization": f"Bearer {SLACK_BOT_TOKEN}"
+            "Authorization": f"Bearer {current_slack_token()}"
         },
```

### d. `slack_api` — one line (`:1043`)

```diff
-    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
+    headers = {"Authorization": f"Bearer {current_slack_token()}"}
```

### e. Lex image path (`:7185`)

```diff
+            lex_bot_id, lex_alias_id, lex_locale_id = current_lex_config()
             lex_response = lex.recognize_text(
-                botId=BOT_ID,
-                botAliasId=BOT_ALIAS_ID,
-                localeId=LOCALE_ID,
+                botId=lex_bot_id,
+                botAliasId=lex_alias_id,
+                localeId=lex_locale_id,
                 sessionId=lex_session_id,
                 text=image_query,
             )
```

### f. Lex text path (`:7506`)

```diff
+        lex_bot_id, lex_alias_id, lex_locale_id = current_lex_config()
         response = lex.recognize_text(
-            botId=BOT_ID,
-            botAliasId=BOT_ALIAS_ID,
-            localeId=LOCALE_ID,
+            botId=lex_bot_id,
+            botAliasId=lex_alias_id,
+            localeId=lex_locale_id,
             sessionId=lex_session_id,
             text=text
         )
```

### g. `process_record` — set and clear the context (after `:6607`)

```diff
     action_value = body.get("action_value")
+
+    # Resolve before any Slack call: fetch_conversation_metadata below is
+    # the first one. Reset in `finally` so a tenant can never leak into the
+    # next record of the same batch.
+    tenant = resolve_tenant(body.get("slack_tenant"))
+    token_ctx = _tenant_ctx.set(tenant)
+    try:
+        return _process_record_inner(body, tenant, ...)
+    finally:
+        _tenant_ctx.reset(token_ctx)
```

Rather than reindent the ~1,200-line body of `process_record`, the cleaner
edit is to set the context in `lambda_handler`'s loop, where the body is one
call:

```diff
     for record in event["Records"]:
         try:
-            process_record(record)
+            body = json.loads(record["body"])
+            tenant = resolve_tenant(body.get("slack_tenant"))
+
+            if tenant and tenant.get("status", "active") != "active":
+                # Not a transient failure — retrying until the redrive policy
+                # gives up would achieve nothing. Log and acknowledge.
+                log_json({
+                    "level": "WARNING",
+                    "message": "tenant_inactive_message_dropped",
+                    "tenant_id": tenant.get("tenant_id"),
+                    "status": tenant.get("status"),
+                })
+                continue
+
+            token_ctx = _tenant_ctx.set(tenant)
+            try:
+                process_record(record)
+            finally:
+                _tenant_ctx.reset(token_ctx)
```

This keeps `process_record` untouched apart from the two Lex sites, which is
the smaller blast radius.

## Deployment plan

1. Deploy with `MULTITENANT_ENABLED=false`. Every path falls back to the env
   vars; behaviour is bit-identical to today. Confirm normal traffic is fine.
2. Add IAM for the worker role: `dynamodb:Query` on `tenants` +
   `tenants/index/*`, and `kms:Decrypt` on the token key.
3. Flip `MULTITENANT_ENABLED=true`. Existing traffic still resolves to no
   tenant (nothing has onboarded yet) and keeps using the fallback.
4. Onboard one real test workspace through `slack_tenant_app` and verify it is
   answered with its own token.

Rollback at any point is a single env-var flip.

## Blockers and open questions

**1. `VERIFY_SLACK_SIGNATURE=false` on the live handler — must be fixed first.**

Confirmed on the deployed Lambda, not just in `.env`. Today it is a harmless
testing shortcut. Once the worker trusts `team_id` to select a token, an
unauthenticated caller can POST a forged `team_id` and make the worker act as
any tenant. The signing secret is per-app, so enabling it is one env var, but
it must precede step 3 above.

**2. Eight downstream Lambdas are unaudited.**

Claude fallback, Jira, Rovo, MCP assist, summarizer, image Rekognition, and
live agent (×2) are invoked by the worker and several likely post to Slack with
their own `SLACK_BOT_TOKEN`. They would still use the single-workspace token.
This is the largest remaining unknown and needs its own survey before
multi-tenant go-live.

**3. Session keys are not namespaced by tenant.**

`{channel}:{user}` is de-facto unique because Slack IDs are globally unique, so
this is not a correctness bug. But `supersede_other_dm_sessions:3366` does a
full-table `scan()` on the live DM path, which degrades as tenants accumulate,
and without a `team_id` attribute there is no way to delete one tenant's
sessions for offboarding or a GDPR request. Recommend adding `team_id` as an
attribute (backward-compatible) rather than changing the key format, which
would strand in-flight sessions and already-rendered button payloads that embed
`session_id`.
