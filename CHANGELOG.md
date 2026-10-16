# Changelog

## Unreleased

### Cell zones that follow the module as the camera sees it

- Zones are four-sided. On a module at a slant (*Rows at the last column*)
  their upper and lower edges run straight along it instead of stepping zone by
  zone.
- *Divider lean* in the split window: the dividers between cells lean by that
  many pixels across the band, as on a module seen at an angle (on the 1006a
  T650 the cell dividers lean about 3 px across the 17 px top face).
- *Camera distance* at zone 1's end and at the far end: the zones narrow
  towards the farther end as a perspective view shows them (a straight row
  whose depth grows along it).
- Drag a zone's corner to move that divider along the row: the zones on both
  sides follow and the gap between them stays. Drag a zone to move every zone of
  the split. Re-splitting keeps dividers moved by hand while the number of
  zones is unchanged; ROI sets save the lean, the distances and the moved
  dividers (sets without them stay readable by 0.7.x).
- Zone edges are computed in exact fractions: an edge that fell exactly on a
  pixel boundary could land a pixel early before.
- The workbook's Settings sheet lists zones as "Zone"; their statistics in the
  player come from the app (the SDK has no four-sided ROI), with the same pixels
  as everywhere else. An upright zone covers exactly the pixels of the box with
  its corners.
- Grabbing a corner where two ROIs' handles meet takes the selected ROI's.

### TC Compare without a TC fit

- The Excel export can fill TC Compare straight from the test's TC file
  (*Fill TC Compare* > *From a TC file, timed by the clocks*), so the zone part
  of the procedure works without running *Fit Emissivity from TCs* first. The
  camera and logger clocks place logger time 0 and set the range to the
  logger's span; T1, T2 and T3 start paired with zones 3, 6 and 9 (*TCs and
  zones*, editable); Use = 1 marks valid TC readings of at least 50 °C with no
  flame jump on the TC. Time 0 stays editable, and A700 recordings get a note
  that their clock can run slow. The recording's TC file and the pairs are
  remembered on Export. The workbook cannot be saved over the TC file.

### Review fixes

Fixes from a review of everything added since 0.5.2 (the TC fit, the
workbook's Emissivity Match, cell zones, the clocks and the player's dialogs).

#### TC fit

- An emissivity that leaves some seconds of an event without any temperature
  (with a high reflected temperature) no longer wins on the seconds it can
  explain: every candidate is compared over the same seconds.
- A TC sheet whose time column stops rising is no longer read with another
  rising column (a TC heating steadily, a scan number) as its time: a column
  headed as time is read over the stretch where it rises, and a column with a
  temperature unit is never the time. TC columns headed in °F or kelvin are
  refused.
- A pixel given for T1 no longer also fixes T10: a given pixel names a TC by
  its name (any case), its number, or a start only its name has, against the
  whole TC table, whichever TCs are chosen.
- A fit with an open-ended validity window ("50-") reopens; the dialog shows
  the open end empty again.
- When no TC is found within 2 minutes of the clocks, the results say so and
  suggest *Anywhere in the recording* or giving the frame (there is still no
  silent switch to the whole recording).
- Saved frame statistics remember the recording's content and are not reused
  for another file of the same name, size and time; a statistics file cut off
  while it was written counts as no cache instead of stopping the fit.
- The summary and the warnings give durations in seconds when a time bin is
  longer than a second.
- A reflected temperature given for the fit on the command line also goes into
  its workbook and is saved with the fit, so a workbook from the reopened fit
  converts the same way.
- The search region of a box is the pixels the box measures (its corners
  rounded as everywhere else), not the pixels it touches.
- A fit kept in the player for a recording is dropped, with the time zero it
  gave, when another file is put at the same path, and a fit read for an
  earlier opening of the path is not used; a rerun that draws no figure no longer reopens the previous
  run's figure; a workbook from a fit saved before 0.7.1 (which did not record
  its object parameters) asks first.

#### Workbook

- The Excel export refuses a recording that cycles through several presets
  (superframing), whose rows would mix the presets.
- The Emissivity Match leaves out the rows where a zone has clipped pixels
  (Status *Saturated*), in Excel and in the values written with the workbook.
- More than 255 ROIs take more overview charts (an Excel chart holds 255
  series); the Match, matching-emissivity and per-name charts say when they
  show only the first ones.

#### Cell zones and ROI sets

- With a gap between zones, the box's ends lose half a gap as well, so every
  zone is as wide (the end zones were wider by half a gap); the shortest box
  the dialog asks for is now exact.
- ROI sets with more zones or a wider gap than the split dialog allows are
  refused, and a set saved for another image size is refused with both sizes
  named.

## 0.7.4 — 2026-10-14

Cell zones for a module that runs at a slant in the picture: the T650 of the
1006 test a looks along the module from above, so its top face rises about
38 px from the far end to the heater end and is only about 17 px tall.

