# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""UI polish regressions (2026-09-26): window chrome, timeline, cursor readout."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QFont, QFontMetrics, QKeyEvent, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QAbstractSlider,
    QAbstractSpinBox,
    QComboBox,
    QLineEdit,
    QScrollArea,
    QTabBar,
    QWidget,
)

from flir_player.geometry import roi_coordinates
from flir_player.main_window import MainWindow
from flir_player.models import RoiShape
from flir_player.style import APP_STYLESHEET, TOKENS
from flir_player.widgets import (
    CAPTION_HEIGHT,
    CaptionButton,
    EdgeResizeGrip,
    ProgressDialog,
    ThermalCanvas,
    TimelineSlider,
    TitleBar,
    TransportBar,
)


def _move(canvas, x, y, buttons=Qt.MouseButton.NoButton) -> None:
    pos = QPointF(x, y)
    canvas.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, pos, pos, Qt.MouseButton.NoButton,
                                      buttons, Qt.KeyboardModifier.NoModifier))


def _canvas(qapp, width=20, height=10, size=(200, 100)) -> ThermalCanvas:
    """A canvas showing a width x height frame whose value is 10 * x + y."""
    canvas = ThermalCanvas()
    canvas.setMinimumSize(1, 1)
    canvas.resize(*size)
    canvas.show()
    raw = np.add.outer(np.arange(height, dtype=float), 10.0 * np.arange(width))
    canvas.set_frame(np.zeros((height, width, 3), np.uint8), raw, "counts")
    canvas.grab()  # lays out the image rectangle
    return canvas


def test_caption_buttons_share_one_click_target_flush_with_each_other(qapp) -> None:
    bar = TitleBar()
    bar.resize(1200, bar.height())
    bar.show()
    qapp.processEvents()
    try:
        buttons = (bar.minimize_button, bar.maximize_button, bar.close_button)
        sizes = {(b.width(), b.height()) for b in buttons}
        assert sizes == {(46, CAPTION_HEIGHT)}  # close was 45 x 45, the others 50 x 59
        assert all(b.y() == 0 for b in buttons)
        assert CAPTION_HEIGHT == bar.height() - 1  # the bottom hairline stays visible
        for left, right in zip(buttons, buttons[1:]):
            assert left.geometry().right() + 1 == right.x()  # no dead gap between them
        assert bar.close_button.geometry().right() == bar.width() - 1
    finally:
        bar.close()


def test_timeline_is_tall_enough_for_its_handle_and_range_markers(qapp) -> None:
    slider = TimelineSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, 100)
    slider.resize(400, slider.sizeHint().height())
    slider.show()
    qapp.processEvents()
    try:
        groove = slider._groove_rect()
        centre = groove.top() + groove.height() / 2.0
        assert slider.height() >= 26
        assert centre - slider._HANDLE_HOVER_RADIUS - 0.5 >= 0  # was clipped by 2px at 15px
        assert centre + slider._HANDLE_HOVER_RADIUS + 0.5 <= slider.height()
        assert groove.top() - slider._MARKER_HEIGHT - 1 >= 0  # play-range triangles
        # the painted handle is round: its top row is lit, not cut flat
        slider.setValue(50)
        image = slider.grab().toImage()
        x = int(slider._value_to_x(50))
        colours = [image.pixelColor(x, y) for y in range(image.height())]
        amber = [y for y, c in enumerate(colours) if c.red() > 200 and c.blue() < 100]
        assert amber and amber[0] > 0 and amber[-1] < image.height() - 1
    finally:
        slider.close()


def test_timeline_hover_tooltip_text_and_marker_cursor(qapp) -> None:
    slider = TimelineSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, 100)
    slider.resize(400, slider.sizeHint().height())
    slider.setEnabled(True)
    slider.show()
    seen = []
    slider.hover_text = lambda value: seen.append(value) or f"Frame {value + 1}"
    slider.set_play_range(20, 80)
    _move(slider, slider._value_to_x(20), slider.height() / 2)
    assert slider.cursor().shape() == Qt.CursorShape.SizeHorCursor
    assert seen[-1] == 20
    _move(slider, slider._value_to_x(50), slider.height() / 2)
    assert slider.cursor().shape() != Qt.CursorShape.SizeHorCursor
    slider.close()


