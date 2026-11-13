"""Design tokens ported from the macOS DesignSystem.swift.

The original uses Tailwind's blue scale on near-black surfaces. The same values
are reused here so the Linux panel reads as the same product, not a lookalike.
"""

from __future__ import annotations

from PyQt6.QtGui import QColor, QFont, QFontDatabase

COLORS = {
    "background": "#101211",
    "surface1": "#171918",
    "surface2": "#202221",
    "surface3": "#272A29",
    "surface4": "#2E3130",
    "border_subtle": "#373B39",
    "border_strong": "#444947",
    "text_primary": "#ECEEED",
    "text_secondary": "#ADB5B2",
    "text_tertiary": "#6B736F",
    "accent": "#2563EB",
    "accent_hover": "#1D4ED8",
    "accent_text": "#60A5FA",
    "success": "#34D399",
    "warning": "#FFB224",
    "destructive": "#E5484D",
    "overlay_cursor_blue": "#3380FF",
}

RADIUS = {"small": 5, "medium": 8, "large": 10, "panel": 12}


def color(name: str, alpha: float | None = None) -> QColor:
    """Return a QColor by token name, optionally with 0..1 alpha."""
    qcolor = QColor(COLORS[name])
    if alpha is not None:
        qcolor.setAlphaF(max(0.0, min(1.0, alpha)))
    return qcolor


def hex_color(value: str, alpha: float | None = None) -> QColor:
    qcolor = QColor(value)
    if alpha is not None:
        qcolor.setAlphaF(max(0.0, min(1.0, alpha)))
    return qcolor


def font(size: int, weight: QFont.Weight = QFont.Weight.Normal, family: str | None = None) -> QFont:
    """Pick a clean UI font that exists on a stock Ubuntu install."""
    if family is None:
        available = set(QFontDatabase.families())
        for candidate in ("Inter", "SF Pro Text", "Segoe UI", "Ubuntu", "Cantarell", "DejaVu Sans"):
            if candidate in available:
                family = candidate
                break
        family = family or "DejaVu Sans"
    result = QFont(family, size)
    result.setWeight(weight)
    result.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return result


def qss() -> str:
    """Application wide stylesheet for the dark panel and dialogs."""
    return f"""
    QWidget {{
        color: {COLORS['text_primary']};
        font-family: "Inter", "SF Pro Text", "Ubuntu", "Cantarell", "DejaVu Sans";
        font-size: 12px;
    }}
    QWidget#Panel {{
        background: {COLORS['background']};
        border: 1px solid {COLORS['border_subtle']};
        border-radius: {RADIUS['panel']}px;
    }}
    QLabel#Title {{ font-size: 14px; font-weight: 600; }}
    QLabel#ChatTitle {{ font-size: 13px; font-weight: 600; }}
    QFrame#TitleBar {{ background: transparent; }}
    QWidget#ControlsColumn {{ background: transparent; }}
    QWidget#ChatColumn {{
        background: {COLORS['surface1']};
        border-bottom-right-radius: {RADIUS['panel']}px;
    }}
    QScrollArea#ChatView, QWidget#ChatBody {{ background: transparent; border: none; }}
    QLabel#SectionLabel {{
        color: {COLORS['text_tertiary']};
        font-size: 10px;
        font-weight: 600;
    }}
    QLabel#Secondary {{ color: {COLORS['text_secondary']}; }}
    QLabel#Tertiary {{ color: {COLORS['text_tertiary']}; }}
    QLabel#Warning {{ color: {COLORS['warning']}; }}
    QLabel#Danger {{ color: {COLORS['destructive']}; }}
    QPushButton {{
        background: transparent;
        border: none;
        padding: 6px 10px;
        color: {COLORS['text_secondary']};
    }}
    QPushButton:hover {{ color: {COLORS['text_primary']}; }}
    QPushButton#Primary {{
        background: {COLORS['accent']};
        color: white;
        border-radius: {RADIUS['large']}px;
        padding: 9px 12px;
        font-weight: 600;
    }}
    QPushButton#Primary:hover {{ background: {COLORS['accent_hover']}; }}
    QPushButton#Primary:disabled {{
        background: rgba(37, 99, 235, 90);
        color: rgba(255, 255, 255, 140);
    }}
    QPushButton#Capsule {{
        background: {COLORS['accent']};
        color: white;
        border-radius: 11px;
        padding: 4px 10px;
        font-size: 11px;
        font-weight: 600;
    }}
    QPushButton#OutlineCapsule {{
        border: 1px solid {COLORS['border_subtle']};
        border-radius: 11px;
        padding: 4px 10px;
        font-size: 11px;
        font-weight: 600;
        color: {COLORS['text_secondary']};
    }}
    QPushButton#Card {{
        background: rgba(255, 255, 255, 15);
        border: 1px solid {COLORS['border_subtle']};
        border-radius: {RADIUS['medium']}px;
        padding: 9px 12px;
        text-align: left;
        color: {COLORS['text_secondary']};
    }}
    QPushButton#Card:hover {{ background: rgba(255, 255, 255, 25); }}
    QLineEdit, QComboBox, QSpinBox {{
        background: rgba(255, 255, 255, 18);
        border: 1px solid {COLORS['border_subtle']};
        border-radius: {RADIUS['medium']}px;
        padding: 7px 10px;
        selection-background-color: {COLORS['accent']};
    }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
        border-color: {COLORS['border_strong']};
    }}
    QComboBox QAbstractItemView {{
        background: {COLORS['surface2']};
        border: 1px solid {COLORS['border_subtle']};
        selection-background-color: {COLORS['surface4']};
        color: {COLORS['text_primary']};
        outline: none;
    }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QCheckBox {{ spacing: 8px; }}
    QCheckBox::indicator {{
        width: 16px; height: 16px; border-radius: 4px;
        border: 1px solid {COLORS['border_strong']};
        background: rgba(255, 255, 255, 10);
    }}
    QCheckBox::indicator:checked {{
        background: {COLORS['accent']};
        border-color: {COLORS['accent']};
    }}
    QScrollArea {{ background: transparent; border: none; }}
    QScrollBar:vertical {{
        background: transparent; width: 8px; margin: 2px;
    }}
    QScrollBar::handle:vertical {{
        background: {COLORS['surface4']}; border-radius: 4px; min-height: 24px;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QToolTip {{
        background: {COLORS['surface2']};
        color: {COLORS['text_primary']};
        border: 1px solid {COLORS['border_subtle']};
        padding: 4px 6px;
    }}
    QDialog {{ background: {COLORS['background']}; }}
    QGroupBox {{
        border: 1px solid {COLORS['border_subtle']};
        border-radius: {RADIUS['medium']}px;
        margin-top: 14px;
        padding: 12px 10px 8px 10px;
        font-weight: 600;
        color: {COLORS['text_secondary']};
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
    }}
    QMenu {{
        background: {COLORS['surface2']};
        border: 1px solid {COLORS['border_subtle']};
        padding: 4px;
    }}
    QMenu::item {{ padding: 6px 18px; border-radius: 5px; }}
    QMenu::item:selected {{ background: {COLORS['surface4']}; }}
    """
