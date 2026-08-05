"""
Provisions per-tenant infrastructure: an S3 bucket (or namespaced prefix
in mock mode) and a set of default pre-populated intents.

Idempotent: safe to call more than once for the same tenant_id.
"""
from app.config import Config
from app.db import store

DEFAULT_INTENTS = {
    "greeting": {"body": "Handles hello/hi/greeting style messages."},
    "fallback": {"body": "Default response when no other intent or connected source matches."},
}


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

    # Namespace default intents with the tenant_id, e.g. "<tenant_id>_greeting"
    namespaced = {f"{tenant_id}_{k}": v for k, v in DEFAULT_INTENTS.items()}
    store.put_intents(tenant_id, namespaced)

    return {"bucket": bucket_name, "intents_seeded": list(namespaced.keys())}