def test_transport_reserves_the_widest_frame_readout(qapp) -> None:
    transport = TransportBar()
    transport.show()
    transport.set_video(123456, 4321.0)
    widest = transport._text_width(transport.frame_label, "Frame 123456 / 123456")
    assert transport._badge.minimumWidth() >= widest
    transport.close()


def test_cursor_readout_is_live_in_every_tool(qapp) -> None:
    canvas = _canvas(qapp)
    try:
        for tool in ("select", "rect", "ellipse", "line", "cursor"):
            canvas.set_roi_tool(tool)
            _move(canvas, 35, 25)  # displayed pixel (3, 2)
            assert canvas._probe == (3, 2, 32.0), tool
            assert canvas.readout_lines()[0].startswith("x 3  y 2"), tool
    finally:
        canvas.close()


def test_drawing_readout_reports_the_pixels_the_roi_will_measure(qapp) -> None:
    canvas = _canvas(qapp)
    try:
        for kind, start, end in (("rect", (22, 13), (68, 47)), ("ellipse", (31, 12), (95, 71)),
                                 ("line", (15, 85), (175, 35))):
            canvas.set_roi_tool(kind)
            QTest.mousePress(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                             QPointF(*start).toPoint())
            _move(canvas, *end, buttons=Qt.MouseButton.LeftButton)
            _, anchor, current = canvas._draft
            text = canvas.readout_lines()[-1]
            ys, xs = roi_coordinates(RoiShape(1, kind, (anchor, current), ""), 10, 20)
            if kind == "line":
                assert text.startswith("Line  (")
                assert f"({xs[0]}, {ys[0]}) → ({xs[-1]}, {ys[-1]})" in text
            elif kind == "rect":
                width, height = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
                assert text == (f"Box  x {xs.min()}–{xs.max()}  y {ys.min()}–{ys.max()}  ·  "
                                f"{width} × {height} px")
            else:
                assert text.startswith(f"Ellipse  x {xs.min()}–{xs.max()}  y {ys.min()}–{ys.max()}  ·  Ø ")
                assert text.endswith(f"  ·  {xs.size} pixels")
            QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                               QPointF(*end).toPoint())
    finally:
        canvas.close()


def test_thin_ellipse_readout_names_only_the_pixels_it_measures(qapp) -> None:
    # Its outline box spans x 10-19, but only x 11-18 pass the centre test.
    canvas = _canvas(qapp, width=40, height=30, size=(400, 300))
    try:
        points = ((10.0, 10.0), (20.0, 12.0))
        ys, xs = roi_coordinates(RoiShape(1, "ellipse", points, ""), 30, 40)
        text = canvas.roi_geometry_text("Ellipse 1", "ellipse", points)
        assert text == (f"Ellipse 1  x {xs.min()}–{xs.max()}  y {ys.min()}–{ys.max()}  ·  "
                        f"Ø 10 × 2 px  ·  {xs.size} pixels")
        assert (xs.min(), xs.max()) == (11, 18)
        assert canvas.roi_geometry_text("Box", "rect", ((3.0, 3.0), (3.0, 9.0))) == "Box  0 × 6 px  ·  no pixels"
    finally:
        canvas.close()


def test_area_extent_matches_roi_coordinates_exactly() -> None:
    import random

    from flir_player.geometry import area_extent
    rng = random.Random(5)
    for _ in range(3000):
        width, height = rng.randint(1, 60), rng.randint(1, 60)
        kind = rng.choice(("ellipse", "ellipse", "rect"))
        if rng.random() < 0.4:  # thin shapes and corners exactly on pixel boundaries
            points = tuple((float(rng.randint(0, width)), float(rng.randint(0, height))) for _ in range(2))
        else:
            points = tuple((rng.uniform(-2, width + 2), rng.uniform(-2, height + 2)) for _ in range(2))
        ys, xs = roi_coordinates(RoiShape(1, kind, points, ""), height, width)
        expected = None if xs.size == 0 else (xs.min(), xs.max(), ys.min(), ys.max(), xs.size)
        assert area_extent(kind, points, height, width) == expected, (kind, points, width, height)


