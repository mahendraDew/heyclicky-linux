"""OpenRouter provider (OpenAI-compatible chat completions).

OpenRouter proxies many models through a single OpenAI-compatible endpoint.
The default model is NVIDIA Nemotron 3.5 Super VL (free tier): a large-context
vision model that tends to be less rate-limited than the Gemma free tier.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import httpx

from .base import ChatRequest, LLMProvider, ProviderError, _dedupe_models


class OpenRouterProvider(LLMProvider):
    id = "openrouter"
    display_name = "OpenRouter"
    supports_vision_default = True
    rate_limit_hint = (
        "OpenRouter's free requests are used up or throttled right now. "
        "Add $10 of credits to unlock 1,000 free requests per day, or add a "
        "Gemini API key in Settings for a fast free vision model."
    )

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 120.0,
        reasoning: str = "off",
    ) -> None:
        super().__init__(model=model, timeout=timeout)
        self.api_key = (api_key or "").strip()
        self.base_url = base_url.rstrip("/")
        self.reasoning = (reasoning or "off").lower()

    def _reasoning_payload(self) -> dict:
        if self.reasoning == "off":
            return {"reasoning": {"enabled": False}}
        if self.reasoning == "low":
            return {"reasoning": {"effort": "low"}}
        return {}

    @property
    def supports_vision(self) -> bool:
        lowered = self.model.lower()
        return any(
            marker in lowered
            for marker in ("vision", "vl", "gemma", "gpt-4", "claude", "omni", "inkling")
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # OpenRouter attribution headers (optional but encouraged).
            "HTTP-Referer": "https://github.com/farzaa/clicky",
            "X-Title": "HeyClicky Linux",
        }

    def health(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "no OPENROUTER_GEMMA_API_KEY configured"
        try:
            response = httpx.get(
                f"{self.base_url}/models", headers=self._headers(), timeout=10.0
            )
        except httpx.HTTPError as exc:
            return False, f"cannot reach OpenRouter: {exc}"
        if response.status_code != 200:
            return False, f"OpenRouter returned {response.status_code}: {response.text[:160]}"
        return True, f"OpenRouter reachable ({self.model})"

    def list_models(self) -> list[str]:
        if not self.api_key:
            return []
        try:
            response = httpx.get(
                f"{self.base_url}/models", headers=self._headers(), timeout=10.0
            )
            payload = response.json()
            return _dedupe_models(item.get("id", "") for item in payload.get("data", []))
        except (httpx.HTTPError, ValueError):
            return []

    def _build_messages(self, request: ChatRequest) -> list[dict]:
        messages: list[dict] = [{"role": "system", "content": request.system_prompt}]
        for user_text, assistant_text in self._history_messages(request):
            if user_text:
                messages.append({"role": "user", "content": user_text})
            if assistant_text:
                messages.append({"role": "assistant", "content": assistant_text})

        if request.images and self.supports_vision:
            blocks: list[dict] = []
            for image in request.images:
                blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{image.mime_type};base64,{image.base64()}"
                        },
                    }
                )
                if image.label:
                    blocks.append({"type": "text", "text": image.label})
            blocks.append({"type": "text", "text": request.user_text})
            messages.append({"role": "user", "content": blocks})
        else:
            text = request.user_text
            if request.images:
                text += "\n\n(the selected model cannot view images, so answer from the text only.)"
            messages.append({"role": "user", "content": text})
        return messages

    def _stream(self, request: ChatRequest, emit: Callable[[str], None], cancel: threading.Event) -> str:
        if not self.api_key:
            raise ProviderError(
                "OpenRouter needs an API key. Add one in Settings or set OPENROUTER_GEMMA_API_KEY."
            )
        body = {
            "model": self.model,
            "messages": self._build_messages(request),
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": True,
            **self._reasoning_payload(),
        }
        response = self._post_stream(
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            json_body=body,
            cancel=cancel,
        )
        chunks: list[str] = []
        try:
            for payload in self._iter_sse(response):
                self._check_cancel(cancel)
                if payload.get("error"):
                    raise ProviderError(f"OpenRouter error: {payload['error']}")
                choices = payload.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if isinstance(content, str) and content:
                    chunks.append(content)
                    emit(content)
        finally:
            response.close()
        return "".join(chunks)
