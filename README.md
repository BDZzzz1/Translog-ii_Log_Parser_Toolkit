# 📊 Translog-II Log Parser + External Activity Toolkit

One repository, two connected analytics tracks:

- 🧠 **Translog-II parser** (`app.py`) for MT/PE process analytics
- 🧭 **External activity toolkit** (`external_activity_recorder.py` + `external_activity_parser.py`) for out-of-Translog behavior

This README consolidates and updates content previously split across:

- `EXTERNAL_ACTIVITY_README.md`
- `EXTERNAL_ACTIVITY_RECORDER_GUIDE.md`

---

## ✨ Why This Project

Translog and external behavior logs are rich but hard to inspect manually.  
This toolkit turns them into interactive dashboards + exportable data for research workflows.

Demos:  
https://bdzzzz1.github.io/external_activity_parser_demo/  
https://bdzzzz1.github.io/translog-ii-log-parser.github.io/

You can use:

- ✅ **Translog parser only**
- ✅ **External recorder/parser only**
- ✅ **Both in tandem** for a fuller post-editing process view

---

## 🧩 What’s Included

### 1) Translog-II Parser (`app.py`)

Builds an interactive HTML report from Translog XML with:

- 📈 time/position trend analytics (reading speed vs edit intensity)
- 🔥 MT/PE heat overlays
- 🧱 segment-level compiled text diagnostics
- 🖱️ cursor timeline and pause analysis
- 🧮 pivot analytics + UTF-8-SIG CSV/PNG exports

### 2) External Activity Recorder (`external_activity_recorder.py`)

Records non-Translog activity on Windows:

- 🪟 foreground window switching
- ⏱️ window dwell spans
- ⌨️ optional global key/mouse events
- 🌐 browser companion extension webhook events

Outputs `external_activity_log.xml` with:

- `SystemEvents`
- `BrowserEvents`
- `InputEvents`
- `WindowDwell`

### 3) External Activity Parser (`external_activity_parser.py`)

Parses external XML into an interactive dashboard with:

- 🧭 main-window presence timeline
- 🔎 research time analytics
- 🎯 left-from-main target count analytics
- 📚 reading speed/edit intensity with return markers
- 🧪 keylogger samples + track-change preview
- 🗂️ data export + pivot (CSV/plot)

---

## 🤝 Tandem Workflow (Recommended)

Use the external toolkit **together** with the Translog parser:

1. Run recorder while participant works.
2. Generate external dashboard from recorder XML.
3. Generate Translog dashboard from Translog XML.
4. Analyze both views together for richer behavioral interpretation.

In short:  
**External recorder + external parser can be used in tandem with the Translog-II log parser** for complete process analytics.

---

## 🖥️ Requirements

- Windows (external recorder depends on `win32gui` / `win32process`)
- Python 3.10+ recommended
- Dependencies in `requirements.txt`

Install:

```bash
pip install -r requirements.txt
```

---

## 🚀 Quick Start

### A) Translog Parser

GUI:

```bash
python app.py
```

Headless:

```bash
python app.py --headless --xml raw_log.xml --output translog_report.html
```

### B) External Recorder

GUI:

```bash
python external_activity_recorder.py
```

CLI:

```bash
python external_activity_recorder.py --cli --output external_activity_log.xml
```

### C) External Parser

GUI:

```bash
python external_activity_parser.py
```

CLI:

```bash
python external_activity_parser.py --cli --external-log external_activity_log.xml --output external_report.html
```

With sync/main/trend options:

```bash
python external_activity_parser.py --cli --external-log external_activity_log.xml --sync-start-time 2026-03-06T20:16:15.169427+08:00 --main-process translog.exe --trend-csv time_based_reading_edit_intensity_trends.csv --window-sec 30 --output external_report.html
```

---

## 🧰 External Browser Extension

Folder:

- `external_activity_extension/`

Setup (Chromium browsers):

1. Open Extensions page
2. Enable Developer mode
3. Load unpacked from `external_activity_extension`

Default webhook:

- `http://127.0.0.1:38953/browser-event`

---

## 🧪 External Dashboard Functions

Current key external panels and controls include:

- **Main Window Presence Timeline**
  - main process query
  - `Ignore first (s)` / `Ignore last (s)`
  - timeline CSV export

- **Research Time Analytics**
  - `Ignore first (s)` / `Ignore last (s)`
  - max/min/avg/median drill-down
  - research CSV export with summary metrics

- **Left-from-Main Target Count**
  - `Ignore first (s)` / `Ignore last (s)`
  - merge-adjacent-returns toggle + gap
  - detailed table (collapsed by default)
  - full CSV export

- **Reading Speed & Edit Intensity with Return Marks**
  - threshold control
  - `Ignore first (s)` / `Ignore last (s)`
  - merge-adjacent-returns toggle + gap
  - raise-percentage and non-zero-percentage modes
  - clickable return markers linked to detail tables
  - combined CSV export + split PNG export

- **Data Export & Pivot (External)**
  - dataset selection + pivot build/chart
  - selected CSV / all-in-one CSV / pivot CSV
  - `Ignore first (s)` / `Ignore last (s)` applied to pivot/export dataset scope

- **External Activity Dashboard summary area**
  - dashboard-level ignore controls to recalculate top-level overview

---

## 🧾 CLI Flag Reference

### Translog (`app.py`)

- `--headless`
- `--xml`
- `--window-sec`
- `--output`

### Recorder (`external_activity_recorder.py`)

- `--output`
- `--port`
- `--poll-interval-ms`
- `--duration-sec`
- `--record-keystrokes`
- `--record-mouse`
- `--gui`
- `--cli`

### External Parser (`external_activity_parser.py`)

- `--external-log`
- `--sync-start-time`
- `--main-process`
- `--trend-csv`
- `--window-sec`
- `--output`
- `--gui`
- `--cli`

---

## 📦 Output Files

- `app.py` → standalone Translog report HTML
- `external_activity_recorder.py` → external XML log
- `external_activity_parser.py` → standalone external report HTML
- Panel exports → UTF-8-SIG CSV and PNG assets where applicable

---

## 🔐 Privacy Notes

- Recorder key/mouse capture may include sensitive content.
- Browser field metadata may include text samples.
- Use only with explicit consent and compliant data governance.

---

## 🛠️ Troubleshooting

- **Graphs not rendering**: regenerate report from latest parser and reload browser.
- **No browser events**: verify extension is loaded and port matches recorder.
- **No return analytics**: set main process name and confirm dwell spans exist.
- **Trend panel empty**: provide `--trend-csv` (CLI) or upload trend CSV in GUI.
- **CSV unexpected**: verify panel-specific ignore values before export.

---

## 🗺️ File Map

```text
translog_log_parser/
├─ app.py
├─ external_activity_recorder.py
├─ external_activity_parser.py
├─ external_activity_extension/
├─ README.md
├─ TECHNICAL_SPECIFICATION.md
├─ XML_PARSING_GUIDE.md
├─ EXTERNAL_ACTIVITY_README.md
├─ EXTERNAL_ACTIVITY_RECORDER_GUIDE.md
└─ requirements.txt
```

---

## 👤 Author

- Jiajun Wu

## 📄 License
GPLv3