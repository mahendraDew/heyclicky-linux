"""Companion state machine primitives shared by the pipeline and the UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class VoiceState(str, Enum):
    """The four states exposed by the original CompanionVoiceState enum."""

    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    RESPONDING = "responding"

    @property
    def label(self) -> str:
        return {
            VoiceState.IDLE: "Idle",
            VoiceState.LISTENING: "Listening",
            VoiceState.PROCESSING: "Thinking",
            VoiceState.RESPONDING: "Speaking",
        }[self]


@dataclass
class Conversation:
    """In-memory chat history, capped at ten exchanges like the original."""

    max_exchanges: int = 10
    exchanges: list[tuple[str, str]] = field(default_factory=list)

    def add(self, user_transcript: str, assistant_response: str) -> None:
        user_transcript = user_transcript.strip()
        assistant_response = assistant_response.strip()
        if not user_transcript and not assistant_response:
            return
        self.exchanges.append((user_transcript, assistant_response))
        if len(self.exchanges) > self.max_exchanges:
            self.exchanges = self.exchanges[-self.max_exchanges :]

    def clear(self) -> None:
        self.exchanges.clear()

    def __len__(self) -> int:
        return len(self.exchanges)
