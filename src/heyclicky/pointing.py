"""Parsing of Clicky's ``[POINT:...]`` response tags.

The macOS app appends a coordinate tag to the end of an LLM response:

    [POINT:1100,42:color inspector]
    [POINT:400,300:terminal:screen2]
    [POINT:none]

This module deliberately keeps the same grammar, including the optional 1-3
word label and the optional 1-based screen number, because prompts and model
output from the original app remain compatible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Same shape as CompanionManager.parsePointingCoordinates. The label is allowed
# to contain spaces and punctuation but not a colon, and cannot start with a
# colon or whitespace. The tag must end the response (trailing whitespace ok).
POINT_TAG_PATTERN = re.compile(
    r"\[POINT:(?:none|(\d+)\s*,\s*(\d+)(?::([^\]:\s][^\]:]*?))?(?::screen(\d+))?)\]\s*$"
)

# Some models wrap the tag in markdown code ticks or append a period. Those are
# left in the spoken text rather than loosening the coordinate format itself.

@dataclass(frozen=True)
class PointingResult:
    """Parsed pointing tag plus the text that should be spoken."""

    spoken_text: str
    coordinate: tuple[int, int] | None = None
    element_label: str | None = None
    screen_number: int | None = None  # 1-based, None means cursor screen

    @property
    def has_coordinate(self) -> bool:
        return self.coordinate is not None

    @property
    def says_none(self) -> bool:
        return self.coordinate is None and self.element_label == "none"


def parse_pointing(response_text: str) -> PointingResult:
    """Split a response into spoken text and an optional pointing target."""
    if not response_text:
        return PointingResult(spoken_text="")

    match = POINT_TAG_PATTERN.search(response_text)
    if match is None:
        return PointingResult(spoken_text=response_text.strip())

    spoken = response_text[: match.start()].strip()

    x_raw, y_raw, label, screen_raw = match.group(1), match.group(2), match.group(3), match.group(4)
    if x_raw is None or y_raw is None:
        return PointingResult(spoken_text=spoken, coordinate=None, element_label="none")

    label = label.strip() if label else None
    screen_number = int(screen_raw) if screen_raw else None
    return PointingResult(
        spoken_text=spoken,
        coordinate=(int(x_raw), int(y_raw)),
        element_label=label,
        screen_number=screen_number,
    )


def strip_point_tag(response_text: str) -> str:
    """Return only the speakable part of a response."""
    return parse_pointing(response_text).spoken_text
