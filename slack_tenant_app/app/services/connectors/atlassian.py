import logging
from urllib.parse import quote

import requests

from app.config import Config
from app.db import store
from app.services.connectors.base import BaseConnector

logger = logging.getLogger(__name__)


class AtlassianConnector(BaseConnector):
    name = "atlassian"
    display_name = "Atlassian"
    kind = "Ticketing & docs"
    color = "#0052CC"
    blurb = "One sign-in covers Jira and Confluence."
    feeds = ["Resolved Jira issues and their resolution comments", "Confluence pages, including diagrams"]
    logo = """<svg viewBox="0 0 256 256" fill="none">
        <path d="M75.7929022,117.949352 C71.973435,113.86918 66.0220743,114.100451 63.4262382,119.292123 L0.791180865,244.565041 C-0.370000214,246.886207 -0.24632242,249.643151 1.11803323,251.85102 C2.48238888,254.058889 4.89280393,255.402741 7.48821365,255.402516 L94.716435,255.402516 C97.5716401,255.468706 100.19751,253.845601 101.414869,251.262074 C120.223468,212.37359 108.82814,153.245434 75.7929022,117.949352 Z" fill="url(#atl-g)"/>
        <path d="M121.756071,4.0114918 C86.7234975,59.5164098 89.0348008,120.989508 112.109989,167.141287 L154.170383,251.262074 C155.438703,253.798733 158.031349,255.401095 160.867416,255.401115 L248.094235,255.401115 C250.689645,255.401339 253.10006,254.057487 254.464416,251.849618 C255.828771,249.64175 255.952449,246.884805 254.791268,244.563639 C254.791268,244.563639 137.44462,9.83670492 134.492768,3.96383607 C131.853481,-1.29371311 125.14944,-1.36519672 121.756071,4.0114918 Z" fill="#2681FF"/>
        <defs><linearGradient id="atl-g" x1="99.6865531%" y1="15.8007988%" x2="39.8359011%" y2="97.4378355%" gradientUnits="objectBoundingBox">
          <stop stop-color="#0052CC" offset="0%"/><stop stop-color="#2684FF" offset="92.3%"/>
        </linearGradient></defs>
      </svg>"""

    # Jira and Confluence share this one Atlassian sign-in, so once
    # connected the dashboard shows them as their own sub-tiles branching
    # off the Atlassian tile — same "one account, several products" shape
    # kb-connect uses. Kept as plain dicts (not connector instances): they
    # aren't independently authorizable, just labels for the two things one
    # Atlassian token already grants access to.
    PRODUCTS = [
        {
            "id": "jira",
            "name": "Jira",
            "blurb": "Resolved issues and the comments that closed them.",
            "logo": """<svg viewBox="0 0 256 256" fill="none">
                <path d="M244.657778,0 L121.706667,0 C121.706667,14.7201046 127.554205,28.837312 137.962891,39.2459977 C148.371577,49.6546835 162.488784,55.5022222 177.208889,55.5022222 L199.857778,55.5022222 L199.857778,77.3688889 C199.877391,107.994155 224.699178,132.815943 255.324444,132.835556 L255.324444,10.6666667 C255.324444,4.77562934 250.548815,3.60722001e-16 244.657778,0 Z" fill="#2684FF"/>
                <path d="M183.822222,61.2622222 L60.8711111,61.2622222 C60.8907238,91.8874888 85.7125112,116.709276 116.337778,116.728889 L138.986667,116.728889 L138.986667,138.666667 C139.025905,169.291923 163.863607,194.097803 194.488889,194.097778 L194.488889,71.9288889 C194.488889,66.0378516 189.71326,61.2622222 183.822222,61.2622222 Z" fill="url(#jira-g1)"/>
                <path d="M122.951111,122.488889 L0,122.488889 C3.75391362e-15,153.14192 24.8491913,177.991111 55.5022222,177.991111 L78.2222222,177.991111 L78.2222222,199.857778 C78.241767,230.45532 103.020285,255.265647 133.617778,255.324444 L133.617778,133.155556 C133.617778,127.264518 128.842148,122.488889 122.951111,122.488889 Z" fill="url(#jira-g2)"/>
                <defs>
                  <linearGradient id="jira-g1" x1="98.0308675%" y1="0.160599572%" x2="58.8877062%" y2="40.7655246%" gradientUnits="objectBoundingBox">
                    <stop stop-color="#0052CC" offset="18%"/><stop stop-color="#2684FF" offset="100%"/>
                  </linearGradient>
                  <linearGradient id="jira-g2" x1="100.665247%" y1="0.45503212%" x2="55.4018095%" y2="44.7269807%" gradientUnits="objectBoundingBox">
                    <stop stop-color="#0052CC" offset="18%"/><stop stop-color="#2684FF" offset="100%"/>
                  </linearGradient>
                </defs>
              </svg>""",
        },
        {
            "id": "confluence",
            "name": "Confluence",
            "blurb": "Pages and diagrams your team has already written up.",
            "logo": """<svg viewBox="0 0 32 32" fill="none">
                <path d="M3.015,23.087c-.289.472-.614,1.02-.891,1.456a.892.892,0,0,0,.3,1.212l5.792,3.564a.89.89,0,0,0,1.226-.29l.008-.013c.231-.387.53-.891.855-1.43,2.294-3.787,4.6-3.323,8.763-1.336l5.743,2.731A.892.892,0,0,0,26,28.559l.011-.024L28.766,22.3a.891.891,0,0,0-.445-1.167c-1.212-.57-3.622-1.707-5.792-2.754C14.724,14.586,8.09,14.831,3.015,23.087Z" fill="url(#conf-g1)"/>
                <path d="M28.985,8.932c.289-.472.614-1.02.891-1.456a.892.892,0,0,0-.3-1.212L23.785,2.7a.89.89,0,0,0-1.236.241.584.584,0,0,0-.033.053c-.232.387-.53.891-.856,1.43-2.294,3.787-4.6,3.323-8.763,1.336L7.172,3.043a.89.89,0,0,0-1.187.421l-.011.024L3.216,9.726a.891.891,0,0,0,.445,1.167c1.212.57,3.622,1.706,5.792,2.753C17.276,17.433,23.91,17.179,28.985,8.932Z" fill="url(#conf-g2)"/>
                <defs>
                  <linearGradient id="conf-g1" x1="28.607" y1="-30.825" x2="11.085" y2="-20.756" gradientUnits="userSpaceOnUse">
                    <stop offset="0.18" stop-color="#0052cc"/><stop offset="1" stop-color="#2684ff"/>
                  </linearGradient>
                  <linearGradient id="conf-g2" x1="3.388" y1="0.857" x2="20.915" y2="10.93" gradientUnits="userSpaceOnUse">
                    <stop offset="0.18" stop-color="#0052cc"/><stop offset="1" stop-color="#2684ff"/>
                  </linearGradient>
                </defs>
              </svg>""",
        },
    ]

    def build_authorize_url(self, tenant_id: str) -> str:
        return (
            "https://auth.atlassian.com/authorize"
            f"?audience=api.atlassian.com"
            f"&client_id={Config.ATLASSIAN_CLIENT_ID}"
            f"&scope={quote('read:jira-work read:confluence-content.summary')}"
            f"&redirect_uri={quote(Config.ATLASSIAN_REDIRECT_URI, safe='')}"
            f"&state={tenant_id}"
            "&response_type=code&prompt=consent"
        )

    def handle_callback(self, tenant_id: str, code: str) -> dict:
        # Mock mode (no real Atlassian app configured): skip the exchange so
        # the demo flow still works with zero credentials, same as before.
        if Config.ATLASSIAN_CLIENT_ID.startswith("dev-"):
            store.mark_service_connected(tenant_id, self.name)
            return self.sync(tenant_id)

        token = self._exchange_code(code)
        if not token:
            # Exchange failed — leave the service unconnected rather than
            # silently marking it connected with no usable token.
            return {"service": self.name, "error": "token exchange failed"}

        cloud_id, site_url = self._accessible_resource(token["access_token"])
        store.mark_service_connected(tenant_id, self.name, credentials={
            "access_token": token.get("access_token"),
            "refresh_token": token.get("refresh_token"),
            "expires_in": token.get("expires_in"),
            "cloud_id": cloud_id,
            "site_url": site_url,
        })
        return self.sync(tenant_id)

    def _exchange_code(self, code: str) -> dict | None:
        try:
            resp = requests.post(
                "https://auth.atlassian.com/oauth/token",
                json={
                    "grant_type": "authorization_code",
                    "client_id": Config.ATLASSIAN_CLIENT_ID,
                    "client_secret": Config.ATLASSIAN_CLIENT_SECRET,
                    "code": code,
                    "redirect_uri": Config.ATLASSIAN_REDIRECT_URI,
                },
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as err:
            logger.warning("Atlassian token exchange failed: %s", err)
            return None

    def _accessible_resource(self, access_token: str) -> tuple[str | None, str | None]:
        try:
            resp = requests.get(
                "https://api.atlassian.com/oauth/token/accessible-resources",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                timeout=10,
            )
            resp.raise_for_status()
            resources = resp.json()
        except requests.RequestException as err:
            logger.warning("Atlassian accessible-resources lookup failed: %s", err)
            return None, None

        if not resources:
            return None, None
        site = resources[0]
        return site.get("id"), site.get("url")

    def sync(self, tenant_id: str) -> dict:
        # Whole-account sync (called right after sign-in): both products at
        # once, each individually so one failing doesn't block the other.
        jira = self.sync_product(tenant_id, "jira")
        confluence = self.sync_product(tenant_id, "confluence")
        intents_added = jira.get("intents_added", []) + confluence.get("intents_added", [])
        return {"service": self.name, "intents_added": intents_added}

    def sync_product(self, tenant_id: str, product_id: str) -> dict:
        """Pulls just one product (jira or confluence) and marks its own
        state — this is what "Enable"/"Read again" on a sub-tile calls, kept
        separate from sync() so re-reading Confluence never re-touches Jira."""
        credentials = store.get_service_credentials(tenant_id, self.name)

        if not credentials or not credentials.get("access_token"):
            # No real token on file (mock mode, or exchange failed earlier) —
            # same placeholder used before this connector could talk to
            # Atlassian for real, one product at a time.
            pulled_intents = {
                f"{tenant_id}_atlassian_{product_id}_faq": {
                    "body": f"Synced from {product_id.capitalize()} overview (mock).",
                    "source": "atlassian",
                    "product": product_id,
                },
            }
            store.clear_product_intents(tenant_id, product_id)
            store.put_intents(tenant_id, pulled_intents)
            store.set_product_state(tenant_id, self.name, product_id, "indexed")
            return {"product": product_id, "intents_added": list(pulled_intents.keys())}

        if product_id == "jira":
            result = self._sync_jira(tenant_id, credentials)
        elif product_id == "confluence":
            result = self._sync_confluence(tenant_id, credentials)
        else:
            result = {"error": f"unknown product {product_id}"}

        if "error" in result:
            store.set_product_state(tenant_id, self.name, product_id, "failed", error=result["error"])
        else:
            store.clear_product_intents(tenant_id, product_id)
            store.put_intents(tenant_id, result["intents"])
            store.set_product_state(tenant_id, self.name, product_id, "indexed")
        return {"product": product_id, "intents_added": list(result.get("intents", {}).keys())}

    def _sync_jira(self, tenant_id: str, credentials: dict) -> dict:
        cloud_id = credentials.get("cloud_id")
        try:
            resp = requests.get(
                f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/search",
                params={"jql": "resolution = Done ORDER BY updated DESC", "maxResults": 5},
                headers={
                    "Authorization": f"Bearer {credentials['access_token']}",
                    "Accept": "application/json",
                },
                timeout=10,
            )
            resp.raise_for_status()
        except requests.RequestException as err:
            logger.warning("Atlassian Jira search failed: %s", err)
            return {"error": "sync failed"}

        intents = {}
        for issue in resp.json().get("issues", []):
            key = issue["key"]
            summary = issue.get("fields", {}).get("summary", "")
            intents[f"{tenant_id}_atlassian_{key}"] = {
                "body": f"{key}: {summary}",
                "source": "atlassian",
                "product": "jira",
            }
        return {"intents": intents}

    def _sync_confluence(self, tenant_id: str, credentials: dict) -> dict:
        cloud_id = credentials.get("cloud_id")
        try:
            resp = requests.get(
                f"https://api.atlassian.com/ex/confluence/{cloud_id}/wiki/rest/api/content",
                params={"limit": 5, "orderby": "-lastmodified"},
                headers={
                    "Authorization": f"Bearer {credentials['access_token']}",
                    "Accept": "application/json",
                },
                timeout=10,
            )
            resp.raise_for_status()
        except requests.RequestException as err:
            logger.warning("Atlassian Confluence content lookup failed: %s", err)
            return {"error": "sync failed"}

        intents = {}
        for page in resp.json().get("results", []):
            page_id = page["id"]
            title = page.get("title", "")
            intents[f"{tenant_id}_atlassian_page_{page_id}"] = {
                "body": title,
                "source": "atlassian",
                "product": "confluence",
            }
        return {"intents": intents}

    def disable_product(self, tenant_id: str, product_id: str) -> None:
        """Turns one product off without touching the Atlassian account
        connection or the other product — Confluence off, Jira untouched."""
        store.clear_product_intents(tenant_id, product_id)
        store.set_product_state(tenant_id, self.name, product_id, "off")
