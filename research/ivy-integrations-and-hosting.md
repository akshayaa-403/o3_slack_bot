# Project IVY — Integration & Hosting Research Report

**Project:** IVY AI-powered IT/HR support assistant (multi-tenant SaaS)
**Scope:** Four integration surfaces + hosting/portability strategy

---

## Executive Summary

Project IVY must be built with a **thin adapter layer**, not hard-wired connectors. Based on market coverage × API maturity × integration feasibility, the definitive build path is:

- **Front-end channels (v1):** Slack, Microsoft Teams, and **Google Chat** (if targeting Google Workspace-heavy buyers). Slack/Teams are the defaults; Google Chat provides meaningful differentiated coverage.
- **ITSM back-ends (v1):** **Freshservice** (mid-market default), **ServiceNow** (enterprise must-have), and **Jira Service Management** (engineering-led organizations). Ship three to avoid feeling like a bespoke integration stack.
- **Knowledge grounding (v1):** **Microsoft 365/SharePoint/OneDrive**, **Slack history**, **Teams history**, and **Confluence**. These cover the highest-value internal knowledge workflows with the strongest permissions-aware retrieval support.
- **Intranet/EX (v1):** **Microsoft Viva/Viva Connections** (as a distribution surface). Defer other intranet platforms (Workvivo, Simpplr, Unily, etc.) to v2/v3 due to fragmented, partially-open APIs.
- **Hosting:** Stay on **AWS** for v1 but design for portability. The hardest components are not cloud runtime (Lambda→Functions is easy) but **identity/consent**, **event-driven semantics**, and **customer-specific permission trimming**.

**Build priority:** Collaboration and ITSM first → knowledge retrieval next → intranet as a later value-add → multi-cloud portability as an architectural requirement rather than a v1 feature.

---

## Category 1 — Collaboration Platforms (IVY's Front End)

### Explicit Verdicts

- **Slack:** YES
- **Microsoft Teams:** YES
- **Google Chat:** PARTIAL (viable but less mature)
- **Zoom Team Chat:** PARTIAL (narrower ecosystem)

### Comparison Table

| Vendor                    | Segment Fit                                                        | Analyst Position                                              | Bot API?          | Auth Model                                                            | Read/Write Scopes                                                            | Events/Webhooks                                 | Effort                | Recommend v1?            |
| ------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------- | ----------------- | --------------------------------------------------------------------- | ---------------------------------------------------------------------------- | ----------------------------------------------- | --------------------- | ------------------------ |
| **Slack**           | SMB to enterprise; mid-market/dev-centric default                  | Leader in modern collaboration; strongest developer ecosystem | **YES**     | OAuth 2.0 (Bot Token + user scopes)                                   | `channels:history`, `chat:write`, `commands`; full interactive replies | Events API + Socket Mode + slash commands       | **Medium**      | **Yes**            |
| **Microsoft Teams** | SMB to enterprise;**enterprise default** for M365 orgs       | Leading enterprise collaboration platform                     | **YES**     | Entra ID OAuth 2.0 + Bot Framework registration; tenant admin consent | Read/write chats/channels; send Adaptive Cards; handle actions               | Bot Framework webhooks + proactive messaging    | **Medium**      | **Yes**            |
| **Google Chat**     | SMB to enterprise;**strong for Google Workspace-heavy orgs** | Emerging/secondary platform; not a default analyst leader     | **PARTIAL** | GCP OAuth 2.0 + app installation; domain-wide delegation optional     | Read/write messages and cards; ecosystem narrower than Slack/Teams           | Event subscriptions + app support (less mature) | **Medium-High** | **Yes** (targeted) |
| **Zoom Team Chat**  | SMB to mid-market; lighter enterprise penetration                  | Emerging/adjacent; not a default leader                       | **PARTIAL** | OAuth 2.0 app credentials; narrower bot ecosystem                     | Post messages; handle basic bot interactions                                 | Webhooks available; less standardized           | **Medium-High** | Later                    |

### API Suitability Detail

