"""AWS Textract.
Needs credentials. Can handle high volume, handwriting, multi-language.
"""

from __future__ import annotations

import os
from typing import Any

from ..base import OcrEngine, OcrLine


class TextractEngine(OcrEngine):
    name = "textract"
    kind = "cloud-api"
    description = "AWS Textract managed OCR; high-volume, handwriting, AWS-native."
    pip_packages = ("boto3",)
    env_vars = ("AWS_REGION", "AWS_ACCESS_KEY_ID")

    def available(self) -> tuple[bool, str]:
        ok, reason = super().available()
        if not ok:
            return ok, reason
        try:
            import boto3
            from botocore.exceptions import BotoCoreError, ClientError

            sts = boto3.client("sts", region_name=os.environ.get("AWS_REGION", "ap-south-1"))
            sts.get_caller_identity()
            return True, "ready (valid AWS credentials)"
        except (BotoCoreError, ClientError) as error:
            return False, f"no valid AWS credentials: {type(error).__name__}"
        except Exception as error:  # noqa: BLE001
            return False, f"AWS check failed: {error}"

    def _load(self) -> None:
        import boto3

        self._client = boto3.client(
            "textract", region_name=os.environ.get("AWS_REGION", "ap-south-1")
        )

    def _extract_lines(self, image_bytes: bytes) -> tuple[list[OcrLine], dict[str, Any]]:
        response = self._client.detect_document_text(Document={"Bytes": image_bytes})
        lines: list[OcrLine] = []
        for block in response.get("Blocks", []):
            if block.get("BlockType") == "LINE":
                text = (block.get("Text") or "").strip()
                if text:
                    conf = block.get("Confidence")
                    lines.append(OcrLine(text=text, confidence=round(float(conf), 2) if conf else None))
        return lines, {"block_count": len(response.get("Blocks", []))}
