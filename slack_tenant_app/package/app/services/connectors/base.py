"""
Every connector (Atlassian, SharePoint, future G Suite, etc.) implements
this interface so they're interchangeable to the rest of the app —
adding a new connector never requires touching main.py's routing logic
beyond registering it.
"""
from abc import ABC, abstractmethod


class BaseConnector(ABC):
    name: str  # e.g. "atlassian", "sharepoint"

    # Presentation metadata for the dashboard tile — kept on the connector
    # itself (not a separate lookup table) so adding a connector never means
    # touching two places to make it show up correctly.
    display_name: str = ""
    kind: str = ""
    color: str = "#8B8B94"
    logo: str = ""
    blurb: str = ""
    feeds: list[str] = []

    @abstractmethod
    def build_authorize_url(self, tenant_id: str) -> str:
        """Returns the URL to redirect the admin to for this service's OAuth consent."""
        ...

    @abstractmethod
    def handle_callback(self, tenant_id: str, code: str) -> dict:
        """Exchanges the OAuth code for tokens for this specific service."""
        ...

    @abstractmethod
    def sync(self, tenant_id: str) -> dict:
        """Pulls data from the service and writes tenant-scoped intents.
        Returns a small summary dict for logging/telemetry."""
        ...