- **Slack:** Full integration via OAuth, Events API, Block Kit. Main gotchas: scope selection, workspace-level installation, message payload differences (channels vs threads vs DMs). *Source: Slack OAuth docs, Events API, Block Kit.*
- **Teams:** Full integration via Bot Framework + Adaptive Cards. Main gotchas: Entra app manifest approval, tenant-scoped consent, card rendering variations across clients. *Source: Teams bot overview.*
- **Google Chat:** Partial—app and card support exist, but the event/card model is less developer-friendly. Requires more bespoke implementation. *Source: Google Chat developer docs.*
- **Zoom Chat:** Partial—supports app interactions, but the bot ecosystem is narrower and less standardized for interactive, workflow-oriented use cases.

### v1 Shortlist, Count & Rationale

- **V1 Shortlist (3):** Slack + Microsoft Teams + Google Chat.
- **V2:** Zoom Team Chat.
- **Coverage:** Slack and Teams cover ~85% of the target collaboration footprint. Google Chat adds meaningful coverage for Workspace-heavy buyers without excessive marginal cost. Zoom is lower priority due to less mature bot surfaces.

---

## Category 2 — ITSM Systems (Ticketing Back End + Learning Loop)

### Explicit Verdicts

- **Freshservice:** YES
- **ServiceNow:** YES
- **Jira Service Management:** YES
- **Zendesk:** YES (but v2)
- **ManageEngine ServiceDesk Plus:** PARTIAL
- **SolarWinds Service Desk:** PARTIAL

### Comparison Table

| Vendor                            | Segment Fit                                               | Analyst Position                                            | Bot API?          | Auth Model                                 | Read/Write Scopes                                                                  | Events/Webhooks              | Effort                | Recommend v1? |
| --------------------------------- | --------------------------------------------------------- | ----------------------------------------------------------- | ----------------- | ------------------------------------------ | ---------------------------------------------------------------------------------- | ---------------------------- | --------------------- | ------------- |
| **Freshservice**            | SMB to mid-market;**mid-market default**            | Strong mid-market ITSM player; default "modern" choice      | **YES**     | API Key / OAuth-style token                | Full CRUD on tickets; search/filter resolved tickets                               | Webhooks + REST              | **Low-Medium**  | **Yes** |
| **ServiceNow**              | Mid-market to enterprise;**enterprise default**     | Leading enterprise ITSM platform                            | **YES**     | OAuth 2.0 / Basic Auth (instance-specific) | Full CRUD on incident/request tables; comments/work notes; resolved-ticket queries | Webhooks + rich REST surface | **Medium-High** | **Yes** |
| **Jira Service Management** | SMB to enterprise;**engineering/DevOps-heavy orgs** | Strong/growing; default where Jira is the workflow backbone | **YES**     | OAuth 2.0 (Atlassian)                      | Full CRUD; resolved ticket retrieval via JQL                                       | Webhooks + REST              | **Medium**      | **Yes** |
| **Zendesk**                 | SMB to mid-market; simpler helpdesks                      | Broad adoption; better for support than ITSM                | **YES**     | OAuth 2.0 / API Tokens                     | Create/read/update/comment; resolved ticket search                                 | Webhooks                     | **Medium**      | Later         |
| **ManageEngine SDP**        | SMB to mid-market                                         | Niche; solid within its ecosystem                           | **PARTIAL** | API Tokens / Basic Auth                    | CRUD + comments possible, but less polished                                        | Webhooks exist, less common  | **Medium-High** | Later         |
| **SolarWinds Service Desk** | SMB to mid-market                                         | Smaller niche player                                        | **PARTIAL** | API-based with admin setup                 | Ticket CRUD possible; less standard                                                | Less common than top three   | **High**        | Later         |

### API Suitability Detail

- **Freshservice:** Excellent REST API for tickets, agents, comments. Gotcha: tenant-specific credentials + rate limits (cache accordingly). *Source: Freshservice API v2.*
- **ServiceNow:** Very broad REST surface. Gotcha: the "right" table/field mapping varies wildly per instance. *Source: ServiceNow REST API + OAuth docs.*
- **JSM:** OAuth 2.0 + REST issue API. Gotcha: project-level permissions + issue-type mapping. *Source: Atlassian developer docs.*
- **Zendesk:** Works fine, but less central than Freshservice/ServiceNow for IT/HR support learning loops. *Source: Zendesk API.*

### v1 Shortlist, Count & Rationale

