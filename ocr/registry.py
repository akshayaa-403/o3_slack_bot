"""Engine registry with lazy imports.

Engines are referenced by ``(module, class)`` strings and only imported when
actually requested, so a missing heavy dependency in one engine never breaks
loading of the others.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import OcrEngine

# name -> (module path, class name)
ENGINES: dict[str, tuple[str, str]] = {
    "textract": ("ocr.engines.textract_engine", "TextractEngine"),
}


def all_engine_names() -> list[str]:
    return list(ENGINES)


def load_engine(name: str, **options) -> "OcrEngine":
    """Instantiate an engine by name (does not load its model yet)."""

    if name not in ENGINES:
        raise KeyError(f"unknown OCR engine '{name}'. Known: {', '.join(ENGINES)}")
    module_path, class_name = ENGINES[name]
    module = importlib.import_module(module_path)
    engine_cls = getattr(module, class_name)
    return engine_cls(**options)


def available_engines(**options) -> dict[str, tuple[bool, str]]:
    """Map every engine name to ``(can_run_here, reason)``."""

    report: dict[str, tuple[bool, str]] = {}
    for name in ENGINES:
        try:
            engine = load_engine(name, **options)
            report[name] = engine.available()
        except Exception as error:  # noqa: BLE001
            report[name] = (False, f"failed to construct: {error}")
    return report