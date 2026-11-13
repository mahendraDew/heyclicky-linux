"""Local Ollama provider using /api/chat (NDJSON streaming).

The user asked for ``qwen2.5`` locally. The text-only qwen2.5 tag cannot see a
screenshot, so the default is ``qwen2.5vl:7b`` (Qwen2.5-VL). If a text-only
model is selected the provider still works: images are dropped with an explicit
note instead of failing the whole request.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import httpx

from .base import ChatRequest, LLMProvider, ProviderError, _dedupe_models


class OllamaProvider(LLMProvider):
    id = "ollama"
    display_name = "Ollama"
    supports_vision_default = False

    def __init__(self, *, base_url: str, model: str, timeout: float = 180.0, keep_alive: str = "10m") -> None:
        super().__init__(model=model, timeout=timeout)
        self.base_url = base_url.rstrip("/")
        self.keep_alive = keep_alive

    @property
    def supports_vision(self) -> bool:
        lowered = self.model.lower()
        return any(
            marker in lowered
            for marker in ("vl", "vision", "llava", "minicpm-v", "moondream", "gemma3", "bakllava")
        )

    def health(self) -> tuple[bool, str]:
        try:
            response = httpx.get(f"{self.base_url}/api/tags", timeout=5.0)
        except httpx.HTTPError as exc:
            return False, f"Ollama not reachable at {self.base_url}: {exc}"
        if response.status_code != 200:
            return False, f"Ollama returned {response.status_code}"
        names = [m.get("name", "") for m in response.json().get("models", [])]
        if self.model not in names and f"{self.model}:latest" not in names:
            return True, f"Ollama reachable, but {self.model} is not pulled yet"
        return True, f"Ollama reachable ({self.model})"

    def list_models(self) -> list[str]:
        try:
            response = httpx.get(f"{self.base_url}/api/tags", timeout=5.0)
            return _dedupe_models(item.get("name", "") for item in response.json().get("models", []))
        except (httpx.HTTPError, ValueError):
            return []

    def _build_messages(self, request: ChatRequest) -> list[dict]:
        messages: list[dict] = [{"role": "system", "content": request.system_prompt}]
        for user_text, assistant_text in self._history_messages(request):
            if user_text:
                messages.append({"role": "user", "content": user_text})
            if assistant_text:
                messages.append({"role": "assistant", "content": assistant_text})

        message: dict = {"role": "user", "content": request.user_text}
        if request.images and self.supports_vision:
            # Ollama accepts raw base64 strings, not data URLs.
            message["images"] = [image.base64() for image in request.images]
            labels = "\n".join(image.label for image in request.images if image.label)
            if labels:
                message["content"] = f"{labels}\n\n{request.user_text}"
        elif request.images:
            message["content"] += (
                "\n\n(the selected local model cannot view images, so answer from the text only.)"
            )
        messages.append(message)
        return messages

    def _stream(self, request: ChatRequest, emit: Callable[[str], None], cancel: threading.Event) -> str:
        body = {
            "model": self.model,
            "messages": self._build_messages(request),
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_tokens,
            },
        }
        try:
            client = httpx.Client(timeout=httpx.Timeout(self.timeout, connect=5.0))
        except Exception as exc:  # pragma: no cover - defensive
            raise ProviderError(f"could not create Ollama client: {exc}") from exc

        try:
            with client.stream("POST", f"{self.base_url}/api/chat", json=body) as response:
                if response.status_code >= 400:
                    detail = response.read().decode("utf-8", "replace")[:800]
                    raise ProviderError(f"Ollama API error ({response.status_code}): {detail}")
                chunks: list[str] = []
                for payload in self._iter_json_lines(response):
                    self._check_cancel(cancel)
                    if payload.get("error"):
                        raise ProviderError(f"Ollama error: {payload['error']}")
                    message = payload.get("message") or {}
                    content = message.get("content")
                    if isinstance(content, str) and content:
                        chunks.append(content)
                        emit(content)
                    if payload.get("done"):
                        break
                return "".join(chunks)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Ollama request failed: {exc}") from exc
        finally:
            client.close()
