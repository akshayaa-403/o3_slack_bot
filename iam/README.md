# IAM policies

## `admin_api_policy.json`

Execution policy for `lambda_o3_admin_api`. Replace `ACCOUNT_ID` and `REGION`
before applying:

```bash
sed -e "s/ACCOUNT_ID/661779458398/g" -e "s/REGION/ap-southeast-2/g" \
  iam/admin_api_policy.json > /tmp/admin_api_policy.json

aws iam put-role-policy \
  --role-name IvvY_Admin_API_Role \
  --policy-name AdminApi \
  --policy-document file:///tmp/admin_api_policy.json
```

The file is deliberately plain JSON with no comment keys — IAM rejects any
unrecognised top-level key, so a `"Comment"` field makes the document
unappliable. The reasoning lives here instead.

### What it grants, and why it is this narrow

| Statement | Why |
|---|---|
| `Logs` | Scoped to this function's own log group, not `logs:*` on `*`. |
| `TenantRegistry` | Read, write and delete the tenant registry. `DeleteItem` is needed by `DELETE /tenants/{id}`. |
| `DraftedIntents` | `Query`, `UpdateItem` and `DeleteItem`. The API records approve/reject decisions and clears a tenant's drafts when that tenant is deleted; it never *creates* drafts — the intent pipeline does that under its own role. |
| `ManageConnectorCredentials` | Create, update and delete secrets under `ivvy-bot/*`. Confined to that prefix so it cannot touch unrelated secrets in the account, including the Slack bot token. |
| `NoReadingSecretsBack` | An explicit `Deny` on `secretsmanager:GetSecretValue`. |
| `ReadMetrics` | `cloudwatch:GetMetricData` cannot be resource-scoped — CloudWatch has no metric-level ARNs — so `*` is the only expressible form. It is read-only. |
| `StartProvisioning` | Start one named state machine. Not `states:*`. |

### On the explicit Deny

The API writes credentials but must never read them back, because no route
should be able to return one to a browser. Relying on "we simply did not grant
`GetSecretValue`" is weaker than it looks: a later policy attached to the same
role, or a broad managed policy someone adds in a hurry, would silently grant
it. An explicit `Deny` cannot be overridden by any subsequent `Allow`, so the
guarantee holds regardless of what else accumulates on the role.

The trade-off is real: if this function ever legitimately needs to read a
secret, this statement must be removed deliberately rather than worked around.
That is the intended friction.

Note that `DeleteSecret` is a separate action, so deleting a tenant destroys its
credentials even though the API was never able to read them. Deletion is
*scheduled* with a 7-day recovery window rather than immediate, so a mistaken
delete is recoverable — but the tokens of a tenant that no longer exists do not
sit around indefinitely either.

Provisioning — which does need to read credentials to reach a customer's Jira —
runs under a **separate** role for exactly this reason.

## `bu_tenant_lambda_policy.json`

Referenced by the Multi-Customer Execution Plan (§3.2) for per-tenant worker
isolation via `aws:PrincipalTag/tenant_id`. Not yet written.
