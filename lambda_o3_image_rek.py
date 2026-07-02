import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import boto3

AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
IMAGE_BUCKET = os.environ.get("IMAGE_BUCKET")
IMAGE_PREFIX = os.environ.get("IMAGE_PREFIX", "slack-images/")
ENABLE_REKOGNITION = os.environ.get("ENABLE_REKOGNITION", "true").lower() == "true"
MAX_INLINE_REKOGNITION_BYTES = int(os.environ.get("MAX_INLINE_REKOGNITION_BYTES", "5000000"))
REKOGNITION_MAX_LABELS = int(os.environ.get("REKOGNITION_MAX_LABELS", "10"))
REKOGNITION_MIN_CONFIDENCE = float(os.environ.get("REKOGNITION_MIN_CONFIDENCE", "70"))
LOG_DETECTED_TEXT = os.environ.get("LOG_DETECTED_TEXT", "true").lower() == "true"
LOG_DETECTED_TEXT_LIMIT = int(os.environ.get("LOG_DETECTED_TEXT_LIMIT", "20"))

s3 = boto3.client("s3", region_name=AWS_REGION)
rekognition = boto3.client("rekognition", region_name=AWS_REGION)


def to_iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def log_json(data):
    print(json.dumps(data, default=str))


def text_or_empty(value):
    if value is None:
        return ""

    return str(value).strip()


def first_image_file(event):
    files = event.get("files") or []
    if not files:
        raise ValueError("Missing image file metadata")

    return files[0]


def image_url(file_info):
    return (
        text_or_empty(file_info.get("url_private_download"))
        or text_or_empty(file_info.get("url_private"))
        or text_or_empty(file_info.get("thumb_1024"))
    )


def download_slack_image(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    is_slack_url = "slack.com" in host or "slack-edge.com" in host

    if is_slack_url and not SLACK_BOT_TOKEN:
        raise ValueError("Missing required environment variable: SLACK_BOT_TOKEN")

    headers = {
        "User-Agent": "Project-IVY-ImageRek/1.0",
    }

    if is_slack_url:
        headers["Authorization"] = f"Bearer {SLACK_BOT_TOKEN}"

    request = urllib.request.Request(
        url,
        headers=headers,
        method="GET",
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read(), response.headers.get("Content-Type")


def safe_s3_part(value, fallback):
    clean = "".join(
        char if char.isalnum() or char in {"-", "_", "."} else "-"
        for char in text_or_empty(value)
    ).strip("-")
    return clean or fallback


def put_image_object(event, file_info, image_bytes, content_type):
    if not IMAGE_BUCKET:
        return None

    session_id = safe_s3_part(event.get("session_id"), "unknown-session")
    file_id = safe_s3_part(file_info.get("id"), "image")
    file_name = safe_s3_part(file_info.get("name"), "upload")
    prefix = IMAGE_PREFIX if IMAGE_PREFIX.endswith("/") else f"{IMAGE_PREFIX}/"
    key = f"{prefix}{session_id}/{file_id}-{file_name}"

    extra_args = {}
    if content_type:
        extra_args["ContentType"] = content_type

    s3.put_object(
        Bucket=IMAGE_BUCKET,
        Key=key,
        Body=image_bytes,
        **extra_args,
    )

    return {
        "bucket": IMAGE_BUCKET,
        "key": key,
    }


def rekognition_image_ref(image_bytes, s3_object):
    if s3_object:
        return {
            "S3Object": {
                "Bucket": s3_object["bucket"],
                "Name": s3_object["key"],
            }
        }

    if len(image_bytes) > MAX_INLINE_REKOGNITION_BYTES:
        raise ValueError("Image is too large for inline Rekognition analysis; configure IMAGE_BUCKET")

    return {
        "Bytes": image_bytes
    }


def detect_image_text(image_ref):
    response = rekognition.detect_text(Image=image_ref)
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


def detect_image_labels(image_ref):
    response = rekognition.detect_labels(
        Image=image_ref,
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


def build_summary(text_lines, labels):
    extracted_text = [line["text"] for line in text_lines]
    label_names = [label["name"] for label in labels]

    parts = []
    if extracted_text:
        parts.append("Detected text: " + " | ".join(extracted_text[:8]))

    if label_names:
        parts.append("Detected labels: " + ", ".join(label_names[:8]))

    if not parts:
        return "No readable text or useful labels were detected in the image."

    return "\n".join(parts)


def log_safe_text_lines(text_lines):
    if not LOG_DETECTED_TEXT:
        return []

    return [
        {
            "text": line.get("text"),
            "confidence": line.get("confidence"),
        }
        for line in text_lines[: max(0, LOG_DETECTED_TEXT_LIMIT)]
    ]


def lambda_handler(event, context):
    analyzed_at = to_iso(datetime.now(timezone.utc))

    try:
        file_info = first_image_file(event)
        url = image_url(file_info)
        if not url:
            raise ValueError("Image file metadata does not include a private Slack URL")

        image_bytes, content_type = download_slack_image(url)
        s3_object = put_image_object(event, file_info, image_bytes, content_type)

        text_lines = []
        labels = []
        if ENABLE_REKOGNITION:
            image_ref = rekognition_image_ref(image_bytes, s3_object)
            text_lines = detect_image_text(image_ref)
            labels = detect_image_labels(image_ref)

        summary = build_summary(text_lines, labels)
        reply = f"I analyzed the image.\n\n{summary}"

        log_json({
            "level": "INFO",
            "message": "image_analysis_completed",
            "event_id": event.get("event_id"),
            "session_id": event.get("session_id"),
            "file_id": file_info.get("id"),
            "s3_bucket": (s3_object or {}).get("bucket"),
            "s3_key": (s3_object or {}).get("key"),
            "text_line_count": len(text_lines),
            "detected_text_lines": log_safe_text_lines(text_lines),
            "detected_text_preview": " | ".join(
                line.get("text", "")
                for line in text_lines[: max(0, LOG_DETECTED_TEXT_LIMIT)]
            ),
            "label_count": len(labels),
            "detected_labels": [
                label.get("name")
                for label in labels[: max(0, REKOGNITION_MAX_LABELS)]
            ],
        })

        return {
            "ok": True,
            "image_status": "completed",
            "analyzed_at": analyzed_at,
            "reply": reply,
            "summary": summary,
            "detected_text": text_lines,
            "labels": labels,
            "s3_object": s3_object,
        }

    except Exception as e:
        log_json({
            "level": "ERROR",
            "message": "image_analysis_failed",
            "event_id": event.get("event_id"),
            "session_id": event.get("session_id"),
            "error": str(e),
        })

        return {
            "ok": False,
            "image_status": "failed",
            "analyzed_at": analyzed_at,
            "error": str(e),
            "error_code": "image_analysis_failed",
        }