def test_long_roi_names_are_elided_so_the_readout_fits_the_view(qapp) -> None:
    canvas = _canvas(qapp, width=40, height=30, size=(320, 240))
    try:
        name = "Line 1 - specimen A surface temperature near the burner, north side"
        canvas.set_rois([RoiShape(1, "line", ((2.5, 3.5), (30.5, 20.5)), name)], 1)
        _move(canvas, 160, 120)
        canvas.grab()
        assert canvas.rect().contains(canvas.readout_rect)
        # the painted row: the name is elided, the measurements survive
        metrics = QFontMetrics(QFont("Consolas", 10))
        details = canvas._readout_entries()[-1][1]
        row = canvas._fit_readout_line(metrics, name, details, 400)
        assert metrics.horizontalAdvance(row) <= 400
        assert row.endswith(details) and row.startswith("Line") and "…" in row
    finally:
        canvas.close()


def test_escape_cancels_the_roi_being_drawn_or_edited(qapp) -> None:
    canvas = _canvas(qapp)
    drawn, moved = [], []
    canvas.roi_drawn.connect(lambda *args: drawn.append(args))
    canvas.roi_moved.connect(moved.append)
    try:
        canvas.set_roi_tool("rect")
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPointF(30, 20).toPoint())
        _move(canvas, 90, 60, buttons=Qt.MouseButton.LeftButton)
        override = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        canvas.event(override)
        assert override.isAccepted()  # the canvas, not the full-screen shortcut, takes Esc
        canvas.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                           QPointF(90, 60).toPoint())
        assert drawn == [] and canvas._draft is None
        # an edit in progress snaps back
        shape = RoiShape(1, "rect", ((2.0, 2.0), (8.0, 6.0)), "Box 1")
        canvas.set_rois([shape], 1)
        canvas.set_roi_tool("select")
        centre = canvas._image_to_widget(5.0, 4.0)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre.toPoint())
        _move(canvas, centre.x() + 40, centre.y(), buttons=Qt.MouseButton.LeftButton)
        canvas.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                           (centre + QPointF(40, 0)).toPoint())
        assert moved == []
        # without a gesture Esc is left to the window
        idle = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        idle.ignore()
        canvas.event(idle)
        assert not idle.isAccepted()
    finally:
        canvas.close()


def test_select_tool_cursor_says_what_a_press_will_do(qapp) -> None:
    canvas = _canvas(qapp)
    try:
        canvas.set_rois([RoiShape(1, "rect", ((2.0, 2.0), (8.0, 6.0)), "Box 1")], 1)
        centre = canvas._image_to_widget(5.0, 4.0)
        _move(canvas, centre.x(), centre.y())
        assert canvas.cursor().shape() == Qt.CursorShape.SizeAllCursor
        corner = canvas._image_to_widget(8.0, 6.0)
        _move(canvas, corner.x(), corner.y())
        assert canvas.cursor().shape() == Qt.CursorShape.SizeFDiagCursor
        # the readout names the hovered ROI's pixel extent
        assert canvas.readout_lines()[-1] == "Box 1  x 2–7  y 2–5  ·  6 × 4 px"
        _move(canvas, *canvas._image_to_widget(15.0, 8.0).toTuple())
        assert canvas.cursor().shape() == Qt.CursorShape.ArrowCursor
    finally:
        canvas.close()


def test_readout_stays_on_screen_when_zoomed_and_dodges_the_pointer(qapp) -> None:
    canvas = _canvas(qapp, width=200, height=100, size=(400, 200))
    try:
        canvas.set_zoom_level(8.0)  # the image's top-left corner is far off-screen
        _move(canvas, 200, 100)
        canvas.grab()
        assert canvas.rect().contains(canvas.readout_rect)
        near = canvas.readout_rect.center()
        _move(canvas, near.x(), near.y())
        canvas.grab()
        assert canvas.rect().contains(canvas.readout_rect)
        assert not canvas.readout_rect.adjusted(-16, -16, 16, 16).contains(near)
    finally:
        canvas.close()


def test_notices_are_shown_on_the_view_and_leave_the_export_tooltip_alone(qapp) -> None:
    window = MainWindow()
    window.show()
    try:
        tooltip = window.title_bar.export_button.toolTip()
        window._notify("Saved frame.png")
        assert window.canvas.notice == "Saved frame.png"
        assert window.title_bar.export_button.toolTip() == tooltip
        window.canvas.clear_notice()
        assert window.canvas.notice == ""
    finally:
        window.close()


