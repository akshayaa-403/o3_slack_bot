from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from .base import now_iso
from .registry import load_engine

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
OCR_ENGINE = os.environ.get("OCR_ENGINE", "textract")

_engine = None

def log_json(data):
    print(json.dumps(data, default=str))

def _get_engine():
    global _engine
    if _engine is None:
        _engine = load_engine(OCR_ENGINE)
    return _engine

def _first_image_url(event):
    files = event.get("files") or []
    if not files:
        raise ValueError("Missing image file metadata")
    file_info = files[0]
    return (
        (file_info.get("url_private_download") or "").strip()
        or (file_info.get("url_private") or "").strip()
        or (file_info.get("thumb_1024") or "").strip()
    )


def _download_image(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    is_slack = "slack.com" in host or "slack-edge.com" in host
    if is_slack and not SLACK_BOT_TOKEN:
        raise ValueError("Missing required environment variable: SLACK_BOT_TOKEN")

    headers = {"User-Agent": "Project-IVY-OCR/1.0"}
    if is_slack:
        headers["Authorization"] = f"Bearer {SLACK_BOT_TOKEN}"

    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


def lambda_handler(event, context):
    analyzed_at = now_iso()
    try:
        url = _first_image_url(event)
        if not url:
            raise ValueError("Image file metadata does not include a private Slack URL")

        image_bytes = _download_image(url)
        engine = _get_engine()

        ok, reason = engine.available()
        if not ok:
            log_json({"level": "ERROR", "message": "ocr_engine_unavailable",
                      "engine": OCR_ENGINE, "reason": reason,
                      "session_id": event.get("session_id")})
            return {
                "ok": False, "image_status": "failed", "analyzed_at": analyzed_at,
                "error": reason, "error_code": "ocr_engine_unavailable", "engine": OCR_ENGINE,
            }

        result = engine.extract(image_bytes)
        log_json({
            "level": "INFO" if result.ok else "ERROR",
            "message": "ocr_completed",
            "engine": OCR_ENGINE,
            "session_id": event.get("session_id"),
            "event_id": event.get("event_id"),
            "line_count": len(result.lines),
            "latency_ms": result.latency_ms,
            "error": result.error,
        })
        return result.to_image_rek_response(analyzed_at)

    except Exception as error:
        log_json({"level": "ERROR", "message": "ocr_lambda_failed",
                  "engine": OCR_ENGINE, "session_id": event.get("session_id"),
                  "error": str(error)})
        return {
            "ok": False, "image_status": "failed", "analyzed_at": analyzed_at,
            "error": str(error), "error_code": "ocr_lambda_failed", "engine": OCR_ENGINE,
        }
