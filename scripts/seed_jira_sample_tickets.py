from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

TIMEOUT_SECONDS = 15

# Same 4 sample tickets as agents.connectors.MockTicketConnector, so a run
# against real Jira produces the same familiar "3 intents from 4 tickets"
# result as the local demo.
SAMPLE_TICKETS = [
    {
        "summary": "Can't reset my ADAM account password",
        "description": "I got an email saying I need to reset my privileged ADAM account "
                       "password but the self-service link errors out.",
        "resolution_comment": "Use the ADAM Self Service portal > Privileged Account > Reset. "
                              "If it errors, clear cookies and retry on the corporate network.",
    },
    {
        "summary": "GlobalProtect VPN keeps disconnecting on my laptop",
        "description": "VPN drops every few minutes since this morning.",
        "resolution_comment": "Update GlobalProtect to the latest build and switch the gateway "
                              "to the regional portal. That stabilised the connection.",
    },
    {
        "summary": "Need access to Smartsheet",
        "description": "How do I get access to Smartsheet? I keep getting an SSO "
                       "'no application found' error.",
        "resolution_comment": "Raised a Smartsheet license request; access granted via SSO.",
    },
    {
        "summary": "Requesting Smartsheet license for planning",
        "description": "I need Smartsheet to manage project plans and resources.",
        "resolution_comment": "Granted Smartsheet access.",
    },
]


def _load_dotenv() -> None:
    path = os.path.join(REPO_ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _adf(text: str) -> dict:
    """Wrap plain text as an Atlassian Document Format paragraph (API v3 needs this)."""
    return {
        "type": "doc", "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


class JiraClient:
    def __init__(self, site_url: str, email: str, token: str):
        self.site_url = site_url.rstrip("/")
        auth = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("utf-8")
        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Basic {auth}",
        }

    def _call(self, method: str, path: str, body=None):
        request = urllib.request.Request(
            f"{self.site_url}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers=self._headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:800]
            raise RuntimeError(f"{method} {path} -> HTTP {error.code}: {detail}") from error

    def project_issue_types(self, project_key: str):
        data = self._call("GET", f"/rest/api/3/project/{project_key}")
        return [t for t in data.get("issueTypes", []) if not t.get("subtask")]

    def create_issue(self, project_key: str, issue_type_name: str, summary: str, description: str) -> str:
        payload = {
            "fields": {
                "project": {"key": project_key},
                "issuetype": {"name": issue_type_name},
                "summary": summary,
                "description": _adf(description),
            }
        }
        return self._call("POST", "/rest/api/3/issue", payload)["key"]

    def add_comment(self, issue_key: str, text: str) -> None:
        self._call("POST", f"/rest/api/3/issue/{issue_key}/comment", {"body": _adf(text)})

    def transitions(self, issue_key: str):
        return self._call("GET", f"/rest/api/3/issue/{issue_key}/transitions").get("transitions", [])

    def transition_to_done(self, issue_key: str) -> str:
        """Move the issue to a 'Done'-category status. Returns the status name landed on."""
        candidates = self.transitions(issue_key)
        done = [t for t in candidates if (t.get("to") or {}).get("statusCategory", {}).get("key") == "done"]
        if not done:
            raise RuntimeError(
                f"no transition to a Done-category status found for {issue_key}. "
                f"Available: {[t.get('name') for t in candidates]}"
            )
        transition = done[0]
        try:
            self._call("POST", f"/rest/api/3/issue/{issue_key}/transitions",
                       {"transition": {"id": transition["id"]}})
        except RuntimeError as error:
            if "resolution" not in str(error).lower():
                raise
            # Workflow requires a resolution on this transition screen; retry with one set.
            self._call("POST", f"/rest/api/3/issue/{issue_key}/transitions", {
                "transition": {"id": transition["id"]},
                "fields": {"resolution": {"name": "Done"}},
            })
        return transition.get("to", {}).get("name", "Done")


def pick_issue_type(issue_types) -> str:
    names = {t["name"]: t for t in issue_types}
    for preferred in ("Task", "Story", "Bug"):
        if preferred in names:
            return preferred
    if not issue_types:
        raise RuntimeError("project has no non-subtask issue types to create against")
    return issue_types[0]["name"]


def parse_args():
    parser = argparse.ArgumentParser(description="Seed sample resolved tickets into a Jira project.")
    parser.add_argument("--project", help="Jira project key (falls back to JIRA_PROJECT_KEY).")
    parser.add_argument("--issue-type", help="Issue type name to create (auto-detected if omitted).")
    parser.add_argument("--dry-run", action="store_true",
                         help="Print what would be created; touch nothing in Jira.")
    return parser.parse_args()


def main() -> None:
    _load_dotenv()
    args = parse_args()

    project_key = args.project or os.environ.get("JIRA_PROJECT_KEY", "")
    if not project_key:
        print("No project key. Pass --project IVY or set JIRA_PROJECT_KEY in .env.")
        sys.exit(1)

    if args.dry_run:
        print(f"[dry-run] Would create {len(SAMPLE_TICKETS)} tickets in project {project_key!r}:")
        for ticket in SAMPLE_TICKETS:
            print(f"  - {ticket['summary']!r}  (resolution comment: {ticket['resolution_comment'][:60]}...)")
        print("\nNo network calls made. Remove --dry-run to actually create them.")
        return

    site_url = os.environ.get("JIRA_BASE_URL", "")
    email = os.environ.get("JIRA_EMAIL", "")
    token = os.environ.get("JIRA_API_TOKEN", "")
    missing = [n for n, v in (("JIRA_BASE_URL", site_url), ("JIRA_EMAIL", email), ("JIRA_API_TOKEN", token)) if not v]
    if missing:
        print(f"Missing Jira credentials in .env: {', '.join(missing)}")
        sys.exit(1)

    client = JiraClient(site_url, email, token)

    if args.issue_type:
        issue_type = args.issue_type
    else:
        issue_type = pick_issue_type(client.project_issue_types(project_key))
        print(f"Using issue type: {issue_type}")

    print(f"Creating {len(SAMPLE_TICKETS)} sample tickets in project {project_key}...\n")
    created = []
    for ticket in SAMPLE_TICKETS:
        try:
            key = client.create_issue(project_key, issue_type, ticket["summary"], ticket["description"])
            client.add_comment(key, ticket["resolution_comment"])
            status = client.transition_to_done(key)
            print(f"  {key}: {ticket['summary']!r} -> {status}")
            created.append(key)
        except RuntimeError as error:
            print(f"  FAILED ({ticket['summary']!r}): {error}")

    print(f"\nCreated {len(created)}/{len(SAMPLE_TICKETS)} tickets: {', '.join(created) or '(none)'}")
    if created:
        print(f"\nNext: python scripts/generate_intents_from_jira.py --project {project_key} --limit 5")


if __name__ == "__main__":
    main()
