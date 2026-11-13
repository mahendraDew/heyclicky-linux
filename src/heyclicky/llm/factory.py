"""Provider factory for Groq, Gemini and Ollama."""

from __future__ import annotations

from ..config import Config, provider_key
from .base import LLMProvider, ProviderError
from .gemini import GeminiProvider
from .groq import GroqProvider
from .ollama import OllamaProvider
from .openrouter import OpenRouterProvider

PROVIDERS = ("openrouter", "groq", "gemini", "ollama")

DISPLAY_NAMES = {
    "openrouter": "OpenRouter",
    "groq": "Groq",
    "gemini": "Gemini",
    "ollama": "Ollama",
}

DEFAULT_MODELS = {
    "openrouter": "nvidia/nemotron-3.5-super-vl:free",
    "groq": "openai/gpt-oss-20b",
    "gemini": "gemini-2.5-flash",
    "ollama": "qwen2.5vl:7b",
}


def make_provider(config: Config, provider_id: str | None = None) -> LLMProvider:
    provider_id = (provider_id or config.get("llm.provider", "openrouter")).lower()
    reasoning = str(config.get("llm.reasoning", "off") or "off").lower()
    if provider_id == "openrouter":
        return OpenRouterProvider(
            api_key=provider_key(config, "openrouter"),
            base_url=config.get("llm.openrouter.base_url", "https://openrouter.ai/api/v1"),
            model=config.get("llm.openrouter.model", DEFAULT_MODELS["openrouter"]),
            reasoning=reasoning,
        )
    if provider_id == "groq":
        return GroqProvider(
            api_key=provider_key(config, "groq"),
            base_url=config.get("llm.groq.base_url", "https://api.groq.com/openai/v1"),
            model=config.get("llm.groq.model", DEFAULT_MODELS["groq"]),
            reasoning=reasoning,
        )
    if provider_id == "gemini":
        return GeminiProvider(
            api_key=provider_key(config, "gemini"),
            base_url=config.get("llm.gemini.base_url", "https://generativelanguage.googleapis.com/v1beta"),
            model=config.get("llm.gemini.model", DEFAULT_MODELS["gemini"]),
        )
    if provider_id == "ollama":
        return OllamaProvider(
            base_url=config.get("llm.ollama.base_url", "http://127.0.0.1:11434"),
            model=config.get("llm.ollama.model", DEFAULT_MODELS["ollama"]),
            keep_alive=config.get("llm.ollama.keep_alive", "10m"),
        )
    raise ProviderError(f"unknown LLM provider: {provider_id}")


def provider_ready(config: Config, provider_id: str) -> bool:
    if provider_id == "groq":
        return bool(config.get("llm.groq.api_key"))
    if provider_id == "openrouter":
        return bool(config.get("llm.openrouter.api_key"))
    if provider_id == "gemini":
        return bool(config.get("llm.gemini.api_key"))
    return provider_id == "ollama"
