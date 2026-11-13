"""Groq provider (OpenAI-compatible chat completions)."""

from __future__ import annotations

import threading
from collections.abc import Callable

import httpx

from .base import ChatRequest, LLMProvider, ProviderError, _dedupe_models


class GroqProvider(LLMProvider):
    id = "groq"
    display_name = "Groq"
    supports_vision_default = True

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

    @property
    def supports_vision(self) -> bool:
        lowered = self.model.lower()
        return any(
            marker in lowered
            for marker in ("llama-4", "llama-3.2", "vision", "maverick", "scout", "qwen2.5-vl")
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def health(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "no GROQ_API_KEY configured"
        try:
            response = httpx.get(
                f"{self.base_url}/models", headers=self._headers(), timeout=10.0
            )
        except httpx.HTTPError as exc:
            return False, f"cannot reach Groq: {exc}"
        if response.status_code != 200:
            return False, f"Groq returned {response.status_code}: {response.text[:160]}"
        return True, f"Groq reachable ({self.model})"

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
            raise ProviderError("Groq needs an API key. Add one in Settings or set GROQ_API_KEY.")
        body = {
            "model": self.model,
            "messages": self._build_messages(request),
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": True,
        }
        # gpt-oss models reason by default on Groq; ask for the shortest
        # reasoning so a voice reply is not delayed by hidden tokens.
        if self.reasoning in ("off", "low"):
            body["reasoning_effort"] = "low"
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
                    raise ProviderError(f"Groq error: {payload['error']}")
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