### Cell zones

- The split dialog has a third pair of edges, *Rows at the last column*
  (*Columns at the last row* for a split along the rows): where the module is
  at the box's other end. Each zone keeps its cell's columns and takes the
  module's rows at its own centre, on the straight line from the box's rows at
  its first column to these, so the zones step along the slant and stay
  ordinary boxes: the workbook, the TC pairing and the ROI map treat them as
  before. Left as the box's rows (the default, and they follow the box until
  edited), nothing changes.
- The dialog's line under the edges gives the zones' rows at both ends, and the
  preview and the TC line follow the slant as it is typed.
- ROI sets keep the slant with the split, so re-splitting starts from it; sets
  without one load as before, and a split without a slant is saved exactly as
  0.7.3 saved it.

[Full comparison](https://github.com/infinitr0us/FLIR_Player/compare/v0.7.3...v0.7.4)

## 0.7.3 — 2026-10-13

The time match of the TC fit, after the engineers' run on the 1006 A700 matched
the logger 33 minutes off.

### TC fit

- *Near the camera and logger clocks* is the new default time match. A lab
  workbook says when logging started (the Date/Time cell of its `LogFile_…`
  sheet, or the sheet's name) and which Log row is logger time 0 (the Log row
  the TC sheet's time formulas subtract, in the rows the reader uses); the
  recording's first frame has the camera's clock. The offset is then searched
  only within 2 minutes of where the clocks put logger time 0. Whole hours
  between the clocks are set aside, because cameras keep UTC or a time zone
  without daylight saving while the logger PC keeps local time; of the offsets
  whole hours apart, the one nearest the recording's start that puts most of
  the log inside the recording is taken. The clock is taken only when every
  time cell read subtracts the same Log row of an elapsed-time column that
  starts at 0 and holds the difference its formula names, so a half-edited
  sheet, another column or a sheet not recalculated gives no clock rather than
  a wrong one. Without a clock, or when no offset near it leaves the logger and
  the recording overlapping, the whole recording is searched, with a warning.
- Why: where a fire spreads cell by cell, every cell's curve resembles the next
  one's some minutes later, so a TC correlates almost equally well at many
  offsets (0.95 from -300 to 3,000 s for T2 of the 1006 A700), and the best
  score can sit far from the truth.
- Whatever the mode, a match more than 2 minutes from the clocks is a warning
  (*Anywhere in the recording*), and so are a match at the edge of the window
  near the clocks and a given frame far from them. The summary, the results
  headline and the figure show where the clocks put logger time 0 (on
  superframing files the dialog gives it in seconds, as their frames are timed
  by the clock); the setup dialog shows it before the run, and `result.json`
  keeps it.
- A fit reopened from before 0.7.3 offers the new default. A given frame is
  kept as given: reopened, the fit offers that frame again (fits before 0.7.3
  recorded it in seconds and reopened as an automatic match).
- An empty `--lag-range` is a clear error instead of a crash.
- "Cell Temp" and "TRIM temp" (lab workbook columns) start unticked, like the
  TC tree: the engineers do not compare them with the IR. Ticked, their long
  cool-down matched the 1006 A700 at the wrong offset.
- Command line: `--near-clocks`.

[Full comparison](https://github.com/infinitr0us/FLIR_Player/compare/v0.7.2...v0.7.3)

## 0.7.2 — 2026-10-13

Two requests from the engineers' first runs of 0.7.0 and 0.7.1.

### Player

- Rotate the image 90° left or right (two buttons next to the flips), for a
  camera mounted on its side. Only the picture turns: ROIs, cell zones, TC
  pixels, the readout and every exported number stay in the recording's own
  pixels, so a turned view measures exactly what the unturned view does. A turn
  turns the picture as it is shown, flips included, and refits the view when the
  picture's shape changes. Image and movie exports show the turned picture; the
  Excel workbook's ROI map, the TC results and numeric TIFFs keep the
  recording's orientation.
- The zone dialog names the end for zone 1 as the picture is shown, so "right
  end" is the right of the turned view too; the zones and the saved split keep
  recording terms.
- A new ROI takes the lowest free number of its kind: after deleting Box 2 of
  Box 1 to 3, the next box is Box 2 again (it used to count on, to Box 4).
  Joining zones back into their box gives it a free name if a new box has
  taken the old one meanwhile.
- A line endpoint dragged on a flipped or turned view lands on the pixel drawn
  under the pointer; it could take the neighbouring one.

### TC fit

- A TC sheet whose time column stops rising is read over the rows where it
  rises, and the summary says which rows were left out; it used to be refused
  ("no increasing time column"). This happens in a lab workbook whose time
  formulas were not filled down after its shift row changed, or that runs past
  the end of its log (the engineers' 20261006b.xlsx has both). Only columns
  headed as time compete when one exists, a column with a temperature unit
  never stands in for time, and dates and clock times keep their time zero.

### Documentation

- The README's cell-zone example no longer quotes the 0903 test: the module fell
  into its container about 8 minutes after ignition, so the later 0903 values
  describe the container wall, not the battery.

[Full comparison](https://github.com/infinitr0us/FLIR_Player/compare/v0.7.1...v0.7.2)

## 0.7.1 — 2026-10-10

Smaller steps for the TC and cell-zone route, found while writing the
engineers' step-by-step guide.

### TC fit

- The player reopens a recording's last TC fit from its results folder
  (`<recording>_tcmatch`) when the recording is opened again, so the workbook
  can be filled from the fit in a later session without running it again; a
  notice says when the fit is back, or why not. Everything is read in the
  background. The fit is offered only to the recording it was made from (name,
  length, size and a content fingerprint, so a moved copy still qualifies), and
  the TC file is read again only if its content is what the fit read (a moved
  copy is found next to the recording; an edited one is reported).
  `result.json` now records the TC file (path and content hash), its sheet, the
  object parameters the fit used and the recording's identity.
- A TC fit stops with a clear message if its TC file or recording changes while
  it is being read.
- The setup dialog offers the last run's search region even when its box is
  gone, so a rerun still finds its saved frame statistics (they are kept per
  region) and takes about a minute.

### Excel workbook

- With *Fill TC Compare from the last TC fit*, the export starts at logger
  time 0 (the ignition frame is the fit's) and covers the logger's first to last
  second, so the workbook's seconds are the TC file's. Any other time 0 still
  works; the dialog says which frame logger time 0 is.
- An area measured by its mean signal (cell zones) is flagged outside the
  calibrated range only when its mean count is, not when one of its pixels is:
  its temperature is that of its mean count. A clipped pixel still flags the
  row as saturated.

### Player

- The Export button opens its menu on any click (the Excel workbook is there).
  Ctrl+E, and the menu's first entry, still save the frame as a PNG.
- The Measurement panel says why it is greyed out in Counts (the values apply
  to temperatures), and the notice about a file's saved ResearchIR values says
  to set Unit to a temperature first.

[Full comparison](https://github.com/infinitr0us/FLIR_Player/compare/v0.7.0...v0.7.1)

## 0.7.0 — 2026-10-10

Cell zones and one emissivity. The fire-safety engineers want the temperature of
every cell of a battery module seen from the side, and one emissivity (by their
practice between 0.90 and 0.98) that best matches the thermocouples on cells 3,
6 and 9. The player can now split a box over the module into one zone per cell,
and the workbook finds that emissivity.

### Cell zones

- A new toolbar button splits the selected box into equal zones: the number of
  zones (18), where zone 1 is (right, left, top or bottom end; the heater end on
  the fire tests), the gap between zones in whole pixels (1 leaves out the mixed
  pixels where two cells meet), the names ("Cell 1", "Cell 2", …) and the box
  edges in pixels. The zones preview on the image while the dialog is open, and
  after a TC fit the dialog says which zone each TC pixel falls in.
- Selecting a zone and pressing the button again re-splits the whole group or
  joins it back into the box. ROI sets keep the groups.
- Labels of narrow numbered boxes shrink to their number ("Cell 7" → "7"), or
  wait for a closer zoom; the selected ROI keeps its name. Exported images and
  the workbook's ROI map do the same (the map is enlarged for narrow zones).

### Excel workbook

- **Emissivity Match** sheet: for every candidate emissivity between two limits
  (0.90 to 0.98 in steps of 0.01 to start), the error between each ROI and its
  TC and over all TCs, the best value, a chart of error against emissivity, each
  TC's own best value (the lowest error between 0.05 and 1.00), and plain notes
  when the best match lies beyond a limit or the TCs want clearly different
  values. A step too fine for the table's 41 rows is widened.
- TC Compare gets a *Use* column (1 or 0) per ROI, picking the rows the match
  counts, and a *Fit row* column showing them.
- *Fill TC Compare from the last TC fit* in the export dialog: the logger data at
  the fitted offset, each TC paired with the smallest ROI holding its pixel (its
  zone), no per-ROI emissivity, and Use = 1 on the seconds the fit used. Only the
  paired ROIs are compared, which keeps the workbook quick to open.
- *Area means from the mean signal* (no per-pixel columns) and an optional *Max
  and min*: both start this way above 8 areas.
- Workbooks open much faster: the row-by-row formulas of Data and TC Compare
  now cite Settings cells instead of defined names, which Excel resolves slowly
  when it opens a file (the names stay defined). An 18-zone workbook of a whole
  test (4,807 rows) opened in about 3 minutes; filled from the TC fit it now
  opens in about 13 seconds.
- With more than 8 ROIs, the "All ROIs" chart colours them in order along one
  blue ramp, and (when the TC fit paired some) only ROIs with a TC get their own
  chart.
- ROIs and TCs are paired by name, so repeated names are refused: the zone
  dialog will not reuse names other ROIs carry, and the export says which
  recording has two ROIs of one name.

### Under the hood

- `zones` (Qt-free) splits boxes and pairs TC pixels with ROIs;
  `tcmatch.tc_prefill` takes the player's ROIs and ignition frame; events keep
  the seconds their fit used.
- The Emissivity Match formulas are array formulas over named ranges, written
  with Python-computed results for viewers that do not recalculate; opt-in tests
  check that Microsoft Excel computes the same.
- 64 new tests, plus 10 opt-in tests that evaluate the Emissivity Match sheet
  in Microsoft Excel. Four review passes found nine issues (TCs paired by name
  across ROIs or added recordings that share a name, a fine step cutting the
  candidate table short, limit notes misreading a best value between
  candidates, a single candidate, or surroundings hotter than the zones,
  per-ROI emissivities stopping the common value, hand-paired zone workbooks
  missing their charts, hidden helper rows covering ROIs of large workbooks);
  all are fixed.

[Full comparison](https://github.com/infinitr0us/FLIR_Player/compare/v0.6.0...v0.7.0)

## 0.6.0 — 2026-10-07

Emissivity from thermocouples. Fire tests film battery cells that carry
thermocouples (TCs); the player can now find each TC in the recording and fit
the surface emissivity from its readings, the method the engineers used by
hand on the 0922 test. Developed on two battery fire tests (0922 and 0903).

### Fit Emissivity from TCs

- *Measurement → Fit Emissivity from TCs…* opens the setup: the TC logger
  file (`.xlsx`, `.csv` or `.txt`; a workbook's sheet named like TC data is
  picked, any other can be chosen), the TCs to use (TCs in air or gas start
  unticked) with an optional "Use only" window of seconds per TC, the search
  region (whole image or a box ROI), the preset of a superframing recording,
  the object parameters (the Measurement panel's or the recording's own), and
  the time match (automatic, or "logger time 0 is at frame…").
- The run reads every frame once and keeps per-second statistics in the
  results folder (`<recording>_tcmatch` next to it), so later runs with the
  same region take a minute or two. Its progress names each step; Cancel
  stops it.
- It finds the logger's time offset and each TC's pixel by correlating
  every pixel's counts with the TC's blackbody signal, chooses the stretches
  to fit by rules that never compare IR with TC (TC working and hot enough, no
  flames in front of the spot, IR in range, no flame or jet on the TC itself),
  and fits the emissivity per stretch. A TC's value is the median of its
  stretches; TCs on differently painted spots keep their own values.
- The results list each TC's pixel, match, emissivity, number of stretches
  and spread, with the summary and a figure per TC. From there: add a box at
  each TC pixel (the block the fit used) and set the ignition frame to logger
  time 0; set the player's emissivity to a TC's value; or save an Excel
  workbook whose ROIs sit at the TC pixels, start at their TC's emissivity,
  and are compared with the logger data already filled into TC Compare.
- Recordings without a factory temperature calibration are refused before
  the run. The workbook cannot separate superframing presets yet.

### Excel workbook

- An ROI's emissivity override on Settings can now be pre-filled (the TC
  workbook does this); the cached values follow it.

### Under the hood

- `tcmatch` (Qt-free) and `tcdata` hold the method; `python -m
  flir_player.tcmatch RECORDING TC_FILE` runs it from the command line.
  `run_analysis` borrows the player's open recording on the decoder thread,
  like the Excel export.
- openpyxl is a runtime dependency (reading TC workbooks). The build's smoke
  test also reads a TC workbook and runs the TC job.
- Horizontal scroll bars are themed like the vertical ones.
- 26 new tests for the dialogs, the job and the workbook. Two review passes
  found fifteen issues (TCs with similar names sharing an emissivity, a given
  frame rounded to a whole second, late cancels reported as errors or
  successes, result files replaced without asking or over the TC file, a
  crash when quitting during a file read, Enter starting a run); all are
  fixed.

[Full comparison](https://github.com/infinitr0us/FLIR_Player/compare/v0.5.3...v0.6.0)

## 0.5.3 — 2026-10-04

Fixes for FLIR A700 recordings saved by ResearchIR, found in the data of a
battery fire test where an A700, a T650sc and thermocouples filmed the same
cells. Other recordings play and export as before.

### Time base

- A700 recordings whose timestamps run slow are timed by frame number at the
  camera rate. The A700 SEQ files stamp their 30 Hz frames 32.8 ms apart, so
  their clock implied 30.48 fps and drifted 1.6 % (about 56 s per hour)
  against the thermocouple logger and the T650sc. This applies when no preset
  rate is known, the clock rate lies 0.5 to 3 % above a camera rate, and the
  typical interval between frames agrees with that average (slow stamps
  lengthen every interval; dropped frames leave most intervals short).
- Other cameras whose timestamps fit that pattern keep their timestamps, and
  the Source tab and sheet note the camera rate they may be running slow
  against: from a few sampled stamps a slow clock cannot be told apart from
  some irregular frame drops. The A700 records at 30 Hz at most, so no finer
  frame grid can mimic its slow clock.
- It covers the elapsed-time readout, playback pacing, the timeline length,
  and the workbook's frame-number time base and multi-recording alignment.
  The Source tab and the workbook's Source sheet show the camera rate, the
  rate the timestamps imply and how far they run slow. Clock-time columns
  still show the file's own timestamps, and the workbook's camera-timestamps
  time base still spaces rows at the timestamps' own mean rate.

### Object parameters

- A ResearchIR workspace saved in a recording can override the object
  parameters every frame records, and the File SDK applies that override on
  open: the A700 file opened with emissivity 1.0, 3 m and transmission 1.0
  instead of the camera's 0.95 and 1 m. The player now opens such files with
  the camera's values, as *Reset* and the Excel export already did. It
  compares what the SDK applied with the camera's values, so any saved
  override is caught. The Source tab lists the saved values, a notice says
  they are not applied, and *Use Saved ResearchIR Values* in the Measurement
  section applies them.
- The reset button is now called *Reset to Camera Values*.
- The workbook's Source sheet lists a saved override that differs from the
  camera's values and says which values the workbook starts from.

### Under the hood

- `sdktime.recording_rate` / `frame_rate` decide the rate for frame-number
  time in one place, for the player and the workbook; `VideoMetadata` carries
  it (`rate`, `frame_timed`) and the saved values.
- `fff.saved_object_parameters` parses ResearchIR's workspace XML, kept in a
  record of the last frame (type 0xF06) or at the end of an ATS file.
- `fff.saved_object_parameters` takes the workspace whose length prefix runs
  to the end of the file and never raises; a partial override is compared
  field by field.
- 31 new tests, on a 150-frame clip of the A700 recording that keeps its saved
  workspace. Two review passes found eight issues (frame drops mistaken for a
  slow clock, the camera-timestamps time base, overrides the parser missed or
  mislabelled, encodings that could stop a file from opening, a partial
  override that stopped the export, the workbook's wording); all are fixed.

[Full comparison](https://github.com/infinitr0us/FLIR_Player/compare/v0.5.2...v0.5.3)

## 0.5.2 — 2026-10-01

A bug-fix release, mostly for the Excel workbook export, from a review of
0.5.0 and 0.5.1. Measurements and file formats are unchanged.

### Excel workbook

- Exports with many area ROIs no longer fail with "TypeError: 'tuple' object
  is not callable". When their pixels do not fit in Excel's 16,384 columns,
  the larger areas are stored as bins, as intended.
- The ROI charts show the thermocouple trace again. It is converted to the
  display unit in a hidden helper column, which Excel left out of the chart.
- A Validation sample outside the calibration curve no longer stops Excel
  from opening the workbook without a repair. Such rows show #NUM!, and the
  check reads CHECK unless the SDK clamped that sample.
- A cancelled or failed export no longer leaves temporary files behind in the
  Windows temp folder.
- The TC Compare sheet's column limit only applies when that sheet is
  included.
- "Every frame" with camera timestamps is explained in the dialog and on the
  Source sheet: rows follow the first recording's mean frame interval and
  take the nearest frame, so a frame can repeat or be skipped where the
  camera clock has gaps. With the frame-number time base there is exactly one
  row per frame, as before.

### Recordings and ROI sets

- ATS/SFMOV recordings that run over New Year keep counting forward instead
  of jumping back a year, in the player and in the workbook (the camera
  clock's day counter restarts at 1).
- Loading a file that is not a valid ROI set (another JSON file, wrong
  types, non-finite coordinates) shows an error instead of doing nothing.
  ROI names with line breaks are read as one line.

### Player

- A finished bitmask export can no longer close the progress dialog of an
  export started after it.
- No error message pops up while the player is closing.
- The Clipping tooltip says what the overlay shows: pixels the SDK clamped at
  the low or high end of the calibrated range, saturated ones included.

### Under the hood

- Bitmask exports report on their own `bitmasks_finished` signal.
- `write_workbook` gives XlsxWriter a private temporary folder and removes it
  however writing ends.
- 34 new regression tests; the real-Excel tests also cover automatic
  atmospheric transmission per recording.

[Full comparison](https://github.com/infinitr0us/FLIR_Player/compare/v0.5.1...v0.5.2)

## 0.5.1 — 2026-09-26

This release polishes the interface: the cursor readout works in every ROI
tool and describes the ROI being drawn, the window chrome behaves like a
native window, and every dialog is themed. Measurements, exports and file
formats are unchanged.

### ROIs and the cursor readout

- The coordinates and value under the pointer are shown with every ROI tool,
  not only Select. A second line gives the geometry of the ROI being drawn,
  moved, resized or hovered, in the pixels its statistics measure: box and
  ellipse pixel ranges and size (ellipses also their pixel count), line
  endpoints, length and angle, spot position and value.
- The readout stays on screen when zoomed in, moves out of the pointer's way,
  and shortens long ROI names rather than running off the view.
- Esc cancels an ROI being drawn or edited. The outline being drawn has a
  dark edge, so it shows on hot imagery.
- In Select mode the pointer shows what a press will do: move, resize, pan
  (when zoomed) or jump the minimap.
- The shape tools have outline icons: the ellipse no longer looks like a
  filled dot, nor the line like "=".

### Window and timeline

- The timeline handle is no longer cut off at the top, nor are the play-range
  markers. The handle lights up on hover, the markers show a resize pointer,
  and hovering the timeline shows the frame and time there.
- Minimize, maximize and close share one 46 × 55 click target with no gaps;
  close turns white on red.
- The frameless window resizes from every edge and corner, not only the
  bottom-right grip.
- The frame and time readouts reserve their full width, so the timeline no
  longer shifts as digits are added during playback.

### Dialogs and feedback

- Message boxes and progress dialogs use the themed title row of the other
  dialogs instead of a native (light, in Windows light mode) title bar. Error
  text wraps inside long paths and can be copied exactly. "Replace existing
  files?" defaults to No. A cancelled export says "Cancelling…" until the job
  has stopped.
- The app declares itself dark to Windows, so windows that keep a native frame
  (the colour picker) get a dark title bar too.
- Saves, exports, extractions and loaded ROI sets are confirmed by a brief
  notice on the image; they used to change only the Export button's tooltip.
  Messages such as "Draw an ROI first" are visible too.
- Spin boxes with steppers (movie, series, extract, reference and Excel
  dialogs) show themed arrows instead of dark blocks; the Batch Extract list
  is no longer a white box; browse buttons show a folder icon; disabled
  checked boxes are muted instead of accent-coloured.
- Error messages appear over the player, not wherever Windows centres them.

### Inspector and analysis panel

- Control groups are evenly spaced, and the Segmentation / Isotherm limit
  fields line up with the other inputs.
- The Temporal tab labels its statistic picker instead of repeating the tab's
  name.
- The image has keyboard focus at startup, so the Open button no longer sits
  in its focus ring.

### Under the hood

- `geometry.area_extent` gives the exact pixel extent and count of a box or
  ellipse in O(rows); it agrees with `roi_coordinates` on every tested shape
  and takes 0.1 ms for a full-frame 1280 × 1024 ellipse (28 ms via the
  coordinates).
- `style.apply_app_theme` sets up style, fonts, stylesheet and colour scheme
  in one place; `MessageDialog` and `ProgressDialog` replace `QMessageBox`
  and `QProgressDialog`.

[Full comparison](https://github.com/infinitr0us/FLIR_Player/compare/v0.5.0...v0.5.1)

## 0.5.0 — 2026-09-26

This release adds an Excel workbook export for comparing IR with
thermocouples in fire tests, and moves to FLIR File SDK 2026.1.2.

### Excel workbook

- *Export → Excel workbook (live temperatures)…* writes the raw counts at the
  ROIs of one or more recordings (e.g. several cameras filming one test),
  sampled every *x* seconds or every frame.
  - Rows are seconds from each recording's ignition frame, so cameras aligned
    by hand at ignition line up.
  - Temperatures are live Excel formulas: changing emissivity, reflected or
    atmosphere temperature, humidity, transmission or an external window
    recalculates every value, chart and summary.
  - Settings can be changed globally, per recording or per ROI.
- The formulas use the recording's own calibration: the FFF CameraInfo record
  of SEQ/CSQ files. It is verified against the File SDK, including under
  changed parameters, before live formulas are written. The Validation sheet
  repeats that check inside the workbook. Parameter combinations outside the
  calibration curve give `#N/A`, not a number.
- Area ROIs keep every pixel, giving an exact mean temperature. Areas above
  400 pixels keep 128 bins of equal apparent-temperature width instead (mean
  within 0.01 K). Extremes and percentiles convert exactly. The export fits
  its pixel storage to Excel's 16,384-column limit.
- Samples outside the calibrated range are shown grey and saturated ones red,
  independent of emissivity. Optionally, values are clamped like FLIR's
  software, on counts, so spots and area means clamp alike.
- TC Compare sheet: paste logger data and map each ROI to a thermocouple. It
  gives IR − TC, the emissivity that would make them agree (above 1 means no
  emissivity can) and the best-fit emissivity over a time window. For areas,
  both come from the mean radiance.
- Summary sheet: baseline, peak and time of peak, rise, time to three
  thresholds, the share of flagged samples, and TC statistics. Also Charts
  (grouped by ROI name across recordings), ROI Map, Source (calibration
  provenance) and a Start Here sheet.
- ROI sets (*Export → Save / Load ROI set…*) keep the ROIs and the ignition
  frame next to the recording, for reuse across tests and for multi-camera
  workbooks.

### FLIR File SDK 2026.1.2

- **The player is tested with FileSDK 2026.1.2 and recommends it.** On the
  sample recordings it returns bit-identical frames, statuses and statistics
  to 5.0.1.
- **The upgrade removes a 5.0.1 crash.** With 5.0.1, switching units on
  recordings with a ResearchIR user calibration crashed the process in 4 of 4
  unit-cycling runs; 2026.1.2 passed all runs.
- **The DLLs load by themselves now.** The player loads the SDK's native DLLs
  from `fnv/_lib` on import, because the 2026.1 extension modules cannot find
  them. `FLIR_SDK_DEBUG=<file>` logs the preload.
- **ATS/SFMOV dates are repaired.** Their clocks carry no year and the SDK
  dates them in 1976, a day early after February. The player takes the year
  from the file's modification time and notes this in the Source panel.
- **Extraction progress counts completed frames with any SDK.** 2024.7 and
  later count from 0.
- The newer SDK also shows two more ATS header entries, `ClockFrequency` and
  `DBMFPixelCount`.

### Fixes

- The clipping overlay also marks pixels the SDK clamped at the high limit
  (saturated), not only those clamped at the low limit.

### Packaging

- **The executable bundles the newest Visual C++ runtime** on the build
  machine (Windows' own or PySide6's) instead of the Conda environment's
  14.27. FileSDK 2026.1 needs 14.40 or newer.
- **The build smoke test ran inside the activated Conda environment**, where
  Conda's DLLs could mask a missing bundled one. It also treated crashes as
  passes, because a crash's negative exit code slips past `if errorlevel 1`.
  It now runs with a Windows-only `PATH`, requires an exit code of exactly 0,
  tests `1.ats` as well as `2.seq` when present, and exports a workbook.

### Under the hood

- New Qt-free modules: `radiometry` (FLIR measurement formula), `fff`
  (CameraInfo parser), `calibration` (SDK verification), `excel_export`
  (sampling), `workbook` (XlsxWriter layout) and `sdktime` (ATS dates).
- New dependency: XlsxWriter; tests use openpyxl.
- Opt-in `tests/test_excel_live.py` (`FLIR_EXCEL_LIVE=1`) checks the formulas
  in Microsoft Excel itself.

[Full comparison](https://github.com/infinitr0us/FLIR_Player/compare/v0.4.2...v0.5.0)

## 0.4.2 — 2026-09-23

This release fixes the findings of a whole-package review of 0.4.1: side
effects of that release's safety fixes, measurement accuracy at the pixel
level, and controls that drifted from the data they describe.

### Measurements

- Spots and line endpoints measure the pixel they are drawn on; before, a
  click in the right or bottom half of a pixel measured its neighbour.
  Flipped views no longer shift boxes/ellipses by one pixel, the hover probe
  reads the pixel under the pointer and draws its marker there (also at
  125–175 % display scaling), and export overlays follow the same convention.
  Boxes and ellipses can include the last row and column.
- Isotherm, segmentation and fixed-range limits survive object-parameter,
  correction and domain-preserving filter changes. When the domain does
  change they re-seed like first enabling them, so an isotherm no longer
  paints the whole frame. A unit change overtaken by a frame step still
  converts them.
- Statistics CSV columns are matched to ROIs by identity, and boxes/ellipses
  too thin to cover a pixel are no longer created, so values can no longer
  shift under another ROI's name.
- Radiance and other small-valued units get enough decimals in the fixed
  range, segmentation and isotherm fields, the legends and the readouts; raw
  CSV export is lossless; emissivity and transmissions take three decimals.
- Whole-image statistics reduce in float64; the temporal average recovers
  exactly after an extreme-magnitude frame leaves its window.

### Exports

- Confirmed replacements are honoured (including the Save dialog's own
  prompt) and restored if the job fails. If a previous version cannot be put
  back, it is kept as "name (previous)" and the error says so. Export dialogs
  suggest unused names and ask before replacing, while still open.
- Outputs publish on drives without hard links (FAT32/exFAT USB sticks).
- Movie decimation explains that the frame rate is not divided by N.

### Interface

- Controls return to the state in force when the decoder rejects a unit,
  parameter, correction, reference or filter change (unless a newer change
  already replaced it); a superseded file open can no longer populate the
  window.
- The hover readout follows new frames, unit changes and flips.
- The palette editor's Color… and Remove Stop act on the clicked stop;
  renaming a custom palette retires the old name; built-in names are reserved.
- Clicking the timeline jumps there (and can be dragged); wheel and keyboard
  steps seek too.
- An external reference can use any of its frames, not only as many as the
  open recording has.
- Integer Counts histograms use integer-aligned bins (no comb pattern).

### Performance

- With a temporal filter, re-serving the current frame (metadata tab,
  clipping toggle, ROI edits) no longer replays its window, and seeks reuse
  cached pre-temporal frames; typed filter values apply on Enter, and pending
  filter states coalesce. A frame whose processing fails (e.g. out of memory)
  is recomputed on retry, never served from the previous window.

### Validation and development

- 266 tests pass, including 52 new regression cases; eight existing Qt
  deprecation warnings remain. With SDK imports disabled, 154 tests pass and
  108 skip.
- Rendered-pixel tests compare the probe, spots and boxes with what the canvas
  draws under every flip, including at 125/150/175 % display scaling.
- Output transactions are tested against missing hard links, failed or
  locked restores, and files changed by other programs mid-job.
- Source smoke tests pass on ATS, SEQ and CSQ and at 125 % scaling; builds
  smoke-test the executable with throw-away preferences.

Native Windows file dialogs, a physical FAT32/exFAT drive and
correction-bearing recordings were not exercised by these tests. This is a
source-only release: obtain the proprietary FLIR File SDK separately and run
from source or build locally.

[Full comparison](https://github.com/infinitr0us/FLIR_Player/compare/v0.4.1...v0.4.2)

## 0.4.1 — 2026-09-05

This release fixes analysis consistency, output safety and interactions between
playback, processing, measurements and exports.

### Output safety and shutdown

- Exports and clip extraction stage their output before publishing. Existing
  destinations are refused; choose a new filename. Source aliases are rejected,
  and failures or cancellation clean up only files owned by the job.
- Batch extraction allocates distinct destinations for colliding basenames and
  writes a JSON report with every input, output path and outcome. Cancellation
  is honored during the current file, including when the SDK returns success.
- Closing waits responsively for the decoder and writers to finish. The source
  smoke-test and capture entry points follow the same shutdown rule.

### Consistent numerical analysis

- Counts reference arithmetic promotes integer operands before calculating,
  preserving negative differences and large products.
- Temporal filters use the trailing source-frame window, independent of cache
  history, seeks, backward steps, metadata refresh and export decimation.
- Reference frames reload inside unit, parameter and correction transitions;
  failed changes preserve the previous measurement state.
- Changes to analysis settings clear temporal plot history with a visible note.
  ROI edits clear the affected history. Display-only changes preserve history.
- Compatible temperature thresholds convert with their units. Temperature
  differences omit absolute-temperature offsets; incompatible domain or source
  changes reset limits to the new frame range.
- Object-parameter edits preserve untouched full-precision values and respect
  the SDK's editability state.

### ROI, export fidelity and controls

- Statistics, histograms, profiles and mask exports share ROI pixel coverage.
  Lines sample each major-axis pixel once. Long diagonal line measurements can
  differ slightly from the SDK's native sampling; all application paths use the
  same documented rule. Ellipses use pixel-center coverage.
- ROI resize handles preserve the opposite corner with reversed endpoints and
  horizontal/vertical flips. Paused plots refresh immediately after selection.
- Statistics CSV saves one coherent displayed snapshot. Still-image export
  captures the selected frame and display state before opening its dialog.
- Numeric TIFF16 preserves unprocessed uint16 Counts exactly. Other values carry
  a reconstruction mapping, clipping policy and analysis provenance in TIFF
  metadata; optional CSV sidecars include the mapping. Numeric TIFF formats
  disable RGB composition controls.
- Plateau-equalized legends use the actual frame transfer function. MP4/WMV
  encoding preserves dimensions and pads odd edges instead of cropping or
  implicitly stretching the image.
- Playback respects both play-range bounds. Spatial kernel controls display
  the odd kernel actually applied.

### Validation and development

- 214 tests pass, including 59 new regression cases; eight existing Qt
  deprecation warnings remain. With SDK imports disabled, 116 tests pass and
  94 skip, including the corrected CSQ availability guard.
- Tests use temporary INI preferences instead of the user's registry settings.
- Real ATS, SEQ and CSQ first/middle/last-frame checks, TIFF and MP4/WMV readback,
  source playback smoke testing and four delayed-close subprocess cases passed.
- Release metadata records successful verification only after the executable
  smoke test passes. Rendering scratch buffers are bounded across resolutions.

Native Windows file dialogs, unavailable correction-bearing recordings and a
new packaged executable are not certified by these tests. This is a source-only
release, consistent with previous releases: obtain the proprietary FLIR File SDK
separately and run from source or build locally.

[Full comparison](https://github.com/infinitr0us/FLIR_Player/compare/v0.4.0...v0.4.1)
