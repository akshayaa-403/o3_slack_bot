"""
Provisions per-tenant infrastructure: an S3 bucket (or namespaced prefix
in mock mode) and the tenant's answering configuration.

Idempotent: safe to call more than once for the same tenant_id.

On global intents. A new tenant must be able to answer generic IT/HR
questions the moment the bot is installed, with no connected sources —
that was an explicit requirement from the design review. It is met by
pointing the tenant at IvvY's existing shared Lex bot (which already
carries the full generic intent set) rather than copying intents into
each tenant.

Copying was the obvious alternative and is worse: it duplicates hundreds
of intents per tenant, and every tenant's copy starts drifting from the
shared bot the moment that bot is updated. A pointer means an
improvement to the shared intent set reaches every tenant at once.

Tenant-specific intents (created when a customer connects Atlassian and
friends) are stored per-tenant and take precedence — the shared bot is
the floor, not the ceiling.
"""
from app.config import Config
from app.db import store


def _ensure_s3_bucket(tenant_id: str) -> str:
    bucket_name = f"{Config.S3_BUCKET_PREFIX}-{tenant_id}"

    if not Config.USE_AWS:
        # Mock mode: nothing to create, just report the name that *would*
        # be provisioned in AWS mode.
        return f"[mock] {bucket_name}"

    import boto3
    from botocore.exceptions import ClientError

    s3 = boto3.client("s3", region_name=Config.AWS_REGION)
    try:
        s3.head_bucket(Bucket=bucket_name)
    except ClientError as e:
        if e.response["Error"]["Code"] != "404":
            raise
        if Config.AWS_REGION == "us-east-1":
            s3.create_bucket(Bucket=bucket_name)
        else:
            s3.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": Config.AWS_REGION},
            )
    return bucket_name


def provision_tenant(tenant_id: str) -> dict:
    bucket_name = _ensure_s3_bucket(tenant_id)

    # Record which Lex bot answers for this tenant. Every new tenant starts
    # on the shared bot, so global intents work from the first message. The
    # field exists (rather than the worker just assuming the shared bot) so
    # a tenant can later be moved to a dedicated bot without a code change.
    answering = {
        "lex_bot_id": Config.GLOBAL_LEX_BOT_ID,
        "lex_bot_alias_id": Config.GLOBAL_LEX_BOT_ALIAS_ID,
        "lex_locale_id": Config.GLOBAL_LEX_LOCALE_ID,
        "scope": "global",
    }
    store.set_answering_config(tenant_id, answering)

    return {"bucket": bucket_name, "answering": answering}
