"""
Lambda entry point for slack_tenant_app.

Mangum is an ASGI-only adapter, but Flask 3.x is WSGI — asgiref's
WsgiToAsgi bridges that gap so the existing Flask app (app.main.app) can
run behind Mangum unchanged. Every route — /login, /dashboard,
/connectors/..., /static/... — is served through one Lambda behind one API
Gateway HTTP API, same shape as lambda_kb_connect_oauth.py's
single-endpoint pattern, just fronting a whole multi-route app instead of
one JSON handler.

USE_AWS must be true (AWS_ACCESS_KEY_ID set, or running with an execution
role that has it implicitly) for this to hold real tenant data across
invocations — Lambda's per-invocation isolation means the in-memory store
demo.py uses locally does not reliably survive between requests here.
"""
from asgiref.wsgi import WsgiToAsgi
from mangum import Mangum

from app.main import app

asgi_app = WsgiToAsgi(app)
handler = Mangum(asgi_app, lifespan="off")
