"""PaddleOCR (Baidu).
Balanced default for complex documents. fast custom deployment, CPU or GPU. First run downloads detection + recognition models.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ..base import OcrEngine, OcrLine

# PaddleOCR's own "ppocr" logger defaults to DEBUG and dumps its full config
# Namespace plus a per-image detect/recognize trace on every call; none of it
# indicates a problem, so keep it at ERROR to only surface real failures.
logging.getLogger("ppocr").setLevel(logging.ERROR)


class PaddleOcrEngine(OcrEngine):
    name = "paddleocr"
    kind = "local-cpu"
    description = "PaddleOCR (Baidu) detection+recognition; robust multi-language, CPU/GPU."
    pip_packages = ("paddleocr", "paddle")
    env_vars = ("OCR_PADDLE_LANG", "OCR_USE_GPU")

    def _import_names(self) -> tuple[str, ...]:
        return ("paddleocr", "paddle")

    def _load(self) -> None:
        from paddleocr import PaddleOCR

        lang = os.environ.get("OCR_PADDLE_LANG", "en")
        use_gpu = os.environ.get("OCR_USE_GPU", "false").lower() == "true"
        attempts = []
        if use_gpu:
            attempts.append(dict(lang=lang, device="gpu", show_log=False))
        attempts.append(dict(lang=lang, show_log=False))
        attempts.append(dict(show_log=False))
        last_error = None
        for kwargs in attempts:
            try:
                self._ocr = PaddleOCR(**kwargs)
                return
            except Exception as error:  # noqa: BLE001
                last_error = error
        raise RuntimeError(f"could not construct PaddleOCR: {last_error}")

    def _extract_lines(self, image_bytes: bytes) -> tuple[list[OcrLine], dict[str, Any]]:
        import numpy as np
        from ..base import load_numpy_image

        image = load_numpy_image(image_bytes)
        raw = self._call(image)
        lines = self._parse(raw)
        return lines, {"backend_return": type(raw).__name__}

    def _call(self, image):
        # cls=False: this engine never sets use_angle_cls=True, so cls=True
        # (ocr()'s default) is a no-op that only produces a per-call warning.
        for method_name, kwargs in (("predict", {}), ("ocr", {"cls": False})):
            method = getattr(self._ocr, method_name, None)
            if method is None:
                continue
            try:
                return method(image, **kwargs)
            except Exception:
                continue
        raise RuntimeError("no working PaddleOCR inference method (predict/ocr)")

    def _parse(self, raw) -> list[OcrLine]:
        lines: list[OcrLine] = []

        if isinstance(raw, list) and raw and isinstance(raw[0], list):
            page = raw[0] if raw and isinstance(raw[0], list) else raw
            for item in page or []:
                try:
                    text, score = item[1][0], float(item[1][1])
                    box = item[0]
                    top = min(p[1] for p in box)
                    if text and text.strip():
                        lines.append((top, OcrLine(text=text.strip(), confidence=round(score * 100, 2))))
                except Exception:
                    continue
            return [line for _, line in sorted(lines, key=lambda t: t[0])]

        if isinstance(raw, list) and raw and isinstance(raw[0], dict):
            page = raw[0]
            texts = page.get("rec_texts") or []
            scores = page.get("rec_scores") or []
            for idx, text in enumerate(texts):
                score = float(scores[idx]) if idx < len(scores) else None
                if text and text.strip():
                    lines.append((idx, OcrLine(
                        text=text.strip(),
                        confidence=round(score * 100, 2) if score is not None else None,
                    )))
            return [line for _, line in lines]

        return []
