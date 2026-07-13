from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from . import metrics
from .base import now_iso
from .ground_truth import GROUND_TRUTH, reference_text
from .registry import ENGINES, all_engine_names, load_engine

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "fixtures" / "images"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

ENVIRONMENT_NOTES = {
    "textract": "Not run here: no valid AWS credentials in this environment. Adapter ready (boto3, AWS-native)."
}

def _one_line(text: str, limit: int = 140) -> str:
    """Collapse whitespace/newlines so a reason fits one markdown table cell."""

    collapsed = " ".join((text or "").split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _fixture_paths() -> list[Path]:
    return [FIXTURES_DIR / name for name in GROUND_TRUTH if (FIXTURES_DIR / name).exists()]


def run_engine(name: str) -> dict:
    """Run one engine over all fixtures and return a structured result dict."""

    engine = load_engine(name)
    ok, reason = engine.available()
    if not ok:
        return {"engine": name, "available": False, "reason": reason, "generated_at": now_iso()}

    import time

    load_start = time.perf_counter()
    try:
        engine.ensure_loaded()
    except Exception as error:
        return {"engine": name, "available": False, "reason": f"load failed: {error}", "generated_at": now_iso()}
    load_ms = round((time.perf_counter() - load_start) * 1000, 1)

    per_image = []
    for path in _fixture_paths():
        image_bytes = path.read_bytes()
        result = engine.extract(image_bytes)
        reference = reference_text(path.name)
        acc = metrics.score(result.text, reference) if result.ok else None
        per_image.append({
            "image": path.name,
            "ok": result.ok,
            "latency_ms": result.latency_ms,
            "predicted_text": result.text,
            "accuracy": acc.to_dict() if acc else None,
            "error": result.error,
        })

    ok_images = [r for r in per_image if r["ok"] and r["accuracy"]]
    summary = {
        "engine": name,
        "kind": engine.kind,
        "description": engine.description,
        "available": True,
        "reason": reason,
        "load_ms": load_ms,
        "images": len(per_image),
        "images_ok": len(ok_images),
        "mean_char_accuracy": round(statistics.mean(r["accuracy"]["char_accuracy"] for r in ok_images), 4) if ok_images else None,
        "mean_word_accuracy": round(statistics.mean(r["accuracy"]["word_accuracy"] for r in ok_images), 4) if ok_images else None,
        "mean_latency_ms": round(statistics.mean(r["latency_ms"] for r in ok_images), 1) if ok_images else None,
        "median_latency_ms": round(statistics.median(r["latency_ms"] for r in ok_images), 1) if ok_images else None,
        "per_image": per_image,
        "generated_at": now_iso(),
    }
    return summary


def cmd_run(engine_names: list[str]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    for name in engine_names:
        print(f"[run] {name} ...", flush=True)
        result = run_engine(name)
        out = RESULTS_DIR / f"result-{name}.json"
        out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        if result.get("available"):
            print(f"  char_acc={result['mean_char_accuracy']} word_acc={result['mean_word_accuracy']} "
                  f"latency={result['mean_latency_ms']}ms load={result['load_ms']}ms -> {out.name}")
        else:
            print(f"  unavailable: {result['reason']} -> {out.name}")


def _live_reason(name: str) -> str:
    try:
        ok, reason = load_engine(name).available()
        if not ok:
            return reason
    except Exception:
        pass
    return ENVIRONMENT_NOTES.get(name, "not benchmarked")


def cmd_report() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in all_engine_names():
        result_path = RESULTS_DIR / f"result-{name}.json"
        if result_path.exists():
            data = json.loads(result_path.read_text(encoding="utf-8"))
        else:
            data = None
        rows.append((name, data))

    lines = [
        "# OCR engine benchmark — Project IVY fixtures",
        "",
        f"_Generated {now_iso()}. Dataset: {len(_fixture_paths())} synthetic UI-card "
        "screenshots in `fixtures/images/`. Accuracy = mean over images vs. the "
        "human transcription in `ocr/ground_truth.py` (character / word level). "
        "Latency is per-image extraction, excluding one-time model load; `load` is "
        "the one-time init/model-load cost._",
        "",
        "| Engine | Type | Char acc. | Word acc. | Latency/img | Model load | Status |",
        "|---|---|---|---|---|---|---|",
    ]

    summary_json = {"generated_at": now_iso(), "engines": {}}
    for name, data in rows:
        kind = (data.get("kind") if data else None) or _kind_of(name)
        char = word = lat = load = "—"
        if data and data.get("available") and data.get("images_ok"):
            char = f"{data['mean_char_accuracy']*100:.1f}%" if data.get("mean_char_accuracy") is not None else "—"
            word = f"{data['mean_word_accuracy']*100:.1f}%" if data.get("mean_word_accuracy") is not None else "—"
            lat = f"{data['mean_latency_ms']:.0f} ms" if data.get("mean_latency_ms") is not None else "—"
            load = f"{data['load_ms']:.0f} ms" if data.get("load_ms") is not None else "—"
            status = f"benchmarked ({data['images_ok']}/{data['images']} imgs)"
        elif data and data.get("available"):
            # Engine loaded but produced no readable text on any image.
            sample_err = next((i.get("error") for i in data.get("per_image", []) if i.get("error")), "no text detected")
            status = f"ran, 0/{data.get('images', 0)} readable — {_one_line(sample_err, 90)}"
        else:
            reason = ENVIRONMENT_NOTES.get(name) or (data or {}).get("reason") or _live_reason(name)
            status = f"not run — {_one_line(reason)}"
        lines.append(f"| `{name}` | {kind} | {char} | {word} | {lat} | {load} | {status} |")
        summary_json["engines"][name] = data or {"engine": name, "available": False, "reason": _live_reason(name)}

    (RESULTS_DIR / "benchmark_results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (RESULTS_DIR / "benchmark_summary.json").write_text(json.dumps(summary_json, indent=2), encoding="utf-8")
    print("\n".join(lines))
    print(f"\nWrote {RESULTS_DIR / 'benchmark_results.md'}")


def _kind_of(name: str) -> str:
    try:
        return load_engine(name).kind
    except Exception:
        return "?"


def main() -> None:
    parser = argparse.ArgumentParser(description="OCR engine benchmark for Project IVY fixtures.")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Benchmark one or more engines in this interpreter.")
    run_parser.add_argument("--engines", nargs="+", default=all_engine_names(),
                            help=f"Engine names to run. Choices: {', '.join(ENGINES)}")

    sub.add_parser("report", help="Merge results/ into a markdown table + JSON summary.")

    args = parser.parse_args()
    if args.command == "run":
        cmd_run(args.engines)
    elif args.command == "report":
        cmd_report()


if __name__ == "__main__":
    main()