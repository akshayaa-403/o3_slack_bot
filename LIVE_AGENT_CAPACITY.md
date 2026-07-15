# Live-agent capacity dispatcher

IVY uses Jira as the ticket system. By default, the `live_agent_capacity`
DynamoDB table is the source of truth for agent shifts, Jira account IDs,
capacity, and load. `o3_live_agent` selects the least-loaded on-shift agent and
reserves a slot with a conditional DynamoDB update before assigning the Jira
issue.

Optionally, IVY can use Jira Service Management Operations on-call schedules as
the source of truth for who is active in the current slot. In that mode, JSM
returns the current on-call responders, and DynamoDB is still used for
`active_count`, `max_capacity`, enable/disable flags, priorities, and atomic
reservation.

If no slot is available, the ticket remains unassigned with
`assignment_status=QUEUED`. A terminal Jira status releases capacity exactly
once and promotes the oldest queued ticket that can be assigned to an on-shift
agent.

## Create the table

```powershell
aws dynamodb create-table `
  --table-name live_agent_capacity `
  --attribute-definitions AttributeName=agent_id,AttributeType=S `
  --key-schema AttributeName=agent_id,KeyType=HASH `
  --billing-mode PAY_PER_REQUEST `
  --region ap-southeast-2
```

## Add agents

Replace the Jira account ID placeholders with Jira Cloud `accountId` values.

```powershell
aws dynamodb put-item --region ap-southeast-2 --table-name live_agent_capacity --item '{"agent_id":{"S":"KARAN"},"display_name":{"S":"Karan"},"jira_account_id":{"S":"<JIRA_ACCOUNT_ID_KARAN>"},"shift_name":{"S":"DAY"},"shift_start":{"S":"08:00"},"shift_end":{"S":"20:00"},"timezone":{"S":"Asia/Kolkata"},"active_count":{"N":"0"},"max_capacity":{"N":"5"},"enabled":{"BOOL":true},"priority":{"N":"1"}}'

aws dynamodb put-item --region ap-southeast-2 --table-name live_agent_capacity --item '{"agent_id":{"S":"VIBHU"},"display_name":{"S":"Vibhu Gandhi"},"jira_account_id":{"S":"<JIRA_ACCOUNT_ID_VIBHU>"},"shift_name":{"S":"NIGHT"},"shift_start":{"S":"20:00"},"shift_end":{"S":"08:00"},"timezone":{"S":"Asia/Kolkata"},"active_count":{"N":"0"},"max_capacity":{"N":"5"},"enabled":{"BOOL":true},"priority":{"N":"1"}}'
```

Add another agent with another `put-item`. Multiple agents can share the same
shift. Lower `active_count` wins, followed by lower `priority`, then display
name.

Normal shifts use `start <= current < end`. Cross-midnight shifts use
`current >= start OR current < end`.

## Use JSM on-call schedules for active-slot membership

Enable this when you want Jira Service Management to determine who is currently
active in the slot instead of maintaining shifts in DynamoDB.

Required Lambda environment variables:

```text
ENABLE_JSM_ONCALL_SOURCE=true
JSM_OPS_CLOUD_ID=<your Atlassian cloud ID>
JSM_ONCALL_SCHEDULE_IDS=<schedule-id-1>[,<schedule-id-2>]
JSM_OPS_API_KEY=<JSM Operations API integration key>
```

Optional:

```text
JSM_OPS_BASE_URL=https://api.atlassian.com/jsm/ops
JSM_OPS_AUTH_HEADER=GenieKey <api-key>
JSM_ONCALL_FALLBACK_TO_SHIFT=true
```

When `ENABLE_JSM_ONCALL_SOURCE=true`, IVY calls:

```text
GET https://api.atlassian.com/jsm/ops/api/{cloudId}/v1/schedules/{scheduleId}/on-calls?flat=true
```

The dispatcher then:

1. extracts the current on-call user IDs from JSM;
2. matches them to DynamoDB agents by `jira_account_id`;
3. skips agents not currently on-call;
4. skips full or disabled agents;
5. assigns the least-loaded eligible agent.

Every JSM on-call responder must still have a DynamoDB row. The row does not
need to define the real shift when JSM on-call mode is working, but it must
include:

```json
{
  "agent_id": "KARAN",
  "display_name": "Karan",
  "jira_account_id": "<same account ID returned by JSM on-call>",
  "active_count": 0,
  "max_capacity": 5,
  "enabled": true,
  "priority": 1
}
```

If JSM on-call lookup fails and `JSM_ONCALL_FALLBACK_TO_SHIFT=true`, IVY falls
back to the DynamoDB shift fields. Set `JSM_ONCALL_FALLBACK_TO_SHIFT=false` if
you prefer queueing instead of fallback assignment when JSM on-call is
unavailable.

## Operations

Change maximum capacity:

```powershell
aws dynamodb update-item --region ap-southeast-2 --table-name live_agent_capacity --key '{"agent_id":{"S":"KARAN"}}' --update-expression "SET max_capacity = :capacity" --expression-attribute-values '{":capacity":{"N":"5"}}'
```

Temporarily disable an agent:

```powershell
aws dynamodb update-item --region ap-southeast-2 --table-name live_agent_capacity --key '{"agent_id":{"S":"KARAN"}}' --update-expression "SET enabled = :disabled" --expression-attribute-values '{":disabled":{"BOOL":false}}'
```

Reset capacity during testing:

```powershell
aws dynamodb update-item --region ap-southeast-2 --table-name live_agent_capacity --key '{"agent_id":{"S":"KARAN"}}' --update-expression "SET active_count = :zero" --expression-attribute-values '{":zero":{"N":"0"}}'
```

Jira callbacks with `Done`, `Resolved`, `Closed`, `Cancelled`, or `Canceled`
release the assigned agent once using the reverse mapping
`live_agent_ticket:<ticket_key>`. The callback then scans queued mappings by
oldest `created_at`, reserves current-shift capacity, assigns the Jira issue,
updates its labels, and notifies the original Slack conversation.
