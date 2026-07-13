from __future__ import annotations

from typing import Any

from .base import now_iso
from .registry import load_engine


def analyze_image_bytes(engine_name: str, image_bytes: bytes) -> dict[str, Any]:
    """Run ``engine_name`` over ``image_bytes`` and return an image-rek-shaped dict.

    The returned dict matches ``lambda_o3_image_rek.py``'s response so it can be
    handed straight to the worker's image handling.
    """

    engine = load_engine(engine_name)
    ok, reason = engine.available()
    analyzed_at = now_iso()
    if not ok:
        return {
            "ok": False,
            "image_status": "failed",
            "analyzed_at": analyzed_at,
            "error": reason,
            "error_code": "ocr_engine_unavailable",
            "engine": engine_name,
        }
    return engine.extract(image_bytes).to_image_rek_response(analyzed_at)