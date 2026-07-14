"""EasyOCR (PyTorch).
Lightweight, accessible Python OCR. Bundles detection + recognition, First run downloads model weights (~100 MB).
"""

from __future__ import annotations

import os
import warnings
from typing import Any

from ..base import OcrEngine, OcrLine

# EasyOCR's CPU quantized RNN path and its DataLoader emit benign UserWarnings
# (deprecated quantize_per_tensor dtypes, pin_memory with no accelerator) on
# every load/inference call; they don't indicate a problem, just noise.
warnings.filterwarnings("ignore", category=UserWarning, module="torch")


class EasyOcrEngine(OcrEngine):
    name = "easyocr"
    kind = "local-cpu"
    description = "EasyOCR (PyTorch) detector+recognizer; CPU/GPU, 80+ languages."
    pip_packages = ("easyocr",)
    env_vars = ("OCR_EASYOCR_LANGS", "OCR_USE_GPU")

    def _load(self) -> None:
        import easyocr

        langs = os.environ.get("OCR_EASYOCR_LANGS", "en").split(",")
        use_gpu = os.environ.get("OCR_USE_GPU", "false").lower() == "true"
        self._reader = easyocr.Reader(
            [lang.strip() for lang in langs if lang.strip()],
            gpu=use_gpu,
            verbose=False,
        )

    def _extract_lines(self, image_bytes: bytes) -> tuple[list[OcrLine], dict[str, Any]]:
        from ..base import load_numpy_image

        image = load_numpy_image(image_bytes)
        detections = self._reader.readtext(image, detail=1, paragraph=False)

        def top_y(det):
            bbox = det[0]
            return min(point[1] for point in bbox)

        lines: list[OcrLine] = []
        for bbox, text, conf in sorted(detections, key=top_y):
            text = (text or "").strip()
            if text:
                lines.append(OcrLine(text=text, confidence=round(float(conf) * 100, 2)))

        return lines, {"detections": len(detections)}
