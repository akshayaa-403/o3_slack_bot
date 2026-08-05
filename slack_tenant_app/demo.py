"""
Runs the entire architecture end-to-end using Flask's test client, so the
whole login -> tenant creation -> provisioning -> connector -> Q&A flow
is demonstrated with zero external dependencies (no real Slack/AWS/Atlassian
credentials needed — everything runs in mock mode).
"""
from app.main import app

client = app.test_client()

print("=" * 70)
print("STEP 1-2: Admin visits /login, sees single 'Continue with Slack' button")
print("=" * 70)
resp = client.get("/login")
assert b"Continue with Slack" in resp.data
assert b'href="/login/google"' not in resp.data and b'href="/login/microsoft"' not in resp.data
print("Login page OK - only Slack option present.\n")

print("=" * 70)
print("STEP 3-4: Slack OAuth callback -> tenant created + infra provisioned")
print("=" * 70)
resp = client.get("/auth/slack/callback?code=abc12345", follow_redirects=True)
assert resp.status_code == 200
print(resp.data.decode()[:300], "...\n")

from app.db import store
tenant = list(store.tenants.values())[0]
tenant_id = tenant["tenant_id"]
print(f"Tenant created: {tenant['name']} (tenant_id={tenant_id})")
print(f"Seeded intents: {list(store.get_intents(tenant_id).keys())}\n")

print("=" * 70)
print("STEP 5a: Connect Atlassian (mock OAuth) -> connector sync runs")
print("=" * 70)
resp = client.get(f"/connectors/atlassian/callback?state={tenant_id}&code=mock", follow_redirects=True)
print(f"Connected services: {list(store.get_tenant(tenant_id)['connected_services'].keys())}")
print(f"Intents after Atlassian sync: {list(store.get_intents(tenant_id).keys())}\n")

print("=" * 70)
print("STEP 5b: Connect SharePoint (mock OAuth) -> connector sync runs")
print("=" * 70)
resp = client.get(f"/connectors/sharepoint/callback?state={tenant_id}&code=mock", follow_redirects=True)
print(f"Connected services: {list(store.get_tenant(tenant_id)['connected_services'].keys())}")
print(f"Intents after SharePoint sync: {list(store.get_intents(tenant_id).keys())}\n")

print("=" * 70)
print("STEP 6: User asks a question -> answered via LLM abstraction")
print("=" * 70)
resp = client.post("/api/query", json={"tenant_id": tenant_id, "question": "How do I request time off?"})
print("Response JSON:", resp.get_json())
assert "model" not in resp.get_json() and "provider" not in resp.get_json()
print("Confirmed: no LLM/model detail leaked to the client.\n")

print("=" * 70)
print("BONUS: Periodic intent regeneration from conversation history")
print("=" * 70)
resp = client.post(f"/internal/regenerate-intents/{tenant_id}")
print("Regeneration result:", resp.get_json())

print("\nEnd-to-end flow completed successfully.")
