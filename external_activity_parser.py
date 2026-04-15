from __future__ import annotations

"""External Activity Parser and Dashboard Generator.

This module parses XML logs produced by ``external_activity_recorder.py`` and
generates a standalone HTML analytics dashboard for external behavior analysis.

Core capabilities:
- Parse recorder XML into typed in-memory buckets (system/browser/input/dwell).
- Derive external metrics (window switches, typing counts, top domains/URLs,
  process dwell, and search/navigation context).
- Build interactive browser-side analytics:
  - main-window presence timeline,
  - research-time sessions with drill-down details,
  - reading-speed/edit-intensity overlays with return markers,
  - sync-aware export and pivot workflows.
- Export CSV datasets in UTF-8-SIG for spreadsheet compatibility.

Input/Output:
- Input: ``ExternalActivityLog`` XML + optional sync start time + optional
  reading/edit trend CSV.
- Output: self-contained HTML report embedding JSON payload and frontend logic.

Execution modes:
- GUI mode (default): Gradio file upload and report generation.
- CLI mode: headless generation with explicit flags.
"""

import argparse
import csv
import json
import math
import os
import shutil
import sqlite3
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

def parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def parse_int(v: str | None, default: int = 0) -> int:
    try:
        return int(v or default)
    except ValueError:
        return default


def domain_of(url: str) -> str:
    if not url:
        return ""
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _normalize_browser_title(title: str) -> str:
    t = str(title or "").strip()
    suffixes = [
        " - Google Chrome",
        " - Microsoft Edge",
        " - Mozilla Firefox",
    ]
    for s in suffixes:
        if t.endswith(s):
            t = t[: -len(s)].strip()
    return t


def recover_urls_from_history(titles: list[str]) -> list[dict[str, Any]]:
    wanted = [_normalize_browser_title(t) for t in titles if t]
    wanted = [w for w in wanted if w]
    if not wanted:
        return []
    candidates = [
        Path(os.getenv("LOCALAPPDATA", "")) / "Google/Chrome/User Data/Default/History",
        Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft/Edge/User Data/Default/History",
    ]
    rows: list[dict[str, Any]] = []
    for db in candidates:
        try:
            if not db.exists():
                continue
            tmp_dir = Path(tempfile.mkdtemp(prefix="ext_hist_"))
            tmp_db = tmp_dir / "History"
            shutil.copy2(db, tmp_db)
            con = sqlite3.connect(str(tmp_db))
            cur = con.cursor()
            cur.execute("SELECT url, title FROM urls ORDER BY last_visit_time DESC LIMIT 15000")
            got = cur.fetchall()
            con.close()
            for url, title in got:
                u = str(url or "").strip()
                t = _normalize_browser_title(str(title or "").strip())
                if not u or not t:
                    continue
                for w in wanted:
                    if w == t or w in t or t in w:
                        rows.append({"title": t, "url": u})
                        break
        except Exception:
            continue
    agg: Counter[str] = Counter([r["url"] for r in rows if r.get("url")])
    return [{"url": u, "count": c} for u, c in agg.most_common(100)]


