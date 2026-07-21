# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import os
from pathlib import Path
from string import Template

from PySide6.QtGui import QFontDatabase


def install_ui_fonts() -> None:
    """Load Windows UI font faces explicitly, including in Qt offscreen mode."""

    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for filename in ("segoeui.ttf", "seguisb.ttf", "segoeuib.ttf"):
        path = fonts_dir / filename
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))


# Design tokens. One neutral-slate ramp with clear elevation steps and a single
# warm amber accent that matches the thermal imagery.
TOKENS = {
    # Surfaces, darkest to lightest
    "BASE": "#0D1015",  # window background
    "STAGE": "#06080B",  # image well behind the thermal frame
    "PANEL": "#131820",  # title bar, inspector, transport
    "CARD": "#182029",  # cards inside the inspector
    "CONTROL": "#1E2632",  # resting inputs and buttons
    "CONTROL_HOVER": "#27313F",
    "CONTROL_PRESSED": "#1A222D",
    # Borders
    "BORDER": "#242D3A",  # hairlines between surfaces
    "BORDER_CTRL": "#333E4D",  # control outlines
    "BORDER_HOVER": "#49586C",
    # Text
    "TEXT": "#E9EDF2",
    "TEXT_SECONDARY": "#C7CED8",
    "MUTED": "#97A1AF",
    "FAINT": "#5F6B7A",
    # Accent
    "ACCENT": "#F5A524",
    "ACCENT_HOVER": "#FFB93E",
    "ACCENT_PRESSED": "#D98E0F",
    "ON_ACCENT": "#241A05",  # icons/text on a filled accent surface
    "ACCENT_TINT": "rgba(245, 165, 36, 34)",  # selected/accented backgrounds
    "DANGER": "#E5484D",
    # Radii
    "R_PANEL": "8px",
    "R_CTRL": "6px",
}

