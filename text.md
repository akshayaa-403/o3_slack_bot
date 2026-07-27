# Overview

`agents/review.py` already separates the neutral card (`build_card()` → `proposal_id`, `title`, `rows`, `actions`) from the platform rendering + delivery (`render_slack_blocks()` + `SlackReviewSurface`). Adding a new platform = one new `render_*()` function + one new `ReviewSurface` subclass. Nothing upstream (Agents 1/2, the loader, the store) changes. So the porting question is really just: **how does each platform deliver a button click back to your code?**

## 1. Microsoft Teams — yes, but architecturally heavier

The card concept ports directly (Slack Block Kit → Adaptive Cards JSON, `Action.Execute` is ideal — you return a replacement card to update it in place). The catch is delivery:

- **No Socket Mode equivalent.** This is the big one. Your Slack demo uses an outbound WebSocket so it runs on your laptop with no public URL. Teams has nothing like it — the Bot Connector pushes every event as an inbound HTTPS POST to a single `/api/messages` endpoint. You need a public HTTPS URL (a Dev Tunnel/ngrok for dev, real hosting + TLS for prod).
- **Mandatory registration objects:** an Azure Bot resource + a Microsoft Entra (Azure AD) app registration + a Teams app manifest package. Slack is just "create an app in the dashboard."
- **SDK churn to know about:** the old Bot Framework SDK (`botbuilder-*`) is archived/EOL as of Dec 31 2025. New code should use the Microsoft 365 Agents SDK (`pip install microsoft-agents-hosting-aiohttp microsoft-agents-hosting-core microsoft-agents-authentication-msal`). Most tutorials you'll find still show `botbuilder-*` — the wire protocol is identical, only the package/class names differ.

**Bottom line:** doable, same pipeline, but you trade "run it on my laptop" for "stand up a public HTTPS service + Azure/Entra registration."

## 2. Other platforms — ranked by how close they are to your Slack Socket Mode setup

| Platform         | Button click arrives via                       | Public HTTPS endpoint?                                        | Port difficulty                                                 |
| ---------------- | ---------------------------------------------- | ------------------------------------------------------------- | --------------------------------------------------------------- |
| Slack (baseline) | WebSocket (Socket Mode)                        | Optional                                                      | —                                                              |
| Discord          | Gateway WebSocket, route by`custom_id`       | Optional                                                      | **Easiest** — near 1:1 with Socket Mode (`discord.py`) |
| Cisco Webex      | WebSocket via`webex-bot` lib, Adaptive Cards | Optional                                                      | **Easy** (`webexpythonsdk` + `webex-bot`)             |
| Google Chat      | HTTPS webhook or Cloud Pub/Sub pull            | Partial (Pub/Sub avoids it, but loses dialogs, heavier setup) | Moderate                                                        |
| MS Teams         | HTTPS`/api/messages`                         | **Required**                                            | Hard                                                            |
| Zoom Team Chat   | HTTPS webhook + OAuth + callback token         | **Required**                                            | Hardest, no SDK                                                 |

**If keeping the no-public-server convenience matters, Discord and Webex are the closest drop-in ports** — both give you an outbound WebSocket and route clicks by an action id, exactly like your current design.

## 3. Automating the ticket-fetching side (your `connectors.py` seam)

Right now `agents/connectors.py` pulls from Jira on demand. Three automation patterns, in order of robustness:

1. **Scheduled polling** — a cron job queries "tickets resolved since last run." Simple, needs no public endpoint, survives downtime (next run catches up). Latency = poll interval.
2. **Event-driven webhooks** — the ticketing system POSTs when a ticket resolves. Near-real-time, but deliveries can be silently dropped, so it can't stand alone.
3. **No-code rules → webhook** — e.g. Jira Automation "Send web request" on "status → Done." Same as (b) but business users own the trigger.

**Recommended architecture: hybrid.** webhook/rule → queue (SQS) → idempotent processor for freshness, plus a scheduled `updated-since` poll as a reconciliation backstop (webhooks are best-effort everywhere). Key your intent store by ticket ID so replays are no-ops.

## Two things specific to you:

> ⚠️ **Your current Jira code may be broken soon/now:** the old `POST /rest/api/3/search` was removed from Jira Cloud in 2025. You must move to `POST /rest/api/3/search/jql` with token-based pagination (`nextPageToken`, no more `startAt`/`total`). Worth checking `scripts/generate_intents_from_jira.py`.

Every major system (Jira, Zendesk, Freshdesk, Salesforce, Dynamics) supports both pull and push — except ServiceNow, which has no externally-registered webhooks (you poll `sys_updated_on`, or an admin wires a Business Rule → Outbound REST).


---

**Tasks:**

- [ ] Check/fix the Jira search endpoint in your generate script (it may already be hitting the removed API — that could be part of your earlier Jira failures).
- [ ] Add a `TeamsReviewSurface` (or Discord, if you want to keep the laptop-friendly WebSocket flow) alongside `SlackReviewSurface` to prove the multi-platform seam.
- [ ] Add a scheduled-poll connector with an `updated-since` cursor so ingestion runs unattended.