- **V1 Shortlist (3):** Freshservice + ServiceNow + Jira Service Management.
- **V2:** Zendesk, ManageEngine, SolarWinds.
- **Coverage:** Freshservice covers the widest mid-market segment; ServiceNow covers enterprise; JSM covers engineering-led orgs. This trio gives broad coverage without turning IVY into a bespoke integration factory.

---

## Category 3 — Internal Knowledge Sources (Grounding for L2/L3)

### Explicit Verdicts

- **Microsoft Graph (SP/OneDrive):** YES
- **Confluence:** YES
- **Slack history:** YES
- **Teams history:** YES
- **Google Drive/Docs:** PARTIAL (viable but permission-complex)
- **Notion:** YES

### Comparison Table

| Source                                  | Segment Fit                                       | Analyst Position                                | Bot API?          | Auth Model                                                    | Read/Write Scopes                              | Governance/ACL                                                                   | Effort                | Recommend v1? |
| --------------------------------------- | ------------------------------------------------- | ----------------------------------------------- | ----------------- | ------------------------------------------------------------- | ---------------------------------------------- | -------------------------------------------------------------------------------- | --------------------- | ------------- |
| **Microsoft Graph (SP/OneDrive)** | SMB to enterprise;**M365 org default**      | Strong enterprise grounding default             | **YES**     | Entra ID OAuth 2.0 (delegated/app permissions; admin consent) | Search/read files, sites, lists                | **Strong** — per-user trimming via delegated tokens or `Sites.Selected` | **Medium**      | **Yes** |
| **Confluence**                    | SMB to enterprise; technical/knowledge-heavy orgs | Strong KB integration target                    | **YES**     | OAuth 2.0 (Atlassian)                                         | Search/read pages and spaces                   | Space-level permissions enforced                                                 | **Medium**      | **Yes** |
| **Slack history**                 | SMB to enterprise; chat-first orgs                | Strong informal knowledge source                | **YES**     | OAuth 2.0 (Bot Token)                                         | `conversations.history`, search              | Channel-membership filtering                                                     | **Medium**      | **Yes** |
| **Teams history**                 | SMB to enterprise; M365 orgs                      | Strong operational chat context                 | **YES**     | Microsoft Graph (Entra)                                       | `Chat.Read.All`, `ChannelMessage.Read.All` | Per-chat/channel permissions                                                     | **Medium**      | **Yes** |
| **Google Drive / Docs**           | SMB to enterprise; Workspace-heavy orgs           | Important but admin-sensitive                   | **PARTIAL** | OAuth 2.0 / Service Account; domain-wide delegation           | Search/read docs and shared drives             | Per-file ACLs; more complex than Graph                                           | **Medium-High** | Later         |
| **Notion**                        | SMB to mid-market; product/startup orgs           | Useful but less common in regulated enterprises | **YES**     | OAuth 2.0 Integration Token                                   | Search/read pages and databases                | Workspace/page permissions                                                       | **Medium**      | Later         |

### Governance Requirement (Critical)

IVY must **never** surface content a user cannot see. This requires a unified permission-filtering layer that respects source-native ACLs (Graph delegated tokens, Slack channel membership, Confluence space permissions).

### v1 Shortlist, Count & Rationale

- **V1 Shortlist (4):** Microsoft Graph/SP/OneDrive + Confluence + Slack history + Teams history.
- **V2:** Google Drive/Docs + Notion.
- **Coverage:** These four cover the most common enterprise IT/HR knowledge workflows (enterprise files, technical KBs, and operational chat history) with the strongest permissions-aware retrieval patterns.

---

## Category 4 — Intranet / Employee Experience Platforms

### Explicit Verdicts

- **Microsoft Viva:** PARTIAL (best treated as a distribution surface, not a standalone API)
- **Workvivo (Zoom):** PARTIAL
- **Simpplr:** PARTIAL
- **Unily:** PARTIAL
- **LumApps:** PARTIAL
- **Staffbase:** PARTIAL

### Comparison Table

