import base64
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError

AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

SCREENSHOT_ISSUE_TABLE = os.environ.get("SCREENSHOT_ISSUE_TABLE", "o3_screenshot_issue_kb")
SCREENSHOT_VECTOR_ENDPOINT = os.environ.get("SCREENSHOT_VECTOR_ENDPOINT", "").rstrip("/")
SCREENSHOT_VECTOR_BACKEND = os.environ.get("SCREENSHOT_VECTOR_BACKEND", "opensearch").lower()
SCREENSHOT_VECTOR_INDEX = os.environ.get("SCREENSHOT_VECTOR_INDEX", "o3-screenshot-issues")
SCREENSHOT_VECTOR_FIELD = os.environ.get("SCREENSHOT_VECTOR_FIELD", "image_vector")
SCREENSHOT_OPENSEARCH_SERVICE = os.environ.get("SCREENSHOT_OPENSEARCH_SERVICE", "aoss")
SCREENSHOT_EMBEDDING_MODEL_ID = os.environ.get("SCREENSHOT_EMBEDDING_MODEL_ID", "amazon.titan-embed-image-v1")
SCREENSHOT_EMBEDDING_MAX_BYTES = int(os.environ.get("SCREENSHOT_EMBEDDING_MAX_BYTES", "5000000"))
SCREENSHOT_VECTOR_DIMENSION = int(os.environ.get("SCREENSHOT_VECTOR_DIMENSION", "1024"))
CREATE_VECTOR_INDEX = os.environ.get("CREATE_VECTOR_INDEX", "false").lower() == "true"
ENABLE_REKOGNITION = os.environ.get("ENABLE_REKOGNITION", "true").lower() == "true"
REKOGNITION_MAX_LABELS = int(os.environ.get("REKOGNITION_MAX_LABELS", "10"))
REKOGNITION_MIN_CONFIDENCE = float(os.environ.get("REKOGNITION_MIN_CONFIDENCE", "70"))

