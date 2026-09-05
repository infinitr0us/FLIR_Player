# Changelog

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
