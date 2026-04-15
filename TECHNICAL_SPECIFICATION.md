# Translog Log Parser — Technical Specification

## 1) Purpose

This specification defines the current architecture, data contracts, algorithms, frontend behaviors, and extension strategy of the project.
Goal: enable a future engineer or AI system to safely implement second-development without re-discovering implicit behavior from code.

---

## 2) System Scope

Input:

- Translog-II XML logs (`.xml`) containing text payloads, event streams, and char-position maps.

Output:

- Single interactive HTML report (self-contained payload + runtime CDN libraries).

Execution:

- GUI mode: `run_gradio()`
- Headless mode: `main()` with `--headless`

Runtime frontend libraries:

- Plotly
- html2canvas

---

## 3) End-to-End Runtime Pipeline

1. `parse_xml(...)` parses XML into normalized Python structures.
2. Backend computation generates:
   - summary metrics,
   - action catalogs and time bins,
   - cursor/time maps,
   - MT→PE segment timeline,
   - heat arrays and chart payloads.
3. `build_report_html(...)` composes:
   - HTML layout,
   - CSS theme,
   - JS logic,
   - embedded JSON payload constants.
4. `generate_report_file(...)` writes report HTML to disk.
5. Browser executes embedded JS for interactivity and exports.

---

## 4) Architecture and Responsibility Boundaries

## 4.1 Backend (Python)

Core responsibilities:

- XML parsing and normalization.
- Metrics and model preparation.
- Static report template emission (HTML/CSS/JS as one string).
- File IO and execution mode dispatch.

Design note:

- This codebase keeps almost all logic in `app.py` (monolithic by design).

## 4.2 Frontend (Embedded JS)

Core responsibilities:

- Rendering interactive charts and overlays.
- User controls, state updates, and panel linkage.
- CSV/PNG export interactions.
- Pivot table and quick preset workflows.

---

## 5) Data Model Contracts

## 5.1 `Event` (dataclass)

Fields:

- `tag`
- `time_ms`
- `cursor`
- `value`
- `event_type`
- `text`
- `block`
- `x`, `y`

Derived:

- `action_label` (normalized action taxonomy; used by trend/action logic)

## 5.2 `CharPos` (dataclass)

Fields:

- `cursor`
- `value`
- `x`, `y`
- `width`, `height`

Used by:

- heat overlays,
- char-position exports,
- cursor-to-text visual linkage.

## 5.3 `parse_xml(...)` output dictionary

- `source_text`
- `target_text`
- `final_text`
- `events`
- `project_start`
- `project_end`
- `target_chars`
- `source_chars`
- `final_chars`

---

## 6) Core Algorithms and Current Semantics

## 6.1 Reading Speed Model

Computed from cursor-section progression:

- section index: `floor(max(0,cursor)/granularity)`
- per-step base: `distFromStart / elapsedFromStartSec`
- revisit deduction: `revisitPenaltyWeight * (overlap / elapsedFromStartSec)`
- step score: `base - deduction`

IME filtering:

- `key:ime` excluded from reading progression stream.

## 6.2 Edit Intensity

- Aggregated from selected action labels by time/position windows.
- User-selectable action set drives intensity line and related exports.

## 6.3 Segment Duration and Vicinity

- Segment timing can be re-estimated via vicinity controls:
  - `Vicinity Before`,
  - `Vicinity After`,
  - `Adaptive` clamp behavior.

## 6.4 Pause Metrics

Window-level and segment-level pause semantics:

- pause is accumulated from inter-event gaps satisfying `gap_ms >= pause_threshold_ms`.
- `pause_threshold_ms` is user-configurable in Data Export.

Segment-level source precedence:

1. Use segment `raw_xml` event times if present.
2. Fallback to vicinity activity events if raw times are unavailable.

Derived fields include:

- `pause_seconds`
- `duration_seconds`
- `active_edit_seconds`
- `pause_ratio_pct`
- `pause_to_active_ratio`
- `pause_source`

---

## 7) Frontend Panel Specifications

## 7.1 Time-based Trend Panel

Features:

- Reading/Editing dual-line plot.
- Reading granularity + revisit penalty controls.
- Mathematical help panel (collapsible).
- PNG/CSV export.

## 7.2 Position-based Trend Panel

Features:

- Position-indexed reading/edit intensity.
- Separate granularity and revisit controls.
- PNG/CSV export.

## 7.3 Heat Map Overlay

