# XML Parsing Guide

This guide documents exactly how this project parses Translog-II XML and how parsed values propagate into analytics and exports.

---

## 1) Parsing Pipeline Summary

`parse_xml(...)` performs:

1. XML load with `xml.etree.ElementTree`.
2. Text extraction (`SourceTextUTF8`, `TargetTextUTF8`, `FinalTextUTF8`).
3. Event extraction from `<Events>` children into `Event` objects.
4. Event sorting by `time_ms`.
5. Char-map extraction (`SourceTextChar`, `TargetTextChar`, `FinalTextChar`) into `CharPos`.
6. Return normalized dictionary used by report-generation functions.

If `<Events>` is missing, parsing fails intentionally.

---

## 2) Expected XML Shape

Representative structure:

```xml
<LogFile>
  <Project>
    <Interface>
      <Standard>
        <Settings>
          <SourceTextUTF8>...</SourceTextUTF8>
          <TargetTextUTF8>...</TargetTextUTF8>
        </Settings>
      </Standard>
    </Interface>
  </Project>

  <Events>
    <System ... />
    <Mouse ... />
    <Key ... />
  </Events>

  <FinalTextUTF8>...</FinalTextUTF8>
  <SourceTextChar>...</SourceTextChar>
  <TargetTextChar>...</TargetTextChar>
  <FinalTextChar>...</FinalTextChar>
</LogFile>
```

Strict requirement:

- `<Events>` must exist.

Soft/optional:

- char maps can be absent; parser returns empty lists for missing maps.

---

## 3) Text Payload Parsing

## 3.1 Parsed fields

- `SourceTextUTF8` → `source_text`
- `TargetTextUTF8` → `target_text`
- `FinalTextUTF8` → `final_text`

## 3.2 Not used for analytics

- `SourceText` / `TargetText` non-UTF8 payloads (e.g., RTF-like).

## 3.3 Downstream usage

- source: source sentence hints and length metrics.
- target: MT baseline for MT→PE diff.
- final: PE endpoint for MT→PE diff and PE-related displays.

---

## 4) Event Parsing Contract

Each `<Events>` child becomes:

```python
Event(tag, time_ms, cursor, value, event_type, text, block, x, y)
```

Field mapping:

- `Time` → `time_ms` (`int`, default `0`)
- `Cursor` → `cursor` (`int | None`)
- `Value` → `value` (`str`, default `""`)
- `Type` → `event_type` (fallback = tag)
- `Text` → `text` (`str`, default `""`)
- `Block` → `block` (`int | None`)
- `X` / `Y` → `x` / `y` (`int | None`)

Ignored attributes currently include examples like:

- `IMEtext`
- `Width`, `Height` (on event nodes)

---

## 5) Event Label Normalization

`action_label` normalizes events for analytics:

- Key:
  - `insert` → `key:insert`
  - `delete` → `key:delete`
  - `ime` → `key:ime`
  - `navi` + value → `key:navi:[left]`, etc.
  - `edit` + value → `key:edit:[ctrl+v]`, etc.
- Mouse:
  - e.g. `mouse:down`, `mouse:up`
- System/others:
  - e.g. `system:start`, `system:stop`

This label powers:

- action summary,
- edit-intensity filtering,
- many exports.

---

## 6) Char Map Parsing

Sections:

- `SourceTextChar`
- `TargetTextChar`
- `FinalTextChar`

Each `<CharPos ... />` is parsed to:

```python
CharPos(cursor, value, x, y, width, height)
```

Used by:

- heat-overlay geometry,
- char-map exports,
- click probe mapping.

---

## 7) `parse_xml(...)` Return Schema

Returned object keys:

- `source_text: str`
- `target_text: str`
- `final_text: str`
- `events: list[Event]`
- `project_start: str`
- `project_end: str`
- `target_chars: list[CharPos]`
- `source_chars: list[CharPos]`
- `final_chars: list[CharPos]`

This schema is consumed by `build_report_html(...)` and helper builders.

---

## 8) How Parsed XML Feeds Analytics

## 8.1 Session metrics

Derived from:

- text lengths,
- event-time ranges,
- Levenshtein distance.

Includes:

- session duration,
- change rate,
- correction count,
- initial delay.

## 8.2 Trend models

- Reading: cursor progression (IME excluded).
- Editing: selected action counts per window/position.

## 8.3 Heat overlays

- Geometry from char maps.
- Intensity from event-derived arrays.

## 8.4 MT→PE segment reconstruction

- `difflib.SequenceMatcher` on target vs final.
- Segment metadata enriched with event cues and raw XML snippets.

## 8.5 Pause-related metrics

Derived from event times:

- timeline pause from adjacent activity-event gaps,
- segment pause ratio from segment raw event times (preferred) or fallback.

Threshold:

- `pause_threshold_ms` is user-configurable in UI.

---

## 9) Event Timing Nuances

## 9.1 Sorted-time assumption

Events are sorted by `time_ms` after parsing.
All timing models assume monotonic order after this step.

## 9.2 `cursor` interpretation

- Cursor is treated as target-pane character index.
- Missing/invalid cursor values can reduce fidelity in cursor-based models.

## 9.3 IME caveat

IME entries can carry noisy cursor behavior; reading models intentionally exclude `key:ime`.

---

## 10) Segment Raw XML vs Derived Activity

Segment-level pause and ratio calculations follow precedence:

1. Parse event times from `segment.raw_xml` lines.
2. If unavailable, derive from vicinity-bounded `activityEvents`.

This explains why segment pause results are intended to match segment raw rows when raw rows are present.

---

## 11) Parser Assumptions and Limitations

- Strict `<Events>` requirement.
- UTF8 text nodes are preferred and expected.
- Some XML attributes are currently ignored.
- Malformed numeric attributes degrade via safe parsing defaults.
- Analytics quality depends on source XML event fidelity.

---

## 12) Extension Checklist (XML Layer)

When extending parser behavior:

1. Add new fields to `Event` or `CharPos` only if needed downstream.
2. Update `parse_xml(...)` mapping logic.
3. Update any affected analytics functions and exports.
4. Update both:
   - `TECHNICAL_SPECIFICATION.md`
   - `README.md`
5. Validate by generating a headless report and checking panel behavior.

---

## 13) Core Functions to Inspect

- `parse_xml`
- `parse_char_map`
- `build_metrics`
- `build_binned_action_counts`
- `build_action_catalog`
- `build_mt_action_heat`
- `reconstruct_full_change_timeline`
- `build_report_html`

These functions define parser → model → report semantics.
