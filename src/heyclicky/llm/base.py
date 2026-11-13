"""LLM provider interface shared by Groq, Gemini and Ollama.

Every provider receives the same normalized request and streams plain text
deltas back. This is the Linux equivalent of the single ``/chat`` route in the
Cloudflare Worker, except requests go straight to the chosen provider.
"""

from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

import httpx


class ProviderError(RuntimeError):
    """A user-facing provider failure (missing key, HTTP error, bad payload)."""


class StreamCancelled(RuntimeError):
    """Raised inside a worker thread when the user starts a new utterance."""


@dataclass(frozen=True)
class ImageAttachment:
    data: bytes
    mime_type: str = "image/jpeg"
    label: str = ""

    def base64(self) -> str:
        import base64

        return base64.b64encode(self.data).decode("ascii")


@dataclass
class ChatRequest:
    system_prompt: str
    user_text: str
    images: list[ImageAttachment] = field(default_factory=list)
    history: list[tuple[str, str]] = field(default_factory=list)
    max_tokens: int = 1024
    temperature: float = 0.4


class LLMProvider(ABC):
    """Base class for streaming vision chat providers."""

    id: str = "base"
    display_name: str = "Base"
    supports_vision_default: bool = True

    def __init__(self, *, model: str, timeout: float = 120.0) -> None:
        self.model = model
        self.timeout = timeout

    # -- capability / health -------------------------------------------
    @property
    def supports_vision(self) -> bool:
        return self.supports_vision_default

    @abstractmethod
    def health(self) -> tuple[bool, str]:
        """Return (ok, human readable detail) for the panel/doctor."""

    @abstractmethod
    def list_models(self) -> list[str]:
        """Best-effort model listing; empty when unavailable."""

    # -- chat -----------------------------------------------------------
    @abstractmethod
    def _stream(self, request: ChatRequest, emit: Callable[[str], None], cancel: threading.Event) -> str:
        """Provider-specific streaming implementation."""

    def stream_chat(
        self,
        request: ChatRequest,
        on_delta: Callable[[str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> str:
        cancel = cancel or threading.Event()
        chunks: list[str] = []

        def emit(text: str) -> None:
            if not text:
                return
            chunks.append(text)
            if on_delta is not None:
                on_delta(text)

        text = self._stream(request, emit, cancel)
        # Some APIs omit a final delta when the stream is aggregated; fall back
        # to the accumulated chunks so callers always receive the full answer.
        if not text:
            text = "".join(chunks)
        return text

    # -- helpers --------------------------------------------------------
    @staticmethod
    def _check_cancel(cancel: threading.Event) -> None:
        if cancel.is_set():
            raise StreamCancelled()

    @staticmethod
    def _iter_sse(response: httpx.Response) -> Iterator[dict]:
        for line in response.iter_lines():
            if not line:
                continue
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line or line == "[DONE]":
                if line == "[DONE]":
                    return
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload

    @staticmethod
    def _iter_json_lines(response: httpx.Response) -> Iterator[dict]:
        for line in response.iter_lines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload

    def _post_stream(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict | None = None,
        cancel: threading.Event | None = None,
    ) -> httpx.Response:
        client = httpx.Client(timeout=httpx.Timeout(self.timeout, connect=15.0))
        request = client.build_request("POST", url, headers=headers, json=json_body)
        try:
            response = client.send(request, stream=True)
        except httpx.HTTPError as exc:
            client.close()
            raise ProviderError(f"{self.display_name} request failed: {exc}") from exc
        if response.status_code >= 400:
            try:
                detail = response.read().decode("utf-8", "replace")[:800]
            finally:
                response.close()
                client.close()
            if response.status_code == 429:
                hint = getattr(self, "rate_limit_hint", "")
                raise ProviderError(
                    hint
                    or f"{self.display_name} is rate-limiting requests right now. Try again in a moment."
                )
            raise ProviderError(f"{self.display_name} API error ({response.status_code}): {detail}")
        return response

    @staticmethod
    def _history_messages(request: ChatRequest) -> list[tuple[str, str]]:
        return [(u.strip(), a.strip()) for u, a in request.history if u.strip() or a.strip()]


def _dedupe_models(models: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for model in models:
        if model and model not in seen:
            seen.add(model)
            ordered.append(model)
    return ordered
