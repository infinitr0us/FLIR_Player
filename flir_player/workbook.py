# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Excel workbook with raw counts and live temperature formulas (XlsxWriter).

Temperatures are Excel formulas over the stored counts, so editing the
emissivity, reflected temperature, transmission or window cells on Settings
recalculates every value, chart and summary. Only classic worksheet functions
are used (SUMPRODUCT, MEDIAN, INDEX/MATCH, AGGREGATE), so the workbook also
works outside Microsoft 365. Every formula carries its result for the initial
settings, computed with the same model, for viewers that do not recalculate.

Data, Counts and Pixels share row numbers: row r of each describes the same
sample. Sheets are written row by row (constant-memory mode).
"""
from __future__ import annotations

import io
import math
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import xlsxwriter
from xlsxwriter.utility import xl_col_to_name, xl_rowcol_to_cell

from .excel_export import AREA_STATS, EXCEL_COLUMNS, ExportData, RoiData, SourceData, same_parameters
from .fff import describe_parameters
from .jobs import JobCancelled
from .radiometry import (
    KELVIN,
    STATUS_LABELS,
    AtmosphereConstants,
    Calibration,
    MeasurementParameters,
    coefficients,
    compensated_signal,
    object_temperature,
    transmission_used,
)

HEADER_ROWS = 5  # data starts on row 6 of Data, Counts and Pixels (0-based 5)
TC_COLUMNS = 10
TC_CAPACITY = 100_000
TC_FIRST = 8  # 0-based row of the first pasted logger row (Excel row 9)
CLAMP_ON = "Clamp like FLIR software"
CLAMP_OFF = "Show extrapolated values (flagged)"
TAU_AUTO = "From distance and humidity"
TAU_MANUAL = "Manual value"
UNITS = ("°C", "K", "°F")
STAT_LABELS = {"value": "Value", "mean": "Mean", "max": "Max", "min": "Min",
               "median": "Median", "p95": "P95"}
BIG = 1e9
INTERNAL = 18  # first column of Settings' collapsed constants
MATCH = "Emissivity Match"
MATCH_DEFAULTS = (0.90, 0.98, 0.01)  # lowest, highest emissivity and step: the engineers' range
MATCH_CANDIDATES = 41  # rows of the candidate table (step 0.002 still spans 0.90-0.98)
OWN_GRID = tuple(round(0.05 + 0.01 * k, 2) for k in range(96))  # each TC's own value: best of 0.05 … 1.00
PROBE = 0.001  # the error just inside a limit tells whether the best match lies beyond it
CHART_ROWS = 21  # rows the Match chart covers below the pairs table
NOTE_LOW = "The best value is the lowest emissivity allowed; the best match may lie below it."
NOTE_HIGH = "The best value is the highest emissivity allowed; the best match may lie above it."
# what the zones read at the best value, from the signs of the ROIs' mean IR − TC there (with a hot surrounding
# the IR responds to emissivity the other way, so the side of the limit says nothing about the sign)
NOTE_COLD = (" There the zones read colder than their TCs: the TCs may read flames or hot gas (TCs on the top face "
             "run hotter than the side the camera sees), or the zones emit less (unpainted).")
NOTE_HOT = (" There the zones read hotter than their TCs: flames or hot gas in front of the zones, reflections, or "
            "TCs reading low (loose contact).")
NOTE_MIXED = " There some zones read hotter and others colder than their TCs."
TC_BLOCK = 10  # TC Compare columns per ROI
# Chart colours. Up to 8 ROIs keep the workbook's own palette; more (cell zones) take one blue
# ramp in ROI order, so neighbouring cells read as neighbours (the reference sequential steps
# 250 to 700; 18 steps are not told apart one by one: the legend and the per-TC charts name them).
PALETTE = ("#C00000", "#0070C0", "#00B050", "#7030A0", "#ED7D31", "#264478", "#9E480E", "#636363")
RAMP = ("#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95",
        "#104281", "#0d366b")
# Emissivity Match chart: the reference categorical order (validated on white), ink for the total
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
INK = "#0b0b0b"


def _abs(sheet: str, row: int, col: int) -> str:
    return f"'{sheet}'!{xl_rowcol_to_cell(row, col, row_abs=True, col_abs=True)}"


def _cell(sheet: str, row: int, col: int) -> str:
    return f"'{sheet}'!{xl_rowcol_to_cell(row, col)}"


def _range(sheet: str, row0: int, col0: int, row1: int, col1: int) -> str:
    return (f"'{sheet}'!{xl_rowcol_to_cell(row0, col0, True, True)}:"
            f"{xl_rowcol_to_cell(row1, col1, True, True)}")


def _num(value: float) -> str:
    """A literal for a formula (parenthesised when negative)."""
    text = repr(float(value))
    return f"({text})" if value < 0 else text


def _finite_or(value: float, error: str):
    """A formula's cached result: the number, or an Excel error code if it is not finite."""
    return float(value) if np.isfinite(value) else error


# single-cell names the row formulas cite (ranges such as ROI_01_T are not replaced: none is listed)
_NAME_TOKEN = re.compile(r"(?<![\w.'!$])(?:ROI_\d+_[A-Z0-9]+|SRC_\d+_[A-Z0-9]+|UNIT_A|UNIT_B|TC_OFFSET|"
                         r"MATCH_FROM|MATCH_TO|CLAMP_MODE)(?![\w(])")


def _unit_affine(unit: str) -> tuple[float, float]:
    return {"K": (1.0, 0.0), "°F": (1.8, -459.67)}.get(unit, (1.0, -KELVIN))


def _ramp(n: int) -> list[str]:
    """``n`` colours along RAMP, darkest first (the first ROI, e.g. cell 1 at the heater end)."""
    stops = [tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in reversed(RAMP)]
    out = []
    for k in range(n):
        x = k / (n - 1) * (len(stops) - 1) if n > 1 else 0.0
        i = min(int(x), len(stops) - 2)
        f = x - i
        rgb = (round(a + (b - a) * f) for a, b in zip(stops[i], stops[i + 1]))
        out.append("#" + "".join(f"{v:02x}" for v in rgb))
    return out


def _step_used(lo: float, hi: float, step: float) -> float:
    """The Match table's step: the one asked for, or wider so its rows reach the highest emissivity."""
    return max(step, (hi - lo) / (MATCH_CANDIDATES - 1))


def _candidates(lo: float, hi: float, step: float) -> list[float]:
    """The candidate emissivities of the Match table's formulas: lo, lo + step, …, and hi itself last."""
    if not (step > 0 and hi >= lo):
        return []
    used = _step_used(lo, hi, step)
    out = [lo]
    for j in range(1, MATCH_CANDIDATES):
        if not lo + (j - 1) * used < hi - 1e-9:
            break
        out.append(min(lo + j * used, hi))
    return out


class _Buffered:
    """Worksheet proxy that collects writes and emits them in row order.

    Constant-memory mode drops cells written to an already-flushed row, so the
    small, freely laid-out sheets are buffered and flushed at the end.
    """

    _WRITES = ("write", "write_number", "write_string", "write_formula", "write_array_formula",
               "write_blank", "write_datetime", "merge_range")

    def __init__(self, ws):
        self._ws = ws
        self._calls: list[tuple[int, int, int, str, tuple, dict]] = []

    def __getattr__(self, name):
        if name in self._WRITES:
            def record(row, col, *args, **kwargs):
                self._calls.append((row, col, len(self._calls), name, args, kwargs))
            return record
        return getattr(self._ws, name)

    def flush(self) -> None:
        for row, col, _order, name, args, kwargs in sorted(self._calls, key=lambda c: c[:3]):
            getattr(self._ws, name)(row, col, *args, **kwargs)
        self._calls.clear()


@dataclass
class _RoiLayout:
    """Where one ROI lives in each sheet."""

    source: SourceData
    roi: RoiData
    number: int  # 1-based over the workbook
    name: str  # "ROI_01"
    src: str  # "SRC_1"
    data_cols: dict  # stat → Data column
    status_col: int
    count_cols: dict  # stat → Counts column
    pixel_col: int | None  # first Pixels column
    title: str
    main: str  # "value" or "mean"