| Vendor                    | Segment Fit                                  | Analyst Position                        | Bot API?          | Auth Model              | Read/Write                                         | Events                             | Effort                | Recommend v1?              |
| ------------------------- | -------------------------------------------- | --------------------------------------- | ----------------- | ----------------------- | -------------------------------------------------- | ---------------------------------- | --------------------- | -------------------------- |
| **Microsoft Viva**  | SMB to enterprise;**M365 org default** | Strong enterprise EX layer atop M365    | **PARTIAL** | Entra ID / Graph        | Reads M365 context; limited content distribution   | Best used via Teams/Graph patterns | **Medium**      | **Yes** (as surface) |
| **Workvivo (Zoom)** | Mid-market/enterprise                        | Emerging/modern EX player; strong brand | **PARTIAL** | Vendor OAuth            | Content/comm use cases, non-standardized           | Limited/predictable                | **Medium-High** | Later                      |
| **Simpplr**         | Mid-market/enterprise                        | Emerging intranet leader                | **PARTIAL** | Vendor app auth         | Content retrieval; less universal than Graph       | Limited public integrations        | **Medium-High** | Later                      |
| **Unily**           | Enterprise                                   | Enterprise-focused EX                   | **PARTIAL** | Vendor-specific         | Content retrieval; bespoke integration             | More bespoke                       | **High**        | Later                      |
| **LumApps**         | SMB/enterprise                               | Lightweight intranet alternative        | **PARTIAL** | Vendor app registration | Content ingestion; indirect API story              | Some support, but heavy lift       | **Medium-High** | Later                      |
| **Staffbase**       | Mid-market/enterprise                        | Comms-led platform                      | **PARTIAL** | OAuth / admin setup     | Comms workflows; less suited for general retrieval | Limited/comms-oriented             | **Medium-High** | Later                      |

### API Suitability Detail

- **Viva:** Not a standalone API-first platform. Best used to surface answers *into* the M365 employee experience via Teams/SharePoint. *Sources: Microsoft Viva & Teams docs.*
- **Workvivo/Simpplr/Unily/LumApps/Staffbase:** Partial. Fragmented integration surfaces. Treat them as "content distribution surfaces" for publishing answers, not as primary data sources for v1.

### v1 Shortlist, Count & Rationale

- **V1 Shortlist (1):** Microsoft Viva / Viva Connections (as a distribution channel).
- **V2:** Workvivo, Simpplr, LumApps, Unily, Staffbase.
- **Coverage:** Keeps v1 focused on the strongest enterprise/Microsoft-first distribution path. Deferring fragmented EX platforms avoids engineering over-investment in low-API-maturity surfaces.

---

## Hosting Decision: AWS Now, Portability Later

### (A) Now — AWS Backend + Connect-Out

IVY's AWS backend (Lambda, SQS, DynamoDB, Bedrock, Textract) must authenticate outward to customer-owned Microsoft Entra/Graph and Google Workspace.

**Customer Ecosystem Authentication:**

| Customer Ecosystem      | Auth Method                                      | Implementation Notes                                                              |
| ----------------------- | ------------------------------------------------ | --------------------------------------------------------------------------------- |
| Microsoft Entra / Graph | OAuth 2.0 (delegated + app permissions)          | App registration per customer; admin consent required for broad scopes            |
| Google Workspace        | OAuth 2.0 (Client Credentials / Service Account) | GCP project + OAuth Client ID; domain-wide delegation for admin-managed retrieval |

**Security Baseline:** Least privilege, tenant-scoped tokens, encryption at rest, audit logging, regional data handling—critical for enterprise/regulated buyers.

### (B) Later — Multi-Cloud Portability

**AWS → Azure → GCP Mapping & Effort:**

| AWS Service     | Azure Equivalent            | GCP Equivalent     | Porting Effort        | Hardest Component                                         |
| --------------- | --------------------------- | ------------------ | --------------------- | --------------------------------------------------------- |
| AWS Lambda      | Azure Functions             | Cloud Functions    | **Low**         | Event trigger mapping                                     |
| Amazon SQS      | Azure Service Bus           | Cloud Pub/Sub      | **Low-Medium**  | Queue semantics/adapter                                   |
| Amazon DynamoDB | Azure Cosmos DB             | Cloud Firestore    | **Medium**      | Schema alignment; single-table design doesn't map cleanly |
| Amazon Bedrock  | Azure OpenAI / AI Foundry   | Vertex AI          | **Medium**      | Model selection, prompt/caching differences               |
| Amazon Lex      | Azure Bot Service           | Dialogflow CX      | **Medium-High** | Dialog flow/state management rework (non-trivial)         |
| Amazon Textract | Azure Document Intelligence | Google Document AI | **Medium**      | Extraction schema adaptation                              |

