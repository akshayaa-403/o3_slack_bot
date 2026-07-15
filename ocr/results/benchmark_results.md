# OCR engine benchmark — Project IVY fixtures

_Generated 2026-07-15T04:17:04+00:00. Dataset: 5 synthetic UI-card screenshots in `fixtures/images/`. Accuracy = mean over images vs. the human transcription in `ocr/ground_truth.py` (character / word level). Latency is per-image extraction, excluding one-time model load; `load` is the one-time init/model-load cost._

| Engine | Type | Char acc. | Word acc. | Latency/img | Model load | Status |
|---|---|---|---|---|---|---|
| `easyocr` | local-cpu | 97.4% | 94.0% | 2367 ms | 13817 ms | benchmarked (5/5 imgs) |
| `paddleocr` | local-cpu | 98.9% | 92.6% | 1190 ms | 10442 ms | benchmarked (5/5 imgs) |
| `textract` | cloud-api | — | — | — | — | not run — Not run here: no valid AWS credentials in this environment. Adapter ready (boto3, AWS-native). |
| `mistral-ocr` | cloud-api | 100.0% | 100.0% | 860 ms | 2110 ms | benchmarked (5/5 imgs) |
