"""Coordinate math for multi-monitor screenshots and the cursor overlay.

Two coordinate systems matter on Linux:

* **Logical / Qt coordinates** - what ``QScreen.geometry()`` and the overlay
  windows use. Fractional scaling may make these smaller than the framebuffer.
* **Screenshot pixel coordinates** - what the model sees. They depend on the
  capture backend, the device pixel ratio and any downscaling we apply.

The model is told the exact pixel size of every image, then returns a pixel
coordinate. :func:`map_screenshot_point` converts that back to logical desktop
coordinates for the target screen. This mirrors CompanionManager.swift's
screenshot-pixels -> display-points conversion, minus the macOS bottom-left
Y flip (Qt uses a top-left origin everywhere).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScreenGeometry:
    """A screen's placement in Qt's virtual desktop, in logical pixels."""

    name: str
    x: int
    y: int
    width: int
    height: int
    device_pixel_ratio: float = 1.0

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def contains(self, global_x: float, global_y: float) -> bool:
        return self.x <= global_x < self.right and self.y <= global_y < self.bottom


def screen_for_point(screens: list[ScreenGeometry], global_x: float, global_y: float) -> ScreenGeometry | None:
    """Return the screen containing a global point, or the first screen."""
    for screen in screens:
        if screen.contains(global_x, global_y):
            return screen
    return screens[0] if screens else None


def map_screenshot_point(
    *,
    screenshot_x: float,
    screenshot_y: float,
    screenshot_width: int,
    screenshot_height: int,
    screen: ScreenGeometry,
) -> tuple[float, float]:
    """Scale a screenshot pixel coordinate to a global logical point.

    Values are clamped to the image before scaling, exactly like the Swift
    implementation, so a hallucinated coordinate cannot fly the cursor off
    screen.
    """
    if screenshot_width <= 0 or screenshot_height <= 0:
        raise ValueError("screenshot dimensions must be positive")

    clamped_x = max(0.0, min(float(screenshot_x), float(screenshot_width)))
    clamped_y = max(0.0, min(float(screenshot_y), float(screenshot_height)))

    local_x = clamped_x * (screen.width / screenshot_width)
    local_y = clamped_y * (screen.height / screenshot_height)
    return screen.x + local_x, screen.y + local_y


def local_to_global(screen: ScreenGeometry, local_x: float, local_y: float) -> tuple[float, float]:
    return screen.x + local_x, screen.y + local_y


def global_to_local(screen: ScreenGeometry, global_x: float, global_y: float) -> tuple[float, float]:
    return global_x - screen.x, global_y - screen.y


def choose_target_screen(
    screens: list[ScreenGeometry], screen_number: int | None, cursor_screen: ScreenGeometry | None
) -> ScreenGeometry | None:
    """Resolve a 1-based model screen number to a screen, with cursor fallback."""
    if screen_number is not None and 1 <= screen_number <= len(screens):
        return screens[screen_number - 1]
    return cursor_screen or (screens[0] if screens else None)


def bezier_point(
    start: tuple[float, float],
    control: tuple[float, float],
    end: tuple[float, float],
    t: float,
) -> tuple[float, float]:
    """Quadratic Bezier position, matching the Swift flight animation."""
    one_minus_t = 1.0 - t
    x = one_minus_t * one_minus_t * start[0] + 2 * one_minus_t * t * control[0] + t * t * end[0]
    y = one_minus_t * one_minus_t * start[1] + 2 * one_minus_t * t * control[1] + t * t * end[1]
    return x, y


def bezier_tangent_rotation(
    start: tuple[float, float],
    control: tuple[float, float],
    end: tuple[float, float],
    t: float,
) -> float:
    """Triangle rotation in degrees so its tip faces the direction of travel."""
    import math

    one_minus_t = 1.0 - t
    tangent_x = 2 * one_minus_t * (control[0] - start[0]) + 2 * t * (end[0] - control[0])
    tangent_y = 2 * one_minus_t * (control[1] - start[1]) + 2 * t * (end[1] - control[1])
    return math.degrees(math.atan2(tangent_y, tangent_x)) + 90.0


def flight_plan(
    start: tuple[float, float], end: tuple[float, float]
) -> tuple[tuple[float, float], float, float]:
    """Return (control point, duration seconds, arc height) for a flight."""
    import math

    distance = math.hypot(end[0] - start[0], end[1] - start[1])
    duration = min(max(distance / 800.0, 0.6), 1.4)
    arc_height = min(distance * 0.2, 80.0)
    midpoint = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
    control = (midpoint[0], midpoint[1] - arc_height)
    return control, duration, arc_height


def smoothstep(t: float) -> float:
    """3t^2 - 2t^3, the ease used by the original bezier flight."""
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)
