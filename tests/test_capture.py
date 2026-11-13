from __future__ import annotations

from heyclicky.capture import ScreenInfo, _match_streams


def _screen(name: str, x: int, y: int, width: int, height: int) -> ScreenInfo:
    return ScreenInfo(name=name, x=x, y=y, width=width, height=height)


def test_match_streams_by_position():
    screens = [
        _screen("eDP-1", 0, 0, 1920, 1080),
        _screen("HDMI-1", 1920, 0, 2560, 1440),
    ]
    streams = [
        (7, {"position": (1920, 0), "size": (2560, 1440)}),
        (3, {"position": (0, 0), "size": (1920, 1080)}),
    ]

    pairs = _match_streams(streams, screens)

    assert [(screen.name, node) for screen, node in pairs] == [
        ("eDP-1", 3),
        ("HDMI-1", 7),
    ]


def test_match_streams_falls_back_to_stream_order():
    screens = [_screen("only", 0, 0, 800, 600)]
    streams = [(5, {})]

    pairs = _match_streams(streams, screens)

    assert [(screen.name, node) for screen, node in pairs] == [("only", 5)]


def test_match_streams_ignores_unshared_screens():
    screens = [
        _screen("shared", 0, 0, 1920, 1080),
        _screen("not-shared", 1920, 0, 1920, 1080),
    ]
    streams = [(2, {"position": (0, 0), "size": (1920, 1080)})]

    pairs = _match_streams(streams, screens)

    assert [(screen.name, node) for screen, node in pairs] == [("shared", 2)]
