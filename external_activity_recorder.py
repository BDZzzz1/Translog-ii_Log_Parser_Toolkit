from __future__ import annotations

"""External Activity Recorder.

This module records post-editor activity outside Translog and serializes it to
an XML log that can be consumed by ``external_activity_parser.py``.

Primary responsibilities:
- Monitor foreground-window switches on Windows and accumulate per-window dwell.
- Optionally capture system keyboard/mouse input using ``pynput`` listeners.
- Accept browser companion events via local HTTP webhook
  (default: ``http://127.0.0.1:38953/browser-event``).
- Persist normalized event streams as UTF-8 XML.

Recorded XML sections:
- ``SystemEvents``: recorder lifecycle and foreground-window transition events.
- ``BrowserEvents``: extension-origin browser tab/navigation/input events.
- ``InputEvents``: optional global keyboard and mouse events.
- ``WindowDwell``: contiguous foreground-window spans with start/end/duration.

Execution modes:
- GUI mode (default): Gradio interface with Start/Stop/Save/Refresh controls.
- CLI mode: long-running recorder with Ctrl+C or fixed-duration termination.

Safety and data handling notes:
- ``safe_text`` sanitizes invalid XML characters and escapes control chars as
  ``\\uXXXX`` sequences, preventing malformed XML from key/control input.
- Browser title -> URL recovery reads recent Chrome/Edge History databases by
  copying them to a temporary location and querying SQLite snapshots.
"""

import argparse
import json
import os
import shutil
import sqlite3
import socket
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import psutil
from pynput import keyboard, mouse
import win32gui
import win32process


def now_local() -> datetime:
    return datetime.now().astimezone()


def iso(dt: datetime) -> str:
    return dt.isoformat()


def safe_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list, tuple)):
        raw = json.dumps(v, ensure_ascii=False)
    else:
        raw = str(v)

    def _is_valid_xml_char(ch: str) -> bool:
        cp = ord(ch)
        return (
            cp == 0x9
            or cp == 0xA
            or cp == 0xD
            or (0x20 <= cp <= 0xD7FF)
            or (0xE000 <= cp <= 0xFFFD)
            or (0x10000 <= cp <= 0x10FFFF)
        )

    out: list[str] = []
    for ch in raw:
        if _is_valid_xml_char(ch):
            out.append(ch)
        else:
            out.append(f"\\u{ord(ch):04x}")
    return "".join(out)


_HISTORY_CACHE: dict[str, Any] = {"ts": 0.0, "rows": []}


def _normalize_browser_title(title: str) -> str:
    t = str(title or "").strip()
    suffixes = [" - Google Chrome", " - Microsoft Edge", " - Mozilla Firefox"]
    for s in suffixes:
        if t.endswith(s):
            t = t[: -len(s)].strip()
    return t


def _load_recent_history_rows() -> list[tuple[str, str]]:
    now = time.time()
    if now - float(_HISTORY_CACHE.get("ts", 0.0)) < 20 and _HISTORY_CACHE.get("rows"):
        return list(_HISTORY_CACHE.get("rows", []))
    candidates = [
        Path(os.getenv("LOCALAPPDATA", "")) / "Google/Chrome/User Data/Default/History",
        Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft/Edge/User Data/Default/History",
    ]
    rows: list[tuple[str, str]] = []
    for db in candidates:
        try:
            if not db.exists():
                continue
            tmp_dir = Path(tempfile.mkdtemp(prefix="ext_hist_rec_"))
            tmp_db = tmp_dir / "History"
            shutil.copy2(db, tmp_db)
            con = sqlite3.connect(str(tmp_db))
            cur = con.cursor()
            cur.execute("SELECT url, title FROM urls ORDER BY last_visit_time DESC LIMIT 8000")
            got = cur.fetchall()
            con.close()
            for url, title in got:
                u = str(url or "").strip()
                t = _normalize_browser_title(str(title or "").strip())
                if u and t:
                    rows.append((u, t))
        except Exception:
            continue
    _HISTORY_CACHE["ts"] = now
    _HISTORY_CACHE["rows"] = rows
    return rows


