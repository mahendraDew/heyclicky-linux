"""Google Gemini provider (streamGenerateContent with SSE)."""

from __future__ import annotations

import threading
from collections.abc import Callable

import httpx

from .base import ChatRequest, LLMProvider, ProviderError, _dedupe_models


class GeminiProvider(LLMProvider):
    id = "gemini"
    display_name = "Gemini"
    supports_vision_default = True

    def __init__(self, *, api_key: str, base_url: str, model: str, timeout: float = 120.0) -> None:
        super().__init__(model=model, timeout=timeout)
        self.api_key = (api_key or "").strip()
        self.base_url = base_url.rstrip("/")

    def health(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "no GEMINI_API_KEY configured"
        try:
            response = httpx.get(
                f"{self.base_url}/models",
                params={"key": self.api_key, "pageSize": 1},
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            return False, f"cannot reach Gemini: {exc}"
        if response.status_code != 200:
            return False, f"Gemini returned {response.status_code}: {response.text[:160]}"
        return True, f"Gemini reachable ({self.model})"

    def list_models(self) -> list[str]:
        if not self.api_key:
            return []
        try:
            response = httpx.get(
                f"{self.base_url}/models",
                params={"key": self.api_key, "pageSize": 200},
                timeout=10.0,
            )
            payload = response.json()
            return _dedupe_models(
                item.get("name", "").removeprefix("models/")
                for item in payload.get("models", [])
                if "generateContent" in (item.get("supportedGenerationMethods") or [])
            )
        except (httpx.HTTPError, ValueError):
            return []

    def _build_contents(self, request: ChatRequest) -> list[dict]:
        contents: list[dict] = []
        for user_text, assistant_text in self._history_messages(request):
            if user_text:
                contents.append({"role": "user", "parts": [{"text": user_text}]})
            if assistant_text:
                contents.append({"role": "model", "parts": [{"text": assistant_text}]})

        parts: list[dict] = []
        if request.images and self.supports_vision:
            for image in request.images:
                parts.append(
                    {
                        "inline_data": {
                            "mime_type": image.mime_type,
                            "data": image.base64(),
                        }
                    }
                )
                if image.label:
                    parts.append({"text": image.label})
        elif request.images:
            parts.append(
                {"text": "(the selected model cannot view images, so answer from the text only.)"}
            )
        parts.append({"text": request.user_text})
        contents.append({"role": "user", "parts": parts})
        return contents

    def _stream(self, request: ChatRequest, emit: Callable[[str], None], cancel: threading.Event) -> str:
        if not self.api_key:
            raise ProviderError("Gemini needs an API key. Add one in Settings or set GEMINI_API_KEY.")
        body = {
            "systemInstruction": {"parts": [{"text": request.system_prompt}]},
            "contents": self._build_contents(request),
            "generationConfig": {
                "maxOutputTokens": request.max_tokens,
                "temperature": request.temperature,
            },
        }
        # The API key is a query parameter for Gemini's REST API, so this path
        # streams directly instead of going through LLMProvider._post_stream.
        client = httpx.Client(timeout=httpx.Timeout(self.timeout, connect=15.0))
        try:
            with client.stream(
                "POST",
                f"{self.base_url}/models/{self.model}:streamGenerateContent",
                params={"alt": "sse", "key": self.api_key},
                json=body,
                headers={"Content-Type": "application/json"},
            ) as stream:
                if stream.status_code >= 400:
                    detail = stream.read().decode("utf-8", "replace")[:800]
                    raise ProviderError(f"Gemini API error ({stream.status_code}): {detail}")

                chunks: list[str] = []
                for payload in self._iter_sse(stream):
                    self._check_cancel(cancel)
                    if error := payload.get("error"):
                        raise ProviderError(f"Gemini error: {error}")
                    feedback = payload.get("promptFeedback") or {}
                    if feedback.get("blockReason"):
                        raise ProviderError(f"Gemini blocked the request: {feedback['blockReason']}")
                    candidates = payload.get("candidates") or []
                    if not candidates:
                        continue
                    parts = (candidates[0].get("content") or {}).get("parts") or []
                    for part in parts:
                        if part.get("thought"):
                            continue
                        text = part.get("text")
                        if isinstance(text, str) and text:
                            chunks.append(text)
                            emit(text)
                return "".join(chunks)
        finally:
            client.close()