**Hardest Parts Overall (Not Just Runtime):**

1. **Identity & tenant-specific permissions** (hardest operational challenge).
2. **Event-driven integration semantics** (Slack/Teams/ITSM webhooks differ across platforms).
3. **Governance & compliance** (region choice, retention, auditability).

### Security, Compliance & Data-Residency Expectations by Segment

| Segment                           | Expected Baseline                                                                                                                                        |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **SMB (<100)**              | SOC 2 Type II, ISO 27001 (increasingly expected); GDPR if EU customers                                                                                   |
| **Mid-market (100–2,000)** | SOC 2 Type II, ISO 27001, GDPR readiness, regional hosting options                                                                                       |
| **Enterprise (2,000+)**     | Full posture: SOC 2, ISO 27001, GDPR, HIPAA (where relevant), private networking, customer-managed keys, audit retention, region-specific data residency |

**Recommended Posture:** Stay AWS for v1 with portability-by-design. Add Azure as v2 target (M365 enterprise demand). Add GCP as v3 target (Google/data-sovereignty requirements).

---

## Console Build Plan — Phased v1 → v2 → v3

### V1 — Core (Ship with IVY 1.0)

| Category                | Platforms                                                                | Count        | Rationale                                                        |
| ----------------------- | ------------------------------------------------------------------------ | ------------ | ---------------------------------------------------------------- |
| **Collaboration** | Slack + Microsoft Teams + Google Chat                                    | **3**  | Covers dominant footprints + Workspace-heavy buyers              |
| **ITSM**          | Freshservice + ServiceNow + Jira Service Management                      | **3**  | Mid-market default + enterprise must-have + engineering-led orgs |
| **Knowledge**     | Microsoft Graph/SP/OneDrive + Confluence + Slack history + Teams history | **4**  | Enterprise files, KBs, and operational chat history              |
| **Intranet**      | Microsoft Viva (as distribution surface)                                 | **1**  | Strongest enterprise EX path; avoid fragmented API surfaces      |
| **Hosting**       | AWS backend with tenant-scoped credentials & connector framework         | —           |                                                                  |
| **V1 Total**      |                                                                          | **11** |                                                                  |

### V2 — Expand (6–12 months post-v1)

| Category                | Platforms Added                       | Count        | Rationale                            |
| ----------------------- | ------------------------------------- | ------------ | ------------------------------------ |
| **Collaboration** | + Zoom Team Chat                      | +1           | Broaden UC coverage                  |
| **ITSM**          | + Zendesk + ManageEngine + SolarWinds | +3           | Cover long-tail helpdesks            |
| **Knowledge**     | + Google Drive/Docs + Notion          | +2           | Workspace-heavy and startup segments |
| **Intranet**      | + Workvivo + Simpplr                  | +2           | Most mature APIs among Leaders       |
| **Hosting**       | + Azure portability                   | —           | M365 enterprise demand               |
| **V2 Total**      |                                       | **+8** |                                      |

### V3 — Long Tail (12–24 months)

| Category                | Platforms Added                             | Count        |
| ----------------------- | ------------------------------------------- | ------------ |
| **Intranet**      | + Unily + LumApps + Interact + Staffbase    | +4           |
| **Hosting**       | + GCP portability + data-residency controls | —           |
| **Learning Loop** | Cross-source knowledge-article generation   | —           |
| **V3 Total**      |                                             | **+4** |

---

## Cited Sources (2026)