def test_frameless_window_resizes_from_every_edge_unless_maximized(qapp) -> None:
    window = MainWindow()
    window.resize(1200, 800)
    window.show()
    qapp.processEvents()
    try:
        grips = window._resize_grips
        assert {grip.edges for grip in grips} == set(EdgeResizeGrip.ALL_EDGES)
        rect = window.rect()
        for grip in grips:
            assert grip.isVisible() and rect.contains(grip.geometry())
            geometry = grip.geometry()
            assert (geometry.left() == 0 or geometry.right() == rect.right()
                    or geometry.top() == 0 or geometry.bottom() == rect.bottom())
        # No grip steals clicks from a control, the inspector scroll bar
        # included; only the caption buttons' top strip (as natively).
        scroll_bar = window.inspector.findChild(QScrollArea).verticalScrollBar()
        assert scroll_bar.isVisible()
        controls = [w for w in window.findChildren(QWidget)
                    if isinstance(w, (QAbstractButton, QAbstractSlider, QAbstractSpinBox, QComboBox,
                                      QLineEdit, QTabBar)) and w.isVisible()]
        assert scroll_bar in controls
        for control in controls:
            area = QRect(control.mapTo(window, QPoint(0, 0)), control.size())
            for grip in grips:
                overlap = area.intersected(grip.geometry())
                if overlap.isEmpty():
                    continue
                assert isinstance(control, CaptionButton), (control, grip.edges)
                assert grip.edges & Qt.Edge.TopEdge and overlap.height() <= EdgeResizeGrip.CORNER
        window.showMaximized()
        qapp.processEvents()
        assert not any(grip.isVisible() for grip in grips)
        window.showNormal()
        qapp.processEvents()
        assert all(grip.isVisible() for grip in grips)
    finally:
        window.close()


def test_stylesheet_themes_spin_steppers_and_disabled_checks(qapp) -> None:
    for rule in ("QAbstractSpinBox::up-arrow", "QAbstractSpinBox::down-arrow",
                 "QCheckBox::indicator:checked:disabled"):
        assert rule in APP_STYLESHEET
    for key in ("CHEVRON_UP_ICON", "CHEVRON_UP_DISABLED_ICON", "CHECK_DISABLED_ICON"):
        from pathlib import Path
        assert Path(TOKENS[key]).is_file()


# --- follow-ups: themed message / progress dialogs, Temporal header ---------------------------


def test_message_dialog_wraps_long_paths_and_keeps_them_copyable(qapp) -> None:
    from flir_player.widgets import MessageDialog
    path = "E:/data/" + "long_folder_name_without_spaces_" * 6 + "/frame.png"
    text = f"PermissionError: [Errno 13] Permission denied: '{path}'"
    dialog = MessageDialog("critical", "Export failed", text)
    dialog.show()
    qapp.processEvents()
    try:
        view = dialog.text_view
        assert dialog.text() == text  # nothing inserted to make it wrap
        assert view.width() <= MessageDialog.TEXT_WIDTH[1]
        assert view.document().size().height() <= view.height()  # every line visible
        assert dialog.width() >= dialog.minimumSizeHint().width()
    finally:
        dialog.close()


def test_question_dialog_defaults_to_no(qapp) -> None:
    from flir_player.widgets import MessageDialog
    dialog = MessageDialog("question", "Replace existing files?", "Replace them?")
    dialog.show()
    qapp.processEvents()
    try:
        focus = dialog.focusWidget()
        assert isinstance(focus, QAbstractButton) and focus.text().replace("&", "") == "No"
        QTest.keyClick(dialog, Qt.Key.Key_Return)
        assert dialog.result() == dialog.DialogCode.Rejected  # Enter does not replace files
    finally:
        dialog.close()


def test_progress_dialog_cancels_once_and_waits_for_the_job(qapp) -> None:
    from flir_player.widgets import ProgressDialog
    dialog = ProgressDialog("Export Movie", "Export Movie…")
    cancels = []
    dialog.canceled.connect(lambda: cancels.append(True))
    try:
        assert dialog.isVisible()
        dialog.setMaximum(200)
        dialog.setValue(50)
        assert (dialog.maximum(), dialog.value()) == (200, 50)
        dialog.reject()  # Esc / the title bar's close button
        dialog.cancel_button.click()
        assert cancels == [True] and dialog.wasCanceled()
        assert dialog.isVisible() and not dialog.cancel_button.isEnabled()  # until the job stops
        dialog.reset()
        assert not dialog.isVisible()
    finally:
        dialog.deleteLater()


