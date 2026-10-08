# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import os
from pathlib import Path
from string import Template

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase


def apply_app_theme(app) -> None:
    """Fusion, the UI fonts, the stylesheet, and the dark colour scheme.

    The app is always dark. Declaring that to Qt gives windows that keep a
    native frame (the colour picker; any stray top-level) a dark Windows title
    bar even when Windows itself is in light mode, and gives anything the
    stylesheet does not cover a dark palette. Themed surfaces are unaffected.
    """
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    app.setStyle("Fusion")
    install_ui_fonts()
    app.setStyleSheet(APP_STYLESHEET)


def install_ui_fonts() -> None:
    """Load Windows UI font faces explicitly, including in Qt offscreen mode."""

    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for filename in ("segoeui.ttf", "seguisb.ttf", "segoeuib.ttf", "consola.ttf"):
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

# Small SVG glyphs referenced by the stylesheet (checkbox tick, combo chevron).
# Resolved relative to this module so the QSS works both from source and from
# the PyInstaller bundle (data files land next to the packaged module).
_ICONS_DIR = Path(__file__).resolve().parent / "icons"
TOKENS.update(
    {
        "CHECK_ICON": (_ICONS_DIR / "check.svg").as_posix(),
        "CHEVRON_ICON": (_ICONS_DIR / "chevron-down.svg").as_posix(),
        "CHEVRON_DISABLED_ICON": (_ICONS_DIR / "chevron-down-disabled.svg").as_posix(),
        "CHEVRON_UP_ICON": (_ICONS_DIR / "chevron-up.svg").as_posix(),
        "CHEVRON_UP_DISABLED_ICON": (_ICONS_DIR / "chevron-up-disabled.svg").as_posix(),
        "CHECK_DISABLED_ICON": (_ICONS_DIR / "check-disabled.svg").as_posix(),
    }
)

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
    font-weight: 600;
    letter-spacing: 1.2px;
    color: $TEXT;
}

QLabel#Filename {
    color: $MUTED;
    font-size: 13px;
}

/* Window caption buttons: one 46 x 55 click target each, flush with each
   other and with the title bar's bottom hairline, as on native Windows. */
QToolButton#CaptionButton, QToolButton#CloseButton {
    background: transparent;
    border: none;
    border-radius: 0;
    padding: 0;
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

/* Drop-down affordance for MenuButtonPopup buttons (title bar open/export).
   The default style primitive renders as an unstyled slab; replace it with a
   small chevron glyph and give it a slim, borderless click zone. */
QToolButton::menu-button {
    background: transparent;
    border: none;
    width: 18px;
}

QToolButton::menu-arrow {
    image: url("$CHEVRON_ICON");
    width: 10px;
    height: 10px;
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

QToolButton#FilledButton:focus {
    border-color: $ACCENT;
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

QToolButton#ExportButton:focus {
    border-color: $ACCENT;
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

/* Filled accent primary action (e.g. dialog OK). The 1px border is always
   present (transparent) so the focus ring does not shift the layout. */
QPushButton[accent="true"] {
    background: $ACCENT;
    color: $ON_ACCENT;
    border: 1px solid transparent;
    font-weight: 600;
}

QPushButton[accent="true"]:hover {
    background: $ACCENT_HOVER;
}

QPushButton[accent="true"]:pressed {
    background: $ACCENT_PRESSED;
}

/* Explicit variant states: they must follow the variant base rules above,
   otherwise the base would override the generic :disabled/:focus styles. */
QPushButton[accent="true"]:focus {
    border-color: $ON_ACCENT;
}

QPushButton[accent="true"]:disabled {
    background: $CONTROL;
    color: $FAINT;
    border-color: transparent;
}

/* Quiet text button (dialog secondary actions) */
QPushButton[variant="ghost"] {
    background: transparent;
    border-color: transparent;
    color: $MUTED;
    min-width: 56px;
    min-height: 28px;
}

QPushButton[variant="ghost"]:hover {
    background: $CONTROL_HOVER;
    color: $TEXT;
}

QPushButton[variant="ghost"]:pressed {
    background: $CONTROL_PRESSED;
}

QPushButton[variant="ghost"]:focus {
    border-color: $ACCENT;
}

QPushButton[variant="ghost"]:disabled {
    background: transparent;
    color: $FAINT;
    border-color: transparent;
}

/* Segmented control (range mode): a fill band holding borderless segments */
QFrame#Segmented {
    background: $CARD;
    border-radius: $R_PANEL;
}

