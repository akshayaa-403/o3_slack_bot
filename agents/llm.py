"""Pluggable LLM client.

Agents depend only on this interface, so the concrete provider is swappable
without touching agent logic. The live provider is **Google Gemini**
(GeminiLLMClient) — it reuses the same GEMINI_API_KEY the Slack worker already
uses (see lambda_o3_slack_worker.py), so no new secret is introduced. Tests run
against MockLLMClient with no network or API keys.

    from agents.llm import GeminiLLMClient, MockLLMClient
    llm = GeminiLLMClient()                                  # prod (reads GEMINI_API_KEY)
    llm = MockLLMClient(responses=['{"request": "..."}'])    # tests
    data = llm.complete_json("extract ... return JSON")
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

# Same defaults as the Slack worker's Gemini path, so both share one config.
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Mistral defaults (OpenAI-compatible REST API).
DEFAULT_MISTRAL_MODEL = "mistral-large-latest"
MISTRAL_ENDPOINT = "https://api.mistral.ai/v1/chat/completions"


class LLMError(Exception):
    """Raised when the model output can't be used (e.g. no parseable JSON)."""


class LLMClient(ABC):
    """Minimal surface the agents need. Concrete providers implement complete()."""

    @abstractmethod
    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> str:
        """Return the model's text completion for a prompt."""

    def complete_json(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Any:
        """complete() + parse the first JSON value out of the response.

        Raises LLMError if nothing parseable is returned.
        """
        text = self.complete(
            prompt, system=system, max_tokens=max_tokens, temperature=temperature
        )
        return extract_json(text)


def extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of a model response.

    Handles ```json fences and surrounding prose. Raises LLMError if none found.
    """
    if text is None:
        raise LLMError("empty LLM response")

    stripped = text.strip()

    # ```json ... ``` or ``` ... ``` fenced block
    fence = re.search(r"```(?:json)?\s*(.+?)```", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()

    # direct parse
    try:
        return json.loads(stripped)
    except ValueError:
        pass

    # first {...} or [...] span
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = stripped.find(open_ch)
        end = stripped.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except ValueError:
                continue

    raise LLMError(f"no JSON found in LLM response: {text[:200]!r}")


class MockLLMClient(LLMClient):
    """Deterministic LLM for tests and demos — no network.

    Either give it a queue of `responses` (returned FIFO), or a `responder`
    callable (prompt, system) -> str. Records every prompt on `.calls`.
    """

    def __init__(
        self,
        responses: Optional[List[str]] = None,
        responder: Optional[Callable[[str, str], str]] = None,
    ):
        self.calls: List[Dict[str, str]] = []
        self._queue = list(responses) if responses is not None else None
        self._responder = responder

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> str:
        self.calls.append({"prompt": prompt, "system": system})
        if self._queue is not None:
            return self._queue.pop(0) if self._queue else "{}"
        if self._responder is not None:
            return self._responder(prompt, system)
        return "{}"


class GeminiLLMClient(LLMClient):
    """Live provider — Google Gemini via the generativelanguage REST API.

    Uses urllib (stdlib) to match the Slack worker's Gemini path exactly, so it
    needs no extra Lambda dependency and reuses the existing GEMINI_API_KEY /
    GEMINI_MODEL config. For JSON tasks it sets responseMimeType=application/json,
    which makes Gemini emit strict JSON — more reliable than parsing prose.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        *,
        timeout: int = 30,
        max_retries: int = 4,
    ) -> None:
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
        self.timeout = timeout
        # Free-tier Gemini caps requests/minute; a burst of runs hits HTTP 429.
        # Retry those (honoring the server's suggested delay) so a run self-heals.
        self.max_retries = max_retries

    @staticmethod
    def _retry_delay_seconds(detail: str, attempt: int) -> float:
        """Seconds to wait before a 429 retry: prefer the server's hint, else back off."""
        match = re.search(r"retry(?:Delay)?[\"'\s:]*([0-9]+(?:\.[0-9]+)?)s", detail)
        if match:
            return min(float(match.group(1)) + 1.0, 30.0)
        return min(2.0 ** attempt, 30.0)  # 2s, 4s, 8s, ...

    def _post(self, body: Dict) -> Dict:
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        url = GEMINI_ENDPOINT.format(model=urllib.parse.quote(self.model, safe=""))

        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            request = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except Exception as error:  # noqa: BLE001 - surface as our own error type
                last_error = error
                detail = ""
                if hasattr(error, "read"):
                    try:
                        detail = error.read().decode("utf-8", errors="replace")[:500]
                    except Exception:  # noqa: BLE001
                        detail = ""
                # Retry rate-limit (429) responses; anything else fails immediately.
                is_rate_limited = isinstance(error, urllib.error.HTTPError) and error.code == 429
                if is_rate_limited and attempt < self.max_retries:
                    time.sleep(self._retry_delay_seconds(detail, attempt))
                    continue
                raise LLMError(f"Gemini request failed: {error} {detail}".strip()) from error

        raise LLMError(f"Gemini request failed after {self.max_retries} attempts: {last_error}")

    @staticmethod
    def _extract_text(response_body: Dict) -> str:
        parts = []
        for candidate in response_body.get("candidates") or []:
            for part in (candidate.get("content") or {}).get("parts") or []:
                text = (part.get("text") or "").strip()
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int = 1024,
        temperature: float = 0.2,
        response_json: bool = False,
    ) -> str:
        generation_config: Dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
        if response_json:
            generation_config["responseMimeType"] = "application/json"

        body: Dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        text = self._extract_text(self._post(body))
        if not text:
            raise LLMError("Gemini returned an empty response")
        return text

    def complete_json(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Any:
        text = self.complete(
            prompt, system=system, max_tokens=max_tokens,
            temperature=temperature, response_json=True,
        )
        return extract_json(text)


class MistralLLMClient(LLMClient):
    """Mistral API client (OpenAI-compatible REST API).

    Uses urllib (stdlib) like the Gemini client, reading MIA_KEY from .env.
    Mistral's chat endpoint is compatible with OpenAI's message format.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        *,
        timeout: int = 30,
    ) -> None:
        self.api_key = api_key or os.environ.get("MIA_KEY", "")
        self.model = model or os.environ.get("MISTRAL_MODEL", DEFAULT_MISTRAL_MODEL)
        self.timeout = timeout

    def _post(self, body: Dict) -> Dict:
        if not self.api_key:
            raise LLMError("MIA_KEY is not set")
        request = urllib.request.Request(
            MISTRAL_ENDPOINT,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as error:  # noqa: BLE001
            detail = ""
            if hasattr(error, "read"):
                try:
                    detail = error.read().decode("utf-8", errors="replace")[:500]
                except Exception:  # noqa: BLE001
                    detail = ""
            raise LLMError(f"Mistral request failed: {error} {detail}".strip()) from error

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        response = self._post(body)
        choices = response.get("choices") or []
        if not choices:
            raise LLMError("Mistral returned no choices")
        text = (choices[0].get("message") or {}).get("content", "").strip()
        if not text:
            raise LLMError("Mistral returned an empty response")
        return text

    def complete_json(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Any:
        text = self.complete(
            prompt, system=system, max_tokens=max_tokens, temperature=temperature
        )
        return extract_json(text)