def test_temporal_tab_has_labelled_controls_instead_of_a_repeated_title(qapp) -> None:
    from flir_player.plots import TemporalPlotPanel
    from PySide6.QtWidgets import QLabel
    panel = TemporalPlotPanel()
    texts = [label.text() for label in panel.findChildren(QLabel)]
    assert "Temporal" not in texts and "Statistic" in texts
    panel.deleteLater()


def test_long_messages_scroll_without_rewrapping_lines_that_fit(qapp) -> None:
    from flir_player.widgets import MessageDialog
    one = MessageDialog("warning", "Batch extract", "file_1.ats: failed (the SDK refused it)")
    many = MessageDialog("warning", "Batch extract",
                         "\n".join(f"file_{i}.ats: failed (the SDK refused it)" for i in range(40)))
    try:
        line = one.text_view.document().size().height()
        view = many.text_view
        assert view.height() == MessageDialog.TEXT_MAX_HEIGHT  # capped; the rest scrolls
        assert view.document().size().height() == 40 * line  # no line wrapped for the scroll bar
    finally:
        one.deleteLater()
        many.deleteLater()


def test_readout_badge_is_not_filled_with_the_selected_rois_colour(qapp) -> None:
    # The selected ROI's handles set its colour as the brush; when it is the
    # last ROI painted, a drawPath outline used to flood the badge with it.
    canvas = _canvas(qapp, width=40, height=30, size=(400, 300))
    try:
        canvas.set_rois([RoiShape(1, "line", ((3.5, 20.5), (12.5, 25.5)), "Line 1"),
                         RoiShape(4, "rect", ((20.0, 15.0), (35.0, 28.0)), "Box 1")], 4)
        centre = canvas._image_to_widget(27.0, 21.0)
        _move(canvas, centre.x(), centre.y())
        image = canvas.grab().toImage()
        rect = canvas.readout_rect
        inside = image.pixelColor(rect.left() + 3, rect.center().y())
        assert max(inside.red(), inside.green(), inside.blue()) < 40, inside.name()
    finally:
        canvas.close()


def test_progress_dialog_lets_the_application_quit(qapp) -> None:
    # Qt closes the modal progress dialog first when quitting; refusing that
    # close aborted the quit before the player's own shutdown handling ran.
    window = MainWindow()
    window.show()
    progress = window._progress_dialog("Export Movie", "Export Movie…")
    cancels = []
    progress.canceled.connect(lambda: cancels.append(True))
    qapp.processEvents()
    QApplication.closeAllWindows()
    qapp.processEvents()
    assert cancels == [True] and not progress.isVisible()
    assert window._closing  # the quit reached the main window's close handler

    class _UserClose:  # Alt+F4 on the dialog itself: it stays up while cancelling
        spontaneous = staticmethod(lambda: True)
        ignored = False

        def ignore(self):
            self.ignored = True

    second = ProgressDialog("Export Movie", "Export Movie…")
    event = _UserClose()
    second.closeEvent(event)
    assert event.ignored and second.wasCanceled() and second.isVisible()
    second.reset()
    second.deleteLater()


def test_errors_are_owned_by_the_player_not_by_a_progress_dialog(qapp, monkeypatch) -> None:
    # A progress dialog is deleted when its job ends; an error it owned would
    # vanish unread. The autouse guard replaces _show_error, so restore it.
    from flir_player.widgets import MessageDialog
    monkeypatch.undo()
    owners = []
    monkeypatch.setattr(MessageDialog, "critical",
                        classmethod(lambda cls, parent, title, text: owners.append(parent)))
    window = MainWindow()
    window.show()
    progress = window._progress_dialog("Export Movie", "Export Movie…")
    try:
        qapp.processEvents()
        window._show_error("FLIR decoding error", "The SDK failed")
        assert owners == [window]
    finally:
        progress.reset()
        progress.deleteLater()
        window.close()