QPushButton#SegmentButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: $R_CTRL;
    min-width: 0;
    min-height: 32px;
    padding: 0 14px;
    color: $MUTED;
    font-weight: 600;
}

QPushButton#SegmentButton:hover {
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
}

QPushButton#SegmentButton:disabled:checked {
    color: $FAINT;
    background: $CONTROL_PRESSED;
    border-color: transparent;
}

/* Transport buttons: ghost at rest so the filled play button stays dominant */
QToolButton#TransportButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: $R_PANEL;
    min-width: 40px;
    min-height: 40px;
}

QToolButton#TransportButton:hover {
    background: $CONTROL_HOVER;
}

QToolButton#TransportButton:pressed {
    background: $CONTROL_PRESSED;
}

QToolButton#TransportButton:checked {
    background: $ACCENT_TINT;
    border-color: $ACCENT;
}

QToolButton#TransportButton:disabled {
    background: transparent;
    border-color: transparent;
}

QToolButton#TransportButton:focus {
    border-color: $ACCENT;
}

/* Secondary transport actions (loop, fullscreen) are one step smaller */
QToolButton#TransportButton[small="true"] {
    min-width: 36px;
    min-height: 36px;
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

/* The resting border is amber-on-amber, so focus needs a contrasting ring */
QToolButton#PlayButton:focus {
    border-color: $ON_ACCENT;
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

/* Inputs are fill-only at rest; a border appears on hover and focus. The 1px
   border is always present (transparent) so text does not shift on hover. */
QComboBox, QDoubleSpinBox, QSpinBox, QLineEdit {
    background: $CONTROL;
    border: 1px solid transparent;
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
    border-color: $BORDER_CTRL;
}

QComboBox:focus, QComboBox:on, QDoubleSpinBox:focus, QSpinBox:focus, QLineEdit:focus {
    border-color: $ACCENT;
}

QComboBox:disabled, QDoubleSpinBox:disabled, QSpinBox:disabled, QLineEdit:disabled {
    color: $FAINT;
    background: $CARD;
    border-color: transparent;
}

QComboBox::drop-down {
    border: none;
    width: 34px;
}

QComboBox::down-arrow {
    image: url("$CHEVRON_ICON");
    width: 10px;
    height: 10px;
}

QComboBox::down-arrow:disabled {
    image: url("$CHEVRON_DISABLED_ICON");
}

/* Spin box steppers (dialog spins that keep their buttons). Unstyled, the
   Fusion primitives render as dark slabs on the themed fill. */
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {
    subcontrol-origin: border;
    width: 20px;
    background: transparent;
    border: none;
    border-radius: 3px;
}

QAbstractSpinBox::up-button {
    subcontrol-position: top right;
    margin: 4px 4px 0 0;
}

QAbstractSpinBox::down-button {
    subcontrol-position: bottom right;
    margin: 0 4px 4px 0;
}

QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {
    background: $BORDER_CTRL;
}

QAbstractSpinBox::up-button:pressed, QAbstractSpinBox::down-button:pressed {
    background: $CONTROL_PRESSED;
}

QAbstractSpinBox::up-arrow {
    image: url("$CHEVRON_UP_ICON");
    width: 9px;
    height: 9px;
}

QAbstractSpinBox::down-arrow {
    image: url("$CHEVRON_ICON");
    width: 9px;
    height: 9px;
}

QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::up-arrow:off {
    image: url("$CHEVRON_UP_DISABLED_ICON");
}

QAbstractSpinBox::down-arrow:disabled, QAbstractSpinBox::down-arrow:off {
    image: url("$CHEVRON_DISABLED_ICON");
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

/* Cards group controls through fill steps only, not nested borders */
QFrame#RangePanel, QFrame#InfoPanel {
    background: $CARD;
    border: none;
    border-radius: $R_PANEL;
}

QFrame#RangePanel:disabled {
    background: $PANEL;
}

