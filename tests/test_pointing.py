from __future__ import annotations

import pytest

from heyclicky.pointing import parse_pointing, strip_point_tag


def test_parses_coordinate_and_label():
    result = parse_pointing("open the search bar [POINT:1100,42:search bar]")
    assert result.spoken_text == "open the search bar"
    assert result.coordinate == (1100, 42)
    assert result.element_label == "search bar"
    assert result.screen_number is None
    assert result.has_coordinate


def test_parses_screen_number():
    result = parse_pointing("that's on your other monitor [POINT:400,300:terminal:screen2]")
    assert result.coordinate == (400, 300)
    assert result.element_label == "terminal"
    assert result.screen_number == 2


def test_parses_none():
    result = parse_pointing("html is the skeleton of a web page [POINT:none]")
    assert result.coordinate is None
    assert result.element_label == "none"
    assert result.says_none
    assert result.spoken_text == "html is the skeleton of a web page"


def test_no_tag_is_all_spoken():
    result = parse_pointing("just a normal answer.")
    assert result.coordinate is None
    assert not result.has_coordinate
    assert result.spoken_text == "just a normal answer."


def test_tolerates_trailing_markdown_noise():
    result = parse_pointing("click that **[POINT:12,34:save]**.`")
    # The tag must end the response, so noise after it means no tag is parsed.
    assert result.coordinate is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("hello [POINT:0,0:top left]", "hello"),
        ("hello [POINT:1920,1080:bottom right]", "hello"),
        ("hello [POINT:none] ", "hello"),
        ("hello [POINT:5,6:save button]", "hello"),
    ],
)
def test_spoken_text_strips_tag(text, expected):
    assert strip_point_tag(text) == expected


def test_label_can_contain_spaces_and_punctuation():
    result = parse_pointing("look here [POINT:10,20:color inspector!]")
    assert result.element_label == "color inspector!"


def test_coordinate_outside_screen_is_kept_for_clamping():
    result = parse_pointing("off screen [POINT:99999,99999:corner]")
    assert result.coordinate == (99999, 99999)
