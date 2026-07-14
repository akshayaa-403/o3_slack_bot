# OCR engine benchmark — Project IVY fixtures

_Generated 2026-07-14T06:07:45+00:00. Dataset: 5 synthetic UI-card screenshots in `fixtures/images/`. Accuracy = mean over images vs. the human transcription in `ocr/ground_truth.py` (character / word level). Latency is per-image extraction, excluding one-time model load; `load` is the one-time init/model-load cost._

| Engine | Type | Char acc. | Word acc. | Latency/img | Model load | Status |
|---|---|---|---|---|---|---|
| `easyocr` | local-cpu | 97.4% | 94.0% | 1993 ms | 5559 ms | benchmarked (5/5 imgs) |
| `paddleocr` | local-cpu | 98.9% | 92.6% | 1389 ms | 3845 ms | benchmarked (5/5 imgs) |
| `textract` | cloud-api | — | — | — | — | not run — Not run here: no valid AWS credentials in this environment. Adapter ready (boto3, AWS-native). |
| `mistral-ocr` | cloud-api | 100.0% | 100.0% | 952 ms | 1340 ms | benchmarked (5/5 imgs) |
