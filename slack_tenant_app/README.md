# Slack App Authentication & Tenant Onboarding

A working implementation of the redesigned auth/onboarding architecture:
single "Continue with Slack" login → tenant creation → infra provisioning
→ optional Atlassian/SharePoint connectors → tenant-scoped Q&A.

## Quickstart (zero cloud setup required)

```bash
pip install -r requirements.txt
python3 demo.py          # runs the entire flow end-to-end, no credentials needed
```

or run the real server:

```bash
FLASK_APP=app.main python3 -m app.main
# visit http://localhost:5000/login
```

By default the app runs in **mock mode**: no `AWS_ACCESS_KEY_ID` or real
Slack/Atlassian/Microsoft credentials needed — `db.py` falls back to an
in-memory store and OAuth exchanges are simulated. Set the env vars in
`app/config.py` to point at real Slack/AWS/Atlassian/Microsoft apps for
production use; no other code changes are required.

## Project layout

```
app/
  config.py                     all env-driven configuration
  db.py                         dual-mode data layer (DynamoDB or in-memory)
  auth/slack_oauth.py           Slack OAuth (login + bot install, one flow)
  services/
    tenant_service.py           auth -> tenant resolution (separate concerns)
    provisioning.py             S3 bucket + default intents per tenant
    qa_service.py                LLM abstraction; model name never leaves this file
    connectors/
      base.py                   BaseConnector interface
      atlassian.py               Jira/Confluence connector
      sharepoint.py              SharePoint connector + connector registry
  main.py                        Flask routes wiring it all together
  templates/                     login.html, dashboard.html
demo.py                          end-to-end run with no external dependencies
```

## Key design decisions (see architecture notes)

- Auth and tenant-creation are separate steps (`slack_oauth.py` vs `tenant_service.py`).
- Provisioning is idempotent and isolated in `provisioning.py`, callable standalone or from a queue worker in production.
- Connectors implement a common interface (`base.py`) so adding a new one (e.g. G Suite) never touches routing logic.
- `qa_service.py` is the only file that knows the LLM provider/model — it's never in an API response or client-visible log.
- Every data-layer call is `tenant_id`-scoped by construction.
