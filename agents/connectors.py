"""Ticket connectors — the pluggable source of a customer's support history.

Agent 1 depends only on the TicketConnector interface, so a new platform
(Zendesk, ServiceNow, Freshservice, ...) is a new subclass and nothing else
changes. Jira is the first concrete connector; MockTicketConnector backs tests
and demos.

A connector returns a list of RawTicket dicts:
    {
      "id": str, "summary": str, "description": str,
      "comments": [str, ...], "status": str, "category": str,
      "created": str, "resolution": str,
    }
Only `id` and `summary` are required; the rest default to "".
"""

from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Dict, List, Optional


def normalize_ticket(raw: Dict) -> Dict:
    """Coerce a source record into the RawTicket shape with safe defaults."""
    return {
        "id": str(raw.get("id") or raw.get("key") or "").strip(),
        "summary": (raw.get("summary") or "").strip(),
        "description": (raw.get("description") or "").strip(),
        "comments": list(raw.get("comments") or []),
        "status": (raw.get("status") or "").strip(),
        "category": (raw.get("category") or "").strip(),
        "created": (raw.get("created") or "").strip(),
        "resolution": (raw.get("resolution") or "").strip(),
    }


class TicketConnector(ABC):
    name = "ticket-connector"

    @abstractmethod
    def fetch_tickets(self, *, limit: Optional[int] = None) -> List[Dict]:
        """Return normalized RawTicket dicts (most useful/most recent first)."""


# --- mock (tests / demos) ---------------------------------------
_SAMPLE_TICKETS = [
    {
        "id": "SUP-1001", "status": "Resolved", "category": "Password & Auth",
        "summary": "Can't reset my ADAM account password",
        "description": "I got an email saying I need to reset my privileged ADAM "
                       "account password but the self-service link errors out.",
        "comments": ["Use the ADAM Self Service portal > Privileged Account > Reset. "
                     "If it errors, clear cookies and retry on the corporate network."],
        "resolution": "Guided user through ADAM Self Service privileged reset flow.",
    },
    {
        "id": "SUP-1002", "status": "Resolved", "category": "VPN & Network",
        "summary": "GlobalProtect VPN keeps disconnecting on my laptop",
        "description": "VPN drops every few minutes since this morning.",
        "comments": ["Update GlobalProtect to the latest build and switch the gateway "
                     "to the regional portal. That stabilised the connection."],
        "resolution": "Updated GlobalProtect client and changed gateway.",
    },
    {
        "id": "SUP-1003", "status": "Resolved", "category": "Software & Access",
        "summary": "Need access to Smartsheet",
        "description": "How do I get access to Smartsheet? I keep getting an SSO "
                       "'no application found' error.",
        "comments": ["Raised a Smartsheet license request; access granted via SSO."],
        "resolution": "Provisioned Smartsheet license through SSO.",
    },
    {
        "id": "SUP-1004", "status": "Resolved", "category": "Software & Access",
        "summary": "Requesting Smartsheet license for planning",
        "description": "I need Smartsheet to manage project plans and resources.",
        "comments": ["Granted Smartsheet access."],
        "resolution": "Provisioned Smartsheet license.",
    },
]


class MockTicketConnector(TicketConnector):
    name = "mock"

    def __init__(self, tickets: Optional[List[Dict]] = None):
        source = tickets if tickets is not None else _SAMPLE_TICKETS
        self._tickets = [normalize_ticket(t) for t in source]

    def fetch_tickets(self, *, limit: Optional[int] = None) -> List[Dict]:
        return self._tickets[:limit] if limit else list(self._tickets)


# --- Jira (first real connector) --------------------------------------------
class JiraConnector(TicketConnector):
    """Pull resolved issues from Jira / Jira Service Management via the REST API.

    Uses basic auth (email + API token). Credentials are passed in (the caller
    resolves them from Secrets Manager), never hard-coded. Not exercised by the
    test suite — tests use MockTicketConnector — but implemented for real use.
    """

    name = "jira"

    def __init__(
        self,
        site_url: str,
        email: str,
        api_token: str,
        *,
        jql: str = "statusCategory = Done ORDER BY resolved DESC",
        timeout: int = 15,
    ):
        self._site_url = site_url.rstrip("/")
        self._email = email
        self._api_token = api_token
        self._jql = jql
        self._timeout = timeout

    def _auth_header(self) -> str:
        token = base64.b64encode(
            f"{self._email}:{self._api_token}".encode("utf-8")
        ).decode("utf-8")
        return f"Basic {token}"

    def _get(self, path: str, params: Dict) -> Dict:
        url = f"{self._site_url}{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "Authorization": self._auth_header()},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _plain_text(adf_or_str) -> str:
        """Flatten a Jira description (plain string or ADF doc) to text."""
        if isinstance(adf_or_str, str):
            return adf_or_str
        if not isinstance(adf_or_str, dict):
            return ""
        chunks: List[str] = []

        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "text" and node.get("text"):
                    chunks.append(node["text"])
                for child in node.get("content", []) or []:
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(adf_or_str)
        return " ".join(chunks).strip()

    def fetch_tickets(self, *, limit: Optional[int] = None) -> List[Dict]:
        tickets: List[Dict] = []
        start_at = 0
        page_size = 50
        while True:
            page = self._get(
                "/rest/api/3/search",
                {
                    "jql": self._jql,
                    "startAt": start_at,
                    "maxResults": page_size,
                    "fields": "summary,description,status,resolution,created,comment",
                },
            )
            issues = page.get("issues", [])
            for issue in issues:
                fields = issue.get("fields", {}) or {}
                comments = [
                    self._plain_text(c.get("body"))
                    for c in (fields.get("comment", {}) or {}).get("comments", [])
                ]
                tickets.append(normalize_ticket({
                    "id": issue.get("key"),
                    "summary": fields.get("summary"),
                    "description": self._plain_text(fields.get("description")),
                    "comments": [c for c in comments if c],
                    "status": (fields.get("status") or {}).get("name"),
                    "resolution": (fields.get("resolution") or {}).get("name"),
                    "created": fields.get("created"),
                }))
                if limit and len(tickets) >= limit:
                    return tickets
            start_at += len(issues)
            if not issues or start_at >= page.get("total", 0):
                break
        return tickets