def recover_url_from_title(title: str) -> str:
    t = _normalize_browser_title(title)
    if not t:
        return ""
    rows = _load_recent_history_rows()
    for url, ht in rows:
        if ht == t or t in ht or ht in t:
            return url
    return ""


@dataclass
class RecorderConfig:
    output: Path
    port: int
    poll_interval_ms: int
    duration_sec: int
    record_keystrokes: bool
    record_mouse: bool


class ExternalActivityRecorder:
    """In-memory recorder engine for external activity sessions.

    The recorder owns all mutable session state (event buckets, dwell spans,
    running/listener/server state) and exposes lifecycle methods:
    ``start()``, ``stop()``, ``write_xml()``, and ``snapshot()``.
    """

    def __init__(self, cfg: RecorderConfig) -> None:
        self.cfg = cfg
        self.start_dt = now_local()
        self.start_monotonic = time.monotonic()
        self.end_dt: datetime | None = None
        self._lock = threading.Lock()
        self._running = threading.Event()
        self._running.set()

        self.system_events: list[dict[str, Any]] = []
        self.browser_events: list[dict[str, Any]] = []
        self.input_events: list[dict[str, Any]] = []
        self.window_dwell: list[dict[str, Any]] = []

        self._last_window_key = ""
        self._last_window_title = ""
        self._last_window_process = ""
        self._last_window_hwnd = 0
        self._last_window_start_ms = 0

        self.keyboard_listener: keyboard.Listener | None = None
        self.mouse_listener: mouse.Listener | None = None
        self.http_server: ThreadingHTTPServer | None = None
        self.http_thread: threading.Thread | None = None

    def _is_browser_process(self, process: str) -> bool:
        p = str(process or "").lower()
        return ("chrome" in p) or ("msedge" in p) or ("firefox" in p)

    def ms(self) -> int:
        return int((time.monotonic() - self.start_monotonic) * 1000)

    def record(self, bucket: str, event_type: str, **fields: Any) -> None:
        row = {
            "type": event_type,
            "tsMs": self.ms(),
            "tsIso": iso(now_local()),
            **fields,
        }
        with self._lock:
            if bucket == "system":
                self.system_events.append(row)
            elif bucket == "browser":
                self.browser_events.append(row)
            else:
                self.input_events.append(row)

    def _current_window(self) -> tuple[int, str, str]:
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd) or ""
        pid = 0
        try:
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        except Exception:
            pid = 0
        process = ""
        if pid > 0:
            try:
                process = psutil.Process(pid).name()
            except Exception:
                process = ""
        return hwnd, title, process

    def _flush_dwell(self, until_ms: int) -> None:
        if not self._last_window_key:
            return
        start_ms = self._last_window_start_ms
        end_ms = max(start_ms, until_ms)
        self.window_dwell.append(
            {
                "windowKey": self._last_window_key,
                "title": self._last_window_title,
                "process": self._last_window_process,
                "hwnd": self._last_window_hwnd,
                "startMs": start_ms,
                "endMs": end_ms,
                "durationMs": end_ms - start_ms,
            }
        )

    def _monitor_windows(self) -> None:
        while self._running.is_set():
            now_ms = self.ms()
            hwnd, title, process = self._current_window()
            key = f"{process}|{title}"
            if key != self._last_window_key:
                self._flush_dwell(now_ms)
                prev_key = self._last_window_key
                self._last_window_key = key
                self._last_window_title = title
                self._last_window_process = process
                self._last_window_hwnd = hwnd
                self._last_window_start_ms = now_ms
                self.record(
                    "system",
                    "window_switch",
                    fromWindow=prev_key,
                    toWindow=key,
                    process=process,
                    title=title,
                    hwnd=hwnd,
                )
                recovered_url = recover_url_from_title(title) if self._is_browser_process(process) else ""
                if recovered_url:
                    self.record("browser", "window_switch_url", tabId=0, windowId=0, url=recovered_url, title=title, reason="history_recover")
            time.sleep(max(0.05, self.cfg.poll_interval_ms / 1000))

    def _on_key_press(self, key: keyboard.KeyCode | keyboard.Key) -> None:
        if not self.cfg.record_keystrokes:
            return
        hwnd, title, process = self._current_window()
        key_text = getattr(key, "char", None) if hasattr(key, "char") else None
        if key_text is None:
            key_text = str(key)
        self.record(
            "input",
            "key_press",
            key=key_text,
            process=process,
            title=title,
            hwnd=hwnd,
        )

    def _on_mouse_click(self, x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        if not self.cfg.record_mouse:
            return
        if not pressed:
            return
        hwnd, title, process = self._current_window()
        self.record(
            "input",
            "mouse_click",
            x=x,
            y=y,
            button=str(button),
            process=process,
            title=title,
            hwnd=hwnd,
        )

    def _make_http_handler(self):
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self) -> None:
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.end_headers()

            def do_POST(self) -> None:
                if self.path != "/browser-event":
                    self._send(404, {"ok": False, "error": "not_found"})
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    data = json.loads(raw.decode("utf-8"))
                except Exception:
                    self._send(400, {"ok": False, "error": "invalid_json"})
                    return
                event_type = str(data.get("type") or "browser_event")
                payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
                recorder.record("browser", event_type, **payload)
                self._send(200, {"ok": True})

            def log_message(self, format: str, *args: Any) -> None:
                return

        return Handler

    def _start_http_server(self) -> None:
        handler = self._make_http_handler()
        self.http_server = ThreadingHTTPServer(("127.0.0.1", self.cfg.port), handler)
        self.http_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
        self.http_thread.start()
        self.record("system", "browser_webhook_started", host="127.0.0.1", port=self.cfg.port)

    def _stop_http_server(self) -> None:
        if self.http_server is not None:
            self.http_server.shutdown()
            self.http_server.server_close()
            self.http_server = None

    def start(self) -> None:
        self._start_http_server()
        self.keyboard_listener = keyboard.Listener(on_press=self._on_key_press)
        self.keyboard_listener.start()
        self.mouse_listener = mouse.Listener(on_click=self._on_mouse_click)
        self.mouse_listener.start()
        self.record("system", "recording_started", host=socket.gethostname(), pid=str(psutil.Process().pid))
        monitor = threading.Thread(target=self._monitor_windows, daemon=True)
        monitor.start()

    def stop(self) -> None:
        if not self._running.is_set():
            return
        self._running.clear()
        self.end_dt = now_local()
        self._flush_dwell(self.ms())
        if self.keyboard_listener is not None:
            self.keyboard_listener.stop()
        if self.mouse_listener is not None:
            self.mouse_listener.stop()
        self._stop_http_server()
        recovered_url = recover_url_from_title(self._last_window_title) if self._is_browser_process(self._last_window_process) else ""
        if self._is_browser_process(self._last_window_process):
            if recovered_url:
                self.record("browser", "window_switch_url", tabId=0, windowId=0, url=recovered_url, title=self._last_window_title, reason="stop_recover")
        self.record("system", "recording_stopped")

    def _append_rows(self, parent: ET.Element, tag: str, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            node = ET.SubElement(parent, tag)
            for k, v in row.items():
                if isinstance(v, (dict, list, tuple)):
                    node.set(k, json.dumps(v, ensure_ascii=False))
                else:
                    node.set(k, safe_text(v))

    def write_xml(self) -> Path:
        if self.end_dt is None:
            self.end_dt = now_local()
        root = ET.Element("ExternalActivityLog")
        root.set("version", "1.0")
        ET.SubElement(root, "startTime").text = iso(self.start_dt)
        ET.SubElement(root, "endTime").text = iso(self.end_dt)
        cfg = ET.SubElement(root, "RecorderConfig")
        cfg.set("port", str(self.cfg.port))
        cfg.set("pollIntervalMs", str(self.cfg.poll_interval_ms))
        cfg.set("recordKeystrokes", str(self.cfg.record_keystrokes).lower())
        cfg.set("recordMouse", str(self.cfg.record_mouse).lower())

        system = ET.SubElement(root, "SystemEvents")
        browser = ET.SubElement(root, "BrowserEvents")
        inputs = ET.SubElement(root, "InputEvents")
        dwell = ET.SubElement(root, "WindowDwell")

        with self._lock:
            self._append_rows(system, "Event", self.system_events)
            self._append_rows(browser, "Event", self.browser_events)
            self._append_rows(inputs, "Event", self.input_events)
            self._append_rows(dwell, "Span", self.window_dwell)

        ET.indent(root)
        self.cfg.output.parent.mkdir(parents=True, exist_ok=True)
        ET.ElementTree(root).write(self.cfg.output, encoding="utf-8", xml_declaration=True)
        return self.cfg.output

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running.is_set(),
                "system_events": len(self.system_events),
                "browser_events": len(self.browser_events),
                "input_events": len(self.input_events),
                "window_dwell": len(self.window_dwell),
                "start_time": iso(self.start_dt),
                "end_time": iso(self.end_dt) if self.end_dt else "",
                "output": str(self.cfg.output),
                "port": self.cfg.port,
            }