s3 = boto3.client("s3", region_name=AWS_REGION)
rekognition = boto3.client("rekognition", region_name=AWS_REGION)
bedrock_runtime = boto3.client("bedrock-runtime", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
issue_table = dynamodb.Table(SCREENSHOT_ISSUE_TABLE)


def to_iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def log_json(data):
    print(json.dumps(data, default=str))


def text_or_empty(value):
    if value is None:
        return ""

    return str(value).strip()


def value_is_false(value):
    return str(value).strip().lower() in {"false", "0", "no", "disabled"}


def s3_location_from_event(event):
    records = event.get("Records") or []
    if records and "s3" in records[0]:
        s3_record = records[0]["s3"]
        return {
            "bucket": s3_record["bucket"]["name"],
            "key": urllib.parse.unquote_plus(s3_record["object"]["key"]),
        }

    bucket = text_or_empty(event.get("bucket") or event.get("s3_bucket"))
    key = text_or_empty(event.get("key") or event.get("s3_key"))
    if not bucket or not key:
        raise ValueError("Missing screenshot S3 bucket/key")

    return {
        "bucket": bucket,
        "key": key,
    }


def metadata_value(metadata, *names):
    for name in names:
        value = metadata.get(name)
        if value:
            return text_or_empty(value)

    return ""


def issue_id_from_key(key):
    name = key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    clean = "".join(
        char.lower() if char.isalnum() else "_"
        for char in name
    )
    clean = "_".join(part for part in clean.split("_") if part)
    return clean or "unknown_screenshot_issue"


def load_image(bucket, key):
    response = s3.get_object(Bucket=bucket, Key=key)
    return response["Body"].read()


def head_metadata(bucket, key):
    response = s3.head_object(Bucket=bucket, Key=key)
    return response.get("Metadata") or {}


def detect_image_text(bucket, key):
    if not ENABLE_REKOGNITION:
        return []

    response = rekognition.detect_text(
        Image={
            "S3Object": {
                "Bucket": bucket,
                "Name": key,
            }
        }
    )
    lines = []

    for detection in response.get("TextDetections") or []:
        if detection.get("Type") != "LINE":
            continue

        text = text_or_empty(detection.get("DetectedText"))
        if text:
            lines.append({
                "text": text,
                "confidence": round(float(detection.get("Confidence", 0)), 2),
            })

    return lines


def detect_image_labels(bucket, key):
    if not ENABLE_REKOGNITION:
        return []

    response = rekognition.detect_labels(
        Image={
            "S3Object": {
                "Bucket": bucket,
                "Name": key,
            }
        },
        MaxLabels=REKOGNITION_MAX_LABELS,
        MinConfidence=REKOGNITION_MIN_CONFIDENCE,
    )

    return [
        {
            "name": label.get("Name"),
            "confidence": round(float(label.get("Confidence", 0)), 2),
        }
        for label in response.get("Labels") or []
        if label.get("Name")
    ]


def create_embedding(image_bytes):
    if len(image_bytes) > SCREENSHOT_EMBEDDING_MAX_BYTES:
        raise ValueError("Image is too large for embedding")

    body = {
        "inputImage": base64.b64encode(image_bytes).decode("utf-8")
    }
    response = bedrock_runtime.invoke_model(
        modelId=SCREENSHOT_EMBEDDING_MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(body).encode("utf-8")
    )
    response_body = json.loads(response["body"].read().decode("utf-8"))
    embedding = response_body.get("embedding")
    if not embedding:
        raise ValueError("Bedrock embedding response did not include an embedding")

    return embedding


def signed_opensearch_request(method, path, payload=None):
    if not SCREENSHOT_VECTOR_ENDPOINT:
        raise ValueError("Missing SCREENSHOT_VECTOR_ENDPOINT")

    body = json.dumps(payload or {}).encode("utf-8")
    url = f"{SCREENSHOT_VECTOR_ENDPOINT}{path}"
    request = AWSRequest(
        method=method,
        url=url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Host": urllib.parse.urlparse(SCREENSHOT_VECTOR_ENDPOINT).netloc,
        }
    )
    SigV4Auth(
        boto3.Session().get_credentials(),
        SCREENSHOT_OPENSEARCH_SERVICE,
        AWS_REGION
    ).add_auth(request)

    prepared = request.prepare()
    urllib_request = urllib.request.Request(
        url,
        data=body,
        headers=dict(prepared.headers.items()),
        method=method
    )

    with urllib.request.urlopen(urllib_request, timeout=20) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def ensure_vector_index():
    if not CREATE_VECTOR_INDEX:
        return False

    payload = {
        "settings": {
            "index": {
                "knn": True
            }
        },
        "mappings": {
            "properties": {
                SCREENSHOT_VECTOR_FIELD: {
                    "type": "knn_vector",
                    "dimension": SCREENSHOT_VECTOR_DIMENSION,
                    "method": {
                        "name": "hnsw",
                        "space_type": "cosinesimil",
                        "engine": "faiss"
                    }
                },
                "issue_id": {
                    "type": "keyword"
                },
                "title": {
                    "type": "text"
                },
                "s3_key": {
                    "type": "keyword"
                }
            }
        }
    }
    path = f"/{urllib.parse.quote(SCREENSHOT_VECTOR_INDEX, safe='')}"

    try:
        signed_opensearch_request("PUT", path, payload)
        return True

    except Exception as e:
        if "resource_already_exists_exception" in str(e):
            return False
        raise


def put_vector_document(issue_id, item, embedding):
    document = {
        "issue_id": issue_id,
        "title": item.get("title"),
        "s3_bucket": item.get("s3_bucket"),
        "s3_key": item.get("s3_key"),
        "tags": item.get("tags", []),
        SCREENSHOT_VECTOR_FIELD: embedding,
    }
    path = (
        f"/{urllib.parse.quote(SCREENSHOT_VECTOR_INDEX, safe='')}"
        f"/_doc/{urllib.parse.quote(issue_id, safe='')}"
    )
    return signed_opensearch_request("PUT", path, document)


def dynamodb_embedding(embedding):
    return [Decimal(str(value)) for value in embedding]


