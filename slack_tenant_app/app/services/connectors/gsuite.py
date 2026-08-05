import logging

from app.config import Config
from app.db import store
from app.services.connectors.base import BaseConnector

logger = logging.getLogger(__name__)


class GSuiteConnector(BaseConnector):
    name = "gsuite"
    display_name = "G Suite"
    kind = "Documents"
    color = "#4285F4"
    blurb = "One sign-in covers Sites, Docs and Sheets."
    feeds = ["Google Sites pages", "Google Docs files", "Google Sheets files"]
    logo = """<svg viewBox="-0.5 0 48 48" fill="none">
        <path d="M9.82727273,24 C9.82727273,22.4757333 10.0804318,21.0144 10.5322727,19.6437333 L2.62345455,13.6042667 C1.08206818,16.7338667 0.213636364,20.2602667 0.213636364,24 C0.213636364,27.7365333 1.081,31.2608 2.62025,34.3882667 L10.5247955,28.3370667 C10.0772273,26.9728 9.82727273,25.5168 9.82727273,24" fill="#FBBC05"/>
        <path d="M23.7136364,10.1333333 C27.025,10.1333333 30.0159091,11.3066667 32.3659091,13.2266667 L39.2022727,6.4 C35.0363636,2.77333333 29.6954545,0.533333333 23.7136364,0.533333333 C14.4268636,0.533333333 6.44540909,5.84426667 2.62345455,13.6042667 L10.5322727,19.6437333 C12.3545909,14.112 17.5491591,10.1333333 23.7136364,10.1333333" fill="#EB4335"/>
        <path d="M23.7136364,37.8666667 C17.5491591,37.8666667 12.3545909,33.888 10.5322727,28.3562667 L2.62345455,34.3946667 C6.44540909,42.1557333 14.4268636,47.4666667 23.7136364,47.4666667 C29.4455,47.4666667 34.9177955,45.4314667 39.0249545,41.6181333 L31.5177727,35.8144 C29.3995682,37.1488 26.7323182,37.8666667 23.7136364,37.8666667" fill="#34A853"/>
        <path d="M46.1454545,24 C46.1454545,22.6133333 45.9318182,21.12 45.6113636,19.7333333 L23.7136364,19.7333333 L23.7136364,28.8 L36.3181818,28.8 C35.6879545,31.8912 33.9724545,34.2677333 31.5177727,35.8144 L39.0249545,41.6181333 C43.3393409,37.6138667 46.1454545,31.6490667 46.1454545,24" fill="#4285F4"/>
      </svg>"""

    def build_authorize_url(self, tenant_id: str) -> str:
        return (
            "https://accounts.google.com/o/oauth2/v2/auth"
            f"?client_id={Config.GSUITE_CLIENT_ID}"
            "&scope=https://www.googleapis.com/auth/drive.readonly"
            f"&redirect_uri={Config.GSUITE_REDIRECT_URI}"
            f"&state={tenant_id}"
            "&response_type=code&access_type=offline&prompt=consent"
        )

    def handle_callback(self, tenant_id: str, code: str) -> dict:
        # Production: exchange `code` at Google's /token endpoint, store tokens.
        store.mark_service_connected(tenant_id, self.name)
        return self.sync(tenant_id)

    def sync(self, tenant_id: str) -> dict:
        pulled_intents = {
            f"{tenant_id}_gsuite_docs": {
                "body": "Synced from Google Drive document overview (mock).",
                "source": "gsuite",
            }
        }
        store.put_intents(tenant_id, pulled_intents)
        return {"service": self.name, "intents_added": list(pulled_intents.keys())}