def run_cli(cfg: RecorderConfig) -> str:
    rec = ExternalActivityRecorder(cfg)
    rec.start()
    print(f"External Activity Recorder started. Webhook: http://127.0.0.1:{cfg.port}/browser-event")
    print("Stop with Ctrl+C.")
    try:
        if cfg.duration_sec > 0:
            end_at = time.time() + cfg.duration_sec
            while time.time() < end_at:
                time.sleep(0.25)
        else:
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        rec.stop()
    out = rec.write_xml()
    return str(out)


def run_gradio() -> None:
    import gradio as gr

    state = {"recorder": None, "saved_path": None}

    def to_gradio_safe_file(path: Path) -> str | None:
        try:
            cache_dir = Path.cwd() / "_gradio_exports"
            cache_dir.mkdir(parents=True, exist_ok=True)
            target = cache_dir / path.name
            if path.resolve() != target.resolve():
                shutil.copy2(path, target)
            return str(target.resolve())
        except Exception:
            return None

    def choose_output_path(current_output: str) -> str:
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            root.deiconify()
            root.lift()
            root.focus_force()
            root.update()
            picked = filedialog.asksaveasfilename(
                title="Choose output XML file",
                defaultextension=".xml",
                initialfile=Path(current_output or "external_activity_log.xml").name,
                initialdir=str(Path(current_output).resolve().parent) if current_output else str(Path.cwd()),
                filetypes=[("XML files", "*.xml"), ("All files", "*.*")],
                parent=root,
            )
            root.attributes("-topmost", False)
            root.destroy()
            return str(Path(picked).resolve()) if picked else current_output
        except Exception:
            return current_output

    def start_recording(
        output: str,
        port: int,
        poll_interval_ms: int,
        record_keystrokes: bool,
        record_mouse: bool,
        st: dict[str, Any],
    ) -> tuple[str, dict[str, Any], str | None]:
        rec = st.get("recorder")
        if rec is not None and rec.snapshot().get("running"):
            snap = rec.snapshot()
            return (
                f"Already running. Webhook: http://127.0.0.1:{snap['port']}/browser-event | "
                f"system={snap['system_events']} browser={snap['browser_events']} input={snap['input_events']}",
                st,
                st.get("saved_path"),
            )
        cfg = RecorderConfig(
            output=Path(output).resolve(),
            port=max(1024, int(port)),
            poll_interval_ms=max(50, int(poll_interval_ms)),
            duration_sec=0,
            record_keystrokes=bool(record_keystrokes),
            record_mouse=bool(record_mouse),
        )
        rec = ExternalActivityRecorder(cfg)
        rec.start()
        st["recorder"] = rec
        st["saved_path"] = None
        snap = rec.snapshot()
        return (
            f"Recording started at {snap['start_time']}. "
            f"Webhook: http://127.0.0.1:{snap['port']}/browser-event",
            st,
            None,
        )

    def stop_recording(st: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
        rec = st.get("recorder")
        if rec is None:
            return "No active recording session.", st, st.get("saved_path")
        rec.stop()
        snap = rec.snapshot()
        return (
            f"Recording stopped at {snap['end_time']}. "
            f"system={snap['system_events']} browser={snap['browser_events']} input={snap['input_events']} "
            f"dwell={snap['window_dwell']}",
            st,
            st.get("saved_path"),
        )

    def refresh_progress(st: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
        rec = st.get("recorder")
        if rec is None:
            return "Recorder not started.", st, st.get("saved_path")
        snap = rec.snapshot()
        return (
            f"running={snap['running']} system={snap['system_events']} browser={snap['browser_events']} "
            f"input={snap['input_events']} dwell={snap['window_dwell']}",
            st,
            st.get("saved_path"),
        )

    def save_recording(output: str, st: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
        rec = st.get("recorder")
        if rec is None:
            return "No recording session to save.", st, None
        if rec.snapshot().get("running"):
            rec.stop()
        output_path = Path(output).expanduser()
        if output_path.exists() and output_path.is_dir():
            output_path = output_path / "external_activity_log.xml"
        if output_path.suffix.lower() != ".xml":
            output_path = output_path.with_suffix(".xml")
        rec.cfg.output = output_path.resolve()
        out = rec.write_xml()
        st["saved_path"] = str(out)
        safe_file = to_gradio_safe_file(Path(out))
        snap = rec.snapshot()
        return (
            f"Saved XML: {out} | start={snap['start_time']} end={snap['end_time']}",
            st,
            safe_file,
        )

    with gr.Blocks(title="External Activity Recorder", theme=gr.themes.Soft()) as demo:
        gr.Markdown(
            """
# External Activity Recorder
Record OS window switching and browser companion events into XML.
Author: Jiajun Wu  
Email: jiajun.aiden.wu@outlook.com
"""
        )
        st = gr.State(state)
        with gr.Row():
            output = gr.Textbox(label="Output XML path", value=str(Path("external_activity_log.xml").resolve()))
            choose_btn = gr.Button("Browse...")
        with gr.Row():
            port = gr.Number(label="Webhook port", value=38953, precision=0)
            poll = gr.Number(label="Window poll interval (ms)", value=500, precision=0)
        with gr.Row():
            rec_keys = gr.Checkbox(label="Record keystrokes", value=False)
            rec_mouse = gr.Checkbox(label="Record mouse clicks", value=False)
        with gr.Row():
            start_btn = gr.Button("Start Recording", variant="primary")
            stop_btn = gr.Button("Stop Recording")
            save_btn = gr.Button("Save XML")
            refresh_btn = gr.Button("Refresh Progress")
        status = gr.Textbox(label="Status", interactive=False)
        out_file = gr.File(label="Saved XML File")

        start_btn.click(
            fn=start_recording,
            inputs=[output, port, poll, rec_keys, rec_mouse, st],
            outputs=[status, st, out_file],
        )
        choose_btn.click(fn=choose_output_path, inputs=[output], outputs=[output])
        stop_btn.click(fn=stop_recording, inputs=[st], outputs=[status, st, out_file])
        save_btn.click(fn=save_recording, inputs=[output, st], outputs=[status, st, out_file])
        refresh_btn.click(fn=refresh_progress, inputs=[st], outputs=[status, st, out_file])
    demo.launch()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=str, default="external_activity_log.xml")
    parser.add_argument("--port", type=int, default=38953)
    parser.add_argument("--poll-interval-ms", type=int, default=500)
    parser.add_argument("--duration-sec", type=int, default=0)
    parser.add_argument("--record-keystrokes", action="store_true")
    parser.add_argument("--record-mouse", action="store_true")
    parser.add_argument("--gui", action="store_true")
    parser.add_argument("--cli", action="store_true")
    args = parser.parse_args()

    if args.gui or not args.cli:
        run_gradio()
        return

    cfg = RecorderConfig(
        output=Path(args.output).resolve(),
        port=max(1024, int(args.port)),
        poll_interval_ms=max(50, int(args.poll_interval_ms)),
        duration_sec=max(0, int(args.duration_sec)),
        record_keystrokes=bool(args.record_keystrokes),
        record_mouse=bool(args.record_mouse),
    )
    out = run_cli(cfg)
    print(out)


if __name__ == "__main__":
    main()
