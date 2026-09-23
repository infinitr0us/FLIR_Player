# Changelog

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
