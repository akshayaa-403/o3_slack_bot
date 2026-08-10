from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class OcrLine:
    """One detected line of text with an optional 0-100 confidence."""

    text: str
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "confidence": self.confidence}


@dataclass
class OcrResult:
    """Normalized output of any engine over a single image."""

    engine: str
    ok: bool = True
    text: str = ""
    lines: list[OcrLine] = field(default_factory=list)
    summary: str = ""
    latency_ms: float | None = None
    error: str | None = None
    error_code: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "ok": self.ok,
            "text": self.text,
            "lines": [line.to_dict() for line in self.lines],
            "summary": self.summary,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "error_code": self.error_code,
            "meta": self.meta,
        }

    def to_image_rek_response(self, analyzed_at: str | None = None) -> dict[str, Any]:
        """Return a dict shaped like ``lambda_o3_image_rek.py``'s response.

        The worker reads ``detected_text`` (``[{text, confidence}]``), ``labels``,
        ``summary`` and ``reply``. Emitting the same keys means an OCR engine can
        replace Rekognition as ``IMAGE_REK_FUNCTION`` with no worker change.
        """

        analyzed_at = analyzed_at or now_iso()
        if not self.ok:
            return {
                "ok": False,
                "image_status": "failed",
                "analyzed_at": analyzed_at,
                "error": self.error or "ocr_failed",
                "error_code": self.error_code or "ocr_failed",
                "engine": self.engine,
            }

        return {
            "ok": True,
            "image_status": "completed",
            "analyzed_at": analyzed_at,
            "reply": f"I analyzed the image.\n\n{self.summary}" if self.summary else "I analyzed the image.",
            "summary": self.summary,
            "detected_text": [line.to_dict() for line in self.lines],
            "labels": [],  # OCR engines return text, not object labels
            "s3_object": None,
            "engine": self.engine,
            "latency_ms": self.latency_ms,
        }


def build_summary(lines: list[OcrLine], max_lines: int = 8) -> str:
    """Mirror the worker/image-rek summary format: ``Detected text: a | b | c``."""

    texts = [line.text.strip() for line in lines if line.text and line.text.strip()]
    if not texts:
        return "No readable text was detected in the image."
    return "Detected text: " + " | ".join(texts[:max_lines])


class OcrEngine(ABC):
    """Base class for all OCR backends.

    Subclasses set the class attributes below and implement ``_load`` (idempotent
    lazy init) and ``_extract_lines`` (the actual OCR). Everything else -- timing,
    error capture, summary building -- is handled here.
    """

    #: short stable identifier used on the CLI and in the registry
    name: str = "base"
    #: one of: "local-cpu", "local-gpu", "vlm", "cloud-api"
    kind: str = "local-cpu"
    #: human-readable one-liner
    description: str = ""
    #: dependency hints surfaced by `available()` and the docs
    pip_packages: tuple[str, ...] = ()
    system_deps: tuple[str, ...] = ()
    env_vars: tuple[str, ...] = ()

    def __init__(self, **options: Any) -> None:
        self.options = options
        self._loaded = False

    # -- capability check -------------------------------------------------
    def available(self) -> tuple[bool, str]:
        """Return ``(can_run, reason)`` with no heavy side effects.

        Default implementation checks that each declared pip package imports.
        Engines with extra requirements (a system binary, valid credentials,
        an env flag) override and call ``super().available()`` first.
        """

        import importlib.util

        missing = [
            pkg
            for pkg in self._import_names()
            if importlib.util.find_spec(pkg) is None
        ]
        if missing:
            return False, f"missing Python package(s): {', '.join(missing)}"
        return True, "ready"

    def _import_names(self) -> tuple[str, ...]:
        """Importable module names to probe; defaults to ``pip_packages``.

        Override when the install name differs from the import name
        (e.g. ``opencv-python`` imports as ``cv2``).
        """

        return self.pip_packages

    # -- lifecycle --------------------------------------------------------
    @abstractmethod
    def _load(self) -> None:
        """Idempotently construct any heavy model/client. Called before use."""

    @abstractmethod
    def _extract_lines(self, image_bytes: bytes) -> tuple[list[OcrLine], dict[str, Any]]:
        """Run OCR and return ``(lines, meta)``. May raise; caller catches."""

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self._load()
            self._loaded = True

    # -- public API -------------------------------------------------------
    def extract(self, image_bytes: bytes) -> OcrResult:
        """Run OCR over one image, capturing timing and errors uniformly."""

        start = time.perf_counter()
        try:
            self.ensure_loaded()
            lines, meta = self._extract_lines(image_bytes)
            latency_ms = round((time.perf_counter() - start) * 1000, 1)
            text = "\n".join(line.text for line in lines if line.text)
            return OcrResult(
                engine=self.name,
                ok=True,
                text=text,
                lines=lines,
                summary=build_summary(lines),
                latency_ms=latency_ms,
                meta=meta,
            )
        except Exception as error:  # noqa: BLE001 - report, don't crash the batch
            latency_ms = round((time.perf_counter() - start) * 1000, 1)
            return OcrResult(
                engine=self.name,
                ok=False,
                latency_ms=latency_ms,
                error=str(error),
                error_code=f"{self.name}_extract_failed",
            )