| Source                         | URL                                                                                                    | Topic           |
| ------------------------------ | ------------------------------------------------------------------------------------------------------ | --------------- |
| Slack OAuth & app installation | https://docs.slack.dev/authentication/installing-with-oauth                                            | Slack auth      |
| Slack Events API               | https://docs.slack.dev/apis/events-api/                                                                | Slack events    |
| Slack Block Kit                | https://docs.slack.dev/block-kit/                                                                      | Slack UI        |
| Microsoft Teams bots           | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/what-are-bots                           | Teams bot       |
| Teams Adaptive Cards           | https://learn.microsoft.com/en-us/microsoftteams/platform/task-modules-and-cards/cards/cards-reference | Teams UI        |
| Google Chat developer docs     | https://developers.google.com/workspace/chat                                                           | Google Chat     |
| Freshservice API v2            | https://api.freshservice.com/v2/                                                                       | Freshservice    |
| Freshworks developer docs      | https://developers.freshworks.com/freshservice/docs/                                                   | Freshservice    |
| ServiceNow REST API            | https://developer.servicenow.com/dev.do#!/reference/api/rome/rest                                      | ServiceNow      |
| ServiceNow OAuth               | https://www.servicenow.com/docs/csh?topicname=t_ConfigureOAuth2.html&version=latest                    | ServiceNow auth |
| JSM developer docs             | https://developer.atlassian.com/cloud/jira/service-desk/                                               | JSM             |
| Zendesk API                    | https://developer.zendesk.com/api-reference/introduction/                                              | Zendesk         |
| Microsoft Graph overview       | https://learn.microsoft.com/en-us/graph/overview                                                       | Graph           |
| Microsoft Graph search         | https://learn.microsoft.com/en-us/graph/search-concept-overview                                        | Graph search    |
| Slack conversation history     | https://docs.slack.dev/reference/methods/conversations.history                                         | Slack history   |
| Slack search messages          | https://docs.slack.dev/reference/methods/search.messages                                               | Slack search    |
| Confluence dev docs            | https://developer.atlassian.com/cloud/confluence/rest/v2/intro/                                        | Confluence      |
| Google Drive API               | https://developers.google.com/drive/api/guides/about-sdk                                               | Google Drive    |
| Notion API                     | https://developers.notion.com/docs/getting-started                                                     | Notion          |
| Microsoft Viva                 | https://www.microsoft.com/en-us/microsoft-viva                                                         | Viva            |
| Workvivo                       | https://www.workvivo.com/                                                                              | Workvivo        |
| Simpplr                        | https://www.simpplr.com/                                                                               | Simpplr         |
| LumApps                        | https://www.lumapps.com/                                                                               | LumApps         |
| Staffbase                      | https://www.staffbase.com/                                                                             | Staffbase       |
| AWS Lambda                     | https://docs.aws.amazon.com/lambda/latest/dg/welcome.html                                              | AWS compute     |
| Azure Functions                | https://learn.microsoft.com/en-us/azure/azure-functions/functions-overview                             | Azure compute   |
| GCP Cloud Functions            | https://cloud.google.com/functions/docs                                                                | GCP compute     |
| AWS SQS                        | https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/welcome.html                | AWS queue       |
| Azure Service Bus              | https://learn.microsoft.com/en-us/azure/service-bus-messaging/service-bus-messaging-overview           | Azure queue     |
| GCP Pub/Sub                    | https://cloud.google.com/pubsub/docs/overview                                                          | GCP queue       |
| Amazon DynamoDB                | https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Introduction.html                     | AWS DB          |
| Azure Cosmos DB                | https://learn.microsoft.com/en-us/azure/cosmos-db/introduction                                         | Azure DB        |
| GCP Firestore                  | https://cloud.google.com/firestore/docs                                                                | GCP DB          |
| Amazon Bedrock                 | https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html                              | AWS AI          |
| Azure OpenAI / AI Foundry      | https://learn.microsoft.com/en-us/azure/ai-foundry/what-is-azure-ai-foundry                            | Azure AI        |
| Vertex AI                      | https://cloud.google.com/vertex-ai/docs/start/introduction-unified-platform                            | GCP AI          |
| Amazon Textract                | https://docs.aws.amazon.com/textract/latest/dg/what-is.html                                            | AWS OCR         |
| Azure Document Intelligence    | https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/overview                     | Azure OCR       |
| Google Document AI             | https://cloud.google.com/document-ai/docs/overview                                                     | GCP OCR         |

---

## Open Contentions / Uncertain Items

- **Analyst labels** for collaboration and intranet platforms vary by report cycle/region; positions above reflect the best public 2025–2026 signal rather than a claim of an exact current Gartner/Forrester report outcome.
- Several **intranet/EX tools** expose less public API detail than collaboration/ITSM platforms. Assessments lean on public product/developer docs rather than guaranteed rate-limit or eventing specifications.
- **Security/compliance** requirements are customer- and region-specific; IVY should implement them as configurable controls rather than a single fixed baseline.