class _Writer:
    def __init__(self, path: Path, data: ExportData, abort=None, tmpdir: str | None = None):
        self.data = data
        self.abort = abort
        self.times = data.times
        self.rows = data.times.size
        self.last = HEADER_ROWS + self.rows - 1  # 0-based last data row
        self.unit = data.options.unit if data.options.unit in UNITS else "°C"
        # constant-memory row files go to ``tmpdir`` (deleted by close() on success)
        self.wb = xlsxwriter.Workbook(str(path), {
            "constant_memory": True, "nan_inf_to_errors": True, "strings_to_numbers": False,
            "strings_to_formulas": False, "strings_to_urls": False, "tmpdir": tmpdir})
        # AGGREGATE (Excel 2010+) is written with its storage prefix by hand:
        # XlsxWriter's use_future_functions would regex-scan every formula.
        self.wb.set_properties({"title": "FLIR IR data — raw counts and live temperatures",
                                "author": "FLIR Thermal Player", "comments": "Generated by FLIR Thermal Player"})
        self._formats()
        self.sheets = {}
        self._refs: dict[str, str] = {}  # single-cell name → its absolute cell (or constant)
        self.layouts: list[_RoiLayout] = []
        prefill = data.options.tc_prefill
        if prefill is not None:
            prefill.check(TC_CAPACITY, TC_COLUMNS)
        self._tc_names: list[str] = list(prefill.names) if prefill is not None else []
        self._roi_tc: dict[str, str] = dict(prefill.roi_tc) if prefill is not None else {}
        for source in data.sources:  # TCs are mapped by ROI name: two ROIs of one recording cannot share one
            names = [roi.shape.name for roi in source.rois]
            repeated = sorted({name for name in self._roi_tc if names.count(name) > 1})
            if repeated:
                raise ValueError(f"{source.label}: more than one ROI is named {', '.join(repeated)}; the workbook "
                                 "pairs ROIs and TCs by name, so give them different names")
        self._roi_eps: dict[str, float] = dict(prefill.roi_eps) if prefill is not None else {}
        # A workbook filled from the TC fit compares only the ROIs paired with a TC (cell zones: a few of
        # many; every comparison column is a formula Excel parses on opening); otherwise every ROI.
        self._tc_only_paired = prefill is not None and bool(prefill.roi_tc)

    def _add_sheets(self) -> None:
        """Create the worksheets (each opens a constant-memory row file)."""
        order = ["Start Here", "Settings", "Data"]
        options = self.data.options.sheets
        if "charts" in options:
            order.append("Charts")
        if "tc" in options:
            order += ["TC Compare", MATCH]
        if "summary" in options:
            order.append("Summary")
        if "roimap" in options:
            order.append("ROI Map")
        if "validation" in options and any(s.validation_rows.size for s in self.data.sources):
            order.append("Validation")
        order += ["Counts", "Pixels", "Source"]
        buffered = {"Start Here", "Summary", "ROI Map", "Validation", "Source", MATCH}
        for name in order:
            ws = self.wb.add_worksheet(name)
            self.sheets[name] = _Buffered(ws) if name in buffered else ws

    # --- formats ---------------------------------------------------------------------

    def _formats(self) -> None:
        f = self.wb.add_format
        self.f_title = f({"bold": True, "font_size": 15, "font_color": "#1F3864"})
        self.f_h2 = f({"bold": True, "font_size": 12, "font_color": "#1F3864"})
        self.f_note = f({"italic": True, "font_color": "#595959", "text_wrap": False})
        self.f_wrap = f({"text_wrap": True, "valign": "top"})
        self.f_bold = f({"bold": True})
        self.f_bold_left = f({"bold": True, "align": "left"})
        self.f_left = f({"align": "left"})
        self.f_head = f({"bold": True, "bg_color": "#D9E1F2", "border": 1, "text_wrap": True,
                         "valign": "top"})
        self.f_group = f({"bold": True, "bg_color": "#B4C6E7", "border": 1})
        self.f_input = f({"bg_color": "#FFF2CC", "border": 1, "locked": False})
        self.f_input_num = f({"bg_color": "#FFF2CC", "border": 1, "locked": False, "num_format": "0.000"})
        self.f_input_int = f({"bg_color": "#FFF2CC", "border": 1, "locked": False, "num_format": "0"})
        self.f_best = f({"bold": True, "font_size": 14, "num_format": "0.000", "bg_color": "#C6EFCE",
                         "font_color": "#006100", "border": 1})
        self.f_k = f({"num_format": "0.0"})
        self.f_used = f({"num_format": '"used "0.000', "font_color": "#595959", "italic": True})
        self.f_hide = f({"font_color": "#FFFFFF"})  # #N/A where a candidate or ROI has no value
        self.f_calc = f({"bg_color": "#F2F2F2", "border": 1, "num_format": "0.000000"})
        self.f_calc_text = f({"bg_color": "#F2F2F2", "border": 1})
        self.f_const = f({"bg_color": "#EDEDED", "border": 1, "num_format": "0.0########"})
        self.f_temp = f({"num_format": "0.00"})
        self.f_time = f({"num_format": "0.0##"})
        self.f_min = f({"num_format": "0.00"})
        self.f_count = f({"num_format": "0.0"})
        self.f_int = f({"num_format": "0"})
        self.f_eps = f({"num_format": "0.000"})
        self.f_clock = f({"num_format": "yyyy-mm-dd hh:mm:ss.000"})
        self.f_grey = f({"font_color": "#7F7F7F", "italic": True})
        self.f_red = f({"bg_color": "#F8CBAD", "font_color": "#9C0006"})
        self.f_pass = f({"bg_color": "#C6EFCE", "font_color": "#006100", "bold": True})
        self.f_fail = f({"bg_color": "#FFC7CE", "font_color": "#9C0006", "bold": True})

    # --- build -------------------------------------------------------------------------

    def build(self) -> None:
        self._add_sheets()
        self._plan_layout()
        self._settings()
        self._data()
        self._counts()
        self._pixels()
        if "TC Compare" in self.sheets:
            self._tc()
            self._match()
        if "Summary" in self.sheets:
            self._summary()
        if "Charts" in self.sheets:
            self._charts()
        if "ROI Map" in self.sheets:
            self._roi_map()
        if "Validation" in self.sheets:
            self._validation()
        self._source()
        self._start()
        for ws in self.sheets.values():
            if isinstance(ws, _Buffered):
                ws.flush()
        self.wb.close()

    def close_files(self) -> None:
        """Close the temporary files build() left open by stopping early: the
        constant-memory row files, and the part being written if close() failed."""
        for part in (*self.wb.worksheets(), *self.wb.charts, *self.wb.drawings, self.wb):
            for attribute in ("row_data_fh", "fh"):
                handle = getattr(part, attribute, None)
                if handle is not None and not getattr(handle, "closed", True):
                    handle.close()

    def _has_tc(self, layout: _RoiLayout) -> bool:
        """Whether the ROI has a block on TC Compare (and so a row on Emissivity Match)."""
        return "TC Compare" in self.sheets and (not self._tc_only_paired
                                                or layout.roi.shape.name in self._roi_tc)

    def _plan_layout(self) -> None:
        data_col = 2  # A: time (s), B: time (min)
        count_col = 1  # A: time; then per source frame + clock
        count_col += 2 * len(self.data.sources)
        pixel_col = 1
        number = 0
        for s_index, source in enumerate(self.data.sources, start=1):
            for roi in source.rois:
                number += 1
                extremes = self.data.options.extremes
                stats = ("value",) if roi.mode == "spot" else tuple(
                    stat for stat in roi.counts if extremes or stat not in ("max", "min"))
                data_cols = {}
                for stat in stats:
                    data_cols[stat] = data_col
                    data_col += 1
                if roi.mode != "spot" and extremes:
                    data_cols["spread"] = data_col
                    data_col += 1
                status_col = data_col
                data_col += 1
                count_cols = {}
                for stat in stats:
                    count_cols[stat] = count_col
                    count_col += 1
                count_cols["pixels"] = count_col
                count_col += 1
                first_pixel = None
                if roi.block is not None:
                    first_pixel = pixel_col
                    pixel_col += roi.block.shape[1]
                self.layouts.append(_RoiLayout(
                    source=source, roi=roi, number=number, name=f"ROI_{number:02d}",
                    src=f"SRC_{s_index}", data_cols=data_cols, status_col=status_col,
                    count_cols=count_cols, pixel_col=first_pixel,
                    title=f"{source.label} · {roi.shape.name}",
                    main="value" if roi.mode == "spot" else "mean"))
        self.data_width = data_col
        self.count_width = count_col
        self.pixel_width = pixel_col
        widths = {"Data": data_col, "Counts": count_col, "Pixels": pixel_col,
                  "TC Compare": TC_COLUMNS + 5 + TC_BLOCK * sum(1 for l in self.layouts if self._has_tc(l))}
        for sheet, width in widths.items():
            if sheet in self.sheets and width > EXCEL_COLUMNS:
                raise ValueError(f"The {sheet} sheet would need {width:,} columns, more than Excel's "
                                 f"{EXCEL_COLUMNS:,}; export fewer ROIs per workbook")

    def _check(self, row: int) -> None:
        """Honour cancellation while writing (every 256 rows)."""
        if self.abort is not None and row % 256 == 0 and self.abort():
            raise JobCancelled("Export cancelled")

    # --- names ---------------------------------------------------------------------------

    def _name(self, name: str, sheet: str, row: int, col: int) -> None:
        self.wb.define_name(name, "=" + _abs(sheet, row, col))
        self._refs[name] = _abs(sheet, row, col)

    def _direct(self, formula: str) -> str:
        """The formula with its single-cell names replaced by the cells they name.

        Excel resolves a defined name slowly when it opens a workbook: 300,000 formulas citing
        per-ROI names took 76 s to open, the same with direct references 13 s
        (local/notes/2026-10-10-cell-zones). The row-by-row formulas of Data and TC Compare
        therefore cite cells; the names stay defined for reading and for the few summary formulas.
        """
        parts = re.split(r'("[^"]*")', formula)  # string literals (odd parts) stay as written
        return "".join(part if k % 2 else _NAME_TOKEN.sub(lambda m: self._refs.get(m.group(0), m.group(0)), part)
                       for k, part in enumerate(parts))

    # --- Settings --------------------------------------------------------------------------

    def _settings(self) -> None:
        ws = self.sheets["Settings"]
        S = "Settings"
        first = self.data.sources[0]
        g = first.initial
        rows: dict[int, list] = {}

        def put(r, c, value, fmt=None, *, formula=None, cached=None):
            rows.setdefault(r, []).append((c, value, fmt, formula, cached))

        put(0, 0, "Settings — change the yellow cells; everything else follows", self.f_title)
        put(1, 0, "Temperatures below are in °C. Blank override cells use the value above them. Calibration "
                  "constants and coefficients are in the collapsed columns to the right (click + above them).",
            self.f_note)
        put(3, 0, "Display unit", self.f_bold)
        put(3, 1, self.unit, self.f_input)
        put(4, 0, "Out-of-range values", self.f_bold)
        put(4, 1, CLAMP_OFF, self.f_input)
        a, b = _unit_affine(self.unit)
        put(3, INTERNAL, "Unit factor", self.f_note)
        put(3, INTERNAL + 1, None, self.f_calc, formula='=IF($B$4="°F",1.8,1)', cached=a)
        put(4, INTERNAL, "Unit offset", self.f_note)
        put(4, INTERNAL + 1, None, self.f_calc, formula='=IF($B$4="K",0,IF($B$4="°F",-459.67,-273.15))',
            cached=b)
        put(5, INTERNAL, "Axis title", self.f_note)
        put(5, INTERNAL + 1, None, self.f_calc_text, formula='="Temperature ("&$B$4&")"',
            cached=f"Temperature ({self.unit})")
        self._name("UNIT", S, 3, 1)
        self._name("CLAMP_MODE", S, 4, 1)
        self._name("UNIT_A", S, 3, INTERNAL + 1)
        self._name("UNIT_B", S, 4, INTERNAL + 1)
        self._name("AXIS_TITLE", S, 5, INTERNAL + 1)
        ws.data_validation(3, 1, 3, 1, {"validate": "list", "source": list(UNITS)})
        ws.data_validation(4, 1, 4, 1, {"validate": "list", "source": [CLAMP_OFF, CLAMP_ON]})

        put(7, 0, "Measurement parameters for every recording and ROI", self.f_h2)
        tau_mode = TAU_AUTO if g.transmission is None else TAU_MANUAL
        globals_ = (
            ("G_EPS", "Emissivity", g.emissivity, self.f_input_num),
            ("G_TREFL", "Reflected apparent temperature (°C)", g.reflected_k - KELVIN, self.f_input_num),
            ("G_TATM", "Atmosphere temperature (°C)", g.atmosphere_k - KELVIN, self.f_input_num),
            ("G_RH", "Relative humidity (%)", g.humidity * 100.0, self.f_input_num),
            ("G_TAUMODE", "Atmospheric transmission", tau_mode, self.f_input),
            ("G_TAU", "Manual transmission (used when chosen above)",
             g.transmission if g.transmission is not None else 1.0, self.f_input_num),
            ("G_WTAU", "External window / optics transmission", g.window_transmission, self.f_input_num),
            ("G_WT", "External window / optics temperature (°C)", g.window_k - KELVIN, self.f_input_num),
        )
        for k, (name, label, value, fmt) in enumerate(globals_):
            r = 8 + k
            put(r, 0, label)
            put(r, 1, value, fmt)
            self._name(name, S, r, 1)
        ws.data_validation(12, 1, 12, 1, {"validate": "list", "source": [TAU_AUTO, TAU_MANUAL]})
        ws.data_validation(8, 1, 8, 1, {"validate": "decimal", "criteria": "between",
                                         "minimum": 0.001, "maximum": 1.0,
                                         "error_message": "Emissivity must be between 0.001 and 1"})

        # recordings
        r0 = 17
        put(r0, 0, "Recordings", self.f_h2)
        put(r0, 4, "Overrides — leave blank to use the values above", self.f_note)
        head = ["#", "Recording", "Camera", "Distance (m)", "Emissivity", "Reflected T (°C)",
                "Atmosphere T (°C)", "Humidity (%)", "Transmission", "Window transmission", "Window T (°C)",
                "Emissivity used", "Reflected (K)", "Atmosphere (K)", "Humidity used (%)", "Transmission used",
                "Window τ used", "Window (K)"] + [""] * (INTERNAL - 18) + [
                "Planck R (R1/R2)", "Planck B", "Planck F", "Planck O", "Atm X", "Atm α1", "Atm α2",
                "Atm β1", "Atm β2", "H₂O max", "Calibrated min (K)", "Calibrated max (K)",
                "Clip min (K)", "Clip max (K)", "Max valid signal", "Calibration"]
        for c, text in enumerate(head):
            put(r0 + 1, c, text, self.f_head)
        self.source_rows = {}
        for s_index, source in enumerate(self.data.sources, start=1):
            r = r0 + 1 + s_index
            self.source_rows[s_index] = r
            src = f"SRC_{s_index}"
            p = source.initial
            cal = source.report.calibration if source.report.verified else None
            put(r, 0, s_index, self.f_int)
            put(r, 1, source.label)
            put(r, 2, f"{source.info['camera']} {source.info['camera_serial']}".strip())
            put(r, 3, p.distance_m, self.f_input_num)
            same = s_index == 1

            def override(value, reference, shown):
                return None if same or math.isclose(value, reference) else shown

            put(r, 4, override(p.emissivity, g.emissivity, p.emissivity), self.f_input_num)
            put(r, 5, override(p.reflected_k, g.reflected_k, p.reflected_k - KELVIN), self.f_input_num)
            put(r, 6, override(p.atmosphere_k, g.atmosphere_k, p.atmosphere_k - KELVIN), self.f_input_num)
            put(r, 7, override(p.humidity, g.humidity, p.humidity * 100.0), self.f_input_num)
            tau_override = None
            if not same and (p.transmission is None) != (g.transmission is None):
                tau_override = transmission_used(cal, p) if cal is not None else p.transmission
            elif not same and p.transmission is not None and not math.isclose(p.transmission, g.transmission or -1):
                tau_override = p.transmission
            put(r, 8, tau_override, self.f_input_num)
            put(r, 9, override(p.window_transmission, g.window_transmission, p.window_transmission),
                self.f_input_num)
            put(r, 10, override(p.window_k, g.window_k, p.window_k - KELVIN), self.f_input_num)
            R = r + 1  # Excel row
            atm = (cal.atmosphere if cal is not None and cal.atmosphere is not None else AtmosphereConstants())
            constants = [None] * 10
            if cal is not None:
                constants = [cal.planck.R, cal.planck.B, cal.planck.F, cal.planck.O,
                             atm.X, atm.alpha1, atm.alpha2, atm.beta1, atm.beta2, atm.h2o_max]
            for k, value in enumerate(constants):
                put(r, INTERNAL + k, value, self.f_const)
            limits = cal.limits if cal is not None else None
            lim = [limits.calibrated[0] if limits and limits.calibrated else None,
                   limits.calibrated[1] if limits and limits.calibrated else None,
                   limits.clip[0] if limits and limits.clip else None,
                   limits.clip[1] if limits and limits.clip else None]
            for k, value in enumerate(lim):
                put(r, INTERNAL + 10 + k, value, self.f_const)
            # T = B/ln(R/v + F) needs v = object signal + O > 0 and ln(...) > 0,
            # i.e. v < R/(1 − F) when F < 1 (the Python model's validity rule).
            smax = (cal.planck.R / (1 - cal.planck.F) if cal is not None and cal.planck.F < 1 else 1e300)
            put(r, INTERNAL + 14, smax if cal is not None else None, self.f_const)
            put(r, INTERNAL + 15, source.report.message if cal is None else cal.source, self.f_calc_text)
            cached = self._source_cached(source)
            put(r, 11, None, self.f_calc, formula=f"=IF(ISNUMBER(E{R}),E{R},G_EPS)", cached=cached["eps"])
            put(r, 12, None, self.f_calc, formula=f"=IF(ISNUMBER(F{R}),F{R},G_TREFL)+273.15",
                cached=cached["trefl"])
            put(r, 13, None, self.f_calc, formula=f"=IF(ISNUMBER(G{R}),G{R},G_TATM)+273.15",
                cached=cached["tatm"])
            put(r, 14, None, self.f_calc, formula=f"=IF(ISNUMBER(H{R}),H{R},G_RH)", cached=cached["rh"])
            k = {key: f"{xl_col_to_name(INTERNAL + j)}{R}" for j, key in enumerate(
                ("R", "B", "F", "O", "X", "A1", "A2", "B1", "B2", "HMAX"))}
            t_c = f"(N{R}-273.15)"
            h2o = (f"MIN({k['HMAX']},O{R}/100*EXP(1.5587+0.06939*{t_c}-0.00027816*{t_c}^2"
                   f"+0.00000068455*{t_c}^3))")
            auto = (f"IF(D{R}<=0,1,{k['X']}*EXP(-SQRT(D{R})*({k['A1']}+{k['B1']}*SQRT({h2o})))"
                    f"+(1-{k['X']})*EXP(-SQRT(D{R})*({k['A2']}+{k['B2']}*SQRT({h2o}))))")
            put(r, 15, None, self.f_calc,
                formula=f'=IF(ISNUMBER(I{R}),I{R},IF(G_TAUMODE="{TAU_MANUAL}",G_TAU,{auto}))',
                cached=cached["tau"])
            put(r, 16, None, self.f_calc, formula=f"=IF(ISNUMBER(J{R}),J{R},G_WTAU)", cached=cached["wtau"])
            put(r, 17, None, self.f_calc, formula=f"=IF(ISNUMBER(K{R}),K{R},G_WT)+273.15", cached=cached["wt"])
            for name, col in (("EPS", 11), ("TREFL", 12), ("TATM", 13), ("RH", 14), ("TAU", 15), ("WTAU", 16),
                              ("WT", 17), ("R", INTERNAL), ("B", INTERNAL + 1), ("F", INTERNAL + 2),
                              ("O", INTERNAL + 3), ("CLIPLO", INTERNAL + 12), ("CLIPHI", INTERNAL + 13),
                              ("SMAX", INTERNAL + 14)):
                self._name(f"{src}_{name}", S, r, col)
            # the calibrated range in raw counts (independent of the object parameters): named constants
            low, high = self._count_range(source)
            self.wb.define_name(f"{src}_CNTLO", f"={_num(low)}")
            self.wb.define_name(f"{src}_CNTHI", f"={_num(high)}")
            self._refs[f"{src}_CNTLO"], self._refs[f"{src}_CNTHI"] = _num(low), _num(high)

        # ROIs
        q0 = r0 + 3 + len(self.data.sources) + 1
        self.roi_head_row = q0
        put(q0, 0, "ROIs", self.f_h2)
        put(q0, 5, "Overrides — leave blank to use the recording's values", self.f_note)
        head = ["#", "Recording", "ROI", "Type", "Pixels", "Emissivity", "Reflected T (°C)",
                "TC column (TC Compare)", "Emissivity used", "Reflected (K)"]
        internal = ["K1", "K2", "Clamp low (K)", "Clamp high (K)", "Clamp low (counts)",
                    "Clamp high (counts)", "A (compensation)", "C (compensation)", "S(reflected)",
                    "TC column #"]
        for c, text in enumerate(head):
            put(q0 + 1, c, text, self.f_head)
        for c, text in enumerate(internal):
            put(q0 + 1, INTERNAL + c, text, self.f_head)
        tc_list = f"='TC Compare'!$B${TC_FIRST + 1}:${xl_col_to_name(TC_COLUMNS)}${TC_FIRST + 1}"
        col = {key: INTERNAL + k for k, key in enumerate(
            ("K1", "K2", "LO", "HI", "CLO", "CHI", "A", "C", "SREFL", "TCCOL"))}
        letter = {key: xl_col_to_name(c) for key, c in col.items()}
        for layout in self.layouts:
            r = q0 + 1 + layout.number
            R = r + 1
            src, name = layout.src, layout.name
            layout.settings_row = r
            roi = layout.roi
            kinds = {"cursor": "Spot", "rect": "Box", "ellipse": "Ellipse", "line": "Line"}
            put(r, 0, layout.number, self.f_int)
            put(r, 1, layout.source.label)
            put(r, 2, roi.shape.name)
            put(r, 3, kinds.get(roi.shape.kind, roi.shape.kind) + {"bins": " (bins)", "mean": " (mean signal)"}.get(
                roi.mode, ""))
            put(r, 4, roi.pixels, self.f_int)
            put(r, 5, self._roi_eps.get(roi.shape.name), self.f_input_num)
            put(r, 6, None, self.f_input_num)
            has_tc = self._has_tc(layout)
            mapped = self._roi_tc.get(roi.shape.name) if has_tc else None
            put(r, 7, mapped, self.f_input if has_tc else None)
            if has_tc:
                ws.data_validation(r, 7, r, 7, {"validate": "list", "source": tc_list,
                                                 "ignore_blank": True})
            cached = self._roi_cached(layout)
            s_expr = lambda t: f"({src}_R/(EXP({src}_B/({t}))-{src}_F)-{src}_O)"  # noqa: E731
            cell = {key: f"{letter[key]}{R}" for key in letter}
            put(r, 8, None, self.f_calc, formula=f"=IF(ISNUMBER(F{R}),F{R},{src}_EPS)", cached=cached["eps"])
            put(r, 9, None, self.f_calc, formula=f"=IF(ISNUMBER(G{R}),G{R}+273.15,{src}_TREFL)",
                cached=cached["trefl"])
            put(r, col["K1"], None, self.f_calc, formula=f"=1/(I{R}*{src}_TAU*{src}_WTAU)", cached=cached["k1"])
            put(r, col["K2"], None, self.f_calc,
                formula=(f"=((1-{src}_WTAU)*{s_expr(f'{src}_WT')}/{src}_WTAU"
                         f"+(1-I{R})*{src}_TAU*{s_expr(f'J{R}')}+(1-{src}_TAU)*{s_expr(f'{src}_TATM')})"
                         f"/(I{R}*{src}_TAU)"), cached=cached["k2"])
            put(r, col["LO"], None, self.f_calc,
                formula=f'=IF(AND(CLAMP_MODE="{CLAMP_ON}",ISNUMBER({src}_CLIPLO)),{src}_CLIPLO,-{BIG:.0E})',
                cached=-BIG)
            put(r, col["HI"], None, self.f_calc,
                formula=f'=IF(AND(CLAMP_MODE="{CLAMP_ON}",ISNUMBER({src}_CLIPHI)),{src}_CLIPHI,{BIG:.0E})',
                cached=BIG)
            put(r, col["CLO"], None, self.f_calc,
                formula=(f"=IF({cell['LO']}>0,({s_expr(cell['LO'])}+{cell['K2']})/{cell['K1']},"
                         f"-{BIG:.0E})"), cached=-BIG)
            put(r, col["CHI"], None, self.f_calc,
                formula=(f"=IF({cell['HI']}<{BIG:.0E},({s_expr(cell['HI'])}+{cell['K2']})/{cell['K1']},"
                         f"{BIG:.0E})"), cached=BIG)
            put(r, col["A"], None, self.f_calc, formula=f"=1/({src}_TAU*{src}_WTAU)", cached=cached["a"])
            put(r, col["C"], None, self.f_calc,
                formula=(f"=((1-{src}_WTAU)*{s_expr(f'{src}_WT')}/{src}_WTAU"
                         f"+(1-{src}_TAU)*{s_expr(f'{src}_TATM')})/{src}_TAU"), cached=cached["c"])
            put(r, col["SREFL"], None, self.f_calc, formula=f"={s_expr(f'J{R}')}", cached=cached["srefl"])
            put(r, col["TCCOL"], None, self.f_calc,
                formula=(f"=IF(H{R}=\"\",\"\",IFERROR(MATCH(H{R},'TC Compare'!$B${TC_FIRST + 1}:"
                         f"${xl_col_to_name(TC_COLUMNS)}${TC_FIRST + 1},0),\"\"))"
                         if has_tc else '=""'),
                cached=self._tc_names.index(mapped) + 1 if mapped else "")
            for label, c in (("EPS", 8), ("TREFL", 9), ("TC", 7)):
                self._name(f"{name}_{label}", S, r, c)
            for label, c in col.items():
                self._name(f"{name}_{label}", S, r, c)
        for r in sorted(rows):
            for c, value, fmt, formula, cached in rows[r]:
                if formula is not None:
                    ws.write_formula(r, c, formula, fmt, cached if cached is not None else 0)
                elif value is None:
                    ws.write_blank(r, c, None, fmt)
                else:
                    ws.write(r, c, value, fmt)
        ws.set_column(0, 0, 44)
        ws.set_column(1, 1, 30)
        ws.set_column(2, 2, 22)
        ws.set_column(3, INTERNAL - 1, 13)
        # Constants and intermediate coefficients: collapsed, expandable with "+".
        ws.set_column(INTERNAL, INTERNAL + 14, 16, self.f_const, {"level": 1, "hidden": True})
        ws.set_column(INTERNAL + 15, INTERNAL + 15, 70)
        ws.freeze_panes(0, 1)

    @staticmethod
    def _count_range(source: SourceData) -> tuple[float, float]:
        """Raw counts at the camera's calibrated range; ±BIG when it is unknown."""
        if source.report.verified:
            calibrated = source.report.calibration.count_limits()["calibrated"]
            if calibrated is not None and all(math.isfinite(v) for v in calibrated):
                return float(min(calibrated)), float(max(calibrated))
        return -BIG, BIG

    def _source_cached(self, source: SourceData) -> dict:
        p = source.initial
        cal = source.report.calibration if source.report.verified else None
        tau = transmission_used(cal, p) if cal is not None else (p.transmission or 1.0)
        return {"eps": p.emissivity, "trefl": p.reflected_k, "tatm": p.atmosphere_k, "rh": p.humidity * 100.0,
                "tau": tau, "wtau": p.window_transmission, "wt": p.window_k}

    def _params(self, layout: _RoiLayout) -> MeasurementParameters:
        """The ROI's parameters as the Settings formulas start: its emissivity override, if any."""
        p = layout.source.initial
        cal = layout.source.report.calibration if layout.source.report.verified else None
        tau = transmission_used(cal, p) if cal is not None else (p.transmission or 1.0)
        eps = self._roi_eps.get(layout.roi.shape.name, p.emissivity)
        return p.with_(transmission=tau, emissivity=eps)

    def _roi_cached(self, layout: _RoiLayout) -> dict:
        cal = layout.source.report.calibration if layout.source.report.verified else None
        p = self._params(layout)
        values = {"eps": p.emissivity, "trefl": p.reflected_k}
        if cal is None:
            return dict(values, k1=0, k2=0, a=0, c=0, srefl=0)
        k1, k2 = coefficients(cal, p)
        a, c = compensated_signal(cal, p, None)
        return dict(values, k1=k1, k2=k2, a=a, c=c, srefl=float(cal.planck.signal(p.reflected_k)))

    # --- formula builders -------------------------------------------------------------------

    # Clamping happens on the counts (to the counts of the clip temperatures), so
    # spots, extremes and pixel means clamp identically; T is monotonic in counts.
    # The curve T = B / ln(R / v + F), v = object signal + O, is only valid for
    # 0 < v < SMAX (the Python model's rule); outside it the result is #N/A.

    def _signal(self, layout: _RoiLayout, counts: str) -> str:
        src, name = layout.src, layout.name
        return f"({name}_K1*MEDIAN({name}_CLO,{counts},{name}_CHI)-{name}_K2+{src}_O)"

    def _scalar_formula(self, layout: _RoiLayout, ref: str) -> str:
        src = layout.src
        v = self._signal(layout, ref)
        return (f"=IF(ISNUMBER({ref}),IFERROR(IF(AND({v}>0,{v}<{src}_SMAX),"
                f"{src}_B/LN({src}_R/{v}+{src}_F)*UNIT_A+UNIT_B,NA()),NA()),NA())")

    def _clamped(self, layout: _RoiLayout, ref: str) -> str:
        """Elementwise counts clamp for arrays (MEDIAN would aggregate)."""
        name = layout.name
        return f"({ref}+({name}_CLO-{ref})*({ref}<{name}_CLO)+({name}_CHI-{ref})*({ref}>{name}_CHI))"

    def _mean_formula(self, layout: _RoiLayout, row: int) -> str:
        src, name = layout.src, layout.name
        roi = layout.roi
        width = roi.block.shape[1]
        if roi.mode == "pixels":
            ref = _range("Pixels", row, layout.pixel_col, row, layout.pixel_col + width - 1)
            weights, values, total = None, ref, f"COUNT({ref})"
        else:
            bins = width // 2
            weights = _range("Pixels", row, layout.pixel_col, row, layout.pixel_col + bins - 1)
            values = _range("Pixels", row, layout.pixel_col + bins, row, layout.pixel_col + width - 1)
            total = f"SUM({weights})"
        element = f"({name}_K1*{self._clamped(layout, values)}-{name}_K2+{src}_O)"
        curve = f"{src}_B/LN({src}_R/{element}+{src}_F)"
        body = f"SUMPRODUCT({weights + ',' if weights else ''}{curve})/{total}"
        # every element is valid iff the (clamped) extremes are: v rises with counts
        low, high = self._signal(layout, f"MIN({values})"), self._signal(layout, f"MAX({values})")
        return (f"=IF({total}=0,NA(),IFERROR(IF(AND({low}>0,{high}<{src}_SMAX),"
                f"({body})*UNIT_A+UNIT_B,NA()),NA()))")

    # --- Data -------------------------------------------------------------------------------

    def _data(self) -> None:
        ws = self.sheets["Data"]
        ws.write(0, 0, "Temperatures (live formulas — change Settings)", self.f_title)
        ws.write(1, 0, "Time is seconds from each recording's ignition frame. Grey italic = outside the "
                       "calibrated range (extrapolated); red = saturated sensor. #N/A = no data.", self.f_note)
        if any(layout.roi.mode == "mean" for layout in self.layouts):
            ws.write(2, 0, "Area means are the temperature of each area's mean signal (mean counts), not the mean "
                           "of its pixels' temperatures; the two agree where the area's temperature is even.",
                     self.f_note)
        ws.write(3, 0, "Time (s)", self.f_head)
        ws.write(3, 1, "Time (min)", self.f_head)
        for layout in self.layouts:
            cols = sorted(list(layout.data_cols.values()) + [layout.status_col])
            ws.merge_range(3, cols[0], 3, cols[-1], layout.title, self.f_group) if len(cols) > 1 \
                else ws.write(3, cols[0], layout.title, self.f_group)
        ws.write(4, 0, "from ignition", self.f_head)
        ws.write(4, 1, "from ignition", self.f_head)
        for layout in self.layouts:
            for stat, col in layout.data_cols.items():
                label = STAT_LABELS.get(stat, "Spread (max−min)" if stat == "spread" else stat)
                ws.write_formula(4, col, f'="{label} ("&UNIT&")"', self.f_head, f"{label} ({self.unit})")
            ws.write(4, layout.status_col, "Status", self.f_head)
        cached = {layout.number: self._cached_temperatures(layout) for layout in self.layouts}
        self._cached = cached
        for i in range(self.rows):
            self._check(i)
            r = HEADER_ROWS + i
            ws.write_number(r, 0, float(self.times[i]), self.f_time)
            ws.write_formula(r, 1, f"=A{r + 1}/60", self.f_min, float(self.times[i]) / 60.0)
            for layout in self.layouts:
                self._data_row(ws, layout, i, r, cached[layout.number])
        # formats: temperatures by status
        for layout in self.layouts:
            status = xl_col_to_name(layout.status_col)
            for col in layout.data_cols.values():
                area = (HEADER_ROWS, col, self.last, col)
                ws.conditional_format(*area, {"type": "formula",
                                              "criteria": f'=${status}{HEADER_ROWS + 1}="Saturated"',
                                              "format": self.f_red})
                ws.conditional_format(*area, {"type": "formula",
                                              "criteria": f'=OR(${status}{HEADER_ROWS + 1}="Below range",'
                                                          f'${status}{HEADER_ROWS + 1}="Above range")',
                                              "format": self.f_grey})
        ws.set_column(0, 1, 11)
        ws.set_column(2, self.data_width, 12)
        ws.freeze_panes(HEADER_ROWS, 2)
        self.time_range = _range("Data", HEADER_ROWS, 0, self.last, 0)
        self.wb.define_name("TIME", "=" + self.time_range)
        for layout in self.layouts:
            col = layout.data_cols[layout.main]
            self.wb.define_name(f"{layout.name}_T", "=" + _range("Data", HEADER_ROWS, col, self.last, col))
            self.wb.define_name(f"{layout.name}_S", "=" + _range("Data", HEADER_ROWS, layout.status_col,
                                                                  self.last, layout.status_col))
            count = layout.count_cols[layout.main]  # spot value or mean count: what TC Compare matches
            self.wb.define_name(f"{layout.name}_CNT", "=" + _range("Counts", HEADER_ROWS, count, self.last, count))

    def _cached_temperatures(self, layout: _RoiLayout) -> dict[str, np.ndarray]:
        cal = layout.source.report.calibration if layout.source.report.verified else None
        roi = layout.roi
        rows = self.rows
        out = {stat: np.full(rows, np.nan) for stat in layout.data_cols}
        if cal is None:
            return out
        params = self._params(layout)
        a, b = _unit_affine(self.unit)
        for stat in roi.counts:
            out[stat] = object_temperature(cal, params, roi.counts[stat]) * a + b
        present = np.isfinite(roi.status) & (roi.status >= 0)
        if roi.mode == "pixels":
            kelvin = object_temperature(cal, params, roi.block[present])
            out["mean"][present] = np.mean(kelvin, axis=1) * a + b
        elif roi.mode == "bins":
            bins = roi.block.shape[1] // 2
            n, m = roi.block[present, :bins], roi.block[present, bins:]
            kelvin = object_temperature(cal, params, m)
            with np.errstate(invalid="ignore", divide="ignore"):
                out["mean"][present] = np.sum(n * kelvin, axis=1) / np.sum(n, axis=1) * a + b
        if "spread" in out:
            out["spread"] = (out["max"] - out["min"])
        return out

    def _data_row(self, ws, layout: _RoiLayout, i: int, r: int, cached: dict) -> None:
        roi = layout.roi
        status = int(roi.status[i])
        if status >= 0:
            ws.write_string(r, layout.status_col, STATUS_LABELS[status] if layout.source.report.verified
                            else ("Saturated" if status == 3 else "Counts only"))
        if not layout.source.report.verified:
            return  # temperatures cannot be reproduced; counts only
        for stat, col in layout.data_cols.items():
            value = cached[stat][i]
            value = float(value) if np.isfinite(value) else "#N/A"
            if stat == "spread":
                mx, mn = xl_rowcol_to_cell(r, layout.data_cols["max"]), xl_rowcol_to_cell(r, layout.data_cols["min"])
                ws.write_formula(r, col, f"=IFERROR({mx}-{mn},NA())", self.f_temp, value)
            elif stat == "mean" and roi.mode in ("pixels", "bins"):
                ws.write_formula(r, col, self._direct(self._mean_formula(layout, r)), self.f_temp, value)
            else:
                ref = _cell("Counts", r, layout.count_cols[stat])
                ws.write_formula(r, col, self._direct(self._scalar_formula(layout, ref)), self.f_temp, value)

    # --- Counts / Pixels ------------------------------------------------------------------

    def _counts(self) -> None:
        ws = self.sheets["Counts"]
        ws.write(0, 0, "Raw counts as stored in the recordings (never edited)", self.f_title)
        ws.write(1, 0, "Frame numbers are 1-based as shown in the player. Area statistics are over the "
                       "ROI's pixels.", self.f_note)
        ws.write(3, 0, "Time (s)", self.f_head)
        for s_index, source in enumerate(self.data.sources):
            c = 1 + 2 * s_index
            ws.merge_range(3, c, 3, c + 1, source.label, self.f_group)
        for layout in self.layouts:
            cols = sorted(layout.count_cols.values())
            ws.merge_range(3, cols[0], 3, cols[-1], layout.title, self.f_group) if len(cols) > 1 \
                else ws.write(3, cols[0], layout.title, self.f_group)
        ws.write(4, 0, "from ignition", self.f_head)
        for s_index, source in enumerate(self.data.sources):
            c = 1 + 2 * s_index
            ws.write(4, c, "Frame", self.f_head)
            ws.write(4, c + 1, "Camera clock", self.f_head)
        for layout in self.layouts:
            for stat, col in layout.count_cols.items():
                ws.write(4, col, "Pixels" if stat == "pixels" else f"{STAT_LABELS[stat]} (counts)", self.f_head)
        for i in range(self.rows):
            self._check(i)
            r = HEADER_ROWS + i
            ws.write_number(r, 0, float(self.times[i]), self.f_time)
            for s_index, source in enumerate(self.data.sources):
                frame = int(source.frames[i])
                if frame >= 0:
                    ws.write_number(r, 1 + 2 * s_index, frame + 1, self.f_int)
                    stamp = source.clock[i]
                    if stamp is not None:
                        ws.write_datetime(r, 2 + 2 * s_index, stamp.replace(tzinfo=None), self.f_clock)
            for layout in self.layouts:
                roi = layout.roi
                if roi.status[i] < 0:
                    continue
                for stat, col in layout.count_cols.items():
                    if stat == "pixels":
                        ws.write_number(r, col, roi.pixels, self.f_int)
                    else:
                        ws.write_number(r, col, float(roi.counts[stat][i]), self.f_count)
        ws.set_column(0, 0, 11)
        for s_index in range(len(self.data.sources)):
            ws.set_column(1 + 2 * s_index, 1 + 2 * s_index, 8)
            ws.set_column(2 + 2 * s_index, 2 + 2 * s_index, 23)
        ws.freeze_panes(HEADER_ROWS, 1)

    def _pixels(self) -> None:
        ws = self.sheets["Pixels"]
        ws.write(0, 0, "Per-pixel counts of area ROIs (used for exact mean temperatures)", self.f_title)
        ws.write(1, 0, "ROIs above the pixel limit store bins of equal apparent-temperature width instead: "
                       "the pixel count of each bin (n), then its mean count (c).", self.f_note)
        ws.write(3, 0, "Time (s)", self.f_head)
        for layout in self.layouts:
            roi = layout.roi
            if roi.block is None:
                continue
            width = roi.block.shape[1]
            ws.merge_range(3, layout.pixel_col, 3, layout.pixel_col + width - 1, layout.title, self.f_group) \
                if width > 1 else ws.write(3, layout.pixel_col, layout.title, self.f_group)
        for layout in self.layouts:
            roi = layout.roi
            if roi.block is None:
                continue
            width = roi.block.shape[1]
            if roi.mode == "bins":
                bins = width // 2
                for k in range(bins):
                    ws.write(4, layout.pixel_col + k, f"n{k + 1}", self.f_head)
                    ws.write(4, layout.pixel_col + bins + k, f"c{k + 1}", self.f_head)
            else:
                for k in range(width):
                    ws.write(4, layout.pixel_col + k, f"p{k + 1}", self.f_head)
        for i in range(self.rows):
            self._check(i)
            r = HEADER_ROWS + i
            ws.write_number(r, 0, float(self.times[i]), self.f_time)
            for layout in self.layouts:
                roi = layout.roi
                if roi.block is None or roi.status[i] < 0:
                    continue
                values = roi.block[i]
                for k, value in enumerate(values):
                    if np.isfinite(value):
                        ws.write_number(r, layout.pixel_col + k, float(value))
        ws.freeze_panes(HEADER_ROWS, 1)
        ws.hide()

    # --- TC Compare --------------------------------------------------------------------------

    def _tc(self) -> None:
        ws = self.sheets["TC Compare"]
        T = "TC Compare"
        last_tc = TC_FIRST + TC_CAPACITY
        tct = _range(T, TC_FIRST + 1, 0, last_tc, 0)
        tcv = _range(T, TC_FIRST + 1, 1, last_tc, TC_COLUMNS)
        ws.write(0, 0, "Thermocouple comparison", self.f_title)
        ws.write(1, 0, "1. Paste your logger data below the yellow header: time in seconds from ignition in "
                       "column A, one thermocouple per column (°C). Rename the headers to your TC names.",
                 self.f_note)
        ws.write(2, 0, "2. On Settings, pick the TC column for each ROI. 3. Tune emissivity; the charts, the "
                       "difference and the matching emissivity update.", self.f_note)
        ws.write(3, 0, "Matching emissivity = the emissivity that makes IR equal the TC. Above 1 means no "
                       "emissivity can: the camera sees more radiation than a blackbody at the TC "
                       "temperature (flames/hot gas in view, the ROI, or the TC contact).", self.f_note)
        ws.write(4, 0, "For area ROIs it is computed from the mean radiance (mean counts), not the mean "
                       "temperature; the two differ when the area spans large temperature differences.",
                 self.f_note)
        ws.write(5, 0, "Logger time offset (s)", self.f_bold)
        prefill = self.data.options.tc_prefill
        ws.write_number(5, 1, prefill.offset_s if prefill else 0.0, self.f_input_num)
        self._name("TC_OFFSET", T, 5, 1)
        # comparison block (to the right), one row per Data row
        c0 = TC_COLUMNS + 2
        # constant-memory sheet: every row is written before the next one
        self.tc_layouts = [layout for layout in self.layouts if self._has_tc(layout)]
        self._refs["MATCH_FROM"], self._refs["MATCH_TO"] = _abs(MATCH, 8, 1), _abs(MATCH, 9, 1)
        if self._tc_only_paired and len(self.tc_layouts) < len(self.layouts):
            ws.write(5, 3, "Only the ROIs the TC fit paired with a TC are compared here.", self.f_note)
        ws.write(6, 0, "Use (yellow, 1 or 0) picks each ROI's rows for the Emissivity Match; Fit row shows the "
                       "rows it counts (also IR and TC present, IR in the camera's calibrated range, inside its "
                       "window).", self.f_note)
        self.tc_cols = {}
        col = c0 + 3
        for layout in self.tc_layouts:
            self.tc_cols[layout.number] = {"ir": col, "tc": col + 1, "dt": col + 2, "eps": col + 3,
                                           "tcd": col + 4, "dts": col + 5, "xy": col + 6, "x2": col + 7,
                                           "use": col + 8, "fit": col + 9}
            col += TC_BLOCK
        match_cache = self._match_inputs()
        ws.write(TC_FIRST - 1, 0, "Paste logger data here (°C) ↓", self.f_bold)
        ws.write(TC_FIRST - 1, c0, "Comparison on the IR sample times", self.f_h2)
        for layout in self.tc_layouts:
            col = self.tc_cols[layout.number]["ir"]
            ws.merge_range(TC_FIRST - 1, col, TC_FIRST - 1, col + 3, layout.title, self.f_group)
        ws.write(TC_FIRST, 0, "Time (s)", self.f_input)
        for k in range(TC_COLUMNS):
            ws.write(TC_FIRST, 1 + k, self._tc_names[k] if k < len(self._tc_names) else f"TC {k + 1}",
                     self.f_input)

        ws.write(TC_FIRST, c0, "Time (s)", self.f_head)
        ws.write(TC_FIRST, c0 + 1, "Logger row", self.f_head)
        ws.write(TC_FIRST, c0 + 2, "Weight", self.f_head)
        for layout in self.tc_layouts:
            col = self.tc_cols[layout.number]["ir"]
            matching = "Matching emissivity" if layout.main == "value" else "Matching ε (mean radiance)"
            for k, label in enumerate(("IR (°C)", "TC (°C)", "IR − TC (K)", matching,
                                       "TC (display unit)", "ΔT (stats)", "x·y", "x²", "Use (1/0)", "Fit row")):
                ws.write(TC_FIRST, col + k, label, self.f_head)
        first_row = TC_FIRST + 1

        def logger_row(i: int) -> None:  # constant_memory: write with the rest of row i
            if prefill is not None and i < len(prefill.times):
                ws.write_number(first_row + i, 0, float(prefill.times[i]))
                for k, column in enumerate(prefill.columns):
                    if math.isfinite(column[i]):
                        ws.write_number(first_row + i, 1 + k, float(column[i]))

        def write(row, col, formula, fmt, value):  # row formulas cite cells, not names (see _direct)
            ws.write_formula(row, col, self._direct(formula), fmt, value)

        for i in range(self.rows):
            self._check(i)
            logger_row(i)
            r = first_row + i
            R = r + 1
            data_r = HEADER_ROWS + i
            t_cell = xl_rowcol_to_cell(r, c0)
            i_cell = xl_rowcol_to_cell(r, c0 + 1)
            w_cell = xl_rowcol_to_cell(r, c0 + 2)
            write(r, c0, f"={_cell('Data', data_r, 0)}", self.f_time, float(self.times[i]))
            write(r, c0 + 1, f"=IFERROR(MATCH({t_cell}-TC_OFFSET,{tct},1),NA())", self.f_int, "#N/A")
            write(r, c0 + 2,
                             f"=IFERROR(({t_cell}-TC_OFFSET-INDEX({tct},{i_cell}))/(INDEX({tct},{i_cell}+1)"
                             f"-INDEX({tct},{i_cell})),NA())", self.f_eps, "#N/A")
            for layout in self.tc_layouts:
                cols = self.tc_cols[layout.number]
                name, src = layout.name, layout.src
                inputs = match_cache[layout.number]
                ws.write_number(r, cols["use"], int(inputs["use"][i]), self.f_input_int)
                if not layout.source.report.verified:
                    ws.write_number(r, cols["fit"], 0, self.f_int)
                    continue
                c_cell = _cell("Counts", data_r, layout.count_cols[layout.main])
                u_cell = xl_rowcol_to_cell(r, cols["use"])
                write(r, cols["fit"],
                                 f"=IF(AND({u_cell}=1,ISNUMBER({xl_rowcol_to_cell(r, cols['tc'])}),ISNUMBER({c_cell}),"
                                 f"{c_cell}>={src}_CNTLO,{c_cell}<={src}_CNTHI,{t_cell}>=MATCH_FROM,"
                                 f"{t_cell}<=MATCH_TO),1,0)", self.f_int, int(inputs["fit"][i]))
                data_col = layout.data_cols[layout.main]
                ir = xl_rowcol_to_cell(r, cols["ir"])
                tc = xl_rowcol_to_cell(r, cols["tc"])
                write(r, cols["ir"], f"=IFERROR(({_cell('Data', data_r, data_col)}-UNIT_B)/UNIT_A"
                                                f"-273.15,NA())", self.f_temp, "#N/A")
                v1 = f"INDEX({tcv},{i_cell},{name}_TCCOL)"
                v2 = f"INDEX({tcv},{i_cell}+1,{name}_TCCOL)"
                t1, t2 = f"INDEX({tct},{i_cell})", f"INDEX({tct},{i_cell}+1)"
                # an exact logger time needs no neighbour; otherwise interpolate
                # between two valid samples with increasing times
                write(r, cols["tc"],
                                 f"=IF(ISNUMBER({name}_TCCOL),IFERROR(IF({t1}={t_cell}-TC_OFFSET,"
                                 f"IF(ISNUMBER({v1}),{v1},NA()),IF(AND(ISNUMBER({v1}),ISNUMBER({v2}),"
                                 f"ISNUMBER({t2}),{t2}>{t1}),{v1}+{w_cell}*({v2}-{v1}),NA())),NA()),NA())",
                                 self.f_temp, "#N/A")
                write(r, cols["dt"], f"=IFERROR({ir}-{tc},NA())", self.f_temp, "#N/A")
                count_col = layout.count_cols[layout.main]
                c_ref = _cell("Counts", data_r, count_col)
                s_tc = f"({src}_R/(EXP({src}_B/({tc}+273.15))-{src}_F)-{src}_O)"
                y = f"({name}_A*{c_ref}-{name}_C-{name}_SREFL)"
                x = f"({s_tc}-{name}_SREFL)"
                both = f"AND(ISNUMBER({c_ref}),ISNUMBER({tc}))"  # an IR and a TC sample
                write(r, cols["eps"], f"=IF({both},IFERROR({y}/{x},NA()),NA())", self.f_eps, "#N/A")
                write(r, cols["tcd"], f"=IFERROR(({tc}+273.15)*UNIT_A+UNIT_B,NA())", self.f_temp,
                                 "#N/A")
                write(r, cols["dts"], f'=IFERROR({ir}-{tc},"")', None, "")
                write(r, cols["xy"], f'=IF({both},IFERROR({x}*{y},""),"")', None, "")
                write(r, cols["x2"], f'=IF({both},IFERROR({x}^2,""),"")', None, "")
        for i in range(self.rows, len(prefill.times) if prefill is not None else 0):
            self._check(i)
            logger_row(i)
        for layout in self.tc_layouts:
            cols = self.tc_cols[layout.number]
            last = first_row + self.rows - 1
            for key in ("tc", "dt", "eps", "dts", "xy", "x2", "use", "fit"):
                self.wb.define_name(f"{layout.name}_{key.upper()}R",
                                    "=" + _range(T, first_row, cols[key], last, cols[key]))
            ws.conditional_format(first_row, cols["eps"], last, cols["eps"],
                                  {"type": "cell", "criteria": ">", "value": 1, "format": self.f_red})
            ws.data_validation(first_row, cols["use"], last, cols["use"],
                               {"validate": "integer", "criteria": "between", "minimum": 0, "maximum": 1,
                                "error_message": "Use is 1 (count this row) or 0 (leave it out)"})
            ws.set_column(cols["tcd"], cols["x2"], None, None, {"hidden": True})
            ws.set_column(cols["use"], cols["fit"], 8)
        self.wb.define_name("TC_TIME", "=" + _range(T, first_row, c0, first_row + self.rows - 1, c0))
        ws.set_column(0, 0, 22)
        ws.set_column(1, TC_COLUMNS, 11)
        ws.set_column(c0, c0 + 2, 10)
        ws.freeze_panes(TC_FIRST + 1, 0)

    # --- Emissivity Match ----------------------------------------------------------------------

    def _tc_at(self, layout: _RoiLayout) -> np.ndarray:
        """The ROI's TC (°C) at the IR sample times, as TC Compare interpolates it (NaN: none)."""
        out = np.full(self.rows, np.nan)
        prefill = self.data.options.tc_prefill
        tc = self._roi_tc.get(layout.roi.shape.name)
        if prefill is None or tc is None or tc not in prefill.names:
            return out
        lt = np.asarray(prefill.times, dtype=np.float64)
        lv = np.asarray(prefill.columns[prefill.names.index(tc)], dtype=np.float64)
        if not lt.size:
            return out
        tau = self.times - prefill.offset_s
        idx = np.searchsorted(lt, tau, side="right") - 1  # MATCH(τ, times, 1): last time ≤ τ
        valid = idx >= 0
        i = np.clip(idx, 0, lt.size - 1)
        j = np.clip(idx + 1, 0, lt.size - 1)
        exact = valid & (lt[i] == tau)
        with np.errstate(invalid="ignore", divide="ignore"):
            between = (valid & ~exact & (idx + 1 < lt.size) & np.isfinite(lv[i]) & np.isfinite(lv[j])
                       & (lt[j] > lt[i]))
            w = (tau - lt[i]) / np.where(lt[j] > lt[i], lt[j] - lt[i], 1.0)
            out[exact] = lv[i][exact]
            out[between] = (lv[i] + w * (lv[j] - lv[i]))[between]
        return out

    def _match_inputs(self) -> dict[int, dict]:
        """Per ROI: Use and Fit row flags, its TC at the IR times and its matched counts."""
        prefill = self.data.options.tc_prefill
        window = (float(self.times[0]), float(self.times[-1]))
        out = {}
        for layout in self.layouts:
            if not self._has_tc(layout):
                continue
            use = prefill.use_flags(layout.roi.shape.name, self.times) if prefill is not None else None
            use = np.ones(self.rows, dtype=np.int8) if use is None else use
            tc = self._tc_at(layout)
            cnt = np.asarray(layout.roi.counts[layout.main], dtype=np.float64)
            low, high = self._count_range(layout.source)
            with np.errstate(invalid="ignore"):
                fit = ((use == 1) & np.isfinite(tc) & np.isfinite(cnt) & (cnt >= low) & (cnt <= high)
                       & (self.times >= window[0]) & (self.times <= window[1]))
            if not layout.source.report.verified:
                fit[:] = False
            out[layout.number] = {"use": use, "fit": fit.astype(np.int8), "tc": tc, "cnt": cnt}
        self._match_cache = out
        return out

    def _match_reference(self, layouts: list[_RoiLayout], eps: list[float]) -> dict:
        """Python results of the Match formulas for the initial settings (same model as fit_eps)."""
        prefill = self.data.options.tc_prefill
        exclude = set(prefill.match_exclude) if prefill is not None else set()
        rows: dict[int, dict] = {}
        for layout in layouts:
            inputs = self._match_cache[layout.number]
            fit = inputs["fit"] == 1
            cal = layout.source.report.calibration
            p = self._params(layout)
            entry = {"rows": int(fit.sum()), "include": layout.roi.shape.name not in exclude,
                     "eps_now": p.emissivity, "err": [math.nan] * len(eps)}
            entry["err_now"] = entry["bias_now"] = entry["own"] = math.nan
            entry["grid"] = [math.nan] * len(OWN_GRID)
            entry["probe"] = (math.nan, math.nan)
            if entry["rows"]:
                c, t = inputs["cnt"][fit], inputs["tc"][fit]

                def stats(e: float, cal=cal, p=p, c=c, t=t) -> tuple[float, float]:  # bound: kept past the loop
                    d = object_temperature(cal, p.with_(emissivity=e), c) - KELVIN - t
                    d = d[np.isfinite(d)]
                    return (float(np.sqrt(np.mean(d ** 2))), float(np.mean(d))) if d.size else (math.nan, math.nan)

                entry["err_now"], entry["bias_now"] = stats(p.emissivity)
                if entry["include"]:
                    entry["err"] = [stats(e)[0] for e in eps]
                entry["grid"] = [stats(e)[0] for e in OWN_GRID]
                finite = [k for k, v in enumerate(entry["grid"]) if math.isfinite(v)]
                if finite:
                    entry["own"] = OWN_GRID[min(finite, key=lambda k: entry["grid"][k])]
                entry["stats"] = stats
            rows[layout.number] = entry
        combined = []
        for k in range(len(eps)):
            errs = [rows[l.number]["err"][k] for l in layouts if math.isfinite(rows[l.number]["err"][k])]
            combined.append(float(np.sqrt(np.mean(np.square(errs)))) if errs else math.nan)
        finite = [k for k, v in enumerate(combined) if math.isfinite(v)]
        best = min(finite, key=lambda k: combined[k]) if finite else None
        now = [rows[l.number]["err_now"] for l in layouts
               if rows[l.number]["include"] and math.isfinite(rows[l.number]["err_now"])]
        own = [rows[l.number]["own"] for l in layouts
               if rows[l.number]["include"] and math.isfinite(rows[l.number]["own"])]
        probes = [math.nan, math.nan]  # the error over all TCs at the best value − PROBE and + PROBE
        for layout in layouts:
            entry = rows[layout.number]
            entry["bias_best"] = (entry["stats"](eps[best])[1] if best is not None and entry["include"]
                                  and entry["rows"] else math.nan)
        if best is not None:
            for side, delta in enumerate((-PROBE, PROBE)):
                errs = []
                for layout in layouts:
                    entry = rows[layout.number]
                    if entry["include"] and entry["rows"]:
                        value = entry["stats"](eps[best] + delta)[0]
                        entry["probe"] = tuple(value if k == side else entry["probe"][k] for k in range(2))
                        if math.isfinite(value):
                            errs.append(value)
                probes[side] = float(np.sqrt(np.mean(np.square(errs)))) if errs else math.nan
        return {"rows": rows, "combined": combined, "best": best,
                "now": float(np.sqrt(np.mean(np.square(now)))) if now else math.nan, "own": own,
                "probes": probes}

    def _match(self) -> None:
        ws = self.sheets[MATCH]
        M = MATCH
        layouts = [layout for layout in self.tc_layouts if layout.source.report.verified]
        lo, hi, step = MATCH_DEFAULTS
        eps = _candidates(lo, hi, step)
        ref = self._match_reference(layouts, eps)
        ws.write(0, 0, "Emissivity match: one emissivity for every TC zone", self.f_title)
        ws.write(1, 0, "The emissivity between the limits below at which the IR of the ROIs with a TC agrees best "
                       "with their TCs. Error = root mean square of IR − TC (K); every TC counts the same.",
                 self.f_note)
        ws.write(2, 0, "Rows counted: Use = 1 on TC Compare (yellow), IR and TC both present, the ROI's mean "
                       "signal inside the camera's calibrated range, inside the window. Pick each ROI's TC on "
                       "Settings.", self.f_note)
        ws.write(3, 0, "When you have the value, type it as the emissivity on Settings: every ROI without its own "
                       "emissivity there, and every chart and summary, follows.", self.f_note)
        if any(layout.roi.shape.name in self._roi_eps for layout in layouts):
            ws.write(4, 0, "Some ROIs start with their own emissivity on Settings (the TC fit's values), which comes "
                           "before the common one: clear those cells to let one emissivity drive them.", self.f_note)
        t0, t1 = float(self.times[0]), float(self.times[-1])
        inputs = (("MATCH_LO", "Lowest emissivity", lo), ("MATCH_HI", "Highest emissivity", hi),
                  ("MATCH_STEP", "Step", step), ("MATCH_FROM", "Window from (s)", t0),
                  ("MATCH_TO", "Window to (s)", t1))
        for k, (name, label, value) in enumerate(inputs):
            ws.write(5 + k, 0, label)
            ws.write_number(5 + k, 1, value, self.f_input_num)
            self._name(name, M, 5 + k, 1)
        ws.data_validation(5, 1, 7, 1, {"validate": "decimal", "criteria": "between", "minimum": 0.001,
                                        "maximum": 1.0, "error_message": "Emissivity values lie in (0, 1]"})
        # the table has MATCH_CANDIDATES rows: a step too fine to reach the highest value is widened
        ws.write_formula(7, 2, f"=MAX(MATCH_STEP,(MATCH_HI-MATCH_LO)/{MATCH_CANDIDATES - 1})", self.f_used,
                         _step_used(lo, hi, step))
        self._name("MATCH_STEPUSED", M, 7, 2)
        n = len(layouts)
        p0 = 12  # first ROI row of the pairs table (columns A-J)
        c0, cc = p0, 11  # first candidate row and column (L): the candidate table sits to the right
        last_c = c0 + MATCH_CANDIDATES - 1
        chart_row = p0 + max(n, 1) + 1  # the chart sits under the pairs table
        # hidden helper rows (the best value ± PROBE, each ROI's bias there, the own-value grid) start below the
        # pairs table, the candidate table and the chart, so hiding them hides nothing else
        h0 = max(last_c + 2, chart_row + CHART_ROWS + 1)
        g0, g1 = h0 + 4, h0 + 3 + len(OWN_GRID)
        grid_eps = _range(M, g0, cc, g1, cc)
        eps_range = _range(M, c0, cc, last_c, cc)
        all_range = _range(M, c0, cc + 1, last_c, cc + 1)
        self.wb.define_name("MATCH_EPS", "=" + eps_range)
        self.wb.define_name("MATCH_ALL", "=" + all_range)
        self._name("MATCH_BEST", M, 5, 6)

        # result block
        best = ref["best"]
        best_eps = eps[best] if best is not None else "–"
        # labels across D:F, values in G (D is the pairs table's narrow Include column)
        ws.merge_range(5, 3, 5, 5, "Best emissivity within the limits", self.f_bold_left)
        ws.write_formula(5, 6, "=IFERROR(INDEX(MATCH_EPS,MATCH(_xlfn.AGGREGATE(5,6,MATCH_ALL),MATCH_ALL,0)),\"–\")",
                         self.f_best, best_eps)
        ws.merge_range(6, 3, 6, 5, "Error there, all TCs (K)", self.f_left)
        self._name("MATCH_ERRBEST", M, 6, 6)
        ws.write_formula(6, 6, '=IFERROR(_xlfn.AGGREGATE(5,6,MATCH_ALL),"–")', self.f_k,
                         ref["combined"][best] if best is not None else "–")
        ws.merge_range(7, 3, 7, 5, "Error with the emissivity on Settings (K)", self.f_left)
        include = _range(M, p0, 3, p0 + max(n, 1) - 1, 3)
        now = _range(M, p0, 6, p0 + max(n, 1) - 1, 6)
        ws.write_array_formula(7, 6, 7, 6,
                               f'{{=IFERROR(SQRT(SUM(IF(({include}="Yes")*ISNUMBER({now}),{now}^2))/'
                               f'SUM(({include}="Yes")*ISNUMBER({now}))),"–")}}', self.f_k,
                               ref["now"] if math.isfinite(ref["now"]) else "–")
        none_text = ("No ROI has rows to compare yet: pick each zone's TC on Settings, and check Use (TC Compare) "
                     "and the window.")
        one_text = "Only one emissivity is tried: the lowest and highest emissivity are the same."
        near_low = ("Inside the limits: the best match lies between the lowest candidates (a finer step shows "
                    "it).")
        near_high = ("Inside the limits: the best match lies between the highest candidates (a finer step shows "
                     "it).")
        # at a limit, the error just inside it says whether the best match may lie beyond the limit; the signs
        # of the ROIs' mean IR − TC there say whether the zones read hotter or colder
        side = (f'IF(COUNTIF(MATCH_BIASROW,">0")=0,"{NOTE_COLD}",IF(COUNTIF(MATCH_BIASROW,"<0")=0,"{NOTE_HOT}",'
                f'"{NOTE_MIXED}"))')
        ws.write_formula(8, 3, f'=IF(NOT(ISNUMBER(MATCH_BEST)),"{none_text}",IF(MATCH_HI-MATCH_LO<1E-9,"{one_text}",'
                               f'IF(MATCH_BEST<=MATCH_LO+1E-9,IF(IFERROR(MATCH_PLUS<MATCH_ERRBEST,FALSE),'
                               f'"{near_low}","{NOTE_LOW}"&{side}),IF(MATCH_BEST>=MATCH_HI-1E-9,'
                               f'IF(IFERROR(MATCH_MINUS<MATCH_ERRBEST,FALSE),"{near_high}","{NOTE_HIGH}"&{side}),'
                               f'"Inside the limits."))))', self.f_note,
                         self._limit_note(ref, eps, lo, hi, none_text, one_text, near_low, near_high, layouts))
        own = _range(M, p0, 8, p0 + max(n, 1) - 1, 8)
        mask = f'({include}="Yes")*ISNUMBER({own})'
        spread_cached = ""
        if len(ref["own"]) > 1 and max(ref["own"]) - min(ref["own"]) > 0.1:
            spread_cached = (f"The TCs' own values run from {min(ref['own']):.2f} to {max(ref['own']):.2f}: one "
                             "emissivity cannot fit them all. Check the paint of each zone, flames on the TCs, and "
                             "which zone each TC is in.")
        ws.write_array_formula(
            9, 3, 9, 3,
            f'{{=IFERROR(IF(MAX(IF({mask},{own}))-MIN(IF({mask},{own}))>0.1,"The TCs\' own values run from "&'
            f'FIXED(MIN(IF({mask},{own})),2)&" to "&FIXED(MAX(IF({mask},{own})),2)&": one emissivity '
            f'cannot fit them all. Check the paint of each zone, flames on the TCs, and which zone each TC is in.",'
            f'""),"")}}', self.f_note, spread_cached)

        # pairs table
        head = ["ROI", "Recording", "TC (Settings)", "Include", "Rows used", "Emissivity now", "Error now (K)",
                "Bias now (K, IR − TC)", "Own best value (0.05 to 1.00)", "Error at the best value (K)"]
        ws.write(p0 - 2, 0, "ROIs and their TCs", self.f_h2)
        for c, text in enumerate(head):
            ws.write(p0 - 1, c, text, self.f_head)
        for k, layout in enumerate(layouts):
            r = p0 + k
            R = r + 1
            nm = layout.name
            e = ref["rows"][layout.number]
            ws.write(r, 0, layout.roi.shape.name)
            ws.write(r, 1, layout.source.label)
            mapped = self._roi_tc.get(layout.roi.shape.name)
            ws.write_formula(r, 2, f'=IF({nm}_TC="","no TC",{nm}_TC)', None, mapped or "no TC")
            ws.write(r, 3, "Yes" if e["include"] else "No", self.f_input)
            ws.write_formula(r, 4, f"=SUM({nm}_FITR)", self.f_int, e["rows"])
            ws.write_formula(r, 5, f"={nm}_EPS", self.f_eps, e["eps_now"])
            ws.write_array_formula(r, 6, r, 6, f'{{=IF(N(E{R})=0,"–",IFERROR({self._rms(layout, f"{nm}_EPS")},"–"))}}',
                                   self.f_k, e["err_now"] if math.isfinite(e["err_now"]) else "–")
            ws.write_array_formula(r, 7, r, 7, f'{{=IF(N(E{R})=0,"–",IFERROR({self._bias(layout, f"{nm}_EPS")},"–"))}}',
                                   self.f_k, e["bias_now"] if math.isfinite(e["bias_now"]) else "–")
            grid_col = _range(M, g0, cc + 2 + k, g1, cc + 2 + k)
            ws.write_formula(r, 8, f'=IF(N(E{R})=0,"–",IFERROR(INDEX({grid_eps},MATCH(_xlfn.AGGREGATE(5,6,'
                                   f'{grid_col}),{grid_col},0)),"–"))', self.f_eps,
                             e["own"] if math.isfinite(e["own"]) else "–")
            col = _range(M, c0, cc + 2 + k, last_c, cc + 2 + k)
            at_best = e["err"][best] if best is not None and math.isfinite(e["err"][best]) else "–"
            ws.write_formula(r, 9, f'=IF(N(E{R})=0,"–",IFERROR(INDEX({col},MATCH(MATCH_BEST,MATCH_EPS,0)),"–"))',
                             self.f_k, at_best)
        if n:
            ws.data_validation(p0, 3, p0 + n - 1, 3, {"validate": "list", "source": ["Yes", "No"]})
            ws.conditional_format(p0, 8, p0 + n - 1, 8, {"type": "cell", "criteria": ">", "value": 1,
                                                          "format": self.f_red})

        # candidate table, right of the pairs table (its columns can be hidden without hiding pairs)
        ws.write(c0 - 2, cc, "Error by emissivity (K)", self.f_h2)
        ws.write(c0 - 1, cc, "Emissivity", self.f_head)
        ws.write(c0 - 1, cc + 1, "All TCs", self.f_head)
        for k, layout in enumerate(layouts):
            ws.write(c0 - 1, cc + 2 + k, layout.title, self.f_head)
        letter = xl_col_to_name(cc)
        for j in range(MATCH_CANDIDATES):
            r = c0 + j
            R = r + 1
            value = eps[j] if j < len(eps) else "#N/A"
            candidate = ("=IF(MATCH_HI>=MATCH_LO,MATCH_LO,NA())" if j == 0 else
                         f"=IF(MATCH_LO+{j - 1}*MATCH_STEPUSED<MATCH_HI-1E-9,"
                         f"MIN(MATCH_LO+{j}*MATCH_STEPUSED,MATCH_HI),NA())")
            ws.write_formula(r, cc, candidate, self.f_eps, value)
            row_cells = _range(M, r, cc + 2, r, cc + 1 + max(n, 1))
            combined = ref["combined"][j] if j < len(eps) and math.isfinite(ref["combined"][j]) else "#N/A"
            ws.write_array_formula(r, cc + 1, r, cc + 1,
                                   f"{{=IFERROR(SQRT(SUM(IFERROR({row_cells}^2,0))/COUNT({row_cells})),NA())}}",
                                   self.f_k, combined)
            for k, layout in enumerate(layouts):
                pr = p0 + k + 1  # Excel row of the ROI in the pairs table
                e_cell = f"${letter}{R}"
                err = ref["rows"][layout.number]["err"][j] if j < len(eps) else math.nan
                ws.write_array_formula(
                    r, cc + 2 + k, r, cc + 2 + k,
                    f'{{=IF(OR(NOT(ISNUMBER({e_cell})),$D${pr}<>"Yes",N($E${pr})=0),NA(),'
                    f'IFERROR({self._rms(layout, e_cell)},NA()))}}', self.f_k, err if math.isfinite(err) else "#N/A")
        area = (c0, cc, last_c, cc + 1 + max(n, 1))
        ws.conditional_format(*area, {"type": "formula", "criteria": f"=ISNA({letter}{c0 + 1})",
                                      "format": self.f_hide})
        self._match_helpers(ws, layouts, ref, eps, p0, cc, h0, g0)
        best_col = xl_col_to_name(cc + 1)
        ws.conditional_format(c0, cc + 1, last_c, cc + 1, {
            "type": "formula", "criteria": f"=AND(ISNUMBER({best_col}{c0 + 1}),{best_col}{c0 + 1}=MATCH_ALL_MIN)",
            "format": self.f_pass})
        self.wb.define_name("MATCH_ALL_MIN", "=_xlfn.AGGREGATE(5,6,MATCH_ALL)")
        if any(self._roi_tc.get(layout.roi.shape.name) for layout in layouts) and not all(
                self._roi_tc.get(layout.roi.shape.name) for layout in layouts):
            ws.write(c0 - 2, cc + 3, "Columns of ROIs without a TC at export are hidden; unhide them after "
                                     "picking a TC.", self.f_note)

        # chart: error against emissivity, below the pairs table
        chart = self.wb.add_chart({"type": "scatter", "subtype": "straight_with_markers"})
        chart.set_title({"name": "Error against emissivity", "name_font": {"size": 12}})
        chart.set_x_axis({"name": "Emissivity", "num_format": "0.00",
                          "major_gridlines": {"visible": True, "line": {"color": "#E0E0E0"}}})
        chart.set_y_axis({"name": "RMS of IR − TC (K)", "num_format": "0",
                          "major_gridlines": {"visible": True, "line": {"color": "#E0E0E0"}}})
        chart.set_legend({"position": "bottom"})
        chart.set_size({"width": 760, "height": 380})
        x = [M, c0, cc, last_c, cc]
        chart.add_series({"name": "All TCs", "categories": x, "values": [M, c0, cc + 1, last_c, cc + 1],
                          "line": {"width": 2.5, "color": INK}, "marker": {"type": "circle", "size": 6,
                                                                           "fill": {"color": INK},
                                                                           "border": {"color": INK}}})
        shown = [(k, layout) for k, layout in enumerate(layouts) if self._roi_tc.get(layout.roi.shape.name)]
        if not shown:  # TCs are picked later in Excel: every ROI's series (empty until then)
            shown = list(enumerate(layouts))
        for j, (k, layout) in enumerate(shown):
            color = SERIES[j % len(SERIES)]
            chart.add_series({"name": [M, c0 - 1, cc + 2 + k], "categories": x,
                              "values": [M, c0, cc + 2 + k, last_c, cc + 2 + k],
                              "line": {"width": 1.5, "color": color},
                              "marker": {"type": "circle", "size": 5, "fill": {"color": color},
                                         "border": {"color": color}}})
        ws.insert_chart(chart_row, 0, chart)
        ws.set_column(0, 0, 22)
        ws.set_column(1, 2, 16)
        ws.set_column(3, 3, 9)
        ws.set_column(4, 5, 11)
        ws.set_column(6, 9, 14)
        ws.set_column(cc, cc + 1, 12)
        paired = [bool(self._roi_tc.get(layout.roi.shape.name)) for layout in layouts]
        for k, has in enumerate(paired):  # ROIs without a TC at export are hidden, unless none has one yet
            ws.set_column(cc + 2 + k, cc + 2 + k, 14, None, {"hidden": True} if any(paired) and not has else {})

    def _limit_note(self, ref, eps, lo, hi, none_text, one_text, near_low, near_high, layouts) -> str:
        """The cached result of the limit note."""
        best = ref["best"]
        if best is None:
            return none_text
        if hi - lo < 1e-9:
            return one_text
        error = ref["combined"][best]
        minus, plus = ref["probes"]
        biases = [ref["rows"][layout.number]["bias_best"] for layout in layouts]
        biases = [b for b in biases if math.isfinite(b)]
        side = (NOTE_COLD if not any(b > 0 for b in biases) else NOTE_HOT if not any(b < 0 for b in biases)
                else NOTE_MIXED)
        if eps[best] <= lo + 1e-9:
            return near_low if math.isfinite(plus) and plus < error else NOTE_LOW + side
        if eps[best] >= hi - 1e-9:
            return near_high if math.isfinite(minus) and minus < error else NOTE_HIGH + side
        return "Inside the limits."

    def _match_helpers(self, ws, layouts, ref, eps, p0, cc, h0, g0) -> None:
        """Hidden rows under the candidate table: the error at the best value ± PROBE (which side of a limit the
        best match lies), and every ROI's error on OWN_GRID (its own value)."""
        M = MATCH
        n = len(layouts)
        ws.set_row(h0, None, None, {"hidden": True})
        ws.write(h0, cc, "Helper rows: the best value ± 0.001, each ROI's mean IR − TC there, then each ROI's "
                         "own-value search", self.f_note)
        best = ref["best"]
        r = h0 + 3
        ws.set_row(r, None, None, {"hidden": True})
        ws.write_formula(r, cc, "=MATCH_BEST", self.f_eps, eps[best] if best is not None else "–")
        bias_cells = _range(M, r, cc + 2, r, cc + 1 + max(n, 1))
        self.wb.define_name("MATCH_BIASROW", "=" + bias_cells)
        for k, layout in enumerate(layouts):
            pr = p0 + k + 1
            value = ref["rows"][layout.number]["bias_best"]
            ws.write_array_formula(
                r, cc + 2 + k, r, cc + 2 + k,
                f'{{=IF(OR(NOT(ISNUMBER(MATCH_BEST)),$D${pr}<>"Yes",N($E${pr})=0),NA(),'
                f'IFERROR({self._bias(layout, "MATCH_BEST")},NA()))}}', self.f_k,
                value if math.isfinite(value) else "#N/A")
        for side, delta in enumerate((-PROBE, PROBE)):
            r = h0 + 1 + side
            ws.set_row(r, None, None, {"hidden": True})
            sign = "-" if delta < 0 else "+"
            ws.write_formula(r, cc, f"=IF(ISNUMBER(MATCH_BEST),MATCH_BEST{sign}{PROBE},NA())", self.f_eps,
                             eps[best] + delta if best is not None else "#N/A")
            row_cells = _range(M, r, cc + 2, r, cc + 1 + max(n, 1))
            value = ref["probes"][side]
            ws.write_array_formula(r, cc + 1, r, cc + 1,
                                   f"{{=IFERROR(SQRT(SUM(IFERROR({row_cells}^2,0))/COUNT({row_cells})),NA())}}",
                                   self.f_k, value if math.isfinite(value) else "#N/A")
            self._name("MATCH_MINUS" if side == 0 else "MATCH_PLUS", M, r, cc + 1)
            e_cell = f"${xl_col_to_name(cc)}{r + 1}"
            for k, layout in enumerate(layouts):
                pr = p0 + k + 1
                err = ref["rows"][layout.number]["probe"][side]
                ws.write_array_formula(
                    r, cc + 2 + k, r, cc + 2 + k,
                    f'{{=IF(OR(NOT(ISNUMBER({e_cell})),$D${pr}<>"Yes",N($E${pr})=0),NA(),'
                    f'IFERROR({self._rms(layout, e_cell)},NA()))}}', self.f_k, err if math.isfinite(err) else "#N/A")
        for j, value in enumerate(OWN_GRID):
            r = g0 + j
            ws.set_row(r, None, None, {"hidden": True})
            ws.write_number(r, cc, value, self.f_eps)
            e_cell = f"${xl_col_to_name(cc)}{r + 1}"
            for k, layout in enumerate(layouts):
                pr = p0 + k + 1
                err = ref["rows"][layout.number]["grid"][j]
                ws.write_array_formula(
                    r, cc + 2 + k, r, cc + 2 + k,
                    f'{{=IF(N($E${pr})=0,NA(),IFERROR({self._rms(layout, e_cell)},NA()))}}', self.f_k,
                    err if math.isfinite(err) else "#N/A")

    def _t_expr(self, layout: _RoiLayout, eps: str) -> str:
        """Array of the ROI's IR temperatures (°C) at emissivity ``eps``: (A·c − C − S_r)/ε + S_r is the
        object signal, exactly K1·c − K2 of the Data formulas; #N/A outside the curve's domain."""
        nm, src = layout.name, layout.src
        v = f"(({nm}_A*{nm}_CNT-{nm}_C-{nm}_SREFL)/{eps}+{nm}_SREFL+{src}_O)"
        return f"(IF(({v}>0)*({v}<{src}_SMAX),{src}_B/LN({src}_R/{v}+{src}_F),NA())-273.15)"

    def _rms(self, layout: _RoiLayout, eps: str) -> str:
        """RMS of IR − TC over the Fit rows whose IR is defined (array formula body)."""
        nm = layout.name
        sq = f"({self._t_expr(layout, eps)}-{nm}_TCR)^2"
        return (f"SQRT(SUM(IF({nm}_FITR=1,IFERROR({sq},0),0))/"
                f"SUM(IF({nm}_FITR=1,IF(ISERROR({sq}),0,1),0)))")

    def _bias(self, layout: _RoiLayout, eps: str) -> str:
        nm = layout.name
        d = f"({self._t_expr(layout, eps)}-{nm}_TCR)"
        return f"SUM(IF({nm}_FITR=1,IFERROR({d},0),0))/SUM(IF({nm}_FITR=1,IF(ISERROR({d}),0,1),0))"

    # --- Summary -------------------------------------------------------------------------------

    def _summary(self) -> None:
        ws = self.sheets["Summary"]
        S = "Summary"
        t0, t1 = float(self.times[0]), float(self.times[-1])
        base = (t0, 0.0) if t0 < 0 else (t0, min(t1, t0 + 60.0))
        thresholds = [_c_to(v, self.unit) for v in (100.0, 300.0, 500.0)]
        prefill = self.data.options.tc_prefill
        tc_window = prefill.window if prefill is not None and prefill.window is not None else (t0, t1)
        ws.write(0, 0, "Summary (live — follows Settings)", self.f_title)
        ws.write(1, 0, "Per ROI: spots use their value, areas their mean. Windows and thresholds are editable.",
                 self.f_note)
        inputs = (("BASE_START", "Baseline window start (s)", base[0]),
                  ("BASE_END", "Baseline window end (s)", base[1]),
                  ("THR_1", "Threshold 1 (display unit)", thresholds[0]),
                  ("THR_2", "Threshold 2 (display unit)", thresholds[1]),
                  ("THR_3", "Threshold 3 (display unit)", thresholds[2]),
                  ("TCW_START", "TC comparison window start (s)", tc_window[0]),
                  ("TCW_END", "TC comparison window end (s)", tc_window[1]))
        for k, (name, label, value) in enumerate(inputs):
            ws.write(3 + k, 0, label)
            ws.write_number(3 + k, 1, value, self.f_input_num)
            self._name(name, S, 3 + k, 1)
        # Window rows: first sample at or after the start, last at or before the
        # end; an empty window has end row < start row and shows "–".
        windows = {}
        for (start_name, end_name, a_name, b_name, row, label, (w0, w1)) in (
                ("BASE_START", "BASE_END", "BASE_A", "BASE_B", 3, "Baseline rows", base),
                ("TCW_START", "TCW_END", "TCW_A", "TCW_B", 8, "TC window rows", tc_window)):
            first = int(np.searchsorted(self.times, w0 - 1e-9))
            last = int(np.searchsorted(self.times, w1 + 1e-9, side="right")) - 1
            windows[a_name] = (first, last)
            ws.write(row, 3, label, self.f_note)
            ws.write_formula(row, 4, f"=IFERROR(MATCH({start_name},TIME,1)+(INDEX(TIME,MATCH({start_name},"
                                     f"TIME,1))<{start_name}),1)", self.f_int, first + 1)
            ws.write_formula(row + 1, 4, f"=IFERROR(MATCH({end_name},TIME,1),0)", self.f_int, last + 1)
            self._name(a_name, S, row, 4)
            self._name(b_name, S, row + 1, 4)
        head = ["Recording", "ROI", "Statistic", "Initial (baseline mean)", "Peak", "Time of peak (s)",
                "Max rise", "Time to threshold 1 (s)", "Time to threshold 2 (s)", "Time to threshold 3 (s)",
                "Below range (%)", "Above range (%)", "Saturated (%)",
                "Mean IR − TC (K)", "RMS IR − TC (K)", "Max |IR − TC| (K)", "Best-fit emissivity",
                "TC samples"]
        h = 12
        for c, text in enumerate(head):
            ws.write(h, c, text, self.f_head)
        tc = "TC Compare" in self.sheets
        for k, layout in enumerate(self.layouts):
            r = h + 1 + k
            R = r + 1
            n = layout.name
            ws.write(r, 0, layout.source.label)
            ws.write(r, 1, layout.roi.shape.name)
            ws.write(r, 2, STAT_LABELS[layout.main])
            if not layout.source.report.verified:
                ws.write(r, 3, "Counts only — no temperature calibration", self.f_note)
                continue
            rng = lambda nm, a_, b_: f"INDEX({nm},{a_}):INDEX({nm},{b_})"  # noqa: E731
            cached = self._summary_cached(layout, windows["BASE_A"], thresholds)
            ws.write_formula(r, 3, f'=IF(BASE_B<BASE_A,"–",IFERROR(_xlfn.AGGREGATE(1,6,'
                                   f'{rng(n + "_T", "BASE_A", "BASE_B")}),"–"))', self.f_temp, cached["initial"])
            ws.write_formula(r, 4, f'=IFERROR(_xlfn.AGGREGATE(4,6,{n}_T),"–")', self.f_temp, cached["peak"])
            # AGGREGATE(15, 6, …) = smallest value ignoring errors: the first time
            # at which the condition holds (other rows divide by FALSE → #DIV/0!).
            ws.write_formula(r, 5, f'=IFERROR(_xlfn.AGGREGATE(15,6,TIME/({n}_T=E{R}),1),"–")', self.f_time,
                             cached["peak_time"])
            ws.write_formula(r, 6, f'=IFERROR(E{R}-D{R},"–")', self.f_temp, cached["rise"])
            for j, thr in enumerate(("THR_1", "THR_2", "THR_3")):
                ws.write_formula(r, 7 + j, f'=IFERROR(_xlfn.AGGREGATE(15,6,TIME/({n}_T>={thr}),1),"not reached")',
                                 self.f_time, cached["thresholds"][j])
            for j, label in enumerate(("Below range", "Above range", "Saturated")):
                ws.write_formula(r, 10 + j, f'=IFERROR(100*COUNTIF({n}_S,"{label}")/COUNTA({n}_S),"–")',
                                 self.f_temp, cached["percent"][j])
            if tc and self._has_tc(layout):
                dts = rng(n + "_DTSR", "TCW_A", "TCW_B")
                empty = "TCW_B<TCW_A"
                ws.write_formula(r, 13, f'=IF({empty},"–",IFERROR(AVERAGE({dts}),"–"))', self.f_temp, "–")
                ws.write_formula(r, 14, f'=IF({empty},"–",IFERROR(SQRT(SUMSQ({dts})/COUNT({dts})),"–"))',
                                 self.f_temp, "–")
                ws.write_formula(r, 15, f'=IF({empty},"–",IF(COUNT({dts})=0,"–",MAX(MAX({dts}),-MIN({dts}))))',
                                 self.f_temp, "–")
                ws.write_formula(r, 16, f'=IF({empty},"–",IFERROR(SUM({rng(n + "_XYR", "TCW_A", "TCW_B")})/'
                                        f'SUM({rng(n + "_X2R", "TCW_A", "TCW_B")}),"–"))', self.f_eps, "–")
                ws.write_formula(r, 17, f'=IF({empty},0,COUNT({dts}))', self.f_int, 0)
        ws.set_column(0, 0, 36)
        ws.set_column(1, 2, 14)
        ws.set_column(3, 17, 13)
        ws.write(h + len(self.layouts) + 2, 0,
                 "Best-fit emissivity: least squares in the radiance domain over the TC window. Values above 1 "
                 "cannot be physical (see TC Compare).", self.f_note)

    def _summary_cached(self, layout: _RoiLayout, window: tuple[int, int], thresholds) -> dict:
        """Results of the Summary formulas for the initial settings."""
        values = self._cached[layout.number][layout.main]
        finite = np.isfinite(values)
        first, last = window
        base = values[first:last + 1] if last >= first else values[:0]
        base = base[np.isfinite(base)]
        initial = float(base.mean()) if base.size else "–"
        peak = float(values[finite].max()) if finite.any() else "–"
        peak_time = float(self.times[np.flatnonzero(values == peak)[0]]) if finite.any() else "–"
        rise = peak - initial if isinstance(peak, float) and isinstance(initial, float) else "–"
        reached = []
        for threshold in thresholds:
            hits = np.flatnonzero(finite & (values >= threshold))
            reached.append(float(self.times[hits[0]]) if hits.size else "not reached")
        status = layout.roi.status
        present = status >= 0
        percent = [float(100.0 * np.count_nonzero(status == code) / np.count_nonzero(present))
                   if present.any() else "–" for code in (1, 2, 3)]
        return {"initial": initial, "peak": peak, "peak_time": peak_time, "rise": rise,
                "thresholds": reached, "percent": percent}

    # --- Charts --------------------------------------------------------------------------------

    def _charts(self) -> None:
        ws = self.sheets["Charts"]
        ws.write(0, 0, "Charts (live)", self.f_title)
        ws.write(1, 0, "Temperature against time from ignition; series follow Settings and the TC mapping.",
                 self.f_note)
        first, last = HEADER_ROWS, self.last
        x = ["Data", first, 0, last, 0]
        verified = [layout for layout in self.layouts if layout.source.report.verified]
        # many ROIs (cell zones): one ordered ramp; when the TC fit paired some, own charts only for those
        # (TCs picked later in Excel need every ROI's chart, as before)
        many = len(verified) > len(PALETTE)
        groups: dict[str, list[_RoiLayout]] = {}
        for layout in verified:
            if not (many and self._tc_only_paired) or self._roi_tc.get(layout.roi.shape.name):
                groups.setdefault(layout.roi.shape.name, []).append(layout)
        palette = list(PALETTE)
        overview_colors = _ramp(len(verified)) if many else None
        position = 0

        t_min, t_max = float(self.times[0]), float(self.times[-1])

        def new_chart(title: str):
            chart = self.wb.add_chart({"type": "scatter", "subtype": "straight"})
            chart.set_title({"name": title, "name_font": {"size": 12}})
            chart.set_x_axis({"name": "Time from ignition (s)", "num_format": "0", "min": t_min, "max": t_max,
                              "major_gridlines": {"visible": True, "line": {"color": "#E0E0E0"}}})
            chart.set_y_axis({"name": f"=Settings!${xl_col_to_name(INTERNAL + 1)}$6", "num_format": "0",
                              "major_gridlines": {"visible": True, "line": {"color": "#E0E0E0"}}})
            chart.set_legend({"position": "bottom"})
            chart.set_size({"width": 900, "height": 420})
            return chart

        def place(chart):
            nonlocal position
            ws.insert_chart(3 + (position // 2) * 22, (position % 2) * 15, chart)
            position += 1

        overview = new_chart("All ROIs" + (" (darkest = first ROI)" if many else ""))
        for k, layout in enumerate(verified):
            col = layout.data_cols[layout.main]
            color = overview_colors[k] if overview_colors else palette[k % len(palette)]
            overview.add_series({"name": layout.title, "categories": x,
                                 "values": ["Data", first, col, last, col],
                                 "line": {"width": 1.25, "color": color}})
        if verified:
            if many:
                overview.set_size({"width": 1820, "height": 420})
            place(overview)
            if many:
                position += 1  # the wide chart takes the whole row
        tc = "TC Compare" in self.sheets
        for name, members in groups.items():
            chart = new_chart(name)
            for j, layout in enumerate(members):
                col = layout.data_cols[layout.main]
                chart.add_series({"name": layout.title, "categories": x,
                                  "values": ["Data", first, col, last, col],
                                  "line": {"width": 1.5, "color": palette[j % len(palette)]}})
            if tc:
                # the TC series (display unit) lives in hidden helper columns,
                # which Excel leaves out of a chart unless told otherwise
                chart.show_hidden_data()
                for layout in (m for m in members if self._has_tc(m)):
                    cols = self.tc_cols[layout.number]
                    chart.add_series({"name": f"TC for {layout.title}",
                                      "categories": ["TC Compare", TC_FIRST + 1, TC_COLUMNS + 2,
                                                     TC_FIRST + self.rows, TC_COLUMNS + 2],
                                      "values": ["TC Compare", TC_FIRST + 1, cols["tcd"],
                                                 TC_FIRST + self.rows, cols["tcd"]],
                                      "line": {"width": 1.5, "color": "#000000", "dash_type": "dash"}})
            place(chart)
        if tc and groups:
            chart = self.wb.add_chart({"type": "scatter", "subtype": "straight"})
            chart.set_title({"name": "Matching emissivity (from TC Compare)", "name_font": {"size": 12}})
            chart.set_x_axis({"name": "Time from ignition (s)", "num_format": "0", "min": t_min, "max": t_max})
            chart.set_y_axis({"name": "Emissivity", "min": 0, "max": 1.5, "num_format": "0.0"})
            chart.set_legend({"position": "bottom"})
            chart.set_size({"width": 900, "height": 420})
            for j, layout in enumerate(l for m in groups.values() for l in m if self._has_tc(l)):
                cols = self.tc_cols[layout.number]
                chart.add_series({"name": layout.title,
                                  "categories": ["TC Compare", TC_FIRST + 1, TC_COLUMNS + 2,
                                                 TC_FIRST + self.rows, TC_COLUMNS + 2],
                                  "values": ["TC Compare", TC_FIRST + 1, cols["eps"],
                                             TC_FIRST + self.rows, cols["eps"]],
                                  "line": {"width": 1.25, "color": palette[j % len(palette)]}})
            place(chart)

    # --- ROI map ---------------------------------------------------------------------------------

    def _roi_map(self) -> None:
        ws = self.sheets["ROI Map"]
        ws.write(0, 0, "Where the ROIs are (ignition frame of each recording, apparent counts)", self.f_title)
        row = 2
        for source in self.data.sources:
            ws.write(row, 0, f"{source.label} — {source.info['file_name']} — frame {source.spec.ignition_frame + 1}",
                     self.f_h2)
            ws.write(row + 1, 0, "ROI", self.f_head)
            for c, text in enumerate(("Type", "Pixels", "x₁", "y₁", "x₂", "y₂"), start=1):
                ws.write(row + 1, c, text, self.f_head)
            for k, roi in enumerate(source.rois):
                r = row + 2 + k
                ws.write(r, 0, roi.shape.name)
                ws.write(r, 1, roi.shape.kind)
                ws.write_number(r, 2, roi.pixels)
                for j, (px, py) in enumerate(roi.shape.points):
                    ws.write_number(r, 3 + 2 * j, round(px, 2))
                    ws.write_number(r, 4 + 2 * j, round(py, 2))
            if source.map_png:
                ws.insert_image(row, 8, f"{source.label}.png",
                                {"image_data": io.BytesIO(source.map_png), "x_scale": 1.0, "y_scale": 1.0,
                                 "object_position": 3})
            image_height = source.info["height"]
            if source.map_png:
                from PIL import Image  # the map is enlarged for narrow zones

                with Image.open(io.BytesIO(source.map_png)) as image:
                    image_height = image.size[1]
            height_rows = max(len(source.rois) + 3, int(image_height / 20) + 3)
            row += height_rows + 2
        ws.set_column(0, 0, 22)

    # --- Validation ------------------------------------------------------------------------------

    def _validation(self) -> None:
        ws = self.sheets["Validation"]
        ws.write(0, 0, "Validation: Excel formulas against the FLIR File SDK", self.f_title)
        ws.write(1, 0, "Each row recomputes a sample with the recording's own parameters (fixed numbers, "
                       "independent of Settings) and compares it with the SDK's temperature. Samples the "
                       "SDK clamped are skipped.", self.f_note)
        head = ["Recording", "ROI", "Statistic", "Time (s)", "Frame", "SDK (K)", "Excel formula (K)",
                "Difference (K)", "|Difference| (K)", "Note"]
        entries = []
        for layout in self.layouts:
            source = layout.source
            if not source.validation_rows.size:
                continue
            stats = [stat for stat in layout.roi.validation if stat in layout.data_cols]
            for stat in stats:
                for k, row in enumerate(source.validation_rows):
                    entries.append((layout, stat, k, int(row)))
        summary_row = 3
        ws.write(summary_row, 0, "Largest |difference| over checked samples (K)", self.f_bold)
        start = summary_row + 3
        for c, text in enumerate(head):
            ws.write(start, c, text, self.f_head)
        first = start + 1
        last = first + len(entries) - 1
        abs_range = _range("Validation", first, 8, max(first, last), 8)
        worst_cached = 0.0
        failed = False
        rows_out = []
        for j, (layout, stat, k, row) in enumerate(entries):
            source = layout.source
            cal = source.report.calibration
            p = source.file_parameters
            p = p.with_(transmission=transmission_used(cal, p))
            k1, k2 = coefficients(cal, p)
            clip = cal.limits.clip or (-BIG, BIG)
            sdk = float(layout.roi.validation[stat][k])
            clamped = bool(layout.roi.validation_clamped[k]) if layout.roi.validation_clamped is not None else False
            data_r = HEADER_ROWS + row
            planck = cal.planck
            curve = lambda ref: (f"{_num(planck.B)}/LN({_num(planck.R)}/({_num(k1)}*{ref}-{_num(k2)}"  # noqa: E731
                                 f"+{_num(planck.O)})+{_num(planck.F)})")
            roi = layout.roi
            if stat == "mean" and roi.mode == "pixels":
                width = roi.block.shape[1]
                ref = _range("Pixels", data_r, layout.pixel_col, data_r, layout.pixel_col + width - 1)
                formula = f"=SUMPRODUCT({curve(ref)})/COUNT({ref})"
                cached = float(np.nanmean(object_temperature(cal, p, roi.block[row])))
                note = "exact mean (all pixels)"
            elif stat == "mean" and roi.mode == "bins":
                width = roi.block.shape[1]
                bins = width // 2
                n_ref = _range("Pixels", data_r, layout.pixel_col, data_r, layout.pixel_col + bins - 1)
                m_ref = _range("Pixels", data_r, layout.pixel_col + bins, data_r, layout.pixel_col + width - 1)
                formula = f"=SUMPRODUCT({n_ref},{curve(m_ref)})/SUM({n_ref})"
                n_, m_ = roi.block[row, :bins], roi.block[row, bins:]
                cached = float(np.sum(n_ * object_temperature(cal, p, m_)) / np.sum(n_))
                note = f"binned mean ({bins} bins of equal apparent-temperature width) — approximation"
            else:
                ref = _cell("Counts", data_r, layout.count_cols[stat])
                formula = f"=MEDIAN({_num(clip[0])},{curve(ref)},{_num(clip[1])})"
                cached = float(np.clip(object_temperature(cal, p, roi.counts[stat][row]), *clip))
                note = "exact (extreme/percentile converts exactly)" if stat != "value" else "exact"
                if stat == "mean" and roi.mode == "mean":
                    note = ("mean signal: the temperature of the mean count, not the mean of the pixels' "
                            "temperatures (an approximation)")
            if clamped:
                note = "skipped: the SDK clamped a pixel of this ROI"
            difference = cached - sdk
            if not clamped and "approximation" not in note:
                if np.isfinite(difference):
                    worst_cached = max(worst_cached, abs(difference))
                else:
                    failed = True  # outside the curve's domain although the SDK did not clamp
            rows_out.append((layout, stat, row, source, sdk, formula, cached, difference, clamped, note))
        passed = not failed and worst_cached <= 0.01
        ws.write_formula(summary_row, 3, f"=MAX({abs_range})", self.f_calc,
                         "#NUM!" if failed else worst_cached)
        ws.write_formula(summary_row, 4,
                         f'=IF(ISNUMBER(D{summary_row + 1}),IF(D{summary_row + 1}<=0.01,'
                         f'"PASS — formulas reproduce the SDK","CHECK"),"CHECK")',
                         None, "PASS — formulas reproduce the SDK" if passed else "CHECK")
        ws.write(summary_row + 1, 0, "Binned means are approximations and excluded from the pass mark; "
                                     "see their own rows.", self.f_note)
        ws.conditional_format(summary_row, 4, summary_row, 4, {"type": "text", "criteria": "begins with",
                                                               "value": "PASS", "format": self.f_pass})
        ws.conditional_format(summary_row, 4, summary_row, 4, {"type": "text", "criteria": "begins with",
                                                               "value": "CHECK", "format": self.f_fail})
        for j, (layout, stat, row, source, sdk, formula, cached, difference, clamped, note) in enumerate(rows_out):
            r = first + j
            R = r + 1
            ws.write(r, 0, source.label)
            ws.write(r, 1, layout.roi.shape.name)
            ws.write(r, 2, STAT_LABELS[stat])
            ws.write_number(r, 3, float(self.times[row]), self.f_time)
            ws.write_number(r, 4, int(source.frames[row]) + 1, self.f_int)
            ws.write_number(r, 5, sdk, self.f_calc)
            # a non-finite result would be written as "nan", which Excel rejects
            # as a corrupt file; the formula's own result there is #NUM! (LN ≤ 0)
            ws.write_formula(r, 6, formula, self.f_calc, _finite_or(cached, "#NUM!"))
            ws.write_formula(r, 7, f"=G{R}-F{R}", self.f_calc, _finite_or(difference, "#NUM!"))
            if clamped or "approximation" in note:
                ws.write_formula(r, 8, '=""', None, "")
            else:
                ws.write_formula(r, 8, f"=ABS(H{R})", self.f_calc, _finite_or(abs(difference), "#NUM!"))
            ws.write(r, 9, note)
        ws.set_column(0, 2, 16)
        ws.set_column(3, 8, 15)
        ws.set_column(9, 9, 48)

    # --- Source ------------------------------------------------------------------------------------

    def _source(self) -> None:
        ws = self.sheets["Source"]
        ws.write(0, 0, "Recordings, calibration and export settings", self.f_title)
        r = 2
        for source in self.data.sources:
            info = source.info
            ws.write(r, 0, source.label, self.f_h2)
            r += 1
            fields = [
                ("File", info["file"]), ("Size (bytes)", info["file_size"]), ("Camera", info["camera"]),
                ("Camera serial", info["camera_serial"]), ("Lens", info["lens"]),
                ("Image size", f"{info['width']} × {info['height']}"), ("Frames", info["frames"]),
                ("Frame rate", f"{info['fps']:.4f} fps (camera rate)" if info.get("rate_corrected")
                 else f"{info['fps']:.4f} fps (average)"),
                ("First frame clock", info["start"].isoformat(sep=" ") if info["start"] else ""),
                ("Last frame clock", info["end"].isoformat(sep=" ") if info["end"] else ""),
                ("Ignition frame (t = 0)", info["ignition_frame"] + 1),
                ("Temperature calibration", source.report.message),
            ]
            if source.report.fff is not None:
                fff = source.report.fff
                fields += [("FFF PlanckR1 / R2", f"{fff['planck_r1']} / {fff['planck_r2']}"),
                           ("FFF PlanckB / F / O", f"{fff['planck_b']} / {fff['planck_f']} / {fff['planck_o']}"),
                           ("FFF recorded emissivity / distance",
                            f"{fff['emissivity']:.3f} / {fff['object_distance']:.2f} m")]
            if source.report.fff_error_k is not None:
                fields.append(("FFF constants vs SDK (neutral), max |ΔT|", f"{source.report.fff_error_k:.2e} K"))
            if source.report.fit_error_k is not None:
                fields.append(("Fitted constants vs SDK (neutral), max |ΔT|", f"{source.report.fit_error_k:.2e} K"))
            for label, error, pixels in source.report.checks:
                fields.append((f"Check: {label}", f"{error:.2e} K over {pixels} pixels"))
            if info.get("saturation_threshold"):
                fields.append(("Camera saturation threshold (counts)", info["saturation_threshold"]))
            if info.get("rate_corrected"):
                slow = (info["clock_fps"] / info["fps"] - 1) * 100
                if self.data.options.time_base == "clock":
                    note = (f"The camera timestamps imply {info['clock_fps']:.4f} fps, {slow:.1f} % slower than "
                            "the camera rate. This workbook follows them (time base: camera timestamps); the "
                            "frame-number time base would use the camera rate instead.")
                else:
                    note = (f"The camera timestamps imply {info['clock_fps']:.4f} fps: they run {slow:.1f} % "
                            "slow, so times come from the frame number at the camera rate. The clock "
                            "columns show the file's own (slow) timestamps.")
                fields.append(("Note", note))
            elif info.get("suggested_fps"):
                fields.append(("Note", f"The camera timestamps imply {info['clock_fps']:.4f} fps, "
                                       f"{(info['clock_fps'] / info['suggested_fps'] - 1) * 100:.1f} % above "
                                       f"{info['suggested_fps']:g} Hz. If the camera ran at {info['suggested_fps']:g} Hz, "
                                       "its clock runs slow; the times here follow the timestamps."))
            if info.get("saved_parameters"):
                start = ("the workbook starts from these values" if info.get("saved_parameters_used")
                         else "the workbook starts from the camera's recorded values"
                         if same_parameters(source.initial, source.file_parameters)
                         else "the workbook starts from the values set in the player")
                fields.append(("Saved ResearchIR override",
                               describe_parameters(info["saved_parameters"], source.report.file_parameters)
                               + f". ResearchIR shows the recording with these; {start}."))
            if info.get("date_inferred"):
                fields.append(("Note", "The ATS clock has no year: the date comes from the file's "
                                       "modification time; times of day are exact."))
            for label, value in fields:
                ws.write(r, 0, label)
                ws.write(r, 1, value)
                r += 1
            r += 1
        opts = self.data.options
        ws.write(r, 0, "Export", self.f_h2)
        r += 1
        for label, value in (
            ("Created", self.data.created.isoformat(sep=" ", timespec="seconds")),
            ("FLIR Thermal Player", self.data.versions.get("tool", "")),
            ("FLIR File SDK", self.data.versions.get("sdk", "")),
            ("Rows", self.rows),
            ("Sampling", f"every {opts.step_s:g} s" if not opts.every_frame else
             "every frame" if opts.time_base == "frames" else
             "every frame interval of the first recording (its mean rate); each row takes the frame "
             "nearest its time, so a frame can repeat or be skipped where the camera clock has gaps"),
            ("Time base", "frame index ÷ frame rate" if opts.time_base == "frames" else "camera timestamps"),
            ("Area means", "from the mean signal (mean counts)" if opts.area_means == "signal" else
             "from every pixel (exact; bins above the pixel limit)"),
            ("Pixel limit for exact means", opts.pixel_limit), ("Bins for larger areas", opts.bins),
        ):
            ws.write(r, 0, label)
            ws.write(r, 1, value)
            r += 1
        ws.set_column(0, 0, 44)
        ws.set_column(1, 1, 90)

    # --- Start Here ----------------------------------------------------------------------------------

    def _start(self) -> None:
        ws = self.sheets["Start Here"]
        ws.set_column(0, 0, 3)
        ws.set_column(1, 1, 120)
        lines = [
            ("FLIR IR data — raw counts with live temperature formulas", self.f_title),
            ("", None),
            ("What this workbook is", self.f_h2),
            ("The camera stores raw counts. This workbook keeps them (Counts, Pixels) and converts them to "
             "temperature with FLIR's own measurement formula as Excel formulas (Data).", self.f_wrap),
            ("Change a yellow cell on Settings — emissivity, reflected temperature, transmission, window — and "
             "every temperature, chart and summary recalculates. Validation shows the formulas reproduce the "
             "FLIR File SDK.", self.f_wrap),
            ("", None),
            ("How to use it", self.f_h2),
            ("1. Settings: set the emissivity (and reflected temperature). Leave override cells blank to use "
             "the global values; fill one to override a recording or an ROI.", self.f_wrap),
            ("2. TC Compare: paste thermocouple data (time from ignition in s, °C); pick each ROI's TC column "
             "on Settings.", self.f_wrap),
            ("3. Charts and Summary follow automatically. Time is seconds from each recording's ignition "
             "frame, so cameras line up.", self.f_wrap),
            ("4. Emissivity Match: the error between the ROIs and their TCs for each emissivity between two "
             "limits (0.90 to 0.98 to start); type the best value as the emissivity on Settings. On TC "
             "Compare, Use (1/0) picks the rows it counts for each ROI.", self.f_wrap),
            ("", None),
            ("Colours", self.f_h2),
            ("Yellow = input. Grey = calculated. In Data: grey italic = outside the camera's calibrated range "
             "(extrapolated, less accurate); red = saturated sensor (value not meaningful).", self.f_wrap),
            ("", None),
            ("The formula", self.f_h2),
            ("S(T) = R / (exp(B/T) − F) − O is the camera's calibration curve (counts of a blackbody at T "
             "kelvin; R, B, F, O on Settings).", self.f_wrap),
            ("Object signal = K1 × counts − K2, with K1 = 1/(ε·τ·τw) and K2 = [(1−τw)·S(Tw)/τw + (1−ε)·τ·S(Tr) "
             "+ (1−τ)·S(Ta)] / (ε·τ); then T = B / ln(R / (object signal + O) + F).", self.f_wrap),
            ("Extremes and percentiles convert exactly from counts. Area means use every pixel (Pixels sheet); "
             "areas above the pixel limit use binned counts (see Validation for their error). A workbook made "
             "with area means from the mean signal stores no pixels: its means are the temperature of the "
             "mean counts.", self.f_wrap),
            ("", None),
            ("Good to know", self.f_h2),
            ("Emissivity cannot pull IR below the ε = 1 (apparent) value. If IR is far above a thermocouple, the "
             "cause is not emissivity: flames or hot gas in view, the ROI, or the TC's contact.", self.f_wrap),
            ("Reflected temperature matters when emissivity is low or the surroundings are hot (flames nearby).",
             self.f_wrap),
            ("Atmospheric transmission models humidity only; smoke between camera and target is not modelled "
             "(use a manual transmission if you know it).", self.f_wrap),
        ]
        example = self._worked_example()
        if example:
            lines += [("", None), ("Worked example (first sample, initial settings)", self.f_h2),
                      (example, self.f_wrap)]
        for r, (text, fmt) in enumerate(lines):
            if text:
                ws.write(r, 1, text, fmt)
        ws.activate()

    def _worked_example(self) -> str:
        for layout in self.layouts:
            source = layout.source
            if not source.report.verified:
                continue
            roi = layout.roi
            stat = "value" if roi.mode == "spot" else "max"
            counts = roi.counts[stat]
            present = np.flatnonzero(np.isfinite(counts))
            if not present.size:
                continue
            i = int(present[0])
            cal = source.report.calibration
            p = self._params(layout)
            k1, k2 = coefficients(cal, p)
            c = float(counts[i])
            kelvin = float(object_temperature(cal, p, c))
            return (f"{layout.title}, {STAT_LABELS[stat].lower()} at t = {self.times[i]:g} s: counts = {c:.1f}; "
                    f"ε = {p.emissivity:.3f}, τ = {p.transmission:.5f}; K1 = {k1:.6f}, K2 = {k2:.3f}; "
                    f"object signal = {k1 * c - k2:.3f}; T = {cal.planck.B:g} / ln({cal.planck.R:.4f} / "
                    f"({k1 * c - k2:.3f} + {cal.planck.O:g}) + {cal.planck.F:g}) = {kelvin:.3f} K = "
                    f"{kelvin - KELVIN:.3f} °C.")
        return ""


def _c_to(celsius: float, unit: str) -> float:
    if unit == "K":
        return celsius + KELVIN
    if unit == "°F":
        return celsius * 1.8 + 32.0
    return celsius


def write_workbook(path: str | Path, data: ExportData, abort=None) -> None:
    """Write the complete workbook for ``data`` to ``path``.

    ``abort`` (a callable) is polled while writing; cancelling raises
    ``JobCancelled`` and leaves ``path`` incomplete for the caller to discard.
    """
    # XlsxWriter deletes its temporary files only in a successful close(), so
    # they go to a private folder that is removed however writing ends.
    tmpdir = tempfile.mkdtemp(prefix="flir-xlsx-")
    try:
        writer = _Writer(Path(path), data, abort, tmpdir)
        try:
            writer.build()
        finally:
            writer.close_files()  # Windows cannot delete open files
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