Features:

- MT/PE text mode switching.
- Reading/editing heat mode switching.
- Normalization control.
- Char click probe and contextual hints.

## 7.4 Compiled Text + Segment Popup

Features:

- MT→PE colored token timeline.
- Segment hover popup:
  - metadata,
  - vicinity timing,
  - raw XML,
  - PNG export.

## 7.5 Cursor Movement Timeline

Features:

- Forward/backward movement.
- Pause/focus-loss lanes.
- Compare points mode.
- Window/range controls.
- PNG/CSV export.

## 7.6 Data Export + Pivot

Features:

- Dataset checkbox matrix with bulk select/clear.
- Pivot controls: Row/Column/Value/Agg/Chart.
- Quick-apply presets.
- Pivot CSV export with conditional segment enrichment.
- Collapsible in-panel help with formulas and examples.

---

## 8) Export Subsystem

## 8.1 CSV encoding

- UTF-8-SIG behavior via BOM prefix (`\uFEFF`).
- MIME: `text/csv;charset=utf-8`.

## 8.2 Export domains

- Summary metrics and action tables.
- Trend and timeline series.
- Segment metadata and vicinity detail.
- Replay/activity event tables.
- Char maps and text payloads.
- Pivot outputs.

## 8.3 Pivot `segment_id` export enrichment

When pivot Row field is `segment_id`, pivot CSV appends:

- `segment_mt_text`
- `segment_pe_text`
- `segment_raw_xml`

---

## 9) Pivot Engine Specification

## 9.1 Internal flow

1. Build object-array datasets (`buildDataExportDatasets`).
2. Merge selected datasets and inject `dataset` key.
3. Infer selectable fields from key union.
4. Aggregate grouped values by selected function.
5. Render table + chart.
6. Export pivot CSV.

## 9.2 Aggregations

- `count`
- `sum`
- `avg`
- `min`
- `max`

Numeric coercion behavior:

- non-numeric values are ignored by numeric aggregations.
- `count` remains the most interpretable for text keys.

## 9.3 Reactive triggers

Pivot recomputation is triggered by:

- dataset selection changes,
- vicinity before/after changes,
- adaptive vicinity toggle,
- pause threshold slider changes.

---

## 10) Key Function Index

Parsing + normalization:

- `parse_xml`
- `parse_char_map`

Metrics + transforms:

- `build_metrics`
- `build_binned_action_counts`
- `build_action_summary`
- `build_action_catalog`
- `build_cursor_first_time`
- `build_paragraph_markers`
- `reconstruct_full_change_timeline`

Report generation:

- `build_report_html`
- `generate_report_file`

Execution:

- `run_gradio`
- `main`

---

## 11) Extension Guidelines

## 11.1 Add new dataset to Data Export/Pivot

1. Extend `buildDataExportDatasets`.
2. Add checkbox in Data Export panel.
3. Add help-field descriptions if new keys are introduced.
4. Add Quick Apply preset if it represents a common workflow.

## 11.2 Add new pivot preset

1. Add button in Quick Apply area.
2. Add preset object in `applyPivotPreset`.
3. Add recommendation entry in Pivot Help section.

## 11.3 Add new pause-like metric

1. Decide source priority (`raw_xml` vs derived events).
2. Add explicit threshold rules and expose if configurable.
3. Document formula in help and this specification.

## 11.4 Add new chart panel

1. Add HTML/CSS block in `build_report_html`.
2. Add payload constant and render function.
3. Register listeners in initialization zone.
4. Add export pathway if needed.

---

## 12) Quality and Regression Checklist

- Headless generation succeeds with sample XML.
- No diagnostics errors after changes.
- Trend, heat, cursor, and segment popup panels render.
- Pivot field list and presets work after reactive control changes.
- Segment pause ratio remains explainable from raw segment events.
- CSV exports open correctly and include expected columns.

---

## 13) Known Tradeoffs

- Single-file architecture improves portability but increases local complexity.
- Several analytics are intentionally heuristic (e.g., vicinity duration).
- Input quality variability strongly affects derived signals.
- CDN dependency exists for full frontend runtime behavior.

---

## 14) Key Files

- `app.py`: backend + frontend template source.
- `README.md`: user-level guide.
- `XML_PARSING_GUIDE.md`: parser-specific schema behavior.
- `raw_log.xml`: sample input.
- generated `sample_report_*.html`: integration artifacts.