def parse_reading_edit_trend_csv(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = [h for h in (reader.fieldnames or []) if h]
        if not headers:
            return []

        def find_col(candidates: list[str]) -> str | None:
            for c in candidates:
                for h in headers:
                    if c in h.lower():
                        return h
            return None

        time_col = find_col(["time_min", "time (min)", "minute", "time_sec", "time (s)", "time_s", "time", "second"])
        edit_col = find_col(["edit_intensity", "edit intensity", "intensity"])
        read_col = find_col(["reading_speed", "reading speed", "read_speed", "reading"])
        time_col_l = str(time_col or "").lower()
        time_unit = "min" if ("min" in time_col_l or "minute" in time_col_l) else "sec"
        for i, r in enumerate(reader, start=1):
            def to_num(v: Any) -> float | None:
                try:
                    return float(str(v).strip())
                except Exception:
                    return None

            t = to_num(r.get(time_col, "")) if time_col else None
            e = to_num(r.get(edit_col, "")) if edit_col else None
            rd = to_num(r.get(read_col, "")) if read_col else None
            if t is None:
                nums = [to_num(r.get(h, "")) for h in headers]
                nums = [n for n in nums if n is not None]
                if nums:
                    t = nums[0]
                    if e is None and len(nums) > 1:
                        e = nums[1]
                    if rd is None and len(nums) > 2:
                        rd = nums[2]
            if t is None:
                continue
            time_min = float(t) if time_unit == "min" else float(t) / 60.0
            rows.append(
                {
                    "row_id": i,
                    "time_min": round(time_min, 6),
                    "time_sec": round(time_min * 60.0, 6),
                    "edit_intensity": round(float(e), 6) if e is not None else 0.0,
                    "reading_speed": round(float(rd), 6) if rd is not None else 0.0,
                }
            )
    rows.sort(key=lambda x: float(x.get("time_sec", 0.0)))
    return rows


@dataclass
class ExternalLog:
    """Normalized external activity log structure parsed from XML."""

    start: datetime | None
    end: datetime | None
    system_events: list[dict[str, Any]]
    browser_events: list[dict[str, Any]]
    input_events: list[dict[str, Any]]
    dwell: list[dict[str, Any]]


def parse_external_log(path: str | Path) -> ExternalLog:
    root = ET.parse(path).getroot()
    start = parse_iso(root.findtext("startTime", default=""))
    end = parse_iso(root.findtext("endTime", default=""))

    def parse_rows(parent_tag: str, row_tag: str) -> list[dict[str, Any]]:
        parent = root.find(parent_tag)
        if parent is None:
            return []
        rows: list[dict[str, Any]] = []
        for node in parent.findall(row_tag):
            row: dict[str, Any] = {}
            for k, v in node.attrib.items():
                if k in {"tsMs", "startMs", "endMs", "durationMs", "hwnd", "port", "tabId", "windowId", "browserTimestampMs", "inputLength"}:
                    row[k] = parse_int(v, 0)
                else:
                    row[k] = v
            rows.append(row)
        rows.sort(key=lambda r: int(r.get("tsMs", r.get("startMs", 0))))
        return rows

    return ExternalLog(
        start=start,
        end=end,
        system_events=parse_rows("SystemEvents", "Event"),
        browser_events=parse_rows("BrowserEvents", "Event"),
        input_events=parse_rows("InputEvents", "Event"),
        dwell=parse_rows("WindowDwell", "Span"),
    )


def parse_translog_context(path: str | Path, pause_threshold_ms: int = 2500) -> dict[str, Any]:
    root = ET.parse(path).getroot()
    project = root.find(".//Project")
    start_text = ""
    end_text = ""
    if project is not None:
        start_text = project.attrib.get("startTime", "") or start_text
        end_text = project.attrib.get("endTime", "") or end_text
    start_text = root.findtext("startTime", default=start_text)
    end_text = root.findtext("endTime", default=end_text)
    start_dt = parse_iso(start_text)
    end_dt = parse_iso(end_text)
    events_root = root.find("Events")
    event_times: list[int] = []
    if events_root is not None:
        for node in events_root:
            event_times.append(parse_int(node.attrib.get("Time"), 0))
    event_times = sorted(t for t in event_times if t >= 0)
    pauses: list[dict[str, Any]] = []
    for i in range(1, len(event_times)):
        prev_t = event_times[i - 1]
        curr_t = event_times[i]
        gap = curr_t - prev_t
        if gap >= pause_threshold_ms:
            row = {"startMsFromSession": prev_t, "endMsFromSession": curr_t, "gapMs": gap}
            if start_dt is not None:
                row["absStartMs"] = to_ms(start_dt + timedelta(milliseconds=prev_t))
                row["absEndMs"] = to_ms(start_dt + timedelta(milliseconds=curr_t))
            pauses.append(row)
    duration_ms = 0
    if start_dt and end_dt:
        duration_ms = max(0, int((end_dt - start_dt).total_seconds() * 1000))
    elif event_times:
        duration_ms = max(0, event_times[-1] - event_times[0])
    return {
        "start": start_dt,
        "end": end_dt,
        "pauses": pauses,
        "duration_ms": duration_ms,
        "event_count": len(event_times),
    }


def summarize_external(ext: ExternalLog) -> dict[str, Any]:
    """Build summary metrics and visualization-ready datasets.

    Returns a JSON-serializable dictionary consumed by the dashboard frontend.
    Includes aggregate counters, top sites/URLs, per-window switch rows, and
    reconstructed typing samples.
    """

    all_events = ext.system_events + ext.browser_events + ext.input_events
    max_ms = max([int(r.get("tsMs", 0)) for r in all_events], default=0)
    duration_ms = 0
    if ext.start and ext.end:
        duration_ms = max(0, int((ext.end - ext.start).total_seconds() * 1000))
    if duration_ms <= 0:
        duration_ms = max_ms

    switch_count = sum(1 for r in ext.system_events if r.get("type") == "window_switch")
    browser_event_count_raw = len(ext.browser_events)
    typed_count = sum(1 for r in ext.browser_events if "input" in str(r.get("type", ""))) + len(ext.input_events)
    tab_switch_count = sum(1 for r in ext.browser_events if "activated" in str(r.get("type", "")))
    tab_close_count = sum(1 for r in ext.browser_events if "removed" in str(r.get("type", "")))
    browser_window_switches = sum(
        1
        for r in ext.system_events
        if r.get("type") == "window_switch"
        and (
            "chrome" in str(r.get("process", "")).lower()
            or "edge" in str(r.get("process", "")).lower()
            or "firefox" in str(r.get("process", "")).lower()
        )
    )
    browser_event_count = browser_event_count_raw if browser_event_count_raw > 0 else browser_window_switches
    tab_switch_count = tab_switch_count if tab_switch_count > 0 else browser_window_switches

    sites = Counter()
    full_urls = Counter()
    for r in ext.browser_events:
        u = str(r.get("url", r.get("pageUrl", ""))).strip()
        if u:
            full_urls[u] += 1
        d = domain_of(u)
        if d:
            sites[d] += 1
    if not full_urls:
        title_pool = [str(r.get("title", "")) for r in ext.system_events] + [str(r.get("title", "")) for r in ext.input_events]
        recovered = recover_urls_from_history(title_pool)
        for row in recovered:
            u = str(row.get("url", "")).strip()
            c = int(row.get("count", 0))
            if u and c > 0:
                full_urls[u] += c
                d = domain_of(u)
                if d:
                    sites[d] += c
    if not sites and full_urls:
        for u, c in full_urls.items():
            d = domain_of(u)
            if d:
                sites[d] += int(c)
    unique_sites = len(sites)

    dwell_by_process: Counter[str] = Counter()
    for d in ext.dwell:
        dwell_by_process[str(d.get("process", "(unknown)"))] += int(d.get("durationMs", 0))

    timeline_bins = max(1, int(math.ceil(duration_ms / 60000)))
    event_count_by_minute = [0] * timeline_bins
    browser_count_by_minute = [0] * timeline_bins
    typing_count_by_minute = [0] * timeline_bins
    for r in ext.system_events:
        idx = min(timeline_bins - 1, max(0, int(int(r.get("tsMs", 0)) / 60000)))
        event_count_by_minute[idx] += 1
    for r in ext.browser_events:
        idx = min(timeline_bins - 1, max(0, int(int(r.get("tsMs", 0)) / 60000)))
        browser_count_by_minute[idx] += 1
        if "input" in str(r.get("type", "")):
            typing_count_by_minute[idx] += 1

    minute_axis = [round(i, 3) for i in range(timeline_bins)]
    top_sites = [{"site": k, "count": v} for k, v in sites.most_common(20)]
    top_urls = [{"url": k, "count": v} for k, v in full_urls.most_common(30)]
    process_dwell_rows = [
        {"process": p, "duration_sec": round(ms / 1000, 3)} for p, ms in dwell_by_process.most_common(20)
    ]
    typing_rows: list[dict[str, Any]] = []
    for r in ext.browser_events:
        if "input" not in str(r.get("type", "")):
            continue
        typing_rows.append(
            {
                "ts_ms": int(r.get("tsMs", 0)),
                "source": "browser",
                "tab_id": int(r.get("tabId", 0)),
                "field": str(r.get("fieldName", "")),
                "field_key": str(r.get("fieldKey", "")),
                "value_sample": str(r.get("valueSample", "")),
                "url": str(r.get("url", r.get("pageUrl", ""))),
                "title": str(r.get("title", "")),
            }
        )
    for r in ext.input_events:
        pass
    key_stream_by_ctx: dict[str, str] = {}

    def apply_key_token(current: str, token: str) -> str:
        t = str(token or "")
        low = t.lower()
        if not t:
            return current
        if "backspace" in low:
            return current[:-1] if current else current
        if "delete" in low:
            return current[:-1] if current else current
        if "space" in low:
            return current + " "
        if "enter" in low:
            return current + "\n"
        if low.startswith("button."):
            return current
        if len(t) == 1:
            return current + t
        if low.startswith("key."):
            return current
        return current + t

    for r in sorted(ext.input_events, key=lambda x: int(x.get("tsMs", 0))):
        title = str(r.get("title", ""))
        process = str(r.get("process", ""))
        ctx = f"{title}|{process}"
        prev_text = key_stream_by_ctx.get(ctx, "")
        token = str(r.get("key", r.get("button", "")))
        new_text = apply_key_token(prev_text, token)
        key_stream_by_ctx[ctx] = new_text
        typing_rows.append(
            {
                "ts_ms": int(r.get("tsMs", 0)),
                "source": "system_keylogger",
                "tab_id": 0,
                "field": token,
                "field_key": f"keylogger::{ctx}",
                "value_sample": new_text,
                "url": "",
                "title": title,
            }
        )
    typing_rows.sort(key=lambda r: int(r.get("ts_ms", 0)))
    typing_rows = typing_rows[:1000]

    browser_rows_sorted = sorted(ext.browser_events, key=lambda r: int(r.get("tsMs", 0)))
    dwell_sorted = sorted(ext.dwell, key=lambda r: int(r.get("startMs", 0)))
    window_switch_rows: list[dict[str, Any]] = []
    switch_idx = 0
    for r in ext.system_events:
        if r.get("type") != "window_switch":
            continue
        ts = int(r.get("tsMs", 0))
        title = str(r.get("title", ""))
        nearest: dict[str, Any] | None = None
        nearest_diff = 10**12
        for b in browser_rows_sorted:
            bts = int(b.get("tsMs", 0))
            diff = abs(bts - ts)
            if diff > 300000:
                continue
            btitle = str(b.get("title", ""))
            if title and btitle and not (title in btitle or btitle in title):
                continue
            if diff < nearest_diff:
                nearest = b
                nearest_diff = diff
        tab_id = int((nearest or {}).get("tabId", 0))
        related_typing = [t for t in typing_rows if (tab_id and int(t.get("tab_id", 0)) == tab_id) or (not tab_id and title and title in str(t.get("title", "")))]
        dwell_ms = 0
        for d in dwell_sorted:
            if abs(int(d.get("startMs", 0)) - ts) > 300000:
                continue
            if title and str(d.get("title", "")) and not (title in str(d.get("title", "")) or str(d.get("title", "")) in title):
                continue
            dwell_ms = int(d.get("durationMs", 0))
            break
        switch_idx += 1
        window_switch_rows.append(
            {
                "switch_id": switch_idx,
                "ts_ms": ts,
                "process": str(r.get("process", "")),
                "title": title,
                "from_window": str(r.get("fromWindow", "")),
                "to_window": str(r.get("toWindow", "")),
                "related_tab_id": tab_id,
                "related_url": str((nearest or {}).get("url", (nearest or {}).get("pageUrl", ""))),
                "duration_ms": dwell_ms,
                "duration_sec": round(dwell_ms / 1000, 3),
                "typing_count": len(related_typing),
                "typing_samples": related_typing[:300],
            }
        )
    window_switch_rows = window_switch_rows[:500]
    return {
        "duration_ms": duration_ms,
        "window_switches": switch_count,
        "browser_events": browser_event_count,
        "browser_events_raw": browser_event_count_raw,
        "typed_events": typed_count,
        "tab_switches": tab_switch_count,
        "tab_closes": tab_close_count,
        "unique_sites": unique_sites,
        "timeline": {
            "minutes": minute_axis,
            "system_events": event_count_by_minute,
            "browser_events": browser_count_by_minute,
            "typing_events": typing_count_by_minute,
        },
        "top_sites": top_sites,
        "top_urls": top_urls,
        "process_dwell": process_dwell_rows,
        "main_process_default": process_dwell_rows[0]["process"] if process_dwell_rows else "",
        "window_switch_rows": window_switch_rows,
        "typing_rows": typing_rows,
    }


def correlate_with_translog(ext: ExternalLog, trans: dict[str, Any]) -> dict[str, Any]:
    if not trans.get("pauses"):
        return {"rows": [], "site_counts": []}
    ext_start = ext.start
    if ext_start is None:
        return {"rows": [], "site_counts": []}

    pause_rows: list[dict[str, Any]] = []
    site_counter: Counter[str] = Counter()
    browser = sorted(ext.browser_events, key=lambda r: int(r.get("tsMs", 0)))
    for p in trans["pauses"]:
        abs_start = int(p.get("absStartMs", 0))
        abs_end = int(p.get("absEndMs", 0))
        if abs_start <= 0 or abs_end <= 0:
            continue
        in_pause = []
        for r in browser:
            abs_ts = to_ms(ext_start) + int(r.get("tsMs", 0))
            if abs_start <= abs_ts <= abs_end:
                in_pause.append(r)
                d = domain_of(str(r.get("url", "")))
                if d:
                    site_counter[d] += 1
        pause_rows.append(
            {
                "pause_start_ms": p["startMsFromSession"],
                "pause_end_ms": p["endMsFromSession"],
                "pause_gap_ms": p["gapMs"],
                "external_events_during_pause": len(in_pause),
                "browser_sites_during_pause": len({domain_of(str(r.get("url", ""))) for r in in_pause if r.get("url")}),
            }
        )
    return {
        "rows": pause_rows[:500],
        "site_counts": [{"site": s, "count": c} for s, c in site_counter.most_common(20)],
    }


def render_external_panel(payload: dict[str, Any], title: str) -> str:
    """Render the dashboard body panel as HTML/CSS/JS string."""
    payload_json = (
        json.dumps(payload, ensure_ascii=False)
        .replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )

    return f"""
<style>
  .ext-btn {{
    display:inline-block;
    padding:6px 12px;
    border-radius:10px;
    border:1px solid #8ba9a0;
    background:linear-gradient(180deg,#f5fbf8,#e8f4ef);
    color:#35504b;
    font-weight:600;
    text-decoration:none;
    cursor:pointer;
    margin-right:6px;
  }}
  .ext-btn:hover {{
    background:linear-gradient(180deg,#ecf8f3,#dbefe7);
  }}
  .ext-btn-primary {{
    border-color:#688a80;
    background:linear-gradient(180deg,#7ea79a,#6a9386);
    color:#fff;
  }}
  .ext-switch-row {{
    cursor:pointer;
  }}
  .ext-switch-row:hover td {{
    background:#eef7f2;
  }}
  .ext-switch-row td:first-child {{
    border-left:4px solid #76a292;
  }}
  .ext-pill {{
    display:inline-block;
    padding:2px 8px;
    border-radius:999px;
    background:#eaf5f0;
    color:#3f655b;
    font-size:11px;
    margin-right:6px;
    margin-bottom:4px;
  }}
  .ext-url-cell {{
    max-width: 520px;
    white-space: normal;
    word-break: break-all;
    color:#2c5a4d;
  }}
  .ext-row-wrap {{
    display:flex;
    flex-wrap:wrap;
    gap:10px 12px;
    align-items:flex-end;
  }}
  .ext-chip-row {{
    border:1px solid rgba(150,170,160,0.35);
    background:#f7fbf9;
    border-radius:10px;
    padding:10px;
  }}
  .ext-scroll-table {{
    overflow-x:auto;
    max-width:100%;
  }}
  .ext-scroll-table table {{
    min-width:980px;
  }}
  .ext-wrap-cell {{
    white-space:normal;
    word-break:break-word;
    max-width:360px;
  }}
  .ext-right-group {{
    margin-left:auto;
    display:flex;
    flex-wrap:wrap;
    gap:8px;
    align-items:flex-end;
  }}
</style>
<div class="panel">
  <h2>{title}</h2>
  <div class="cards">
    <div class="card"><h4>Duration</h4><div class="value"><span id="extDuration"></span></div></div>
    <div class="card"><h4>Window Switches</h4><div class="value"><span id="extSwitches"></span></div></div>
    <div class="card"><h4>Browser Events</h4><div class="value"><span id="extBrowser"></span></div></div>
    <div class="card"><h4>Typing Events</h4><div class="value"><span id="extTyping"></span></div></div>
    <div class="card"><h4>Tab Switches</h4><div class="value"><span id="extTabSwitch"></span></div></div>
    <div class="card"><h4>Unique Sites</h4><div class="value"><span id="extSites"></span></div></div>
    <div class="card"><h4>Sync Offset (s)</h4><div class="value"><span id="extSyncOffset"></span></div></div>
  </div>
  <div class="replay-row ext-row-wrap ext-chip-row" style="margin-top:10px;">
    <label>Ignore first (s) <input id="dashIgnoreStartSec" type="number" min="0" step="1" value="0" style="width:80px;" /></label>
    <label>Ignore last (s) <input id="dashIgnoreEndSec" type="number" min="0" step="1" value="0" style="width:80px;" /></label>
    <button class="ext-btn" onclick="renderExternalOverview()">Apply Dashboard Ignore</button>
  </div>
  <div class="replay-view">
    <h3>Main Window Presence Timeline</h3>
    <div class="replay-row">
      <label>Main process name <input id="mainProcessInput" type="text" style="min-width:280px;" /></label>
      <label>Ignore first (s) <input id="mainIgnoreStartSec" type="number" min="0" step="1" value="0" style="width:80px;" /></label>
      <label>Ignore last (s) <input id="mainIgnoreEndSec" type="number" min="0" step="1" value="0" style="width:80px;" /></label>
      <button class="ext-btn ext-btn-primary" onclick="renderMainProcessTimeline()">Draw Timeline</button>
      <button class="ext-btn" onclick="exportMainWindowTimelineCSV()">Export Timeline CSV</button>
    </div>
    <div id="mainWindowStats" style="font-size:12px;color:#5d746f;margin-top:8px;"></div>
    <div class="trend-plot-wrap"><div id="mainWindowTimelinePlot" class="trend-plot"></div></div>
  </div>
  <div class="replay-view">
    <h3>Research Time Analytics</h3>
    <div class="replay-row">
      <label>Ignore first (s) <input id="researchIgnoreStartSec" type="number" min="0" step="1" value="0" style="width:80px;" /></label>
      <label>Ignore last (s) <input id="researchIgnoreEndSec" type="number" min="0" step="1" value="0" style="width:80px;" /></label>
      <button class="ext-btn ext-btn-primary" onclick="renderResearchAnalytics()">Calculate Research Time</button>
      <button class="ext-btn" onclick="showResearchStat('max')">Max</button>
      <button class="ext-btn" onclick="showResearchStat('min')">Min</button>
      <button class="ext-btn" onclick="showResearchStat('avg')">Average</button>
      <button class="ext-btn" onclick="showResearchStat('median')">Median</button>
      <button class="ext-btn" onclick="exportResearchAnalyticsCSV()">Export Research CSV</button>
    </div>
    <div id="researchStats" style="font-size:12px;color:#5d746f;margin-top:8px;"></div>
    <div class="trend-plot-wrap"><div id="researchTimelinePlot" class="trend-plot"></div></div>
    <div id="researchDetailTable" class="raw-xml"></div>
  </div>
  <div class="replay-view">
    <h3>Left-from-Main Target Count</h3>
    <div class="replay-row ext-row-wrap ext-chip-row">
      <label>Ignore first (s) <input id="leaveIgnoreStartSec" type="number" min="0" step="1" value="0" style="width:80px;" /></label>
      <label>Ignore last (s) <input id="leaveIgnoreEndSec" type="number" min="0" step="1" value="0" style="width:80px;" /></label>
      <label><input id="leaveMergeToggle" type="checkbox" /> Merge adjacent returns</label>
      <label>Merge gap (s) <input id="leaveMergeGapSec" type="number" min="0" step="0.1" value="5" style="width:80px;" /></label>
      <button class="ext-btn ext-btn-primary" onclick="renderLeaveTurnAnalytics()">Calculate Leave Target Count</button>
      <button class="ext-btn" onclick="exportLeaveTurnCSV()">Export Leave Target CSV</button>
    </div>
    <div id="leaveTurnStats" style="font-size:12px;color:#5d746f;margin-top:8px;"></div>
    <div class="trend-plot-wrap"><div id="leaveTurnPlot" class="trend-plot"></div></div>
    <details class="replay-view" style="margin-top:8px;">
      <summary>Detailed Table</summary>
      <div id="leaveTurnTable" class="raw-xml"></div>
    </details>
  </div>
  <div class="replay-view">
    <h3>Reading Speed & Edit Intensity with Return Marks</h3>
    <div class="replay-row ext-row-wrap ext-chip-row">
      <label>Time threshold (s) <input id="returnThresholdSec" type="number" min="0" step="1" value="30" style="width:90px;" /></label>
      <label>Ignore first (s) <input id="ignoreStartSec" type="number" min="0" step="1" value="0" style="width:80px;" /></label>
      <label>Ignore last (s) <input id="ignoreEndSec" type="number" min="0" step="1" value="0" style="width:80px;" /></label>
      <label><input id="mergeReturnsToggle" type="checkbox" /> Merge adjacent returns</label>
      <label>Merge gap (s) <input id="mergeReturnsGapSec" type="number" min="0" step="0.1" value="5" style="width:80px;" /></label>
      <button class="ext-btn ext-btn-primary" onclick="renderReadingEditTrend()">Calculate Raise Percentage</button>
      <button class="ext-btn" onclick="renderReadingEditTrendNonZero()">Calculate Non-Zero Percentage</button>
      <div class="ext-right-group"><button class="ext-btn" onclick="exportTrendCombinedCSV()">Export Combined CSV</button></div>
    </div>
    <div class="replay-row ext-row-wrap ext-chip-row" style="margin-top:12px;">
      <label>PNG files <input id="trendPngParts" type="number" min="1" step="1" value="3" style="width:70px;" /></label>
      <label>Split % (optional) <input id="trendPngSplits" type="text" placeholder="e.g. 30,30,40" style="min-width:170px;" /></label>
      <button class="ext-btn" onclick="exportReadingEditTrendPNGs()">Export Trend PNGs</button>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px;">
      <div id="trendThresholdHint" style="font-size:12px;color:#36564c;border:1px solid #cae0d7;background:#f3fbf7;border-radius:10px;padding:8px;"></div>
      <div id="trendRaiseStats" style="font-size:12px;color:#36564c;border:1px solid #cae0d7;background:#f3fbf7;border-radius:10px;padding:8px;"></div>
    </div>
    <div class="trend-plot-wrap"><div id="readingEditTrendPlot" class="trend-plot"></div></div>
    <div id="trendReturnDetail" class="raw-xml"></div>
  </div>
  <div class="trend-plot-wrap"><div id="externalSitePlot" class="trend-plot"></div></div>
  <details class="replay-view">
    <summary>Top Exact URLs</summary>
    <div id="externalUrlTable" class="raw-xml"></div>
  </details>
  <details class="replay-view">
    <summary>Window Switch Details</summary>
    <div id="externalWindowSwitchTable" class="raw-xml"></div>
  </details>
  <details class="replay-view">
    <summary>Keylogger Data</summary>
    <div style="margin-bottom:8px;color:#5d746f;font-size:12px;">
      Typing/Input Samples are captured browser field inputs and system input events. Click a row in Window Switch Details to filter inputs for that tab/window.
    </div>
    <div id="externalTypingVisual" style="margin-bottom:8px;"></div>
    <div id="externalTypingTable" class="raw-xml"></div>
  </details>
  <div class="panel" style="margin-top:12px;">
    <h3>Data Export & Pivot (External)</h3>
    <div class="replay-row ext-row-wrap ext-chip-row" id="extDatasetChecks"></div>
    <div class="replay-row ext-row-wrap ext-chip-row" style="margin-top:10px;">
      <label>Ignore first (s) <input id="pivotIgnoreStartSec" type="number" min="0" step="1" value="0" style="width:80px;" /></label>
      <label>Ignore last (s) <input id="pivotIgnoreEndSec" type="number" min="0" step="1" value="0" style="width:80px;" /></label>
    </div>
    <div class="replay-row ext-row-wrap ext-chip-row" style="margin-top:10px;">
      <div class="ext-right-group">
        <button class="ext-btn" onclick="toggleExternalDatasets(true)">Select All</button>
        <button class="ext-btn" onclick="toggleExternalDatasets(false)">Clear All</button>
        <button class="ext-btn ext-btn-primary" onclick="exportExternalSelectedCSV()">Export Selected CSV</button>
        <button class="ext-btn ext-btn-primary" onclick="exportExternalAllCSV()">Export ALL-in-One CSV</button>
      </div>
    </div>
    <div class="replay-row ext-row-wrap ext-chip-row" style="margin-top:10px;">
      <label>Row <select id="extPivotRow"></select></label>
      <label>Column <select id="extPivotCol"></select></label>
      <label>Value <select id="extPivotVal"></select></label>
      <label>Agg <select id="extPivotAgg">
        <option value="count">count</option>
        <option value="sum">sum</option>
        <option value="avg">avg</option>
        <option value="min">min</option>
        <option value="max">max</option>
      </select></label>
      <label>Chart <select id="extPivotChart">
        <option value="bar">bar</option>
        <option value="line">line</option>
        <option value="scatter">scatter</option>
      </select></label>
    </div>
    <div class="replay-row ext-row-wrap ext-chip-row" style="margin-top:10px;">
      <div class="ext-right-group">
        <button class="ext-btn" onclick="buildExternalPivot()">Build Pivot</button>
        <button class="ext-btn" onclick="renderExternalPivotChart()">Render Pivot Chart</button>
        <button class="ext-btn ext-btn-primary" onclick="exportExternalPivotCSV()">Export Pivot CSV</button>
      </div>
    </div>
    <div id="extPivotTable" class="replay-view"></div>
    <div id="extPivotChartWrap" class="trend-plot-wrap"><div id="extPivotChartPlot" class="trend-plot"></div></div>
  </div>
</div>
<script>
  const externalPayload = {payload_json};
  let extPivotExport = null;
  let mainWindowTimelineExport = [];
  let researchSessionExport = [];
  let leaveTurnExport = [];
  let trendRaiseEvalCurrent = [];
  let trendEvalModeCurrent = "raise";
  let externalOverviewWsRows = [];
  let externalOverviewTypingRows = [];
  function withSyncRows(rows) {{
    const offset = Number((externalPayload.summary || {{}}).sync_offset_ms || 0);
    return (rows || []).map((r) => {{
      const ts = Number(r.ts_ms ?? r.tsMs ?? r.startMs ?? 0);
      const endTs = Number(r.end_ms ?? r.endMs ?? ts);
      const syncTs = ts + offset;
      const syncEndTs = endTs + offset;
      return {{
        ...r,
        sync_offset_ms: offset,
        sync_ts_ms: syncTs,
        sync_ts_sec: Number((syncTs / 1000).toFixed(3)),
        sync_ts_min: Number((syncTs / 60000).toFixed(3)),
        sync_end_ms: syncEndTs,
        sync_end_sec: Number((syncEndTs / 1000).toFixed(3)),
      }};
    }});
  }}
  function extDownloadCSV(name, headers, rows) {{
    const lines = [];
    lines.push(headers.map(h => `"${{String(h).replace(/"/g,'""')}}"`).join(","));
    rows.forEach(r => lines.push(r.map(v => `"${{String(v ?? "").replace(/"/g,'""')}}"`).join(",")));
    const blob = new Blob(["\\uFEFF" + lines.join("\\n")], {{ type: "text/csv;charset=utf-8-sig;" }});
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {{ URL.revokeObjectURL(a.href); a.remove(); }}, 0);
  }}
  function buildExternalDatasets() {{
    const ds = externalPayload.datasets || {{}};
    return {{
      system_events: withSyncRows(ds.system_events || []),
      browser_events: withSyncRows(ds.browser_events || []),
      input_events: withSyncRows(ds.input_events || []),
      window_dwell: withSyncRows(ds.window_dwell || []),
      window_switch_rows: withSyncRows(ds.window_switch_rows || []),
      typing_rows: withSyncRows(ds.typing_rows || []),
      reading_edit_trends: withSyncRows(ds.reading_edit_trends || []),
      top_sites: withSyncRows(ds.top_sites || []),
      top_urls: withSyncRows(ds.top_urls || [])
    }};
  }}
  function getPivotIgnoreWindow() {{
    const ignoreStartSec = Math.max(0, Number(document.getElementById("pivotIgnoreStartSec")?.value || 0));
    const ignoreEndSec = Math.max(0, Number(document.getElementById("pivotIgnoreEndSec")?.value || 0));
    const sessionEndSec = Math.max(0, Number((externalPayload.summary || {{}}).duration_ms || 0) / 1000);
    const endBound = Math.max(ignoreStartSec, sessionEndSec - ignoreEndSec);
    return {{ ignoreStartSec, ignoreEndSec, sessionEndSec, endBound }};
  }}
  function urlHost(v) {{
    try {{
      return new URL(String(v || "")).hostname.toLowerCase();
    }} catch (_e) {{
      return "";
    }}
  }}
  function buildExternalDatasetsForPivot() {{
    const ds = buildExternalDatasets();
    const w = getPivotIgnoreWindow();
    const ignoreStartSec = w.ignoreStartSec;
    const endBound = w.endBound;
    const out = {{}};
    Object.keys(ds).forEach((k) => {{
      out[k] = (ds[k] || []).filter((r) => {{
        const st = Number(r.sync_ts_sec ?? Number.NaN);
        const ed = Number(r.sync_end_sec ?? st);
        if (!Number.isFinite(st)) return true;
        return st < endBound && ed > ignoreStartSec;
      }});
    }});
    const siteCounter = new Map();
    const urlCounter = new Map();
    (out.browser_events || []).forEach((r) => {{
      const u = String(r.url || r.pageUrl || r.related_url || "").trim();
      if (!u) return;
      const h = urlHost(u);
      if (h) siteCounter.set(h, Number(siteCounter.get(h) || 0) + 1);
      urlCounter.set(u, Number(urlCounter.get(u) || 0) + 1);
    }});
    out.top_sites = Array.from(siteCounter.entries()).map(([site, count]) => ({{ site, count }})).sort((a,b)=>Number(b.count)-Number(a.count)).slice(0, 20);
    out.top_urls = Array.from(urlCounter.entries()).map(([url, count]) => ({{ url, count }})).sort((a,b)=>Number(b.count)-Number(a.count)).slice(0, 100);
    return out;
  }}
  function toggleExternalDatasets(on) {{
    document.querySelectorAll(".ext-ds").forEach(n => n.checked = !!on);
    refreshExternalPivotFields();
  }}
  function refreshExternalPivotFields() {{
    const selected = Array.from(document.querySelectorAll(".ext-ds:checked")).map(n => n.value);
    const ds = buildExternalDatasetsForPivot();
    const fields = new Set(["dataset"]);
    selected.forEach(k => (ds[k] || []).forEach(r => Object.keys(r || {{}}).forEach(f => fields.add(f))));
    const arr = Array.from(fields).sort((a,b)=>String(a).localeCompare(String(b)));
    const row = document.getElementById("extPivotRow");
    const col = document.getElementById("extPivotCol");
    const val = document.getElementById("extPivotVal");
    [row,col,val].forEach(sel => {{
      const prev = sel.value;
      sel.innerHTML = arr.map(f => `<option value="${{f}}">${{f}}</option>`).join("");
      if (arr.includes(prev)) sel.value = prev;
    }});
    if (arr.includes("sync_ts_min") && !row.value) row.value = "sync_ts_min";
    if (arr.includes("dataset") && !col.value) col.value = "";
    if (arr.includes("count") && !val.value) val.value = "count";
    if (!col.querySelector('option[value=""]')) {{
      const o = document.createElement("option");
      o.value = "";
      o.textContent = "(none)";
      col.prepend(o);
    }}
  }}
  function extAgg(values, mode) {{
    if (mode === "count") return values.length;
    const nums = values.map(v => Number(v)).filter(n => Number.isFinite(n));
    if (!nums.length) return 0;
    if (mode === "sum") return nums.reduce((a,b)=>a+b,0);
    if (mode === "avg") return nums.reduce((a,b)=>a+b,0)/nums.length;
    if (mode === "min") return Math.min(...nums);
    if (mode === "max") return Math.max(...nums);
    return 0;
  }}
  function buildExternalPivot() {{
    const selected = Array.from(document.querySelectorAll(".ext-ds:checked")).map(n => n.value);
    const ds = buildExternalDatasetsForPivot();
    const rows = [];
    selected.forEach(k => (ds[k] || []).forEach(r => rows.push({{dataset:k, ...r}})));
    const rowField = document.getElementById("extPivotRow").value || "dataset";
    const colField = document.getElementById("extPivotCol").value || "";
    const valField = document.getElementById("extPivotVal").value || "ts_ms";
    const agg = document.getElementById("extPivotAgg").value || "count";
    const map = new Map();
    rows.forEach(r => {{
      const rk = String(r[rowField] ?? "(null)");
      const ck = colField ? String(r[colField] ?? "(null)") : "";
      const key = `${{rk}}|||${{ck}}`;
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(r[valField]);
    }});
    const rowKeys = Array.from(new Set(Array.from(map.keys()).map(k => k.split("|||")[0]))).sort((a,b)=>String(a).localeCompare(String(b),undefined,{{numeric:true}}));
    const colKeys = Array.from(new Set(Array.from(map.keys()).map(k => k.split("|||")[1]))).sort((a,b)=>String(a).localeCompare(String(b),undefined,{{numeric:true}}));
    const headers = [rowField, ...colKeys];
    const body = rowKeys.map(rk => {{
      const row = [rk];
      colKeys.forEach(ck => row.push(Number(extAgg(map.get(`${{rk}}|||${{ck}}`) || [], agg).toFixed(6))));
      return row;
    }});
    extPivotExport = {{headers, body}};
    const html = `<table class="summary-table"><thead><tr>${{headers.map(h=>`<th>${{h}}</th>`).join("")}}</tr></thead><tbody>${{body.map(r=>`<tr>${{r.map(c=>`<td>${{c}}</td>`).join("")}}</tr>`).join("")}}</tbody></table>`;
    document.getElementById("extPivotTable").innerHTML = html;
    return extPivotExport;
  }}
  function renderExternalPivotChart() {{
    const pv = extPivotExport || buildExternalPivot();
    if (!pv || pv.headers.length < 2) return;
    const chart = document.getElementById("extPivotChart").value || "bar";
    const x = pv.body.map(r => r[0]);
    const traces = pv.headers.slice(1).map((h, i) => {{
      const y = pv.body.map(r => Number(r[i+1] || 0));
      return chart === "scatter"
        ? {{x, y, mode:"markers", type:"scatter", name:h}}
        : {{x, y, mode:"lines+markers", type: chart === "line" ? "scatter" : "bar", name:h}};
    }});
    Plotly.react("extPivotChartPlot", traces, {{
      title: "External Pivot Chart",
      paper_bgcolor: "rgba(255,255,255,0)",
      plot_bgcolor: "rgba(255,255,255,0)"
    }}, {{ responsive: true, displaylogo: false }});
  }}
  function exportExternalPivotCSV() {{
    const pv = extPivotExport || buildExternalPivot();
    if (!pv) return;
    extDownloadCSV("external_pivot.csv", pv.headers, pv.body);
  }}
  function exportExternalSelectedCSV() {{
    const selected = Array.from(document.querySelectorAll(".ext-ds:checked")).map(n => n.value);
    const ds = buildExternalDatasetsForPivot();
    selected.forEach(name => {{
      const rows = ds[name] || [];
      const headers = Array.from(new Set(rows.flatMap(r => Object.keys(r || {{}}))));
      const body = rows.map(r => headers.map(h => r[h] ?? ""));
      extDownloadCSV(`external_${{name}}.csv`, headers, body);
    }});
  }}
  function exportExternalAllCSV() {{
    const ds = buildExternalDatasetsForPivot();
    const w = getPivotIgnoreWindow();
    const rows = [];
    const browserRows = ds.browser_events || [];
    const typingRows = ds.typing_rows || [];
    const wsRows = ds.window_switch_rows || [];
    const siteSet = new Set();
    let tabSwitches = 0;
    browserRows.forEach((r) => {{
      const t = String(r.type || "").toLowerCase();
      if (t.includes("tab") && (t.includes("switch") || t.includes("activated"))) tabSwitches += 1;
      const u = String(r.url || r.pageUrl || r.related_url || "").trim();
      const h = urlHost(u);
      if (h) siteSet.add(h);
    }});
    const summaryRows = [
      ["Ignore First (s)", Number(w.ignoreStartSec.toFixed(3))],
      ["Ignore Last (s)", Number(w.ignoreEndSec.toFixed(3))],
      ["Duration (s)", Number(Math.max(0, w.endBound - w.ignoreStartSec).toFixed(3))],
      ["Window Switches", Number(wsRows.length || 0)],
      ["Browser Events", Number(browserRows.length || 0)],
      ["Unique Sites", Number(siteSet.size || 0)],
      ["Tab Switches", Number(tabSwitches || 0)],
      ["Typing Events", Number(typingRows.length || 0)],
      ["Sync Offset (s)", Number((Number((externalPayload.summary || {{}}).sync_offset_ms || 0) / 1000).toFixed(3))]
    ];
    summaryRows.forEach(([name, value]) => {{
      rows.push({{
        record_type: "summary_metric",
        dataset: "summary_metrics",
        metric_name: String(name),
        metric_value: String(value),
        event_type: "",
        process: "",
        title: "",
        url: "",
        tab_id: "",
        window_id: "",
        field: "",
        typed_text: "",
        duration_ms: "",
        duration_sec: "",
        count: "",
        original_ts_ms: "",
        sync_ts_ms: "",
        sync_ts_sec: "",
        sync_offset_ms: String((externalPayload.summary || {{}}).sync_offset_ms ?? 0),
        main_process_query: "",
        presence_state: "",
        leave_count: "",
        return_count: ""
      }});
    }});
    Object.keys(ds).forEach((name) => {{
      (ds[name] || []).forEach((r) => {{
        rows.push({{
          record_type: "dataset_row",
          dataset: name,
          metric_name: "",
          metric_value: "",
          event_type: String(r.type ?? ""),
          process: String(r.process ?? ""),
          title: String(r.title ?? ""),
          url: String(r.url ?? r.related_url ?? ""),
          tab_id: String(r.tabId ?? r.tab_id ?? r.related_tab_id ?? ""),
          window_id: String(r.windowId ?? ""),
          field: String(r.field ?? r.fieldName ?? ""),
          typed_text: String(r.value_sample ?? r.valueSample ?? ""),
          duration_ms: String(r.duration_ms ?? r.durationMs ?? ""),
          duration_sec: String(r.duration_sec ?? ""),
          count: String(r.count ?? ""),
          original_ts_ms: String(r.ts_ms ?? r.tsMs ?? r.startMs ?? ""),
          sync_ts_ms: String(r.sync_ts_ms ?? ""),
          sync_ts_sec: String(r.sync_ts_sec ?? ""),
          sync_offset_ms: String(r.sync_offset_ms ?? ""),
          main_process_query: "",
          presence_state: "",
          leave_count: "",
          return_count: ""
        }});
      }});
    }});
    const headers = ["record_type","dataset","metric_name","metric_value","event_type","process","title","url","tab_id","window_id","field","typed_text","duration_ms","duration_sec","count","original_ts_ms","sync_ts_ms","sync_ts_sec","sync_offset_ms","main_process_query","presence_state","leave_count","return_count"];
    const body = rows.map(r => headers.map(h => r[h] ?? ""));
    extDownloadCSV("external_all_data.csv", headers, body);
  }}
  function buildMainWindowTimelineRows(ignoreStartSec, ignoreEndSec) {{
    const ds = buildExternalDatasets();
    const dwell = [...(ds.window_dwell || [])].sort((a,b) => Number(a.sync_ts_ms||0) - Number(b.sync_ts_ms||0));
    const input = document.getElementById("mainProcessInput");
    const query = String(input?.value || "").trim().toLowerCase();
    const igStartRaw = ignoreStartSec ?? document.getElementById("mainIgnoreStartSec")?.value ?? 0;
    const igEndRaw = ignoreEndSec ?? document.getElementById("mainIgnoreEndSec")?.value ?? 0;
    const igStart = Math.max(0, Number(igStartRaw));
    const igEnd = Math.max(0, Number(igEndRaw));
    const sessionEndSec = Math.max(0, Number((externalPayload.summary || {{}}).duration_ms || 0) / 1000);
    const endBound = Math.max(igStart, sessionEndSec - igEnd);
    const rows = [];
    let leaveCount = 0;
    let backCount = 0;
    let prevIn = null;
    dwell.forEach((r) => {{
      const proc = String(r.process || "").toLowerCase();
      const rawStart = Number(r.sync_ts_sec || 0);
      const rawEnd = Number(r.sync_end_sec || rawStart);
      if (!(rawStart < endBound && rawEnd > igStart)) return;
      const start = Math.max(rawStart, igStart);
      const end = Math.min(rawEnd, endBound);
      const dur = Math.max(0, end - start);
      if (dur <= 0) return;
      const inside = !!query && proc.includes(query);
      if (prevIn === true && inside === false) leaveCount += 1;
      if (prevIn === false && inside === true) backCount += 1;
      prevIn = inside;
      rows.push({{
        state: inside ? "in_main_window" : "outside_main_window",
        process: String(r.process || ""),
        title: String(r.title || ""),
        sync_start_sec: Number(start.toFixed(3)),
        sync_end_sec: Number(end.toFixed(3)),
        duration_sec: Number(dur.toFixed(3)),
        sync_offset_sec: Number((Number(r.sync_offset_ms || 0) / 1000).toFixed(3)),
      }});
    }});
    return {{ query, rows, leaveCount, backCount }};
  }}
  function exportMainWindowTimelineCSV() {{
    const info = buildMainWindowTimelineRows();
    const headers = ["main_process_query","state","process","title","sync_start_sec","sync_end_sec","duration_sec","leave_count","return_count","sync_offset_sec"];
    const body = info.rows.map(r => [info.query, r.state, r.process, r.title, r.sync_start_sec, r.sync_end_sec, r.duration_sec, info.leaveCount, info.backCount, r.sync_offset_sec]);
    extDownloadCSV("main_window_timeline.csv", headers, body);
  }}
  function exportResearchAnalyticsCSV() {{
    const researchIgStart = Math.max(0, Number(document.getElementById("researchIgnoreStartSec")?.value || 0));
    const researchIgEnd = Math.max(0, Number(document.getElementById("researchIgnoreEndSec")?.value || 0));
    const sessions = (buildResearchSessions(researchIgStart, researchIgEnd).sessions || []).map((s, i) => ({{...s, id: i + 1}}));
    if (!sessions.length) return;
    const vals = sessions.map(s => Number(s.duration_sec || 0)).sort((a,b)=>a-b);
    const avg = vals.reduce((a,b)=>a+b,0)/vals.length;
    const median = vals.length % 2 ? vals[(vals.length-1)/2] : (vals[vals.length/2-1] + vals[vals.length/2]) / 2;
    const summaryText = `Sessions=${{sessions.length}} | min=${{vals[0].toFixed(3)}}s | max=${{vals[vals.length-1].toFixed(3)}}s | avg=${{avg.toFixed(3)}}s | median=${{median.toFixed(3)}}s`;
    const headers = ["record_type","metric_name","metric_value","session_id","start_sec","end_sec","duration_sec","url","outside_processes","outside_titles"];
    const body = [];
    body.push(["summary_metric","sessions",String(sessions.length),"","","","","","",""]);
    body.push(["summary_metric","min_sec",vals[0].toFixed(3),"","","","","","",""]);
    body.push(["summary_metric","max_sec",vals[vals.length-1].toFixed(3),"","","","","","",""]);
    body.push(["summary_metric","avg_sec",avg.toFixed(3),"","","","","","",""]);
    body.push(["summary_metric","median_sec",median.toFixed(3),"","","","","","",""]);
    body.push(["summary_metric","summary_line",summaryText,"","","","","","",""]);
    sessions.flatMap((s) => sessionToRows(s)).forEach((r) => {{
      body.push(["session_row","","",r[0],r[1],r[2],r[3],r[4],r[5],r[6]]);
    }});
    extDownloadCSV("research_time_analytics.csv", headers, body);
  }}
  function getReadingTrendRowsWithIgnore() {{
    const rows = buildExternalDatasets().reading_edit_trends || [];
    const ignoreStartSec = Math.max(0, Number(document.getElementById("ignoreStartSec")?.value || 0));
    const ignoreEndSec = Math.max(0, Number(document.getElementById("ignoreEndSec")?.value || 0));
    const sessionEndSec = Math.max(0, Number((externalPayload.summary || {{}}).duration_ms || 0) / 1000);
    const endBound = Math.max(ignoreStartSec, sessionEndSec - ignoreEndSec);
    return rows.filter((r) => {{
      const t = Number(r.time_sec || 0);
      return t >= ignoreStartSec && t <= endBound;
    }});
  }}
  function getReturnEvents() {{
    const ignoreStartSec = Math.max(0, Number(document.getElementById("ignoreStartSec")?.value || 0));
    const ignoreEndSec = Math.max(0, Number(document.getElementById("ignoreEndSec")?.value || 0));
    const sessionEndSec = Math.max(0, Number((externalPayload.summary || {{}}).duration_ms || 0) / 1000);
    const sessions = (buildResearchSessions(0, 0).sessions || []).map((s, i) => ({{
      session_id: i + 1,
      return_sec: Number(s.end_sec || 0)
    }})).filter(s => Number.isFinite(s.return_sec) && s.return_sec >= 0).filter((s) => {{
      if (s.return_sec < ignoreStartSec) return false;
      if (sessionEndSec > 0 && s.return_sec > Math.max(0, sessionEndSec - ignoreEndSec)) return false;
      return true;
    }}).sort((a,b)=>a.return_sec-b.return_sec);
    const mergeOn = !!document.getElementById("mergeReturnsToggle")?.checked;
    const gapSec = Math.max(0, Number(document.getElementById("mergeReturnsGapSec")?.value || 0));
    if (!mergeOn) {{
      return sessions.map((s, i) => ({{
        return_id: i + 1,
        return_sec: Number(s.return_sec.toFixed(3)),
        session_ids: [s.session_id]
      }}));
    }}
    const grouped = [];
    let buf = [];
    sessions.forEach((s) => {{
      if (!buf.length) {{
        buf = [s];
        return;
      }}
      const prev = buf[buf.length - 1];
      if (s.return_sec - prev.return_sec <= gapSec) {{
        buf.push(s);
      }} else {{
        grouped.push(buf);
        buf = [s];
      }}
    }});
    if (buf.length) grouped.push(buf);
    return grouped.map((g, i) => {{
      const last = g[g.length - 1];
      return {{
        return_id: i + 1,
        return_sec: Number(last.return_sec.toFixed(3)),
        session_ids: g.map(x => x.session_id)
      }};
    }});
  }}
  function evaluateReturnRaise(trendRows, thresholdSec, mode) {{
    const ret = getReturnEvents();
    const sorted = [...(trendRows || [])].sort((a,b)=>Number(a.time_sec||0)-Number(b.time_sec||0));
    const evalRows = [];
    ret.forEach((ev) => {{
      const rt = Number(ev.return_sec || 0);
      const before = [...sorted].filter(r => Number(r.time_sec||0) <= rt).slice(-1)[0];
      const baseline = Number(before?.edit_intensity ?? 0);
      const look = sorted.filter(r => Number(r.time_sec||0) > rt && Number(r.time_sec||0) <= rt + thresholdSec);
      const peak = look.length ? Math.max(...look.map(r => Number(r.edit_intensity || 0))) : baseline;
      const raised = mode === "non_zero" ? (look.some(r => Number(r.edit_intensity || 0) !== 0)) : (peak > baseline);
      const firstRaise = look.find(r => Number(r.edit_intensity || 0) > baseline);
      const firstNonZero = look.find(r => Number(r.edit_intensity || 0) !== 0);
      evalRows.push({{
        return_id: Number(ev.return_id || 0),
        return_sec: Number(rt.toFixed(3)),
        return_min: Number((rt / 60).toFixed(3)),
        session_ids_csv: (ev.session_ids || []).join("|"),
        return_group_size: Number((ev.session_ids || []).length || 1),
        baseline_edit_intensity: Number(baseline.toFixed(6)),
        peak_within_threshold: Number(peak.toFixed(6)),
        raised: raised ? 1 : 0,
        first_raise_time_sec: firstRaise ? Number(Number(firstRaise.time_sec).toFixed(3)) : "",
        first_raise_time_min: firstRaise ? Number((Number(firstRaise.time_sec) / 60).toFixed(3)) : "",
        first_non_zero_time_sec: firstNonZero ? Number(Number(firstNonZero.time_sec).toFixed(3)) : "",
        first_non_zero_time_min: firstNonZero ? Number((Number(firstNonZero.time_sec) / 60).toFixed(3)) : "",
        baseline_time_sec: before ? Number(Number(before.time_sec || 0).toFixed(3)) : "",
        baseline_time_min: before ? Number((Number(before.time_sec || 0) / 60).toFixed(3)) : "",
      }});
    }});
    return evalRows;
  }}
  function renderTrendReturnDetailRow(row) {{
    const headers = ["return_id","session_ids_csv","return_group_size","return_sec","return_min","baseline_time_sec","baseline_time_min","baseline_edit_intensity","peak_within_threshold","raised","first_raise_time_sec","first_raise_time_min","first_non_zero_time_sec","first_non_zero_time_min"];
    const html = `<div class="ext-scroll-table"><table class="summary-table"><thead><tr>${{headers.map(h=>`<th>${{h}}</th>`).join("")}}</tr></thead>` +
      `<tbody><tr>${{headers.map(h=>`<td class="ext-wrap-cell">${{String(row?.[h] ?? "")}}</td>`).join("")}}</tr></tbody></table></div>`;
    const d = document.getElementById("trendReturnDetail");
    if (d) d.innerHTML = html;
  }}
  function renderResearchDetailBySessionIds(ids, label) {{
    const idSet = new Set((ids || []).map(v => Number(v)));
    const picks = (researchSessionExport || []).filter(s => idSet.has(Number(s.id || 0)));
    if (!picks.length) return;
    const headers = ["session_id","start_sec","end_sec","duration_sec","url","outside_processes","outside_titles"];
    const rows = picks.flatMap((s) => sessionToRows(s));
    const table = `<div style="margin-top:8px;font-size:12px;color:#5d746f;">Selected: ${{label}}</div>` +
      `<div class="ext-scroll-table"><table class="summary-table"><thead><tr>${{headers.map(h=>`<th>${{h}}</th>`).join("")}}</tr></thead><tbody>${{rows.map(r=>`<tr>${{r.map(c=>`<td class="${{String(c).includes('http')?'ext-url-cell':'ext-wrap-cell'}}">${{String(c ?? "")}}</td>`).join("")}}</tr>`).join("")}}</tbody></table></div>`;
    const wrap = document.getElementById("researchDetailTable");
    if (wrap) wrap.innerHTML = table;
  }}
  function renderReadingEditTrend(mode) {{
    const evalMode = mode || "raise";
    trendEvalModeCurrent = evalMode;
    const trendWindow = Number((externalPayload.summary || {{}}).trend_window_sec || 0);
    const threshold = Number(document.getElementById("returnThresholdSec")?.value || 0);
    const ignoreStartSec = Math.max(0, Number(document.getElementById("ignoreStartSec")?.value || 0));
    const ignoreEndSec = Math.max(0, Number(document.getElementById("ignoreEndSec")?.value || 0));
    const hint = document.getElementById("trendThresholdHint");
    if (hint) hint.innerHTML = `<b>Threshold Rule</b><br/>Set threshold ≥ trend window: <b>${{trendWindow}} s</b><br/><b>Return Ignore Window</b><br/>Ignore first <b>${{ignoreStartSec}} s</b> and last <b>${{ignoreEndSec}} s</b> of session.`;
    if (threshold < trendWindow) {{
      const stats = document.getElementById("trendRaiseStats");
      if (stats) stats.innerHTML = `<b>Status:</b> Invalid threshold <b>${{threshold}} s</b>. Required: ≥ <b>${{trendWindow}} s</b>.`;
      return;
    }}
    const rows = getReadingTrendRowsWithIgnore();
    if (!rows.length) {{
      const stats = document.getElementById("trendRaiseStats");
      if (stats) stats.innerHTML = "<b>Status:</b> No reading/edit trend data found. Upload CSV in Gradio UI first.";
      return;
    }}
    const retEvents = getReturnEvents();
    const evalRows = evaluateReturnRaise(rows, threshold, evalMode);
    trendRaiseEvalCurrent = evalRows;
    const raisedCount = evalRows.filter(r => Number(r.raised) === 1).length;
    const pct = evalRows.length ? (raisedCount / evalRows.length) * 100 : 0;
    const stats = document.getElementById("trendRaiseStats");
    if (stats) {{
      const label = evalMode === "non_zero" ? "Non-zero-after-return ratio" : "Raised-after-return ratio";
      stats.innerHTML = `<b>${{label}}</b><br/><span style="font-size:15px;color:#2f5f52;">${{raisedCount}} / ${{evalRows.length}} = <b>${{pct.toFixed(2)}}%</b></span>`;
    }}
    const retMins = retEvents.map(v => Number((Number(v.return_sec || 0) / 60).toFixed(6)));
    const shapes = retMins.map((x) => ({{
      type: "line", x0: x, x1: x, y0: 0, y1: 1, yref: "paper",
      line: {{ color: "#b85b61", width: 2, dash: "dot" }}
    }}));
    const anns = retMins.map((x, i) => ({{
      x, y: 1, yref: "paper", text: `R${{i+1}}`, showarrow: true, arrowhead: 2, ax: 0, ay: -18, font: {{size:10,color:"#b85b61"}}
    }}));
    Plotly.react("readingEditTrendPlot", [
      {{
        x: rows.map(r => Number(r.time_min || (Number(r.time_sec || 0) / 60))),
        y: rows.map(r => Number(r.reading_speed || 0)),
        type: "scatter",
        mode: "lines",
        name: "Reading Speed",
        yaxis: "y1",
        line: {{ color: "#4f7f72" }}
      }},
      {{
        x: rows.map(r => Number(r.time_min || (Number(r.time_sec || 0) / 60))),
        y: rows.map(r => Number(r.edit_intensity || 0)),
        type: "scatter",
        mode: "lines+markers",
        name: "Edit Intensity",
        yaxis: "y2",
        line: {{ color: "#7f5ea6" }},
        marker: {{ size: 6 }}
      }},
      {{
        x: retMins,
        y: retMins.map(() => 0),
        mode: "markers",
        type: "scatter",
        name: "Returns",
        marker: {{ color: "#b85b61", symbol: "diamond", size: 10 }},
        customdata: evalRows.map(r => r.return_id),
        text: evalRows.map(r => `R${{r.return_id}}`),
        textposition: "top center"
      }}
    ], {{
      title: "Reading Speed / Edit Intensity with Return Marks",
      paper_bgcolor: "rgba(255,255,255,0)",
      plot_bgcolor: "rgba(255,255,255,0)",
      xaxis: {{ title: "Time (min)" }},
      yaxis: {{ title: "Reading Speed", side: "left" }},
      yaxis2: {{ title: "Edit Intensity", overlaying: "y", side: "right" }},
      shapes: shapes,
      annotations: anns,
      margin: {{ l: 55, r: 55, t: 55, b: 50 }}
    }}, {{ responsive: true, displaylogo: false }});
    const chart = document.getElementById("readingEditTrendPlot");
    if (chart) {{
      chart.on("plotly_click", (ev) => {{
        const rid = Number(ev?.points?.[0]?.customdata ?? 0);
        if (Number.isFinite(rid) && rid > 0) {{
          const rr = evalRows.find(r => Number(r.return_id) === rid);
          if (rr) {{
            renderTrendReturnDetailRow(rr);
            const ids = String(rr.session_ids_csv || "").split("|").map(x => Number(x)).filter(n => Number.isFinite(n));
            renderResearchDetailBySessionIds(ids, `return=${{rr.return_id}}`);
            return;
          }}
        }}
        const x = Number(ev?.points?.[0]?.x ?? Number.NaN);
        if (!Number.isFinite(x)) return;
        if (!evalRows.length) return;
        const nearest = evalRows.reduce((best, r) => Math.abs(Number(r.return_min)-x) < Math.abs(Number(best.return_min)-x) ? r : best, evalRows[0]);
        renderTrendReturnDetailRow(nearest);
        const ids = String(nearest.session_ids_csv || "").split("|").map(x => Number(x)).filter(n => Number.isFinite(n));
        renderResearchDetailBySessionIds(ids, `return=${{nearest.return_id}}`);
      }});
    }}
    if (evalRows.length) {{
      renderTrendReturnDetailRow(evalRows[0]);
      const ids = String(evalRows[0].session_ids_csv || "").split("|").map(x => Number(x)).filter(n => Number.isFinite(n));
      renderResearchDetailBySessionIds(ids, `return=${{evalRows[0].return_id}}`);
    }}
  }}
  function renderReadingEditTrendNonZero() {{
    renderReadingEditTrend("non_zero");
  }}
  async function exportReadingEditTrendPNGs() {{
    const chartId = "readingEditTrendPlot";
    const chart = document.getElementById(chartId);
    if (!chart) return;
    const rows = buildExternalDatasets().reading_edit_trends || [];
    if (!rows.length) return;
    const xVals = rows.map(r => Number(r.time_min || (Number(r.time_sec || 0) / 60))).filter(Number.isFinite);
    if (!xVals.length) return;
    const minX = Math.min(...xVals);
    const maxX = Math.max(...xVals);
    const total = Math.max(1e-9, maxX - minX);
    const count = Math.max(1, Number(document.getElementById("trendPngParts")?.value || 1));
    const splitRaw = String(document.getElementById("trendPngSplits")?.value || "").trim();
    let percentages = [];
    if (splitRaw) {{
      percentages = splitRaw.split(",").map(v => Number(String(v).trim())).filter(v => Number.isFinite(v) && v > 0);
    }}
    if (!percentages.length) {{
      percentages = Array.from({{length: count}}, () => 100 / count);
    }}
    const sumP = percentages.reduce((a,b)=>a+b,0) || 100;
    const norm = percentages.map(p => p / sumP);
    const originalRange = chart.layout?.xaxis?.range ? [...chart.layout.xaxis.range] : null;
    let cursor = minX;
    for (let i = 0; i < norm.length; i++) {{
      const span = total * norm[i];
      const from = cursor;
      const to = i === norm.length - 1 ? maxX : (cursor + span);
      cursor = to;
      await Plotly.relayout(chartId, {{ "xaxis.range": [from, to] }});
      const dataUrl = await Plotly.toImage(chartId, {{ format: "png", width: 1600, height: 520, scale: 1 }});
      const a = document.createElement("a");
      a.href = dataUrl;
      a.download = `reading_edit_returns_part_${{i+1}}.png`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    }}
    if (originalRange) {{
      await Plotly.relayout(chartId, {{ "xaxis.range": originalRange }});
    }} else {{
      await Plotly.relayout(chartId, {{ "xaxis.autorange": true }});
    }}
  }}
  function exportTrendCombinedCSV() {{
    const trend = getReadingTrendRowsWithIgnore();
    const threshold = Number(document.getElementById("returnThresholdSec")?.value || 0);
    const evalRows = evaluateReturnRaise(trend, threshold, trendEvalModeCurrent || "raise");
    const headers = ["record_type","time_min","time_sec","reading_speed","edit_intensity","return_id","session_ids_csv","return_group_size","return_min","return_sec","baseline_time_min","baseline_time_sec","baseline_edit_intensity","peak_within_threshold","raised","first_raise_time_min","first_raise_time_sec","first_non_zero_time_min","first_non_zero_time_sec"];
    const body = [];
    trend.forEach((r) => body.push(["trend_row", r.time_min, r.time_sec, r.reading_speed, r.edit_intensity, "", "", "", "", "", "", "", "", "", "", "", "", "", ""]));
    evalRows.forEach((r) => body.push(["return_eval", "", "", "", "", r.return_id, r.session_ids_csv, r.return_group_size, r.return_min, r.return_sec, r.baseline_time_min, r.baseline_time_sec, r.baseline_edit_intensity, r.peak_within_threshold, r.raised, r.first_raise_time_min, r.first_raise_time_sec, r.first_non_zero_time_min, r.first_non_zero_time_sec]));
    extDownloadCSV("reading_edit_returns_combined.csv", headers, body);
  }}
  function renderMainProcessTimeline() {{
    const info = buildMainWindowTimelineRows();
    const query = info.query;
    const rows = info.rows;
    if (!rows.length || !query) {{
      Plotly.react("mainWindowTimelinePlot", [], {{
        title: "Main Window Presence Timeline",
        paper_bgcolor: "rgba(255,255,255,0)",
        plot_bgcolor: "rgba(255,255,255,0)"
      }}, {{ responsive: true, displaylogo: false }});
      document.getElementById("mainWindowStats").textContent = "Enter a main process name to draw timeline.";
      return;
    }}
    const inX = [], inBase = [], inY = [];
    const outX = [], outBase = [], outY = [];
    rows.forEach((r) => {{
      const start = Number(r.sync_start_sec || 0);
      const dur = Number(r.duration_sec || 0);
      const inside = String(r.state || "") === "in_main_window";
      if (inside) {{
        inX.push(dur); inBase.push(start); inY.push(1);
      }} else {{
        outX.push(dur); outBase.push(start); outY.push(0);
      }}
    }});
    mainWindowTimelineExport = rows;
    Plotly.react("mainWindowTimelinePlot", [
      {{
        type: "bar", orientation: "h", x: inX, base: inBase, y: inY,
        marker: {{ color: "#5ea68d" }}, name: "In Main Window"
      }},
      {{
        type: "bar", orientation: "h", x: outX, base: outBase, y: outY,
        marker: {{ color: "#d49295" }}, name: "Outside Main Window"
      }}
    ], {{
      title: "Main Window Presence Timeline",
      barmode: "overlay",
      paper_bgcolor: "rgba(255,255,255,0)",
      plot_bgcolor: "rgba(255,255,255,0)",
      xaxis: {{ title: "Sync Time (s)" }},
      yaxis: {{ title: "", showticklabels: false, ticks: "" }},
      margin: {{ l: 70, r: 22, t: 50, b: 55 }}
    }}, {{ responsive: true, displaylogo: false }});
    document.getElementById("mainWindowStats").textContent = `Main process="${{query}}" | Leaves=${{info.leaveCount}} | Returns=${{info.backCount}}`;
  }}
  function buildResearchSessions(ignoreStartSec, ignoreEndSec) {{
    const info = buildMainWindowTimelineRows(ignoreStartSec, ignoreEndSec);
    const ds = buildExternalDatasets();
    const rows = info.rows || [];
    const q = String(info.query || "").trim().toLowerCase();
    if (!q) return {{query:q, sessions:[]}};
    const sessions = [];
    let current = null;
    rows.forEach((r) => {{
      const inside = String(r.state || "") === "in_main_window";
      const startSec = Number(r.sync_start_sec || 0);
      const endSec = Number(r.sync_end_sec || startSec);
      if (!inside && current == null) {{
        current = {{
          start_sec: startSec,
          outside_titles: new Set([String(r.title || "")]),
          outside_processes: new Set([String(r.process || "")]),
        }};
      }} else if (!inside && current != null) {{
        current.outside_titles.add(String(r.title || ""));
        current.outside_processes.add(String(r.process || ""));
      }} else if (inside && current != null) {{
        current.end_sec = startSec;
        current.duration_sec = Number(Math.max(0, current.end_sec - current.start_sec).toFixed(3));
        const urlSet = new Set();
        (ds.browser_events || []).forEach((b) => {{
          const t = Number(b.sync_ts_sec || 0);
          if (t >= current.start_sec && t <= current.end_sec) {{
            const u = String(b.url || b.pageUrl || "").trim();
            if (u) urlSet.add(u);
          }}
        }});
        (ds.window_switch_rows || []).forEach((w) => {{
          const t = Number(w.sync_ts_sec || 0);
          if (t >= current.start_sec && t <= current.end_sec) {{
            const u = String(w.related_url || "").trim();
            if (u) urlSet.add(u);
          }}
        }});
        current.urls = Array.from(urlSet);
        current.outside_titles = Array.from(current.outside_titles).filter(Boolean);
        current.outside_processes = Array.from(current.outside_processes).filter(Boolean);
        sessions.push(current);
        current = null;
      }}
      if (inside && current == null) {{
      }}
      if (!inside && current != null) {{
        current.end_sec = endSec;
      }}
    }});
    return {{query:q, sessions}};
  }}
  function sessionToRows(s) {{
    const urls = (s.urls || []).length ? s.urls : [""];
    return urls.map((u) => [s.id, s.start_sec, s.end_sec, s.duration_sec, u, (s.outside_processes || []).join(" | "), (s.outside_titles || []).join(" | ")]);
  }}
  function renderResearchDetail(s, label) {{
    if (!s) return;
    const headers = ["session_id","start_sec","end_sec","duration_sec","url","outside_processes","outside_titles"];
    const rows = sessionToRows(s);
    const table = `<div style="margin-top:8px;font-size:12px;color:#5d746f;">Selected: ${{label}}</div>` +
      `<div class="ext-scroll-table"><table class="summary-table"><thead><tr>${{headers.map(h=>`<th>${{h}}</th>`).join("")}}</tr></thead><tbody>${{rows.map(r=>`<tr>${{r.map(c=>`<td class="${{String(c).includes('http')?'ext-url-cell':'ext-wrap-cell'}}">${{String(c ?? "")}}</td>`).join("")}}</tr>`).join("")}}</tbody></table></div>`;
    const wrap = document.getElementById("researchDetailTable");
    if (wrap) wrap.innerHTML = table;
  }}
  function showResearchStat(kind) {{
    if (!researchSessionExport.length) return;
    const arr = [...researchSessionExport].sort((a,b)=>Number(a.duration_sec||0)-Number(b.duration_sec||0));
    const vals = arr.map(s => Number(s.duration_sec || 0));
    const avg = vals.reduce((a,b)=>a+b,0)/vals.length;
    const median = vals.length % 2 ? vals[(vals.length-1)/2] : (vals[vals.length/2-1] + vals[vals.length/2]) / 2;
    let target = arr[0];
    let label = kind;
    if (kind === "max") target = arr[arr.length-1];
    if (kind === "min") target = arr[0];
    if (kind === "avg") {{
      target = arr.reduce((best, s) => Math.abs(Number(s.duration_sec)-avg) < Math.abs(Number(best.duration_sec)-avg) ? s : best, arr[0]);
      label = `average≈${{avg.toFixed(3)}}s`;
    }}
    if (kind === "median") {{
      target = arr.reduce((best, s) => Math.abs(Number(s.duration_sec)-median) < Math.abs(Number(best.duration_sec)-median) ? s : best, arr[0]);
      label = `median=${{median.toFixed(3)}}s`;
    }}
    renderResearchDetail(target, label);
  }}
  function renderResearchAnalytics() {{
    const researchIgStart = Math.max(0, Number(document.getElementById("researchIgnoreStartSec")?.value || 0));
    const researchIgEnd = Math.max(0, Number(document.getElementById("researchIgnoreEndSec")?.value || 0));
    const built = buildResearchSessions(researchIgStart, researchIgEnd);
    const sessions = built.sessions.map((s, i) => ({{...s, id: i + 1}})).filter(s => Number(s.duration_sec || 0) >= 0);
    researchSessionExport = sessions;
    if (!sessions.length) {{
      Plotly.react("researchTimelinePlot", [], {{
        title: "Research Time Sessions",
        paper_bgcolor: "rgba(255,255,255,0)",
        plot_bgcolor: "rgba(255,255,255,0)"
      }}, {{ responsive: true, displaylogo: false }});
      const statsWrap = document.getElementById("researchStats");
      if (statsWrap) statsWrap.textContent = "No complete leave→return research sessions found. Set main process first.";
      const detail = document.getElementById("researchDetailTable");
      if (detail) detail.innerHTML = "";
      return;
    }}
    const durs = sessions.map(s => Number(s.duration_sec || 0)).sort((a,b)=>a-b);
    const sum = durs.reduce((a,b)=>a+b,0);
    const avg = sum / durs.length;
    const median = durs.length % 2 ? durs[(durs.length-1)/2] : (durs[durs.length/2-1] + durs[durs.length/2]) / 2;
    const mn = durs[0];
    const mx = durs[durs.length-1];
    const statsWrap = document.getElementById("researchStats");
    if (statsWrap) statsWrap.textContent = `Sessions=${{sessions.length}} | min=${{mn.toFixed(3)}}s | max=${{mx.toFixed(3)}}s | avg=${{avg.toFixed(3)}}s | median=${{median.toFixed(3)}}s`;
    Plotly.react("researchTimelinePlot", [{{
      x: sessions.map(s => s.id),
      y: sessions.map(s => Number(s.duration_sec || 0)),
      type: "scatter",
      mode: "lines+markers",
      marker: {{ color: "#6a9386", size: 9 }},
      name: "Research Duration (s)",
      customdata: sessions.map(s => s.id)
    }}], {{
      title: "Research Time Sessions",
      paper_bgcolor: "rgba(255,255,255,0)",
      plot_bgcolor: "rgba(255,255,255,0)",
      xaxis: {{ title: "Session ID" }},
      yaxis: {{ title: "Duration (s)" }},
      margin: {{ l: 55, r: 20, t: 50, b: 50 }}
    }}, {{ responsive: true, displaylogo: false }});
    const chart = document.getElementById("researchTimelinePlot");
    if (chart) {{
      chart.on("plotly_click", (ev) => {{
        const sid = Number(ev?.points?.[0]?.customdata || 0);
        const s = sessions.find(x => Number(x.id) === sid);
        if (s) renderResearchDetail(s, `point(session=${{sid}})`);
      }});
    }}
    showResearchStat("avg");
  }}
  function buildLeaveTurnRows() {{
    const leaveIgStart = Math.max(0, Number(document.getElementById("leaveIgnoreStartSec")?.value || 0));
    const leaveIgEnd = Math.max(0, Number(document.getElementById("leaveIgnoreEndSec")?.value || 0));
    const built = buildResearchSessions(leaveIgStart, leaveIgEnd);
    const sessions = built.sessions.map((s, i) => ({{...s, id: i + 1}}));
    const ds = buildExternalDatasets();
    if (!sessions.length) return [];
    const mergeOn = !!document.getElementById("leaveMergeToggle")?.checked;
    const mergeGap = Math.max(0, Number(document.getElementById("leaveMergeGapSec")?.value || 0));
    const sorted = [...sessions].sort((a,b) => Number(a.end_sec||0) - Number(b.end_sec||0));
    const groups = [];
    if (!mergeOn) {{
      sorted.forEach(s => groups.push([s]));
    }} else {{
      let buf = [];
      sorted.forEach((s) => {{
        if (!buf.length) {{
          buf = [s];
          return;
        }}
        const prev = buf[buf.length - 1];
        if (Number(s.end_sec || 0) - Number(prev.end_sec || 0) <= mergeGap) {{
          buf.push(s);
        }} else {{
          groups.push(buf);
          buf = [s];
        }}
      }});
      if (buf.length) groups.push(buf);
    }}
    const rows = groups.map((grp, idx) => {{
      const st = Number(Math.min(...grp.map(g => Number(g.start_sec || 0))).toFixed(3));
      const ed = Number(Math.max(...grp.map(g => Number(g.end_sec || 0))).toFixed(3));
      const dur = Number((ed - st).toFixed(3));
      const winSet = new Set();
      const tabSet = new Set();
      (ds.window_dwell || []).forEach((w) => {{
        const ws = Number(w.sync_ts_sec || 0);
        const we = Number(w.sync_end_sec || ws);
        const overlap = ws < ed && we > st;
        if (!overlap) return;
        const proc = String(w.process || "");
        const ttl = String(w.title || "");
        const key = `${{proc}}|${{ttl}}`;
        if (key.trim()) winSet.add(key);
      }});
      (ds.browser_events || []).forEach((b) => {{
        const t = Number(b.sync_ts_sec || 0);
        if (t < st || t > ed) return;
        const tab = Number(b.tabId ?? b.tab_id ?? 0);
        if (Number.isFinite(tab) && tab > 0) tabSet.add(String(tab));
      }});
      (ds.window_switch_rows || []).forEach((w) => {{
        const t = Number(w.sync_ts_sec || 0);
        if (t < st || t > ed) return;
        const tab = Number(w.related_tab_id ?? 0);
        if (Number.isFinite(tab) && tab > 0) tabSet.add(String(tab));
      }});
      const winList = Array.from(winSet);
      const tabList = Array.from(tabSet);
      return {{
        session_id: idx + 1,
        source_session_ids: grp.map(g => Number(g.id || 0)).join("|"),
        grouped_session_count: grp.length,
        start_sec: st,
        end_sec: ed,
        duration_sec: dur,
        distinct_window_count: winList.length,
        distinct_tab_count: tabList.length,
        total_distinct_targets: winList.length + tabList.length,
        windows_preview: winList.join(" || "),
        tabs_preview: tabList.join(","),
      }};
    }});
    return rows;
  }}
  function exportLeaveTurnCSV() {{
    const rows = buildLeaveTurnRows();
    if (!rows.length) return;
    const counts = rows.map(r => Number(r.total_distinct_targets || 0));
    const mn = Math.min(...counts);
    const mx = Math.max(...counts);
    const avg = counts.reduce((a,b)=>a+b,0)/Math.max(1, counts.length);
    const summary = `Sessions=${{rows.length}} | min=${{mn}} | max=${{mx}} | avg=${{avg.toFixed(3)}}`;
    const headers = ["record_type","metric_name","metric_value","session_id","source_session_ids","grouped_session_count","start_sec","end_sec","duration_sec","distinct_window_count","distinct_tab_count","total_distinct_targets","windows_preview","tabs_preview"];
    const body = [];
    body.push(["summary_metric","sessions",String(rows.length),"","","","","","","","","","",""]);
    body.push(["summary_metric","min",String(mn),"","","","","","","","","","",""]);
    body.push(["summary_metric","max",String(mx),"","","","","","","","","","",""]);
    body.push(["summary_metric","avg",avg.toFixed(3),"","","","","","","","","","",""]);
    body.push(["summary_metric","summary_line",summary,"","","","","","","","","","",""]);
    rows.forEach((r) => body.push(["session_row","", "", r.session_id, r.source_session_ids, r.grouped_session_count, r.start_sec, r.end_sec, r.duration_sec, r.distinct_window_count, r.distinct_tab_count, r.total_distinct_targets, r.windows_preview, r.tabs_preview]));
    extDownloadCSV("left_from_main_target_count.csv", headers, body);
  }}
  function renderLeaveTurnAnalytics() {{
    const rows = buildLeaveTurnRows();
    if (!rows.length) {{
      const stats = document.getElementById("leaveTurnStats");
      if (stats) stats.textContent = "No leave-from-main sessions found. Set main process first.";
      Plotly.react("leaveTurnPlot", [], {{
        title: "Distinct Targets per Leave Session",
        paper_bgcolor: "rgba(255,255,255,0)",
        plot_bgcolor: "rgba(255,255,255,0)"
      }}, {{ responsive: true, displaylogo: false }});
      const wrap = document.getElementById("leaveTurnTable");
      if (wrap) wrap.innerHTML = "";
      leaveTurnExport = [];
      return;
    }}
    leaveTurnExport = rows;
    const counts = rows.map(r => Number(r.total_distinct_targets || 0));
    const mn = Math.min(...counts);
    const mx = Math.max(...counts);
    const avg = counts.reduce((a,b)=>a+b,0)/Math.max(1, counts.length);
    const stats = document.getElementById("leaveTurnStats");
    if (stats) stats.textContent = `Sessions=${{rows.length}} | min=${{mn}} | max=${{mx}} | avg=${{avg.toFixed(3)}} | grouped=${{!!document.getElementById("leaveMergeToggle")?.checked}}`;
    Plotly.react("leaveTurnPlot", [{{
      x: rows.map(r => r.session_id),
      y: rows.map(r => r.total_distinct_targets),
      type: "bar",
      name: "Distinct Tabs/Windows"
    }}], {{
      title: "Distinct Targets per Leave Session",
      paper_bgcolor: "rgba(255,255,255,0)",
      plot_bgcolor: "rgba(255,255,255,0)",
      xaxis: {{ title: "Session ID" }},
      yaxis: {{ title: "Distinct Target Count" }},
      margin: {{ l: 55, r: 20, t: 50, b: 50 }}
    }}, {{ responsive: true, displaylogo: false }});
    const headers = ["session_id","source_session_ids","grouped_session_count","start_sec","end_sec","duration_sec","distinct_window_count","distinct_tab_count","total_distinct_targets","windows_preview","tabs_preview"];
    const body = rows.map(r => `<tr>${{headers.map(h => `<td class="${{(h.includes('preview') ? 'ext-wrap-cell' : '')}}">${{String(r[h] ?? "")}}</td>`).join("")}}</tr>`).join("");
    const table = `<div class="ext-scroll-table"><table class="summary-table"><thead><tr>${{headers.map(h=>`<th>${{h}}</th>`).join("")}}</tr></thead><tbody>${{body}}</tbody></table></div>`;
    const wrap = document.getElementById("leaveTurnTable");
    if (wrap) wrap.innerHTML = table;
  }}
  (function() {{
    const s = externalPayload.summary || {{}};
    const mainInput = document.getElementById("mainProcessInput");
    if (mainInput) mainInput.value = String(s.main_process_default || "");

    function esc(v) {{
      return String(v ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
    }}
    function diffTrack(prevText, nextText) {{
      const a = String(prevText || "");
      const b = String(nextText || "");
      if (!a) return `<span style="color:#2a6f55;">${{esc(b)}}</span>`;
      let s = 0;
      while (s < a.length && s < b.length && a[s] === b[s]) s += 1;
      let ea = a.length - 1;
      let eb = b.length - 1;
      while (ea >= s && eb >= s && a[ea] === b[eb]) {{
        ea -= 1; eb -= 1;
      }}
      const pre = esc(a.slice(0, s));
      const del = esc(a.slice(s, ea + 1));
      const ins = esc(b.slice(s, eb + 1));
      const suf = esc(a.slice(ea + 1));
      const delPart = del ? `<del style="background:#ffe8ea;color:#9f2f3d;text-decoration-thickness:2px;">${{del}}</del>` : "";
      const insPart = ins ? `<ins style="background:#e7f8ee;color:#1c6f4d;text-decoration:none;">${{ins}}</ins>` : "";
      return `${{pre}}${{delPart}}${{insPart}}${{suf}}`;
    }}
    function renderTypingRows(rows) {{
      const tpHeaders = ["ts_ms","sync_ts_sec","source","tab_id","field","title"];
      const tpHead = `<tr>${{tpHeaders.map(h => `<th>${{h}}</th>`).join("")}}</tr>`;
      const tpBody = rows.map(r => `<tr>${{tpHeaders.map(h => `<td>${{String(r[h] ?? "")}}</td>`).join("")}}</tr>`).join("");
      const tpWrap = document.getElementById("externalTypingTable");
      if (tpWrap) tpWrap.innerHTML = `<table class="summary-table"><thead>${{tpHead}}</thead><tbody>${{tpBody}}</tbody></table>`;
      const visual = document.getElementById("externalTypingVisual");
      if (visual) {{
        const sorted = [...rows]
          .filter((r) => String(r.value_sample || "").length > 0)
          .sort((a,b) => Number(a.ts_ms||0) - Number(b.ts_ms||0));
        const byField = new Map();
        sorted.forEach(r => {{
          const k = String(r.field_key || r.field || "field");
          if (!byField.has(k)) byField.set(k, []);
          byField.get(k).push(r);
        }});
        let selectedKey = "";
        let selectedArr = [];
        byField.forEach((arr, key) => {{
          if (arr.length > selectedArr.length) {{
            selectedKey = key;
            selectedArr = arr;
          }}
        }});
        if (!selectedArr.length) {{
          visual.innerHTML = `<div style="padding:8px;border:1px solid #d5e8df;border-radius:10px;background:#f7fcf9;color:#5f7a71;">No typing samples.</div>`;
          return;
        }}
        const latest = selectedArr[selectedArr.length - 1];
        const prev = selectedArr.length >= 2 ? selectedArr[selectedArr.length - 2] : {{ value_sample: "" }};
        const track = diffTrack(String(prev.value_sample || ""), String(latest.value_sample || ""));
        visual.innerHTML =
          `<div style="font-size:12px;color:#5f7a71;margin:4px 0;">Track Changes Preview:</div>` +
          `<div style="padding:10px 12px;border:2px solid #bfd9cd;border-radius:12px;background:#ffffff;overflow-x:auto;white-space:nowrap;font-family:Consolas,monospace;font-size:15px;">${{track}}</div>`;
      }}
    }}
    function renderExternalOverview() {{
      const igStart = Math.max(0, Number(document.getElementById("dashIgnoreStartSec")?.value || 0));
      const igEnd = Math.max(0, Number(document.getElementById("dashIgnoreEndSec")?.value || 0));
      const sessionEndSec = Math.max(0, Number((externalPayload.summary || {{}}).duration_ms || 0) / 1000);
      const endBound = Math.max(igStart, sessionEndSec - igEnd);
      const ds = buildExternalDatasets();
      const overlap = (st, ed) => st < endBound && ed > igStart;
      const browserRows = (ds.browser_events || []).filter(r => {{
        const st = Number(r.sync_ts_sec || 0);
        return st >= igStart && st <= endBound;
      }});
      const typingRows = (ds.typing_rows || []).filter(r => {{
        const st = Number(r.sync_ts_sec || 0);
        return st >= igStart && st <= endBound;
      }});
      const wsRows = (ds.window_switch_rows || []).filter(r => {{
        const st = Number(r.sync_ts_sec || 0);
        const ed = Number(r.sync_end_sec || st);
        return overlap(st, ed);
      }});
      externalOverviewWsRows = wsRows;
      externalOverviewTypingRows = typingRows;
      const windowSec = Math.max(0, endBound - igStart);
      document.getElementById("extDuration").textContent = `${{Number(windowSec.toFixed(2))}}s`;
      document.getElementById("extSwitches").textContent = String(wsRows.length);
      document.getElementById("extBrowser").textContent = String(browserRows.length);
      document.getElementById("extTyping").textContent = String(typingRows.length);
      const tabSwitches = browserRows.filter(r => {{
        const t = String(r.type || "").toLowerCase();
        return t.includes("tab") && (t.includes("switch") || t.includes("activated"));
      }}).length;
      document.getElementById("extTabSwitch").textContent = String(tabSwitches);
      const host = (u) => {{
        try {{
          return new URL(String(u || "")).hostname.toLowerCase();
        }} catch (_e) {{
          return "";
        }}
      }};
      const siteCounter = new Map();
      const urlCounter = new Map();
      browserRows.forEach((r) => {{
        const u = String(r.url || r.pageUrl || r.related_url || "").trim();
        if (!u) return;
        const h = host(u);
        if (h) siteCounter.set(h, Number(siteCounter.get(h) || 0) + 1);
        urlCounter.set(u, Number(urlCounter.get(u) || 0) + 1);
      }});
      document.getElementById("extSites").textContent = String(siteCounter.size);
      document.getElementById("extSyncOffset").textContent = Number((Number(s.sync_offset_ms || 0)/1000).toFixed(3));
      const sites = Array.from(siteCounter.entries()).map(([site, count]) => ({{site, count}})).sort((a,b) => Number(b.count)-Number(a.count)).slice(0,20);
      Plotly.react("externalSitePlot", [{{
        x: sites.map(r => r.site),
        y: sites.map(r => r.count),
        type: "bar",
        name: "Site Visit Events"
      }}], {{
        title: "Most Visited Sites (event count)",
        paper_bgcolor: "rgba(255,255,255,0)",
        plot_bgcolor: "rgba(255,255,255,0)",
        xaxis: {{ title: "Site" }},
        yaxis: {{ title: "Events" }},
        margin: {{ l: 45, r: 22, t: 50, b: 95 }}
      }}, {{ responsive: true, displaylogo: false }});
      const urls = Array.from(urlCounter.entries()).map(([url, count]) => ({{url, count}})).sort((a,b)=>Number(b.count)-Number(a.count)).slice(0,100);
      const uHead = `<tr><th>id</th><th>url</th><th>count</th></tr>`;
      const uBody = urls.map((r, i) => `<tr><td>${{i + 1}}</td><td class="ext-url-cell">${{String(r.url ?? "")}}</td><td>${{String(r.count ?? "")}}</td></tr>`).join("");
      const uWrap = document.getElementById("externalUrlTable");
      if (uWrap) uWrap.innerHTML = `<table class="summary-table"><thead>${{uHead}}</thead><tbody>${{uBody}}</tbody></table>`;
      const wsHeaders = ["ts_ms","sync_ts_sec","duration_sec","process","title","related_url","related_tab_id","typing_count"];
      const wsHead = `<tr>${{wsHeaders.map(h => `<th>${{h}}</th>`).join("")}}</tr>`;
      const wsBody = wsRows.map(r => {{
        return `<tr class="ext-switch-row" data-switch-id="${{String(r.switch_id || "")}}">${{
          wsHeaders.map(h => h === "related_url" ? `<td class="ext-url-cell">${{String(r[h] ?? "")}}</td>` : `<td>${{String(r[h] ?? "")}}</td>`).join("")
        }}</tr>`;
      }}).join("");
      const wsWrap = document.getElementById("externalWindowSwitchTable");
      if (wsWrap) wsWrap.innerHTML = `<table class="summary-table"><thead>${{wsHead}}</thead><tbody>${{wsBody}}</tbody></table>`;
      renderTypingRows(typingRows);
      document.querySelectorAll(".ext-switch-row").forEach(node => {{
        node.addEventListener("click", () => {{
          const sid = Number(node.getAttribute("data-switch-id") || "0");
          const row = externalOverviewWsRows.find(r => Number(r.switch_id || 0) === sid);
          const rows = row ? (row.typing_samples || []) : externalOverviewTypingRows;
          renderTypingRows(rows);
        }});
      }});
    }}
    window.renderExternalOverview = renderExternalOverview;

    const checkWrap = document.getElementById("extDatasetChecks");
    const ds = buildExternalDatasets();
    const names = Object.keys(ds);
    if (checkWrap) {{
      checkWrap.innerHTML = names.map(n => `<label style="margin-right:10px;"><input class="ext-ds" type="checkbox" value="${{n}}" checked/> ${{n}}</label>`).join("");
      checkWrap.querySelectorAll(".ext-ds").forEach(n => n.addEventListener("change", refreshExternalPivotFields));
    }}
    refreshExternalPivotFields();
    buildExternalPivot();
    renderExternalPivotChart();
    renderExternalOverview();
    document.getElementById("dashIgnoreStartSec")?.addEventListener("change", renderExternalOverview);
    document.getElementById("dashIgnoreEndSec")?.addEventListener("change", renderExternalOverview);
    document.getElementById("mainIgnoreStartSec")?.addEventListener("change", renderMainProcessTimeline);
    document.getElementById("mainIgnoreEndSec")?.addEventListener("change", renderMainProcessTimeline);
    document.getElementById("researchIgnoreStartSec")?.addEventListener("change", renderResearchAnalytics);
    document.getElementById("researchIgnoreEndSec")?.addEventListener("change", renderResearchAnalytics);
    renderMainProcessTimeline();
    renderResearchAnalytics();
    document.getElementById("leaveIgnoreStartSec")?.addEventListener("change", renderLeaveTurnAnalytics);
    document.getElementById("leaveIgnoreEndSec")?.addEventListener("change", renderLeaveTurnAnalytics);
    document.getElementById("leaveMergeToggle")?.addEventListener("change", renderLeaveTurnAnalytics);
    document.getElementById("leaveMergeGapSec")?.addEventListener("change", renderLeaveTurnAnalytics);
    document.getElementById("pivotIgnoreStartSec")?.addEventListener("change", () => {{ refreshExternalPivotFields(); buildExternalPivot(); renderExternalPivotChart(); }});
    document.getElementById("pivotIgnoreEndSec")?.addEventListener("change", () => {{ refreshExternalPivotFields(); buildExternalPivot(); renderExternalPivotChart(); }});
    renderLeaveTurnAnalytics();
    renderReadingEditTrend();
  }})();
</script>
"""


def build_external_dashboard(
    external_log_path: str | Path,
    sync_start_time_iso: str | None,
    window_sec: int,
    main_process_name: str | None = None,
    trend_csv_path: str | Path | None = None,
) -> str:
    """Create the full standalone HTML dashboard for one external XML log."""

    ext = parse_external_log(external_log_path)
    summary = summarize_external(ext)

    datasets = {
        "system_events": ext.system_events,
        "browser_events": ext.browser_events,
        "input_events": ext.input_events,
        "window_dwell": ext.dwell,
        "window_switch_rows": summary.get("window_switch_rows", []),
        "typing_rows": summary.get("typing_rows", []),
        "reading_edit_trends": parse_reading_edit_trend_csv(trend_csv_path) if trend_csv_path else [],
    }
    datasets["top_sites"] = summary.get("top_sites", [])
    datasets["top_urls"] = summary.get("top_urls", [])
    sync_start = parse_iso(sync_start_time_iso or "")
    if sync_start is not None and ext.start is not None:
        summary["sync_start_iso"] = sync_start.isoformat()
        summary["sync_offset_ms"] = to_ms(sync_start) - to_ms(ext.start)
    else:
        summary["sync_start_iso"] = ""
        summary["sync_offset_ms"] = 0
    if main_process_name:
        summary["main_process_default"] = str(main_process_name).strip()
    summary["trend_window_sec"] = int(window_sec)
    payload = {"summary": summary, "datasets": datasets}
    panel = render_external_panel(payload, "External Activity Dashboard")
    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>External Activity Dashboard</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    body {{ font-family: "Segoe UI", Arial, sans-serif; background:#f2f6f3; margin:0; color:#324a45; }}
    .shell {{ max-width: 1200px; margin: 0 auto; padding: 18px; }}
    .panel {{ border-radius: 18px; background:#fff; padding:18px; box-shadow:0 8px 24px rgba(78,95,90,0.12); margin-bottom:14px; }}
    .hero {{
      border-radius: 18px;
      padding: 18px;
      background: linear-gradient(135deg,#4f7f72,#6ea091);
      color: #fff;
      box-shadow:0 12px 28px rgba(58,84,76,0.28);
      margin-bottom:14px;
    }}
    .hero h1 {{ margin:0 0 6px; font-size:28px; }}
    .hero .sub {{ opacity:0.92; font-size:13px; }}
    .hero .chips {{ margin-top:10px; display:flex; gap:8px; flex-wrap:wrap; }}
    .hero .chip {{ background:rgba(255,255,255,0.2); border:1px solid rgba(255,255,255,0.35); padding:4px 10px; border-radius:999px; font-size:12px; }}
    .cards {{ display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }}
    .card {{ background:#f5faf7; border:1px solid rgba(145,166,157,0.3); border-radius:12px; padding:10px; }}
    .card h4 {{ margin:0 0 4px; font-size:13px; color:#5c746f; }}
    .value {{ font-size:18px; font-weight:700; color:#35504b; }}
    .trend-plot-wrap {{ margin-top: 12px; }}
    .trend-plot {{ width:100%; min-height: 360px; }}
    .replay-view {{ margin-top: 12px; border:1px solid rgba(150,170,160,0.35); border-radius:12px; padding:10px; background:#fbfdfc; }}
    .summary-table {{ width:100%; border-collapse:collapse; font-size:12px; }}
    .summary-table th,.summary-table td {{ padding:6px 5px; border-bottom:1px solid rgba(127,145,136,0.25); text-align:left; }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="hero">
      <h1>External Activity Parser</h1>
      <div class="sub">Interactive external-activity analytics, URL intelligence, sync-aware exports, and typing change visualization.</div>
      <div class="chips">
        <span class="chip">Author: Jiajun Wu</span>
        <span class="chip">Email: jiajun.aiden.wu@outlook.com</span>
      </div>
    </div>
    {panel}
  </div>
</body>
</html>
"""


def generate_external_report(
    external_log_path: str | Path,
    sync_start_time_iso: str | None = None,
    main_process_name: str | None = None,
    trend_csv_path: str | Path | None = None,
    output_path: str | Path | None = None,
    window_sec: int = 30,
) -> str:
    html = build_external_dashboard(
        external_log_path=external_log_path,
        sync_start_time_iso=sync_start_time_iso,
        window_sec=window_sec,
        main_process_name=main_process_name,
        trend_csv_path=trend_csv_path,
    )
    if output_path:
        out = Path(output_path).resolve()
    else:
        out = Path(external_log_path).resolve().parent / "external_activity_report.html"
    out.write_text(html, encoding="utf-8")
    return str(out)


def run_gradio() -> None:
    import gradio as gr

    def do_generate(external_xml: Any, sync_start_iso: str, main_process_name: str, trend_csv: Any, window_sec: int) -> tuple[str, str]:
        external_path = external_xml.name if hasattr(external_xml, "name") else str(external_xml)
        if not external_path or not Path(external_path).exists():
            raise gr.Error("Please upload a valid External Activity XML file.")
        trend_path = trend_csv.name if hasattr(trend_csv, "name") else str(trend_csv) if trend_csv else ""
        if trend_path and not Path(trend_path).exists():
            raise gr.Error("Provided trend CSV path is invalid.")
        out = generate_external_report(
            external_log_path=external_path,
            sync_start_time_iso=(sync_start_iso or "").strip() or None,
            main_process_name=(main_process_name or "").strip() or None,
            trend_csv_path=trend_path or None,
            output_path=None,
            window_sec=int(window_sec),
        )
        return f"Report generated: {out}", out

    with gr.Blocks(title="External Activity Log Parser", theme=gr.themes.Soft()) as demo:
        gr.Markdown(
            """
# External Activity Log Parser & Integrator
Upload an External Activity Recorder XML file.  
Paste Translog startTime (ISO) to align external timeline start without uploading Translog XML.
Author: Jiajun Wu  
Email: jiajun.aiden.wu@outlook.com
"""
        )
        with gr.Row():
            external_input = gr.File(label="External Activity XML", file_types=[".xml"], type="filepath")
            sync_start_iso = gr.Textbox(label="Sync startTime (optional, ISO)", placeholder="2026-03-06T20:16:15.169427+08:00")
            main_process_name = gr.Textbox(label="Main process name (optional)", placeholder="e.g. translog.exe")
            trend_csv = gr.File(label="Reading/Edit Trend CSV (optional)", file_types=[".csv"], type="filepath")
        with gr.Row():
            window = gr.Slider(minimum=10, maximum=120, value=30, step=5, label="Trend window (seconds)")
        run_btn = gr.Button("Generate External Activity HTML Report", variant="primary")
        status = gr.Textbox(label="Status", interactive=False)
        out = gr.File(label="Generated Report (HTML)")
        run_btn.click(fn=do_generate, inputs=[external_input, sync_start_iso, main_process_name, trend_csv, window], outputs=[status, out])
    demo.launch()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--external-log", type=str, default="")
    parser.add_argument("--sync-start-time", type=str, default="")
    parser.add_argument("--main-process", type=str, default="")
    parser.add_argument("--trend-csv", type=str, default="")
    parser.add_argument("--output", type=str, default="")
    parser.add_argument("--window-sec", type=int, default=30)
    parser.add_argument("--gui", action="store_true")
    parser.add_argument("--cli", action="store_true")
    args = parser.parse_args()

    if args.gui or not args.cli:
        run_gradio()
        return

    if not args.external_log:
        raise ValueError("--external-log is required in CLI mode.")

    out = generate_external_report(
        external_log_path=args.external_log,
        sync_start_time_iso=args.sync_start_time or None,
        main_process_name=args.main_process or None,
        trend_csv_path=args.trend_csv or None,
        output_path=args.output or None,
        window_sec=max(5, int(args.window_sec)),
    )
    print(out)


if __name__ == "__main__":
    main()
