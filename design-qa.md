# Design QA

> **Note:** the interface has since been redesigned beyond the original mock.
> Current visual truth: the design tokens in `flir_player/style.py` and
> `design/current-ui.png` (verification captures in `artifacts/redesign/`).
> The sections below document the earlier mock-fidelity pass.

- Source visual truth: `design/option-1-reference.png`
- Implementation screenshot: `artifacts/implementation/final-loaded.png`
- Full-view comparison: `artifacts/implementation/qa-final-full-comparison.png`
- Viewport: 1487 × 1058 px
- State: `fire2.seq`, frame 1 of 2685, Counts, Iron, Dynamic range, paused

**Findings**

- No actionable P0, P1, or P2 differences remain.
- [P3] The source mock vertically stretches the 640 × 480 frame to fill more of the stage. The implementation intentionally preserves the camera's 4:3 pixel geometry and letterboxes it. This protects spatial and pixel-probe accuracy.
- [P3] The mock uses decorative timeline tick dots; the implementation uses a quieter continuous native slider whose handle, filled progress, focus state, and keyboard behavior carry the interaction.
- [P3] The mock's surface treatment has a faint generated texture. The implementation uses sampled solid tokens so the native Qt interface remains crisp and stable at different sizes.

**Required Fidelity Surfaces**

- Fonts and typography: Segoe UI regular, semibold, and bold faces are loaded explicitly. Header, section, field, metadata, and timecode hierarchy match the source without clipping at the target viewport.
- Spacing and layout rhythm: 64 px title bar, 116 px transport bar, fixed inspector track, image-stage margins, button sizes, field spacing, dividers, and radii reproduce the selected composition. A compact inspector state prevents overlap at 1080 × 720.
- Colors and visual tokens: title/transport `#181c20`-family surfaces, inspector `#1a1e22`, black image stage, amber `#f29f05` interaction accent, neutral borders, disabled values, and focus states were aligned to sampled source colors.
- Image quality and asset fidelity: the live File SDK frame is rendered directly from its NumPy data with a 256-entry Iron LUT and nearest-pixel scaling. The image is not a placeholder and is not stretched. The calibrated color scale uses the real frame extrema (6445–6649), not the mock's invented values.
- Copy and content: application title, filename, visualization labels, range mode, frame count, timecode, speed, resolution, and export language match the source. Camera identity and cursor value were added as useful real metadata.
- Icons: the final app uses one Font Awesome 6 family through QtAwesome for file, export, transport, window, chevron, unit, and full-screen actions. There are no handcrafted SVG or text-glyph icon substitutes.
- Interaction states: empty/disabled, loaded, dynamic/fixed range, unit change, palette change, playback, pause, seek, speed, export, full screen, focus, hover-capable controls, and compact-height behavior are implemented.
- Accessibility: controls have labels/tooltips, 40 px-or-larger primary targets, keyboard shortcuts, visible focus borders, disabled-state contrast, and non-color state cues. Exact screen-reader output was not evaluated in the offscreen visual pass.

**Focused Region Evidence**

- Header comparison: `artifacts/implementation/qa-focus-header-final.png`
- Inspector comparison: `artifacts/implementation/qa-focus-inspector-final.png`
- Transport comparison: `artifacts/implementation/qa-focus-transport-final.png`
- Responsive implementation: `artifacts/implementation/responsive-1080x720-pass-2.png`
- Empty/disabled implementation: `artifacts/implementation/empty-state.png`

Focused comparisons were required because icon sizing, small labels, control heights, disabled states, and timecode density were not readable enough in the downscaled full-view comparison.

**Comparison History**

1. Initial implementation — `artifacts/implementation/qa-pass-1.png`
   - Earlier P1: title and transport surfaces inherited the near-black root instead of the selected graphite tokens.
   - Earlier P2: controls used inconsistent platform icons and several icons disappeared in Qt's offscreen platform.
   - Earlier P2: inspector typography and fields were materially smaller and denser than the source.
   - Fixes: enabled styled native surfaces, sampled the source palette, loaded Segoe UI explicitly, adopted QtAwesome, and normalized field/icon sizes.

2. First visual correction — `artifacts/implementation/qa-pass-2.png`
   - Post-fix evidence: header, inspector, transport, typography, and icons matched their source regions.
   - Newly found P2: at 1080 × 720, disabled fixed-range controls collided with Frame Info.
   - Fixes: added a compact inspector policy that shows Frame Info in Dynamic mode and prioritizes fixed controls in Fixed mode; added explicit disabled states.

3. Final correction — `artifacts/implementation/final-loaded.png`
   - Post-fix evidence: no collisions at 1080 × 720, one coherent icon family, enlarged controls/type, matched surface tokens, and real unit iconography.
   - Functional evidence: `8 passed in 2.07s`; both supplied formats decoded; 1× playback reached frame 36 after 1.2 s for the 29.98 fps SEQ and frame 44 for the 36.47 fps ATS; `pip check` reported no broken requirements.

**Primary Interactions Tested**

- Open and decode both supplied FLIR recordings.
- Timestamp-paced play/pause, frame seek, and clean close.
- Factory temperature conversion, palette change, dynamic/fixed range, PNG export, and full-screen enter/exit.
- Final native capture completed without Qt exceptions or decoder errors.

**Open Questions**

- None blocking handoff.

**Implementation Checklist**

- [x] Match the selected desktop composition and visual tokens.
- [x] Preserve real thermal geometry and calibrated values.
- [x] Implement the complete primary playback and inspection journey.
- [x] Verify compact-height behavior and disabled states.
- [x] Pass functional, dependency, and final visual gates.

**Follow-up Polish**

- Optional P3: add a user-selectable Fill mode for people who prefer edge-to-edge video and accept cropping; keep Fit as the scientific default.

final result: passed