_APP_STYLESHEET = Template(r"""
* {
    font-family: "Segoe UI", "Arial";
    font-size: 13px;
    color: $TEXT;
}

/* ---------------- Window frame and major surfaces ---------------- */

QFrame#Root {
    background: $BASE;
    border: 1px solid $BORDER;
}

QWidget#TitleBar {
    background: $PANEL;
    border-bottom: 1px solid $BORDER;
}

QWidget#Stage {
    background: $STAGE;
}

QWidget#Inspector {
    background: $PANEL;
    border-left: 1px solid $BORDER;
}

QWidget#TransportBar {
    background: $PANEL;
    border-top: 1px solid $BORDER;
}

/* ---------------- Title bar ---------------- */

QLabel#AppTitle {
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 1.6px;
    color: $TEXT;
}

QLabel#Filename {
    color: $MUTED;
    font-size: 13px;
}

QToolButton#CaptionButton {
    background: transparent;
    border: none;
    border-radius: 0;
    min-width: 46px;
    max-width: 46px;
    min-height: 55px;
    max-height: 55px;
}

QToolButton#CaptionButton:hover {
    background: $CONTROL_HOVER;
}

QToolButton#CaptionButton:pressed {
    background: $CONTROL_PRESSED;
}

QToolButton#CloseButton:hover {
    background: $DANGER;
}

QToolButton#CloseButton:pressed {
    background: #B93A3E;
}

/* ---------------- Buttons ---------------- */

/* Ghost icon button (title bar actions) */
QToolButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: $R_CTRL;
    min-width: 36px;
    min-height: 36px;
    padding: 2px;
}

QToolButton:hover {
    background: $CONTROL_HOVER;
}

QToolButton:pressed {
    background: $CONTROL_PRESSED;
}

QToolButton:focus {
    border-color: $ACCENT;
}

QToolButton:disabled {
    background: transparent;
    border-color: transparent;
}

/* Filled button (title bar open action) */
QToolButton#FilledButton {
    background: $CONTROL;
    border: 1px solid $BORDER_CTRL;
}

QToolButton#FilledButton:hover {
    background: $CONTROL_HOVER;
    border-color: $BORDER_HOVER;
}

QToolButton#FilledButton:pressed {
    background: $CONTROL_PRESSED;
}

/* Accent outline button (export) */
QToolButton#ExportButton {
    color: $ACCENT;
    border: 1px solid $BORDER_CTRL;
    min-width: 108px;
    min-height: 36px;
    padding-left: 12px;
    padding-right: 12px;
    font-size: 13px;
    font-weight: 600;
}

QToolButton#ExportButton:hover {
    background: $ACCENT_TINT;
    border-color: $ACCENT;
}

QToolButton#ExportButton:pressed {
    background: $CONTROL_PRESSED;
}

QToolButton#ExportButton:disabled {
    color: $FAINT;
    background: transparent;
    border-color: $BORDER;
}

/* Generic push buttons (dialogs, message boxes) */
QPushButton {
    background: $CONTROL;
    border: 1px solid $BORDER_CTRL;
    border-radius: $R_CTRL;
    min-width: 88px;
    min-height: 30px;
    padding: 4px 14px;
}

QPushButton:hover {
    background: $CONTROL_HOVER;
    border-color: $BORDER_HOVER;
}

QPushButton:pressed {
    background: $CONTROL_PRESSED;
}

QPushButton:focus {
    border-color: $ACCENT;
}

QPushButton:disabled {
    color: $FAINT;
    background: $CARD;
    border-color: $BORDER;
}

/* Segmented control (range mode) */
QPushButton#SegmentButton {
    background: $CONTROL;
    border: 1px solid $BORDER_CTRL;
    border-radius: 0;
    min-width: 0;
    min-height: 36px;
    padding: 0 14px;
    color: $MUTED;
    font-weight: 600;
}

QPushButton#SegmentButton[segment="left"] {
    border-top-left-radius: $R_CTRL;
    border-bottom-left-radius: $R_CTRL;
}

QPushButton#SegmentButton[segment="center"] {
    border-left: none;
}

QPushButton#SegmentButton[segment="right"] {
    border-left: none;
    border-top-right-radius: $R_CTRL;
    border-bottom-right-radius: $R_CTRL;
}

QPushButton#SegmentButton:hover {
    background: $CONTROL_HOVER;
    color: $TEXT;
}

QPushButton#SegmentButton:checked {
    background: $ACCENT_TINT;
    border-color: $ACCENT;
    color: $ACCENT;
}

QPushButton#SegmentButton:checked:hover {
    color: $ACCENT_HOVER;
}

QPushButton#SegmentButton:disabled {
    color: $FAINT;
    background: $CARD;
    border-color: $BORDER;
}

QPushButton#SegmentButton:disabled:checked {
    color: $FAINT;
    background: $CONTROL_PRESSED;
    border-color: $BORDER_CTRL;
}

/* Transport buttons */
QToolButton#TransportButton {
    background: $CONTROL;
    border: 1px solid $BORDER_CTRL;
    border-radius: $R_PANEL;
    min-width: 40px;
    min-height: 40px;
}

QToolButton#TransportButton:hover {
    background: $CONTROL_HOVER;
    border-color: $BORDER_HOVER;
}

QToolButton#TransportButton:pressed {
    background: $CONTROL_PRESSED;
}

QToolButton#TransportButton:disabled {
    background: $CARD;
    border-color: $BORDER;
}

/* Compact close button in the bottom tab bar corner (must not overlap tabs) */
QToolButton#TabCloseButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: $R_PANEL;
    min-width: 22px;
    min-height: 22px;
    max-width: 22px;
    max-height: 22px;
}

QToolButton#TabCloseButton:hover {
    background: $CONTROL_HOVER;
    border-color: $BORDER_HOVER;
}

QToolButton#TabCloseButton:pressed {
    background: $CONTROL_PRESSED;
}

/* Primary play/pause action: the one filled accent surface in the app */
QToolButton#PlayButton {
    background: $ACCENT;
    border: 1px solid $ACCENT;
    border-radius: 10px;
    min-width: 48px;
    min-height: 48px;
}

QToolButton#PlayButton:hover {
    background: $ACCENT_HOVER;
    border-color: $ACCENT_HOVER;
}

QToolButton#PlayButton:pressed {
    background: $ACCENT_PRESSED;
    border-color: $ACCENT_PRESSED;
}

QToolButton#PlayButton:disabled {
    background: $CONTROL;
    border-color: $BORDER;
}

/* ---------------- Inspector ---------------- */

QLabel#SectionTitle {
    font-size: 15px;
    font-weight: 600;
    color: $TEXT;
}

QLabel#FieldLabel {
    color: $MUTED;
    font-size: 12px;
    font-weight: 600;
}

QComboBox, QDoubleSpinBox, QSpinBox, QLineEdit {
    background: $CONTROL;
    border: 1px solid $BORDER_CTRL;
    border-radius: $R_CTRL;
    min-height: 36px;
    padding: 0 10px;
    selection-background-color: $CONTROL_HOVER;
}

QComboBox {
    padding: 0 36px 0 12px;
}

QComboBox:hover, QDoubleSpinBox:hover, QSpinBox:hover, QLineEdit:hover {
    background: $CONTROL_HOVER;
    border-color: $BORDER_HOVER;
}

QComboBox:focus, QComboBox:on, QDoubleSpinBox:focus, QSpinBox:focus, QLineEdit:focus {
    border-color: $ACCENT;
}

QComboBox:disabled, QDoubleSpinBox:disabled, QSpinBox:disabled, QLineEdit:disabled {
    color: $FAINT;
    background: $CARD;
    border-color: $BORDER;
}

QComboBox::drop-down {
    border: none;
    width: 34px;
}

QComboBox::down-arrow {
    width: 0;
    height: 0;
}

QComboBox QAbstractItemView {
    background: $CARD;
    border: 1px solid $BORDER_CTRL;
    border-radius: $R_PANEL;
    padding: 4px;
    selection-background-color: $ACCENT_TINT;
    selection-color: $ACCENT;
    outline: none;
}

QComboBox QAbstractItemView::item {
    min-height: 30px;
    padding: 0 10px;
    border-radius: 4px;
}

QFrame#RangePanel, QFrame#InfoPanel {
    background: $CARD;
    border: 1px solid $BORDER;
    border-radius: $R_PANEL;
}

QFrame#RangePanel:disabled {
    background: $PANEL;
}

QFrame#RangePanel:disabled QLabel {
    color: $FAINT;
}

QLabel#InfoValue {
    color: $TEXT_SECONDARY;
    font-size: 13px;
}

/* ---------------- Transport ---------------- */

QSlider::groove:horizontal {
    height: 6px;
    border-radius: 3px;
    background: $BORDER_CTRL;
}

QSlider::sub-page:horizontal {
    background: $ACCENT;
    border-radius: 3px;
}

QSlider::handle:horizontal {
    width: 16px;
    height: 16px;
    margin: -5px 0;
    border-radius: 8px;
    background: $ACCENT;
}

QSlider::handle:horizontal:hover {
    background: $ACCENT_HOVER;
}

QSlider::handle:horizontal:pressed {
    background: $ACCENT_PRESSED;
}

QSlider::groove:horizontal:disabled {
    background: $BORDER;
}

QSlider::sub-page:horizontal:disabled {
    background: $BORDER;
}

QSlider::handle:horizontal:disabled {
    background: $FAINT;
}

QFrame#TransportBadge {
    background: $CARD;
    border: 1px solid $BORDER;
    border-radius: 20px;
}

QLabel#TimeLabel {
    font-size: 13px;
    color: $MUTED;
}

QLabel#TimeCurrent {
    color: $TEXT;
}

QLabel#FrameLabel {
    font-size: 13px;
    color: $TEXT_SECONDARY;
}

/* ---------------- Overlays ---------------- */

QMenu {
    background: $CARD;
    border: 1px solid $BORDER_CTRL;
    border-radius: $R_PANEL;
    padding: 5px;
}

QMenu::item {
    min-height: 30px;
    padding: 0 24px 0 12px;
    border-radius: 4px;
}

QMenu::item:selected {
    background: $CONTROL_HOVER;
}

QMenu::item:disabled {
    color: $FAINT;
}

QMenu::separator {
    height: 1px;
    background: $BORDER;
    margin: 5px 8px;
}

QToolTip {
    color: $TEXT;
    background: $CONTROL;
    border: 1px solid $BORDER_CTRL;
    border-radius: 4px;
    padding: 6px 8px;
}

QSizeGrip {
    background: transparent;
    width: 14px;
    height: 14px;
}

/* ---------------- Analysis toolbar (ROI tools) ---------------- */

QToolButton#AnalysisButton {
    background: $CONTROL;
    border: 1px solid $BORDER_CTRL;
    border-radius: $R_PANEL;
    min-width: 32px;
    min-height: 32px;
    max-width: 32px;
    max-height: 32px;
}

QToolButton#AnalysisButton:hover {
    background: $CONTROL_HOVER;
    border-color: $BORDER_HOVER;
}

QToolButton#AnalysisButton:pressed {
    background: $CONTROL_PRESSED;
}

QToolButton#AnalysisButton:checked {
    background: $ACCENT_TINT;
    border-color: $ACCENT;
}

QToolButton#AnalysisButton:disabled {
    background: $CARD;
    border-color: $BORDER;
}

/* ---------------- Statistics table ---------------- */

QTableWidget {
    background: $CARD;
    gridline-color: $BORDER;
    border: 1px solid $BORDER;
    border-radius: $R_PANEL;
    selection-background-color: $ACCENT_TINT;
    selection-color: $TEXT;
}

QTableWidget::item {
    padding: 2px 8px;
    border: none;
}

QHeaderView {
    background: $CARD;
    border: none;
}

QHeaderView::section {
    background: $CONTROL;
    color: $MUTED;
    font-size: 12px;
    font-weight: 600;
    border: none;
    border-right: 1px solid $BORDER;
    border-bottom: 1px solid $BORDER;
    padding: 3px 8px;
}

QTableCornerButton::section {
    background: $CONTROL;
    border: none;
    border-right: 1px solid $BORDER;
    border-bottom: 1px solid $BORDER;
}

/* ---------------- Inspector scroll area ---------------- */

QScrollArea#InspectorScroll {
    background: transparent;
    border: none;
}

QWidget#InspectorContent {
    background: transparent;
}

QScrollArea#PickerScroll {
    background: transparent;
    border: none;
}

QWidget#PickerContent {
    background: transparent;
}

QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 4px 2px 4px 2px;
}

QScrollBar::handle:vertical {
    background: $CONTROL_HOVER;
    border-radius: 4px;
    min-height: 24px;
}

QScrollBar::handle:vertical:hover {
    background: $BORDER_HOVER;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
    border: none;
    height: 0;
}

/* Compact spin rows (measurement/object parameters) */

QDoubleSpinBox[compact="true"], QSpinBox[compact="true"] {
    min-height: 26px;
    max-height: 26px;
}

/* ---------------- Checkboxes ---------------- */

QCheckBox {
    color: $TEXT_SECONDARY;
    spacing: 8px;
}

QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid $BORDER_CTRL;
    border-radius: 4px;
    background: $CONTROL;
}

QCheckBox::indicator:hover {
    border-color: $BORDER_HOVER;
}

QCheckBox::indicator:checked {
    background: $ACCENT;
    border-color: $ACCENT;
}

QCheckBox:disabled {
    color: $FAINT;
}

/* ---------------- Bottom analysis tabs ---------------- */

QTabWidget#BottomTabs::pane {
    background: $CARD;
    border: 1px solid $BORDER;
    border-radius: $R_PANEL;
}

QTabWidget#BottomTabs QTabBar::tab {
    background: transparent;
    color: $MUTED;
    font-size: 12px;
    font-weight: 600;
    padding: 6px 16px;
    margin-right: 2px;
    border: 1px solid transparent;
    border-bottom: none;
    border-top-left-radius: $R_CTRL;
    border-top-right-radius: $R_CTRL;
}

QTabWidget#BottomTabs QTabBar::tab:selected {
    background: $CARD;
    color: $TEXT;
    border-color: $BORDER;
}

QTabWidget#BottomTabs QTabBar::tab:hover:!selected {
    color: $TEXT_SECONDARY;
}

/* ---------------- Dialogs (extract, progress, messages) ---------------- */

QDialog {
    background: $PANEL;
    border: 1px solid $BORDER;
}

QDialog QLabel {
    color: $TEXT_SECONDARY;
}

QProgressBar {
    background: $CONTROL;
    border: 1px solid $BORDER_CTRL;
    border-radius: $R_CTRL;
    min-height: 18px;
    text-align: center;
    color: $TEXT;
}

QProgressBar::chunk {
    background: $ACCENT;
    border-radius: 4px;
}
""")

APP_STYLESHEET = _APP_STYLESHEET.substitute(TOKENS)
