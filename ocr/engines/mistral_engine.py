"""Mistral OCR (uses API).
Advanced managed OCR that returns structured output (markdown, tables, equations) for complex multimodal documents. Uses the ``mistralai`` SDK's ``ocr.process`` endpoint with a base64 image data URI.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

from ..base import OcrEngine, OcrLine

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _load_env_file() -> None:
    """Populate ``os.environ`` from ``ocr/.env`` (existing env vars win)."""

    if not _ENV_FILE.exists():
        return
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class MistralOcrEngine(OcrEngine):
    name = "mistral-ocr"
    kind = "cloud-api"
    description = "Mistral OCR API; structured markdown/tables/equations from complex docs."
    pip_packages = ("mistralai",)
    env_vars = ("MISTRAL_API_KEY", "OCR_MISTRAL_MODEL")

    def available(self) -> tuple[bool, str]:
        ok, reason = super().available()
        if not ok:
            return ok, reason
        _load_env_file()
        if not os.environ.get("MISTRAL_API_KEY"):
            return False, "no MISTRAL_API_KEY set"
        return True, "ready (MISTRAL_API_KEY present)"

    def _load(self) -> None:
        from mistralai import Mistral

        self._client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])
        self._model = os.environ.get("OCR_MISTRAL_MODEL", "mistral-ocr-latest")

    def _extract_lines(self, image_bytes: bytes) -> tuple[list[OcrLine], dict[str, Any]]:
        data_uri = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
        response = self._client.ocr.process(
            model=self._model,
            document={"type": "image_url", "image_url": data_uri},
        )

        lines: list[OcrLine] = []
        pages = getattr(response, "pages", None) or []
        for page in pages:
            markdown = getattr(page, "markdown", "") or ""
            for raw_line in markdown.splitlines():
                text = raw_line.strip().lstrip("#").strip()
                if text:
                    lines.append(OcrLine(text=text))
        return lines, {"model": self._model, "pages": len(pages)}
