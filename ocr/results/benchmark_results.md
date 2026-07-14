# OCR engine benchmark — Project IVY fixtures

* _Dataset: 5 screenshots in `fixtures/images/`._
* _Accuracy = mean over images vs. the human transcription in `ocr/ground_truth.py` (character / word level)._
* _Latency is per-image extraction, excluding one-time model load; `load` is the one-time init/model-load cost._

| Engine          | Type      | Char acc. | Word acc. | Latency/img | Model load | Status                                                                                                    |
| --------------- | --------- | --------- | --------- | ----------- | ---------- | --------------------------------------------------------------------------------------------------------- |
| `easyocr`     | local-cpu | 97.4%     | 94.0%     | 3798 ms     | 15488 ms   | benchmarked (5/5 imgs)                                                                                    |
| `paddleocr`   | local-cpu | 98.9%     | 92.6%     | 1118 ms     | 29825 ms   | benchmarked (5/5 imgs)                                                                                    |
| `textract`    | cloud-api | —        | —        | —          | —         | not run — Not run here: no valid AWS credentials in this environment. Adapter ready (boto3, AWS-native). |
| `mistral-ocr` | cloud-api | 100.0%    | 100.0%    | 834 ms      | 1100 ms    | benchmarked (5/5 imgs)                                                                                    |