QFrame#RangePanel:disabled QLabel {
    color: $FAINT;
}

/* 1px section separator inside the inspector */
QFrame#Hairline {
    background: $BORDER;
    max-height: 1px;
    border: none;
}

QLabel#InfoValue {
    color: $TEXT_SECONDARY;
}

/* Numeric readouts use a tabular font so columns and timecodes do not jitter */
QLabel#TimeCurrent, QLabel#TimeLabel, QLabel#FrameLabel, QLabel#InfoValue,
QDoubleSpinBox, QSpinBox {
    font-family: "Consolas";
}

QLabel#InfoValue {
    font-size: 12px;
}

/* Small tertiary caption (grid labels, input captions) */
QLabel#Caption {
    color: $FAINT;
    font-size: 11px;
}

/* Disclosure row (collapsible inspector sub-groups) */
QToolButton#DisclosureButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: $R_CTRL;
    min-height: 26px;
    color: $MUTED;
    font-size: 12px;
    font-weight: 600;
}

QToolButton#DisclosureButton:hover {
    background: $CONTROL_HOVER;
    color: $TEXT;
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
    border: none;
    border-radius: 10px;
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
    background: transparent;
    border: 1px solid transparent;
    border-radius: $R_PANEL;
    min-width: 32px;
    min-height: 32px;
    max-width: 32px;
    max-height: 32px;
}

QToolButton#AnalysisButton:hover {
    background: $CONTROL_HOVER;
}

QToolButton#AnalysisButton:pressed {
    background: $CONTROL_PRESSED;
}

QToolButton#AnalysisButton:checked {
    background: $ACCENT_TINT;
    border-color: $ACCENT;
}

QToolButton#AnalysisButton:disabled {
    background: transparent;
    border-color: transparent;
}

QToolButton#AnalysisButton:focus {
    border-color: $ACCENT;
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

/* ---------------- Lists (batch extract recordings) ---------------- */

/* QListWidget only: combo-box popups are list views with their own rules. */
QListWidget {
    background: $CARD;
    border: 1px solid $BORDER;
    border-radius: $R_PANEL;
    padding: 4px;
    outline: none;
}

QListWidget::item {
    min-height: 26px;
    padding: 0 8px;
    border-radius: 4px;
    color: $TEXT_SECONDARY;
}

QListWidget::item:hover {
    background: $CONTROL_HOVER;
}

QListWidget::item:selected {
    background: $ACCENT_TINT;
    color: $TEXT;
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

QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 2px 4px 2px 4px;
}

QScrollBar::handle:horizontal {
    background: $CONTROL_HOVER;
    border-radius: 4px;
    min-width: 24px;
}

QScrollBar::handle:horizontal:hover {
    background: $BORDER_HOVER;
}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: transparent;
    border: none;
    width: 0;
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
    image: url("$CHECK_ICON");
}

/* A disabled box keeps showing its state, but not as an active accent. */
QCheckBox::indicator:disabled {
    background: $CARD;
    border-color: $BORDER;
}

QCheckBox::indicator:checked:disabled {
    background: $CONTROL_HOVER;
    border-color: $BORDER_CTRL;
    image: url("$CHECK_DISABLED_ICON");
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
    border: none;
    border-bottom: 2px solid transparent;
}

QTabWidget#BottomTabs QTabBar::tab:selected {
    color: $TEXT;
    border-bottom-color: $ACCENT;
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

/* Frameless dialog title row (replaces the native light title bar) */
QWidget#DialogTitleBar {
    background: transparent;
    border-bottom: 1px solid $BORDER;
}

/* Message dialog text: a read-only text view that reads as a label */
QTextEdit#MessageText {
    background: transparent;
    border: none;
    color: $TEXT_SECONDARY;
    selection-background-color: $ACCENT_TINT;
    selection-color: $TEXT;
}

/* A read-only report inside a tab card (TC calibration summary) */
QTextEdit#ReportText {
    background: transparent;
    border: none;
    color: $TEXT_SECONDARY;
    padding: 4px;
    selection-background-color: $ACCENT_TINT;
    selection-color: $TEXT;
}

QLabel#DialogTitle {
    font-size: 13px;
    font-weight: 600;
    color: $TEXT;
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
