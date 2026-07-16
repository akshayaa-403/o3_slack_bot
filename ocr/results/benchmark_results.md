# OCR engine benchmark — Project IVY fixtures

_Generated 2026-07-15T11:43:06+00:00. Dataset: 5 synthetic UI-card screenshots in `fixtures/images/`. Accuracy = mean over images vs. the human transcription in `ocr/ground_truth.py` (character / word level). Latency is per-image extraction, excluding one-time model load; `load` is the one-time init/model-load cost._

| Engine | Type | Char acc. | Word acc. | Latency/img | Model load | Status |
|---|---|---|---|---|---|---|
| `easyocr` | local-cpu | 97.4% | 94.0% | 2184 ms | 12142 ms | benchmarked (5/5 imgs) |
| `paddleocr` | local-cpu | 98.9% | 92.6% | 1075 ms | 10594 ms | benchmarked (5/5 imgs) |
| `textract` | cloud-api | — | — | — | — | ran, 0/5 readable — An error occurred (SubscriptionRequiredException) when calling the DetectDocumentText ope… |
| `mistral-ocr` | cloud-api | 100.0% | 100.0% | 823 ms | 1420 ms | benchmarked (5/5 imgs) |
