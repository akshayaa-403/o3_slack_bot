import os
from pathlib import Path

from dotenv import load_dotenv

# Reuses the same repo-root .env kb-connect's OAuth Lambda already reads
# ATLASSIAN_CLIENT_ID/SECRET from — one Atlassian app registration, two
# consumers, no credentials duplicated between them. Gated behind an
# explicit opt-in (USE_REAL_ATLASSIAN=1) rather than loaded unconditionally:
# this repo's root .env also carries real SLACK_CLIENT_ID/SECRET, and
# demo.py's whole point is running end-to-end with zero real credentials —
# loading it unconditionally would silently break that guarantee the moment
# any real key exists in .env, exactly as it did here.
if os.getenv("USE_REAL_ATLASSIAN"):
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")


class Config:
    # --- Slack OAuth ---
    SLACK_CLIENT_ID = os.getenv("SLACK_CLIENT_ID", "dev-client-id")
    SLACK_CLIENT_SECRET = os.getenv("SLACK_CLIENT_SECRET", "dev-client-secret")
    SLACK_REDIRECT_URI = os.getenv("SLACK_REDIRECT_URI", "http://localhost:5000/auth/slack/callback")
    SLACK_BOT_SCOPES = "chat:write,commands,app_mentions:read"
    SLACK_USER_SCOPES = "identity.basic,identity.email"

    # --- Atlassian OAuth ---
    ATLASSIAN_CLIENT_ID = os.getenv("ATLASSIAN_CLIENT_ID", "dev-atlassian-id")
    ATLASSIAN_CLIENT_SECRET = os.getenv("ATLASSIAN_CLIENT_SECRET", "dev-atlassian-secret")
    ATLASSIAN_REDIRECT_URI = os.getenv("ATLASSIAN_REDIRECT_URI", "http://localhost:5000/connectors/atlassian/callback")

    # --- Microsoft SharePoint OAuth ---
    MS_CLIENT_ID = os.getenv("MS_CLIENT_ID", "dev-ms-id")
    MS_CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET", "dev-ms-secret")
    MS_REDIRECT_URI = os.getenv("MS_REDIRECT_URI", "http://localhost:5000/connectors/sharepoint/callback")

    # --- Google G Suite OAuth ---
    GSUITE_CLIENT_ID = os.getenv("GSUITE_CLIENT_ID", "dev-gsuite-id")
    GSUITE_CLIENT_SECRET = os.getenv("GSUITE_CLIENT_SECRET", "dev-gsuite-secret")
    GSUITE_REDIRECT_URI = os.getenv("GSUITE_REDIRECT_URI", "http://localhost:5000/connectors/gsuite/callback")

    # --- Data layer ---
    # If AWS creds are present, the app talks to real DynamoDB/S3.
    # Otherwise it transparently falls back to an in-memory store so the
    # whole flow is runnable locally with zero cloud setup.
    USE_AWS = bool(os.getenv("AWS_ACCESS_KEY_ID"))
    AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
    S3_BUCKET_PREFIX = os.getenv("S3_BUCKET_PREFIX", "tenant-data")

    # --- LLM provider (never exposed to the client) ---
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "mock")  # "anthropic" | "mock"
    LLM_MODEL = os.getenv("LLM_MODEL", "internal-default")  # never surfaced in API responses

    SECRET_KEY = os.getenv("APP_SECRET_KEY", "dev-secret-change-me")
