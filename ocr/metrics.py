from __future__ import annotations

import re
from dataclasses import dataclass


def _edit_distance(a: list, b: list) -> int:

    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + cost,
            ))
        previous = current
    return previous[-1]


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _tokens(text: str) -> list[str]:
    return _normalize_ws(text).lower().split()


@dataclass
class AccuracyScore:
    cer: float
    wer: float
    char_accuracy: float
    word_accuracy: float
    ref_chars: int
    ref_words: int

    def to_dict(self) -> dict:
        return {
            "cer": round(self.cer, 4),
            "wer": round(self.wer, 4),
            "char_accuracy": round(self.char_accuracy, 4),
            "word_accuracy": round(self.word_accuracy, 4),
            "ref_chars": self.ref_chars,
            "ref_words": self.ref_words,
        }


def score(prediction: str, reference: str) -> AccuracyScore:
    """Score a predicted transcription against the reference ground truth."""

    ref_norm = _normalize_ws(reference)
    pred_norm = _normalize_ws(prediction)

    # Character error rate
    ref_chars = list(ref_norm)
    cer_distance = _edit_distance(list(pred_norm), ref_chars)
    cer = cer_distance / max(1, len(ref_chars))

    # Word error rate
    ref_words = _tokens(reference)
    wer_distance = _edit_distance(_tokens(prediction), ref_words)
    wer = wer_distance / max(1, len(ref_words))

    return AccuracyScore(
        cer=cer,
        wer=wer,
        char_accuracy=max(0.0, 1.0 - cer),
        word_accuracy=max(0.0, 1.0 - wer),
        ref_chars=len(ref_chars),
        ref_words=len(ref_words),
    )
