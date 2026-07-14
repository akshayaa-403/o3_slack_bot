# Deploying the PaddleOCR image-analysis Lambda

This Lambda replaces the Rekognition-based `IMAGE_REK_FUNCTION`. It downloads the
Slack image, runs **PaddleOCR**, and returns the same response shape the worker
already expects (`ok`, `image_status`, `detected_text`, `summary`, `reply`,
`labels`). The worker then sends the extracted text to Gemini.

Because `paddlepaddle` + `paddleocr` + `opencv` far exceed the 250 MB zip limit,
this ships as a **container image** (`ocr/Dockerfile`).

---

## Part A — Build & test locally (no AWS needed)

The AWS base image bundles the Runtime Interface Emulator, so the handler runs
exactly as it would in Lambda, on your machine.

1. **Build the image** (from repo root; ~2–2.5 GB, first build is slow):
   ```bash
   docker build -t ivy-ocr ocr/
   ```

2. **Serve a fixture image** so the container can download one (separate terminal,
   from repo root):
   ```bash
   python -m http.server 8000 --directory fixtures/images
   ```

3. **Run the container** (exposes the emulator on port 9000):
   ```bash
   docker run --rm -p 9000:8080 ivy-ocr
   ```

4. **Invoke it** with the sample event (another terminal). On PowerShell use
   `curl.exe` (plain `curl` is an alias for `Invoke-WebRequest`):
   ```bash
   curl.exe -s "http://localhost:9000/2015-03-31/functions/function/invocations" \
     -d "@ocr/sample_lambda_event.json"
   ```
   Expect JSON with `"ok": true`, `"image_status": "completed"`, and a
   `detected_text` array of the text PaddleOCR read from the screenshot.
   `sample_lambda_event.json` points at `host.docker.internal:8000` (the host's
   file server); it is not a Slack URL, so no bot token is needed for the test.

If that returns real OCR text, the image is correct and ready to hand off.

### Verified local test

Confirmed working against `fixtures/images/adam_login_mfa_error.png`. Response
(abridged) — all 7 text lines extracted at 94–99% confidence:

```json
{
  "ok": true,
  "image_status": "completed",
  "engine": "paddleocr",
  "latency_ms": 4280.5,
  "detected_text": [
    {"text": "ADAM Portal", "confidence": 97.08},
    {"text": "Error: ADAM portal shows blank page after MFA", "confidence": 99.15},
    {"text": "Request: help me fix ADAM login issue.", "confidence": 97.02}
  ]
}
```

`latency_ms` ~4.3 s here is the **cold** call (includes model load); warm calls
are ~1 s. The ≥60 s Lambda timeout below covers cold starts comfortably.

---

## Part B — Deploy to AWS (needs an account with ECR + Lambda + IAM access)

Replace `<acct>` and `<region>` (e.g. `ap-southeast-2`) throughout.

1. **Create an ECR repo and push the image:**
   ```bash
   aws ecr create-repository --repository-name ivy-ocr --region <region>
   aws ecr get-login-password --region <region> \
     | docker login --username AWS --password-stdin <acct>.dkr.ecr.<region>.amazonaws.com
   docker tag ivy-ocr:latest <acct>.dkr.ecr.<region>.amazonaws.com/ivy-ocr:latest
   docker push <acct>.dkr.ecr.<region>.amazonaws.com/ivy-ocr:latest
   ```

2. **Create the Lambda from the image** under a NEW name (do not touch the
   existing Rekognition function yet):
   - Name: `O3_Image_OCR`
   - Package type: Image → the URI pushed above
   - **Memory: 2048 MB or more** (PaddleOCR is CPU-heavy; low memory = slow)
   - **Timeout: 60 s or more** (cold load ~4 s + ~1 s/image)
   - Execution role needs CloudWatch Logs (basic Lambda role is enough; no
     Rekognition/S3 permissions are required by this engine).

3. **Set environment variables** on `O3_Image_OCR`:
   - `OCR_ENGINE=paddleocr` (already the image default; set explicitly to be safe)
   - `SLACK_BOT_TOKEN=<the bot token>` (needed to download `url_private` images)
   - `AWS_REGION=<region>`

4. **Smoke-test in AWS** before cutover — invoke `O3_Image_OCR` directly with a
   real Slack file event (or the Test tab) and confirm `detected_text` comes back.

5. **Cut over** — on the **worker** Lambda (`O3_slack_worker` /
   `lambda_o3_slack_worker.py`):
   - Set `IMAGE_REK_FUNCTION=O3_Image_OCR`
   - Ensure the worker's execution role has `lambda:InvokeFunction` on
     `O3_Image_OCR`.

6. **Verify end-to-end** — upload a screenshot in a Slack DM to the bot. Expected
   flow: worker → `O3_Image_OCR` (PaddleOCR) → extracted text → Gemini → reply.
   Worker logs should show `image_flow_completed` with
   `image_resolution_source: "gemini"` and `response_source: "image_gemini"`.

### Rollback
Point `IMAGE_REK_FUNCTION` back at the previous Rekognition function name. No
other change is needed; the response shape is identical.

---

## Notes
- Model weights are baked into the image at build time (`HOME=/opt`,
  `~/.paddleocr`). No model download happens at cold start, and the Lambda needs
  no outbound internet for models — only to reach `slack.com` for the image and
  (in the worker) Gemini.
- The worker change that routes OCR text → Gemini is already in
  `lambda_o3_slack_worker.py` (`invoke_gemini_from_text`); deploy the worker too
  if that change hasn't shipped.
