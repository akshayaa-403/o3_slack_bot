"""
Envelope encryption for OAuth tokens held at rest.

The architecture doc requires connector tokens to be encrypted at rest
rather than sitting in DynamoDB as plain attributes. A stolen table
export, an over-broad IAM read, or a support engineer browsing items
would otherwise hand over live Jira/Confluence/Slack access for every
tenant at once.

Direct KMS Encrypt is used rather than a data key: these payloads are
small credential dicts (a few hundred bytes, well inside KMS's 4KB
limit), so the extra round trip and key-caching complexity of envelope
encryption buys nothing here.

Ciphertext is stored as a base64 string under a versioned wrapper:

    {"__enc__": "kms.v1", "data": "<base64 ciphertext>"}

The marker makes the format self-describing, so decrypt can tell an
encrypted value from a legacy plaintext one and migrate rows lazily
instead of needing a backfill. It is also why the scheme can be changed
later without a flag day — a future kms.v2 simply reads alongside v1.

Encryption is bound to the tenant via an encryption context, which KMS
authenticates: ciphertext encrypted for tenant A will not decrypt if
presented as tenant B's, so a swapped row is rejected rather than
silently yielding another tenant's token.

In local/dev mode (no AWS credentials) this is a pass-through, keeping
demo.py runnable with no cloud setup.
"""
import base64
import json
import logging

from app.config import Config

logger = logging.getLogger(__name__)

ENVELOPE_MARKER = "__enc__"
SCHEME_V1 = "kms.v1"

_kms_client = None


def _kms():
    global _kms_client
    if _kms_client is None:
        import boto3
        _kms_client = boto3.client("kms", region_name=Config.AWS_REGION)
    return _kms_client


def _encryption_context(tenant_id: str) -> dict:
    # Authenticated (not secret) data: KMS requires an exact match on
    # decrypt, which is what stops one tenant's ciphertext being replayed
    # as another's.
    return {"tenant_id": tenant_id, "purpose": "connector-oauth-token"}


def is_encrypted(value) -> bool:
    return isinstance(value, dict) and value.get(ENVELOPE_MARKER) == SCHEME_V1


def encrypt_credentials(tenant_id: str, credentials: dict) -> dict:
    """Encrypt a credentials dict for storage.

    Returns the envelope wrapper on success. With encryption disabled
    (local dev, or no key configured) the dict passes through unchanged.
    """
    if not credentials:
        return credentials or {}
    if not Config.USE_AWS or not Config.KMS_KEY_ID:
        return credentials

    plaintext = json.dumps(credentials, separators=(",", ":")).encode("utf-8")
    resp = _kms().encrypt(
        KeyId=Config.KMS_KEY_ID,
        Plaintext=plaintext,
        EncryptionContext=_encryption_context(tenant_id),
    )
    return {
        ENVELOPE_MARKER: SCHEME_V1,
        "data": base64.b64encode(resp["CiphertextBlob"]).decode("ascii"),
    }


def decrypt_credentials(tenant_id: str, stored) -> dict:
    """Inverse of encrypt_credentials.

    A value written before encryption was switched on is returned as-is,
    so existing tenants keep working and re-encrypt on their next write
    rather than needing a migration.
    """
    if not stored:
        return {}
    if not is_encrypted(stored):
        # Legacy plaintext row.
        return stored if isinstance(stored, dict) else {}

    blob = base64.b64decode(stored["data"])
    try:
        resp = _kms().decrypt(
            CiphertextBlob=blob,
            EncryptionContext=_encryption_context(tenant_id),
        )
    except Exception as exc:
        # Log the error *type* only — never the ciphertext, and never a
        # traceback, whose frames can carry credential fragments.
        #
        # InvalidCiphertext specifically means the encryption context did
        # not match, i.e. this row was encrypted for a different tenant.
        # That is the isolation guarantee doing its job, and it is worth
        # surfacing as a warning rather than losing in stack-trace noise:
        # legitimately it never happens.
        if exc.__class__.__name__ == "InvalidCiphertextException":
            logger.warning(
                "Credential row rejected: ciphertext not valid for tenant %s "
                "(encryption context mismatch)", tenant_id,
            )
        else:
            logger.error(
                "Could not decrypt credentials for tenant %s: %s",
                tenant_id, exc.__class__.__name__,
            )
        # Callers treat {} as "not connected", so this degrades to a
        # reconnect prompt rather than a 500.
        return {}

    return json.loads(resp["Plaintext"].decode("utf-8"))