def build_issue_item(event, bucket, key, metadata, text_lines, labels, embedding):
    issue_id = text_or_empty(
        event.get("issue_id")
        or metadata_value(metadata, "issue-id", "issue_id")
        or issue_id_from_key(key)
    )
    title = text_or_empty(
        event.get("title")
        or metadata_value(metadata, "title")
        or issue_id.replace("_", " ").title()
    )
    lex_query = text_or_empty(
        event.get("lex_query")
        or metadata_value(metadata, "lex-query", "lex_query")
    )
    expected_lex_intent = text_or_empty(
        event.get("expected_lex_intent")
        or metadata_value(metadata, "expected-lex-intent", "expected_lex_intent")
    )
    tags = event.get("tags")
    if isinstance(tags, str):
        tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
    if not isinstance(tags, list):
        metadata_tags = metadata_value(metadata, "tags")
        tags = [tag.strip() for tag in metadata_tags.split(",") if tag.strip()]

    if not lex_query:
        raise ValueError("Missing lex_query for screenshot issue")

    now_iso = to_iso(datetime.now(timezone.utc))
    return {
        "issue_id": issue_id,
        "title": title,
        "lex_query": lex_query,
        "expected_lex_intent": expected_lex_intent or None,
        "enabled": not value_is_false(
            event.get("enabled", metadata_value(metadata, "enabled"))
        ),
        "tags": tags,
        "s3_bucket": bucket,
        "s3_key": key,
        "embedding_model_id": SCREENSHOT_EMBEDDING_MODEL_ID,
        "image_embedding": dynamodb_embedding(embedding),
        "detected_text_preview": " | ".join(line["text"] for line in text_lines[:12]),
        "detected_labels": [label["name"] for label in labels[:REKOGNITION_MAX_LABELS]],
        "created_at": now_iso,
        "updated_at": now_iso,
    }


def index_screenshot(event):
    location = s3_location_from_event(event)
    bucket = location["bucket"]
    key = location["key"]

    metadata = head_metadata(bucket, key)
    image_bytes = load_image(bucket, key)
    text_lines = detect_image_text(bucket, key)
    labels = detect_image_labels(bucket, key)
    embedding = create_embedding(image_bytes)
    issue_item = build_issue_item(event, bucket, key, metadata, text_lines, labels, embedding)

    issue_table.put_item(Item=issue_item)
    vector_response = {
        "backend": "dynamodb",
        "skipped_opensearch": True,
    }
    if SCREENSHOT_VECTOR_BACKEND == "opensearch":
        vector_response = put_vector_document(issue_item["issue_id"], issue_item, embedding)

    log_json({
        "level": "INFO",
        "message": "screenshot_issue_indexed",
        "issue_id": issue_item["issue_id"],
        "title": issue_item["title"],
        "s3_bucket": bucket,
        "s3_key": key,
        "lex_query": issue_item["lex_query"],
        "expected_lex_intent": issue_item.get("expected_lex_intent"),
        "vector_index": SCREENSHOT_VECTOR_INDEX,
        "text_line_count": len(text_lines),
        "label_count": len(labels)
    })

    return {
        "ok": True,
        "issue_id": issue_item["issue_id"],
        "s3_bucket": bucket,
        "s3_key": key,
        "vector_index": SCREENSHOT_VECTOR_INDEX,
        "vector_response": vector_response,
    }


def lambda_handler(event, context):
    try:
        ensure_vector_index()
        items = event.get("items")
        if not items:
            return index_screenshot(event)

        results = []
        for item in items:
            try:
                results.append(index_screenshot(item))

            except Exception as e:
                log_json({
                    "level": "ERROR",
                    "message": "screenshot_issue_item_index_failed",
                    "issue_id": item.get("issue_id"),
                    "bucket": item.get("bucket") or item.get("s3_bucket"),
                    "key": item.get("key") or item.get("s3_key"),
                    "error": str(e),
                })
                results.append({
                    "ok": False,
                    "issue_id": item.get("issue_id"),
                    "s3_bucket": item.get("bucket") or item.get("s3_bucket"),
                    "s3_key": item.get("key") or item.get("s3_key"),
                    "error": str(e),
                    "error_code": "screenshot_issue_item_index_failed",
                })

        failed = [result for result in results if not result.get("ok")]
        return {
            "ok": not failed,
            "indexed_count": len(results) - len(failed),
            "failed_count": len(failed),
            "results": results,
        }

    except Exception as e:
        log_json({
            "level": "ERROR",
            "message": "screenshot_issue_index_failed",
            "error": str(e),
        })
        return {
            "ok": False,
            "error": str(e),
            "error_code": "screenshot_issue_index_failed",
        }
