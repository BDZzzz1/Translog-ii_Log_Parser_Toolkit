# External Activity Recorder + Parser Technical Specification

## 1) Scope

This specification defines architecture, data contracts, runtime flows, and extension points for:

- `external_activity_recorder.py`
- `external_activity_parser.py`
- browser companion extension (`external_activity_extension/background.js`)

It focuses on **external activity** analytics and excludes internals of `app.py` (Translog core report generator).

---

## 2) Recorder Architecture

## 2.1 Core modules

- Foreground window monitor:
  - polls active window (`win32gui` + `win32process`),
  - emits switch events,
  - accumulates dwell spans.
- Global input listeners:
  - keyboard (`pynput.keyboard.Listener`),
  - mouse (`pynput.mouse.Listener`).
- Browser webhook server:
  - local `ThreadingHTTPServer`,
  - accepts JSON POST on `/browser-event`.
- XML serializer:
  - writes normalized buckets into XML sections.

## 2.2 Lifecycle

1. `start()`
   - starts HTTP server, listeners, monitor thread.
   - writes `recording_started` system event.
2. runtime
   - monitor tracks active-window transitions.
   - webhook stores browser companion events.
3. `stop()`
   - stops running flag/listeners/server,
   - flushes final dwell,
   - writes `recording_stopped`,
   - attempts browser-title URL recovery for current browser window.
4. `write_xml()`
   - writes full log file and recorder config metadata.

## 2.3 XML sanitization rule

`safe_text` enforces XML-valid character set:

- valid XML chars are preserved,
- invalid control chars are converted to `\uXXXX`.

This prevents malformed XML when key/control input contains non-XML codepoints.

---

## 3) Recorder Data Contract (XML)

Root:

- `ExternalActivityLog` (attribute: `version`)
- `startTime`
- `endTime`
- `RecorderConfig`

Sections:

- `SystemEvents/Event`
- `BrowserEvents/Event`
- `InputEvents/Event`
- `WindowDwell/Span`

Common event fields:

- `type`, `tsMs`, `tsIso`, plus event-specific attributes.

Window dwell span fields:

- `windowKey`, `title`, `process`, `hwnd`,
- `startMs`, `endMs`, `durationMs`.

---

## 4) Parser Architecture

## 4.1 Parse stage

- `parse_external_log(path)` reads XML and converts known numeric attributes.
- Produces `ExternalLog` dataclass:
  - `start`, `end`
  - `system_events`, `browser_events`, `input_events`, `dwell`

## 4.2 Summarization stage

`summarize_external(ext)` derives:

- duration/counter metrics,
- top sites/domains and exact URLs,
- process dwell aggregates,
- window switch rows with related URL and typing samples,
- typing reconstruction (browser input + system key stream),
- synchronization-ready tabular datasets.

## 4.3 Dashboard rendering stage

`render_external_panel(payload, title)` returns embedded HTML/CSS/JS:

- cards, interactive charts, details tables,
- return analytics logic,
- export workflows.

`build_external_dashboard(...)` embeds payload and wraps full standalone HTML.

---

## 5) Time & Sync Model

Base timeline:

- recorder-relative milliseconds (`tsMs`/`startMs`/`endMs`).

Sync offset:

- `sync_offset_ms = to_ms(sync_start_time) - to_ms(external_start_time)`
- frontend computes sync fields:
  - `sync_ts_ms`, `sync_ts_sec`, `sync_ts_min`,
  - `sync_end_ms`, `sync_end_sec`.

---

## 6) Main Window + Research Model

Main-window detection:

- row is considered “in main window” if dwell process string contains user query.

Return events:

- derived from transitions outside -> inside.
- optional adjacent-return merge:
  - enabled by toggle,
  - merges returns with gap <= user-provided seconds.

Research sessions:

- outside-start to next inside-start.
- includes session URLs, outside titles/processes.

Research stats:

- sessions count, min/max/avg/median duration.

---

## 7) Reading/Edit Trend Model

Input source:

- optional trend CSV from Gradio/CLI.

Time normalization:

- minute-style columns are treated as minutes,
- second-style columns converted to minutes for plotting,
- both `time_min` and `time_sec` retained.

Return-evaluation modes:

- `raise`: peak edit intensity in threshold window > baseline at return.
- `non_zero`: any non-zero edit intensity in threshold window.

Derived return fields:

- return id, grouped session ids, group size,
- baseline time/intensity,
- first raise time,
- first non-zero time.

---

## 8) Export Contracts

CSV exports are UTF-8-SIG.

Supported exports:

- main window timeline CSV,
- research analytics CSV (includes summary metrics + summary line + session rows),
- dataset-selected CSV(s),
- all-in-one external CSV,
- pivot CSV,
- reading/edit + return combined CSV.

PNG exports:

- split-range trend PNG export supports:
  - equal partition by part count,
  - custom ratio list (e.g., `30,30,40`).

---

## 9) Gradio Contracts

Recorder Gradio:

- output path, port, polling interval, key/mouse toggles, controls:
  - start, stop, save, refresh.

Parser Gradio:

- external XML input,
- optional sync start ISO,
- optional main process name,
- optional trend CSV,
- trend window (seconds),
- generate report output.

---

## 10) Extension Points

Recorder:

- Add new webhook event types by extending extension payload schema and recorder append logic.

Parser:

- Add new dataset for pivot:
  - include in backend payload,
  - expose in frontend `buildExternalDatasets`.
- Add new chart:
  - derive series in JS from payload and render with Plotly.
- Add export fields:
  - append columns in CSV header/body generation.

---

## 11) Validation Checklist

After code/doc changes:

1. `python -m py_compile external_activity_recorder.py external_activity_parser.py`
2. Generate recorder XML (GUI or CLI).
3. Generate parser report (CLI with and without trend CSV).
4. Verify:
   - tables/charts render,
   - return-click linkage updates detail panes,
   - CSV and PNG exports complete.

