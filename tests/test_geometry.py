from __future__ import annotations

import pytest

from heyclicky.geometry import (
    ScreenGeometry,
    bezier_point,
    bezier_tangent_rotation,
    choose_target_screen,
    flight_plan,
    global_to_local,
    local_to_global,
    map_screenshot_point,
    screen_for_point,
    smoothstep,
)


def test_screen_contains_half_open_bounds():
    screen = ScreenGeometry("DP-1", 0, 0, 100, 50)
    assert screen.contains(0, 0)
    assert screen.contains(99.9, 49.9)
    assert not screen.contains(100, 10)
    assert not screen.contains(10, 50)


def test_screen_for_point_falls_back_to_first():
    screens = [ScreenGeometry("a", 0, 0, 100, 100), ScreenGeometry("b", 100, 0, 100, 100)]
    assert screen_for_point(screens, 150, 10).name == "b"
    assert screen_for_point(screens, -10, 10).name == "a"
    assert screen_for_point([], 0, 0) is None


def test_map_screenshot_point_scales_and_offsets():
    screen = ScreenGeometry("DP-1", 100, 50, 1920, 1080)
    x, y = map_screenshot_point(
        screenshot_x=100,
        screenshot_y=200,
        screenshot_width=1280,
        screenshot_height=720,
        screen=screen,
    )
    assert x == pytest.approx(100 + 100 * 1.5)
    assert y == pytest.approx(50 + 200 * 1.5)


def test_map_screenshot_point_clamps():
    screen = ScreenGeometry("DP-1", 0, 0, 100, 100)
    x, y = map_screenshot_point(
        screenshot_x=-50,
        screenshot_y=999,
        screenshot_width=100,
        screenshot_height=100,
        screen=screen,
    )
    assert (x, y) == (0, 100)


def test_map_screenshot_point_rejects_empty_image():
    with pytest.raises(ValueError):
        map_screenshot_point(
            screenshot_x=0,
            screenshot_y=0,
            screenshot_width=0,
            screenshot_height=10,
            screen=ScreenGeometry("DP-1", 0, 0, 10, 10),
        )


def test_local_global_roundtrip():
    screen = ScreenGeometry("DP-1", 500, 200, 800, 600)
    assert local_to_global(screen, 20, 30) == (520, 230)
    assert global_to_local(screen, 520, 230) == (20, 30)


def test_choose_target_screen():
    screens = [ScreenGeometry("a", 0, 0, 10, 10), ScreenGeometry("b", 10, 0, 10, 10)]
    assert choose_target_screen(screens, 2, screens[0]).name == "b"
    assert choose_target_screen(screens, 9, screens[1]).name == "b"
    assert choose_target_screen(screens, None, screens[0]).name == "a"


def test_bezier_and_tangent():
    start, control, end = (0.0, 0.0), (5.0, -5.0), (10.0, 0.0)
    assert bezier_point(start, control, end, 0.0) == (0.0, 0.0)
    assert bezier_point(start, control, end, 1.0) == (10.0, 0.0)
    assert bezier_point(start, control, end, 0.5) == (5.0, -2.5)
    assert bezier_tangent_rotation(start, control, end, 0.5) == pytest.approx(90.0)


def test_flight_plan_bounds():
    control, duration, arc = flight_plan((0, 0), (100, 0))
    assert 0.6 <= duration <= 1.4
    assert arc <= 80.0
    assert control[1] < 0

    _control, duration, _arc = flight_plan((0, 0), (5000, 0))
    assert duration == pytest.approx(1.4)


def test_smoothstep():
    assert smoothstep(0.0) == 0.0
    assert smoothstep(1.0) == 1.0
    assert smoothstep(0.5) == pytest.approx(0.5)
    assert smoothstep(-1.0) == 0.0
    assert smoothstep(2.0) == 1.0
