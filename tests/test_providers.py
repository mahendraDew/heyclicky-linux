from __future__ import annotations

import json

from heyclicky.llm.base import ChatRequest, ImageAttachment, LLMProvider
from heyclicky.llm.gemini import GeminiProvider
from heyclicky.llm.groq import GroqProvider
from heyclicky.llm.ollama import OllamaProvider


def _request(model: str = "test", images: bool = True) -> ChatRequest:
    attachment = ImageAttachment(data=b"jpeg-bytes", mime_type="image/jpeg", label="screen 1 of 1")
    return ChatRequest(
        system_prompt="you are clicky",
        user_text="what is this?",
        images=[attachment] if images else [],
        history=[("earlier question", "earlier answer")],
    )


def test_groq_payload_embeds_base64_image_and_history():
    provider = GroqProvider(api_key="k", base_url="https://api.groq.com", model="llama-4-scout")
    messages = provider._build_messages(_request())
    assert messages[0] == {"role": "system", "content": "you are clicky"}
    assert messages[1] == {"role": "user", "content": "earlier question"}
    assert messages[2] == {"role": "assistant", "content": "earlier answer"}
    blocks = messages[3]["content"]
    assert blocks[0]["type"] == "image_url"
    assert blocks[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert blocks[1] == {"type": "text", "text": "screen 1 of 1"}
    assert blocks[-1] == {"type": "text", "text": "what is this?"}


def test_groq_text_only_model_drops_images_with_note():
    provider = GroqProvider(api_key="k", base_url="https://x", model="mixtral-8x7b")
    messages = provider._build_messages(_request())
    assert isinstance(messages[-1]["content"], str)
    assert "cannot view images" in messages[-1]["content"]


def test_gemini_payload_uses_inline_data():
    provider = GeminiProvider(api_key="k", base_url="https://x", model="gemini-2.5-flash")
    contents = provider._build_contents(_request())
    assert contents[0] == {"role": "user", "parts": [{"text": "earlier question"}]}
    assert contents[1] == {"role": "model", "parts": [{"text": "earlier answer"}]}
    parts = contents[2]["parts"]
    assert parts[0]["inline_data"]["mime_type"] == "image/jpeg"
    assert parts[1] == {"text": "screen 1 of 1"}
    assert parts[-1] == {"text": "what is this?"}


def test_ollama_vision_model_sends_raw_base64():
    provider = OllamaProvider(base_url="http://127.0.0.1:11434", model="qwen2.5vl:7b")
    messages = provider._build_messages(_request())
    assert messages[-1]["images"] == ["anBlZy1ieXRlcw=="]
    assert "screen 1 of 1" in messages[-1]["content"]


def test_ollama_text_model_adds_note():
    provider = OllamaProvider(base_url="http://127.0.0.1:11434", model="qwen2.5:7b")
    messages = provider._build_messages(_request())
    assert "images" not in messages[-1]
    assert "cannot view images" in messages[-1]["content"]


def test_ollama_without_images_has_plain_content():
    provider = OllamaProvider(base_url="http://127.0.0.1:11434", model="qwen2.5:7b")
    messages = provider._build_messages(_request(images=False))
    assert messages[-1]["content"] == "what is this?"


def test_sse_parser_skips_comments_and_stops_on_done():
    class Response:
        def iter_lines(self):
            return iter(
                [
                    "",
                    ": keep-alive",
                    'data: {"a": 1}',
                    "event: message",
                    'data:{"b": 2}',
                    "data: [DONE]",
                    'data: {"c": 3}',
                ]
            )

    payloads = list(LLMProvider._iter_sse(Response()))
    assert payloads == [{"a": 1}, {"b": 2}]


def test_json_lines_parser_ignores_bad_lines():
    class Response:
        def iter_lines(self):
            return iter(["", json.dumps({"message": {"content": "hi"}}), "not json", '{"done": true}'])

    payloads = list(LLMProvider._iter_json_lines(Response()))
    assert payloads == [{"message": {"content": "hi"}}, {"done": True}]
