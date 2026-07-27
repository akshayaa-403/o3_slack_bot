"""Quick test of Jira API credentials."""
import os
import sys
import base64
import json
import urllib.request
import urllib.error

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

def _load_dotenv():
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

_load_dotenv()

base_url = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
email = os.environ.get("JIRA_EMAIL", "")
token = os.environ.get("JIRA_API_TOKEN", "")
project_key = os.environ.get("JIRA_PROJECT_KEY", "")

print(f"Testing Jira credentials...")
print(f"  Base URL: {base_url}")
print(f"  Email: {email}")
print(f"  Project key: {project_key}")
print(f"  Token length: {len(token) if token else 0} chars")
print()

if not all([base_url, email, token, project_key]):
    print("ERROR: Missing credentials in .env")
    sys.exit(1)

auth = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("utf-8")
headers = {
    "Accept": "application/json",
    "Authorization": f"Basic {auth}",
}

try:
    print("1. Testing authentication by fetching project...")
    req = urllib.request.Request(
        f"{base_url}/rest/api/3/project/{project_key}",
        headers=headers,
        method="GET"
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        data = json.loads(response.read())
        print(f"   OK Project found: {data.get('name')} ({data.get('key')})")
except urllib.error.HTTPError as e:
    print(f"   FAILED: HTTP {e.code}")
    detail = e.read().decode("utf-8", errors="replace")[:500]
    print(f"   {detail}")
    sys.exit(1)

try:
    print("\n2. Testing issue creation...")
    payload = {
        "fields": {
            "project": {"key": project_key},
            "issuetype": {"name": "Task"},
            "summary": "TEST ISSUE - delete me",
            "description": {
                "type": "doc",
                "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Test"}]}]
            }
        }
    }
    req = urllib.request.Request(
        f"{base_url}/rest/api/3/issue",
        data=json.dumps(payload).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        result = json.loads(response.read())
        issue_key = result.get("key")
        print(f"   OK Issue created: {issue_key}")
        print(f"\n   Cleanup: go delete {issue_key} manually from your Jira board")
except urllib.error.HTTPError as e:
    print(f"   FAILED: HTTP {e.code}")
    detail = e.read().decode("utf-8", errors="replace")[:500]
    print(f"   {detail}")
    sys.exit(1)

print("\nAll tests passed!")
