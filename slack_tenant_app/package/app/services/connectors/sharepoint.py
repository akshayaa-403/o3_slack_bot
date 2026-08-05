from app.config import Config
from app.db import store
from app.services.connectors.base import BaseConnector


class SharePointConnector(BaseConnector):
    name = "sharepoint"
    display_name = "SharePoint"
    kind = "Documents"
    color = "#5059C9"
    blurb = "Read the documents your team already keeps in SharePoint and OneDrive."
    feeds = ["SharePoint site documents", "Text inside screenshots and scans"]
    logo = """<svg viewBox="0 0 48 48" fill="none">
        <rect x="2" y="2" width="20" height="20" fill="#F25022"/>
        <rect x="26" y="2" width="20" height="20" fill="#7FBA00"/>
        <rect x="2" y="26" width="20" height="20" fill="#00A4EF"/>
        <rect x="26" y="26" width="20" height="20" fill="#FFB900"/>
      </svg>"""

    def build_authorize_url(self, tenant_id: str) -> str:
        return (
            "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
            f"?client_id={Config.MS_CLIENT_ID}"
            "&scope=Sites.Read.All offline_access"
            f"&redirect_uri={Config.MS_REDIRECT_URI}"
            f"&state={tenant_id}"
            "&response_type=code"
        )

    def handle_callback(self, tenant_id: str, code: str) -> dict:
        # Production: exchange `code` at the /token endpoint, store tokens.
        store.mark_service_connected(tenant_id, self.name)
        return self.sync(tenant_id)

    def sync(self, tenant_id: str) -> dict:
        pulled_intents = {
            f"{tenant_id}_sharepoint_docs": {
                "body": "Synced from SharePoint document library (mock).",
                "source": "sharepoint",
            }
        }
        store.put_intents(tenant_id, pulled_intents)
        return {"service": self.name, "intents_added": list(pulled_intents.keys())}


CONNECTORS = {}


def register_connectors():
    from app.services.connectors.atlassian import AtlassianConnector
    from app.services.connectors.gsuite import GSuiteConnector
    CONNECTORS["atlassian"] = AtlassianConnector()
    CONNECTORS["sharepoint"] = SharePointConnector()
    CONNECTORS["gsuite"] = GSuiteConnector()
    return CONNECTORS
