"""Translog-ii post-editing analytics and report generator.

This module parses a Translog XML log and produces a standalone interactive HTML
report containing trend charts, heat overlays, compiled MT→PE timelines, cursor
movement analytics, change-segment diagnostics, and export workflows.

It supports both:
- GUI mode via Gradio (`run_gradio`)
- Headless CLI mode (`main` with `--headless`)
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
import uuid
import xml.etree.ElementTree as ET


@dataclass
class Event:
    """Normalized Translog event row.

    Each XML event node is converted into this dataclass to simplify
    downstream analytics and rendering.
    """
    tag: str
    time_ms: int
    cursor: int | None
    value: str
    event_type: str
    text: str
    block: int | None
    x: int | None
    y: int | None

    @property
    def action_label(self) -> str:
        if self.tag == "Key":
            event_type = self.event_type.lower()
            value = self.value.lower()
            if event_type in {"insert", "delete", "ime"}:
                return f"key:{event_type}"
            if event_type == "navi":
                return f"key:navi:{value}"
            if event_type == "edit":
                return f"key:edit:{value}"
            return f"key:{event_type}"
        if self.tag == "Mouse":
            return f"mouse:{self.value.lower()}"
        return f"{self.tag.lower()}:{self.value.lower()}"


@dataclass
class CharPos:
    """Character layout position from Translog char maps."""
    cursor: int
    value: str
    x: int
    y: int
    width: int
    height: int


def parse_int(value: str | None, default: int | None = None) -> int | None:
    """Parse integer-like strings safely.

    Returns `default` if value is None or cannot be converted.
    """
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def parse_xml(xml_path: str | Path) -> dict[str, Any]:
    """Parse Translog XML into a normalized project dictionary.

    Extracts source/target/final texts, event stream, project metadata, and
    character position maps required by visual panels.
    """
    root = ET.parse(xml_path).getroot()
    settings = root.find(".//Settings")
    events_root = root.find("Events")
    if events_root is None:
        raise ValueError("No <Events> section found.")
    project = root.find(".//Project")
    source_text = (settings.findtext("SourceTextUTF8", default="") if settings is not None else "")
    target_text = (settings.findtext("TargetTextUTF8", default="") if settings is not None else "")
    final_text = root.findtext("FinalTextUTF8", default="")
    event_rows: list[Event] = []
    for node in events_root:
        time_ms = parse_int(node.attrib.get("Time"), 0) or 0
        event_rows.append(
            Event(
                tag=node.tag,
                time_ms=time_ms,
                cursor=parse_int(node.attrib.get("Cursor")),
                value=node.attrib.get("Value", ""),
                event_type=node.attrib.get("Type", node.tag),
                text=node.attrib.get("Text", ""),
                block=parse_int(node.attrib.get("Block")),
                x=parse_int(node.attrib.get("X")),
                y=parse_int(node.attrib.get("Y")),
            )
        )
    event_rows.sort(key=lambda e: e.time_ms)
    meta_start = (project.attrib.get("startTime", "") if project is not None else "") if project is not None else ""
    meta_end = (project.attrib.get("endTime", "") if project is not None else "") if project is not None else ""
    target_chars = parse_char_map(root.find("TargetTextChar"))
    source_chars = parse_char_map(root.find("SourceTextChar"))
    final_chars = parse_char_map(root.find("FinalTextChar"))
    return {
        "source_text": source_text,
        "target_text": target_text,
        "final_text": final_text,
        "events": event_rows,
        "project_start": meta_start,
        "project_end": meta_end,
        "target_chars": target_chars,
        "source_chars": source_chars,
        "final_chars": final_chars,
    }


def parse_char_map(node: ET.Element | None) -> list[CharPos]:
    """Parse `<CharPos>` entries into `CharPos` list."""
    if node is None:
        return []
    result: list[CharPos] = []
    for c in node.findall("CharPos"):
        cursor = parse_int(c.attrib.get("Cursor"), 0) or 0
        result.append(
            CharPos(
                cursor=cursor,
                value=c.attrib.get("Value", ""),
                x=parse_int(c.attrib.get("X"), 0) or 0,
                y=parse_int(c.attrib.get("Y"), 0) or 0,
                width=parse_int(c.attrib.get("Width"), 0) or 0,
                height=parse_int(c.attrib.get("Height"), 0) or 0,
            )
        )
    return result


def parse_time(value: str) -> datetime | None:
    """Parse project-level timestamps using known Translog-compatible formats."""
    if not value:
        return None
    for fmt in ("%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def levenshtein(a: str, b: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            ins = curr[j - 1] + 1
            dele = prev[j] + 1
            sub = prev[j - 1] + (ca != cb)
            curr.append(min(ins, dele, sub))
        prev = curr
    return prev[-1]


def find_first_meaningful(events: list[Event]) -> int | None:
    """Find first meaningful key-edit timestamp used for initial delay metric."""
    for e in events:
        if e.tag != "Key":
            continue
        t = e.event_type.lower()
        if t in {"insert", "delete", "edit"}:
            return e.time_ms
        if t == "ime" and e.value not in {"", "[]"}:
            return e.time_ms
    return None


def build_binned_action_counts(events: list[Event], window_sec: int) -> dict[str, Any]:
    """Aggregate event counts into fixed time windows by action label."""
    if not events:
        return {"x": [0.0], "window_sec": max(1, window_sec), "action_counts": {}}
    window_sec = max(1, window_sec)
    max_time = max(e.time_ms for e in events)
    bins = max(1, math.ceil((max_time + 1) / (window_sec * 1000)))
    x = [round((i + 1) * window_sec / 60, 2) for i in range(bins)]
    action_counts: dict[str, list[int]] = {}
    for e in events:
        idx = min(bins - 1, e.time_ms // (window_sec * 1000))
        label = e.action_label
        if label not in action_counts:
            action_counts[label] = [0] * bins
        action_counts[label][idx] += 1
    return {"x": x, "window_sec": window_sec, "action_counts": action_counts}


def build_action_catalog(events: list[Event]) -> list[dict[str, Any]]:
    """Build sorted action catalog for UI selector controls."""
    counter: Counter[str] = Counter()
    for e in events:
        label = e.action_label
        if label.startswith("system:"):
            continue
        counter[label] += 1
    return [{"label": label, "count": count} for label, count in counter.most_common()]


def build_action_summary(events: list[Event]) -> dict[str, int]:
    """Count all action labels for summary table output."""
    counter: Counter[str] = Counter()
    for e in events:
        counter[e.action_label] += 1
    return dict(counter.most_common())


def build_cursor_first_time(events: list[Event], max_cursor: int) -> list[int]:
    """Build first-visit timestamp lookup per cursor index.

    Missing indices inherit the latest seen timestamp for monotonic lookup.
    """
    table = [0] * (max_cursor + 2)
    seen: dict[int, int] = {}
    for e in events:
        if e.cursor is None:
            continue
        c = min(max(e.cursor, 0), max_cursor)
        if c not in seen:
            seen[c] = e.time_ms
    last = 0
    for i in range(max_cursor + 1):
        if i in seen:
            last = seen[i]
        table[i] = last
    table[max_cursor + 1] = table[max_cursor]
    return table


def build_paragraph_markers(target_text: str, events: list[Event]) -> list[dict[str, Any]]:
    """Create paragraph markers mapped to first-visit time/cursor estimates."""
    if not target_text:
        return []
    marker_cursors = [0]
    for i, ch in enumerate(target_text):
        if ch == "\n" and i + 1 < len(target_text):
            marker_cursors.append(i + 1)
    marker_time: dict[int, int] = {}
    markers_sorted = sorted(set(marker_cursors))
    ptr = 0
    max_reached = -1
    events_sorted = sorted(events, key=lambda e: e.time_ms)
    for e in events_sorted:
        if e.cursor is None:
            continue
        c = max(0, min(e.cursor, len(target_text)))
        if c > max_reached:
            max_reached = c
        while ptr < len(markers_sorted) and markers_sorted[ptr] <= max_reached:
            marker_time[markers_sorted[ptr]] = e.time_ms
            ptr += 1
        if ptr >= len(markers_sorted):
            break
    fallback_time = events_sorted[-1].time_ms if events_sorted else 0
    markers: list[dict[str, Any]] = []
    total = max(1, len(marker_cursors) - 1)
    for idx, c in enumerate(marker_cursors):
        c = min(c, len(target_text))
        t = marker_time.get(c, fallback_time)
        markers.append(
            {
                "label": f"P{idx + 1}",
                "paragraph_index": idx + 1,
                "time_ms": t,
                "elapsed_sec": round(t / 1000, 4),
                "minute": round(t / 60000, 4),
                "cursor": c,
                "progress_pct": round((idx / total) * 100, 3),
            }
        )
    return markers


def build_activity_events(events: list[Event]) -> list[dict[str, Any]]:
    """Convert events into compact activity rows used by frontend JS."""
    rows: list[dict[str, Any]] = []
    for e in events:
        rows.append(
            {
                "time_ms": e.time_ms,
                "cursor": e.cursor if e.cursor is not None else -1,
                "label": e.action_label,
            }
        )
    return rows


def build_event_xml_line(e: Event) -> str:
    """Serialize an `Event` back to concise XML-like one-line text."""
    parts = [f'<{e.tag} Time="{e.time_ms}"']
    if e.cursor is not None:
        parts.append(f' Cursor="{e.cursor}"')
    if e.event_type:
        parts.append(f' Type="{html.escape(e.event_type)}"')
    if e.value:
        parts.append(f' Value="{html.escape(e.value)}"')
    if e.text:
        parts.append(f' Text="{html.escape(e.text)}"')
    if e.block is not None:
        parts.append(f' Block="{e.block}"')
    parts.append(" />")
    return "".join(parts)


def extract_transient_clusters(events: list[Event]) -> list[dict[str, Any]]:
    """Heuristically detect transient text clusters (inserted then deleted)."""
    active_by_char: dict[str, list[dict[str, Any]]] = {}
    transient_chars: list[dict[str, Any]] = []
    for e in events:
        if e.tag != "Key":
            continue
        ev_type = e.event_type.lower()
        if ev_type == "insert" and e.value:
            base = e.cursor or 0
            for idx, ch in enumerate(e.value):
                active_by_char.setdefault(ch, []).append(
                    {"char": ch, "cursor": base + idx, "time_ms": e.time_ms, "ins_event": e}
                )
        elif ev_type == "edit" and "ctrl+v" in e.value.lower() and e.text:
            base = e.cursor or 0
            for idx, ch in enumerate(e.text):
                active_by_char.setdefault(ch, []).append(
                    {"char": ch, "cursor": base + idx, "time_ms": e.time_ms, "ins_event": e}
                )
        elif ev_type in {"delete", "edit"} and e.text:
            for ch in e.text:
                stack = active_by_char.get(ch, [])
                if stack:
                    src = stack.pop()
                    transient_chars.append(
                        {
                            "char": ch,
                            "cursor": src["cursor"],
                            "insert_time_ms": src["time_ms"],
                            "delete_time_ms": e.time_ms,
                            "insert_event": build_event_xml_line(src["ins_event"]),
                            "delete_event": build_event_xml_line(e),
                        }
                    )
    if not transient_chars:
        return []
    transient_chars.sort(key=lambda x: (x["delete_time_ms"], x["cursor"]))
    clusters: list[dict[str, Any]] = []
    cluster: dict[str, Any] | None = None
    for item in transient_chars:
        if cluster is None:
            cluster = {
                "cursor": item["cursor"],
                "text": item["char"],
                "start_ms": item["insert_time_ms"],
                "end_ms": item["delete_time_ms"],
                "raw_events": [item["insert_event"], item["delete_event"]],
            }
            continue
        close_time = item["delete_time_ms"] - cluster["end_ms"] <= 1200
        close_cursor = abs(item["cursor"] - (cluster["cursor"] + len(cluster["text"]))) <= 2
        if close_time and close_cursor:
            cluster["text"] += item["char"]
            cluster["end_ms"] = max(cluster["end_ms"], item["delete_time_ms"])
            cluster["raw_events"].append(item["delete_event"])
        else:
            clusters.append(cluster)
            cluster = {
                "cursor": item["cursor"],
                "text": item["char"],
                "start_ms": item["insert_time_ms"],
                "end_ms": item["delete_time_ms"],
                "raw_events": [item["insert_event"], item["delete_event"]],
            }
    if cluster:
        clusters.append(cluster)
    return clusters


def estimate_segment_meta(segment: dict[str, Any], events: list[Event], default_session_ms: int) -> dict[str, Any]:
    """Estimate timing metadata for one MT→PE change segment.

    Uses cursor proximity and content cues to infer likely editing cluster and
    duration window around the segment.
    """
    mt_text = segment.get("mt_text", "")
    pe_text = segment.get("pe_text", "")
    start_cursor = segment.get("anchor_cursor", 0)
    end_cursor = start_cursor + max(1, len(mt_text), len(pe_text))
    edit_events = [e for e in events if e.tag == "Key" and e.event_type.lower() in {"insert", "delete", "edit"}]
    hits: list[tuple[Event, int]] = []
    for e in edit_events:
        score = 0
        if e.cursor is not None and start_cursor - 2 <= e.cursor <= end_cursor + 2:
            score += 2
        if e.text and mt_text and (e.text in mt_text or mt_text[: min(5, len(mt_text))] in e.text):
            score += 2
        if e.value and pe_text and len(e.value) == 1 and e.value in pe_text:
            score += 1
        if score > 0:
            hits.append((e, score))
    if hits:
        hits.sort(key=lambda x: x[0].time_ms)
        clusters: list[list[tuple[Event, int]]] = []
        current: list[tuple[Event, int]] = [hits[0]]
        for item in hits[1:]:
            gap = item[0].time_ms - current[-1][0].time_ms
            if gap <= 12000:
                current.append(item)
            else:
                clusters.append(current)
                current = [item]
        clusters.append(current)
        best_cluster = max(
            clusters,
            key=lambda c: (sum(s for _, s in c), len(c), -abs((c[0][0].cursor or start_cursor) - start_cursor)),
        )
        raw_hits = [h for h, _ in best_cluster]
        t0 = min(h.time_ms for h in raw_hits)
        t1 = max(h.time_ms for h in raw_hits)
    else:
        t0 = 0
        t1 = default_session_ms
    if t1 <= t0:
        t1 = t0 + 200
    if t1 - t0 > 180000:
        t1 = t0 + 180000
    raw_lines = [build_event_xml_line(h) for h in raw_hits[:20]] if hits else []
    points = [h.time_ms for h in raw_hits[:40]] if hits else [t0, t1]
    return {
        "id": segment["id"],
        "type": segment.get("type", "change"),
        "mt_text": mt_text[:120],
        "pe_text": pe_text[:120],
        "anchor_cursor": segment.get("anchor_cursor", 0),
        "span_len": max(1, len(mt_text), len(pe_text)),
        "start_ms": t0,
        "end_ms": t1,
        "duration_ms": t1 - t0,
        "raw_xml": raw_lines,
        "points_ms": points,
    }


def reconstruct_full_change_timeline(
    target_text: str, final_text: str, events: list[Event]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Reconstruct token-level MT→PE timeline and segment metadata.

    The output token stream supports compiled colored rendering; segment metadata
    supports hover diagnostics, duration estimation, and popup detail panels.
    """
    tokens: list[dict[str, Any]] = []
    idx = 0
    segments: list[dict[str, Any]] = []
    mt_cursor = 0
    pe_cursor = 0
    seg_counter = 0
    matcher = difflib.SequenceMatcher(a=target_text, b=final_text, autojunk=False)
    for op, a0, a1, b0, b1 in matcher.get_opcodes():
        changed_seg_id: str | None = None
        if op != "equal":
            changed_seg_id = f"seg-{seg_counter}"
            seg_counter += 1
            segments.append(
                {
                    "id": changed_seg_id,
                    "type": op,
                    "mt_text": target_text[a0:a1],
                    "pe_text": final_text[b0:b1],
                    "anchor_cursor": a0,
                }
            )
        if op == "equal":
            for ch in target_text[a0:a1]:
                tokens.append(
                    {
                        "id": f"orig-{idx}",
                        "text": ch,
                        "kind": "origin",
                        "alive": True,
                        "seg_id": "",
                        "anchor_cursor": mt_cursor,
                    }
                )
                idx += 1
                mt_cursor += 1
                pe_cursor += 1
        elif op == "delete":
            for ch in target_text[a0:a1]:
                tokens.append(
                    {
                        "id": f"orig-{idx}",
                        "text": ch,
                        "kind": "origin",
                        "alive": False,
                        "seg_id": changed_seg_id or "",
                        "anchor_cursor": mt_cursor,
                    }
                )
                idx += 1
                mt_cursor += 1
        elif op == "insert":
            for ch in final_text[b0:b1]:
                tokens.append(
                    {
                        "id": f"ins-{idx}",
                        "text": ch,
                        "kind": "inserted",
                        "alive": True,
                        "seg_id": changed_seg_id or "",
                        "anchor_cursor": mt_cursor,
                    }
                )
                idx += 1
                pe_cursor += 1
        elif op == "replace":
            for ch in target_text[a0:a1]:
                tokens.append(
                    {
                        "id": f"orig-{idx}",
                        "text": ch,
                        "kind": "origin",
                        "alive": False,
                        "seg_id": changed_seg_id or "",
                        "anchor_cursor": mt_cursor,
                    }
                )
                idx += 1
                mt_cursor += 1
            for ch in final_text[b0:b1]:
                tokens.append(
                    {
                        "id": f"ins-{idx}",
                        "text": ch,
                        "kind": "inserted",
                        "alive": True,
                        "seg_id": changed_seg_id or "",
                        "anchor_cursor": a0,
                    }
                )
                idx += 1
                pe_cursor += 1
    transient_clusters = extract_transient_clusters(events)
    transient_insert_map: dict[int, list[dict[str, Any]]] = {}
    for c in transient_clusters:
        transient_insert_map.setdefault(c["cursor"], []).append(c)
    if transient_insert_map:
        merged: list[dict[str, Any]] = []
        consumed_cursors: set[int] = set()
        for t in tokens:
            cursor = t.get("anchor_cursor", 0)
            if cursor not in consumed_cursors:
                extra = transient_insert_map.get(cursor, [])
                for cluster in extra:
                    seg_id = f"transient-{seg_counter}"
                    seg_counter += 1
                    segments.append(
                        {
                            "id": seg_id,
                            "type": "transient",
                            "mt_text": "",
                            "pe_text": cluster["text"],
                            "anchor_cursor": cluster["cursor"],
                            "raw_xml": cluster["raw_events"][:6],
                            "start_ms": cluster["start_ms"],
                            "end_ms": cluster["end_ms"],
                        }
                    )
                    for ch in cluster["text"]:
                        merged.append(
                            {
                                "id": f"ins-{idx}",
                                "text": ch,
                                "kind": "inserted",
                                "alive": False,
                                "seg_id": seg_id,
                                "anchor_cursor": cluster["cursor"],
                            }
                        )
                        idx += 1
                consumed_cursors.add(cursor)
            merged.append(t)
        remaining_cursors = sorted(c for c in transient_insert_map if c not in consumed_cursors)
        for cursor in remaining_cursors:
            for cluster in transient_insert_map[cursor]:
                seg_id = f"transient-{seg_counter}"
                seg_counter += 1
                segments.append(
                    {
                        "id": seg_id,
                        "type": "transient",
                        "mt_text": "",
                        "pe_text": cluster["text"],
                        "anchor_cursor": cluster["cursor"],
                        "raw_xml": cluster["raw_events"][:6],
                        "start_ms": cluster["start_ms"],
                        "end_ms": cluster["end_ms"],
                    }
                )
                for ch in cluster["text"]:
                    merged.append(
                        {
                            "id": f"ins-{idx}",
                            "text": ch,
                            "kind": "inserted",
                            "alive": False,
                            "seg_id": seg_id,
                            "anchor_cursor": cluster["cursor"],
                        }
                    )
                    idx += 1
        tokens = merged
    session_ms = events[-1].time_ms if events else 0
    segment_meta: list[dict[str, Any]] = []
    for seg in segments:
        if seg.get("type") == "transient":
            segment_meta.append(
                {
                    "id": seg["id"],
                    "type": "transient",
                    "mt_text": "",
                    "pe_text": seg.get("pe_text", "")[:120],
                    "anchor_cursor": seg.get("anchor_cursor", 0),
                    "span_len": max(1, len(seg.get("pe_text", ""))),
                    "start_ms": seg.get("start_ms", 0),
                    "end_ms": seg.get("end_ms", 0),
                    "duration_ms": max(1, seg.get("end_ms", 0) - seg.get("start_ms", 0)),
                    "raw_xml": seg.get("raw_xml", []),
                    "points_ms": [seg.get("start_ms", 0), seg.get("end_ms", 0)],
                }
            )
        else:
            segment_meta.append(estimate_segment_meta(seg, events, session_ms))
    return tokens, segment_meta


def render_compiled_text(tokens: list[dict[str, Any]]) -> str:
    """Render timeline tokens into semantic HTML spans."""
    spans: list[str] = []
    for t in tokens:
        text = html.escape(t["text"]).replace("\n", "<br/>")
        seg_id = t.get("seg_id", "")
        if t["kind"] == "origin" and t["alive"]:
            cls = "txt-origin"
        elif t["kind"] == "origin" and not t["alive"]:
            cls = "txt-origin-deleted"
        elif t["kind"] == "inserted" and t["alive"]:
            cls = "txt-inserted-alive"
        elif isinstance(seg_id, str) and seg_id.startswith("transient-"):
            cls = "txt-transient-deleted"
        else:
            cls = "txt-inserted-deleted"
        seg_attr = html.escape(seg_id)
        extra_cls = " chg-span" if seg_attr else ""
        spans.append(
            f'<span class="{cls}{extra_cls}" data-seg="{seg_attr}" data-cursor="{t.get("anchor_cursor", 0)}" title="id={t["id"]}">{text}</span>'
        )
    return "".join(spans)


def build_mt_action_heat(events: list[Event], mt_char_count: int) -> dict[str, list[float]]:
    """Build action-indexed per-cursor heat arrays from event traces."""
    heat_counts: dict[str, list[float]] = {}
    length = max(1, mt_char_count)
    for e in events:
        if e.cursor is None:
            continue
        cursor = e.cursor
        if cursor < 0 or cursor >= length:
            continue
        label = e.action_label
        if label not in heat_counts:
            heat_counts[label] = [0.0] * length
        span = e.block if e.block and e.block > 1 else 1
        for offset in range(span):
            idx = cursor + offset
            if idx >= length:
                break
            heat_counts[label][idx] += 1.0
    return heat_counts


def normalize(values: list[float]) -> list[float]:
    """Normalize a numeric array to [0, 1] with zero-safe fallback."""
    if not values:
        return []
    m = max(values)
    if m <= 0:
        return [0.0 for _ in values]
    return [v / m for v in values]


def render_heat_canvas(chars: list[CharPos], panel_title: str, panel_id: str, hidden: bool = False) -> str:
    """Render one heat overlay panel with absolute-positioned characters."""
    if not chars:
        extra_cls = " hidden" if hidden else ""
        return f"<div class='heat-panel{extra_cls}' id='{html.escape(panel_id)}'><h3>{html.escape(panel_title)}</h3><p>No char map found.</p></div>"
    min_x = min(c.x for c in chars)
    max_x = max(c.x + c.width for c in chars)
    min_y = min(c.y for c in chars)
    max_y = max(c.y + c.height for c in chars)
    pad = 30
    width = max_x - min_x + pad * 2
    height = max_y - min_y + pad * 2
    layer = [f"<div class='heat-canvas' data-base-width='{width}' data-base-height='{height}' style='width:{width}px;height:{height}px;'>"]
    for idx, c in enumerate(chars):
        x = c.x - min_x + pad
        y = c.y - min_y + pad
        char_val = html.escape(c.value).replace("\n", "↵")
        w = max(12, c.width)
        h = max(18, c.height)
        style = f"left:{x}px;top:{y}px;width:{w}px;height:{h}px;"
        layer.append(
            f"<span class='heat-char' data-idx='{idx}' data-cursor='{c.cursor}' data-bx='{x}' data-by='{y}' data-bw='{w}' data-bh='{h}' style='{style}' title='Cursor {c.cursor}'>{char_val}</span>"
        )
    layer.append("</div>")
    extra_cls = " hidden" if hidden else ""
    return f"<div class='heat-panel{extra_cls}' id='{html.escape(panel_id)}'><h3>{html.escape(panel_title)}</h3><p class='heat-legend'>Reading Speed Heat</p>{''.join(layer)}</div>"


def build_metrics(data: dict[str, Any]) -> dict[str, Any]:
    """Compute key report metrics used in top dashboard cards."""
    events: list[Event] = data["events"]
    source_text: str = data["source_text"]
    target_text: str = data["target_text"]
    final_text: str = data["final_text"]
    project_start = parse_time(data["project_start"])
    project_end = parse_time(data["project_end"])
    if project_start and project_end:
        session_sec = max(0.0, (project_end - project_start).total_seconds())
    elif events:
        session_sec = (events[-1].time_ms - events[0].time_ms) / 1000
    else:
        session_sec = 0.0
    dist = levenshtein(target_text, final_text)
    change_rate = (dist / max(1, len(target_text))) * 100
    correction_count = sum(1 for e in events if e.tag == "Key" and e.event_type.lower() in {"delete", "edit"})
    first_edit_time = find_first_meaningful(events)
    initial_delay_ms = first_edit_time if first_edit_time is not None else 0
    return {
        "session_sec": round(session_sec, 3),
        "change_rate": round(change_rate, 3),
        "corrections": correction_count,
        "initial_delay_sec": round(initial_delay_ms / 1000, 3),
        "source_len": len(source_text),
        "mt_len": len(target_text),
        "pe_len": len(final_text),
    }


def split_source_sentences(text: str) -> list[str]:
    """Split source text into sentence-like chunks for explanatory tooltips."""
    raw = text.replace("\r\n", "\n").replace("\r", "\n")
    chunks = re.split(r"(?<=[。！？!?；;:])\s+|\n+", raw)
    sentences = [s.strip() for s in chunks if s and s.strip()]
    if sentences:
        return sentences
    return [raw.strip()] if raw.strip() else []


def build_report_html(
    data: dict[str, Any],
    window_sec: int,
) -> str:
    """Assemble full standalone HTML report (styles, scripts, and payloads)."""
    events: list[Event] = data["events"]
    metrics = build_metrics(data)
    action_summary = build_action_summary(events)
    action_catalog = build_action_catalog(events)
    trend_payload = build_binned_action_counts(events, window_sec=window_sec)
    paragraph_markers = build_paragraph_markers(data["target_text"], events)
    activity_events = build_activity_events(events)
    mt_heat_payload = build_mt_action_heat(events, mt_char_count=len(data["target_chars"]))
    pe_heat_payload = build_mt_action_heat(events, mt_char_count=len(data["final_chars"]))
    max_cursor_for_time = max(1, len(data["target_text"]), len(data["final_text"]), len(data["target_chars"]), len(data["final_chars"]))
    cursor_time_map = build_cursor_first_time(events, max_cursor=max_cursor_for_time)
    tokens, segment_meta = reconstruct_full_change_timeline(data["target_text"], data["final_text"], events)
    compiled = render_compiled_text(tokens)
    mt_panel = render_heat_canvas(data["target_chars"], "MT Text Heat Overlay", "mtHeatPanel", hidden=False)
    pe_panel = render_heat_canvas(data["final_chars"], "Post-edited Text Heat Overlay", "peHeatPanel", hidden=True)
    source_sentences = split_source_sentences(data["source_text"])
    total_events = max(1, len(events))
    action_html = "".join(
        f"<tr><td>{html.escape(k)}</td><td>{v}</td><td>{(v / total_events) * 100:.2f}%</td></tr>"
        for k, v in list(action_summary.items())[:30]
    )
    replay_events = [
        {
            "time_ms": e.time_ms,
            "tag": e.tag,
            "event_type": e.event_type,
            "value": e.value,
            "text": e.text,
            "cursor": e.cursor if e.cursor is not None else -1,
            "block": e.block if e.block is not None else 0,
            "label": e.action_label,
        }
        for e in events
    ]
    metrics_payload = {
        "session_sec": metrics["session_sec"],
        "change_rate": metrics["change_rate"],
        "corrections": metrics["corrections"],
        "initial_delay_sec": metrics["initial_delay_sec"],
        "source_len": metrics["source_len"],
        "mt_len": metrics["mt_len"],
        "pe_len": metrics["pe_len"],
        "total_events": len(events),
    }
    target_chars_payload = [
        {"cursor": c.cursor, "value": c.value, "x": c.x, "y": c.y, "width": c.width, "height": c.height}
        for c in data["target_chars"]
    ]
    source_chars_payload = [
        {"cursor": c.cursor, "value": c.value, "x": c.x, "y": c.y, "width": c.width, "height": c.height}
        for c in data["source_chars"]
    ]
    final_chars_payload = [
        {"cursor": c.cursor, "value": c.value, "x": c.x, "y": c.y, "width": c.width, "height": c.height}
        for c in data["final_chars"]
    ]

    default_edit = {"key:insert", "key:delete", "key:edit:[ctrl+v]", "key:edit:[ctrl+x]"}

    def build_checkbox_group(input_class: str, defaults: set[str]) -> str:
        rows: list[str] = []
        for item in action_catalog:
            label = item["label"]
            checked = "checked" if label in defaults else ""
            rows.append(
                f"<label class='action-option'><input type='checkbox' class='{input_class}' value='{html.escape(label)}' {checked}/> "
                f"<span>{html.escape(label)}</span><em>{item['count']}</em></label>"
            )
        return "".join(rows)

    edit_options = build_checkbox_group("edit-action", default_edit)

    report = f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Translog PE Process Report</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js"></script>
  <style>
    body {{
      margin: 0;
      font-family: "Segoe UI", "Inter", Arial, sans-serif;
      background: #f7f3ee;
      background-image:
        radial-gradient(circle at 12% 15%, rgba(255, 255, 255, 0.75) 0, rgba(255, 255, 255, 0) 220px),
        radial-gradient(circle at 90% 6%, rgba(225, 236, 233, 0.62) 0, rgba(225, 236, 233, 0) 240px),
        repeating-linear-gradient(45deg, rgba(148, 121, 94, 0.03) 0, rgba(148, 121, 94, 0.03) 2px, transparent 2px, transparent 8px);
      color: #2d3a36;
      line-height: 1.5;
    }}
    .container {{
      width: min(1440px, 94vw);
      margin: 34px auto 70px;
    }}
    .title {{
      font-size: 32px;
      font-weight: 650;
      letter-spacing: 0.2px;
      margin-bottom: 10px;
      color: #334746;
    }}
    .subtitle {{
      color: #5f6f6a;
      margin-bottom: 22px;
    }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 14px;
      margin-bottom: 20px;
    }}
    .card {{
      border: 1px solid rgba(165, 181, 172, 0.5);
      background: rgba(255, 255, 255, 0.68);
      border-radius: 18px;
      padding: 17px 18px;
      box-shadow: 0 8px 20px rgba(110, 120, 108, 0.08);
      backdrop-filter: blur(3px);
    }}
    .card h4 {{
      margin: 0 0 6px;
      color: #73857b;
      font-size: 12px;
      letter-spacing: 0.5px;
      text-transform: uppercase;
    }}
    .card .value {{
      font-size: 28px;
      font-weight: 560;
      color: #334746;
    }}
    .panel {{
      border: 1px solid rgba(166, 182, 174, 0.42);
      border-radius: 22px;
      background: rgba(255, 255, 255, 0.72);
      padding: 22px;
      margin-top: 16px;
      box-shadow: 0 10px 26px rgba(96, 109, 96, 0.09);
    }}
    .hidden {{
      display: none;
    }}
    .panel h2 {{
      margin: 0 0 12px;
      font-size: 24px;
      font-weight: 600;
      color: #324a45;
      letter-spacing: 0.2px;
    }}
    #speedPlot {{
      width: 100%;
      min-height: 420px;
    }}
    .button {{
      display: inline-block;
      margin-bottom: 10px;
      border: 1px solid rgba(137, 158, 147, 0.5);
      background: #f2f6f3;
      color: #35504b;
      border-radius: 999px;
      padding: 9px 16px;
      cursor: pointer;
      font-size: 13px;
      font-weight: 560;
      margin-right: 8px;
    }}
    .button.pivot-mini {{
      padding: 5px 10px;
      font-size: 11px;
      margin-bottom: 6px;
      margin-right: 6px;
    }}
    .legend-list {{
      margin-top: 6px;
      color: #5a6b67;
    }}
    .grid-two {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
    }}
    .summary-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    .summary-table th {{
      text-align: left;
      color: #55706a;
      font-weight: 600;
      border-bottom: 1px solid rgba(124, 145, 136, 0.35);
      padding: 7px 5px;
    }}
    .summary-table td {{
      border-bottom: 1px solid rgba(124, 145, 136, 0.22);
      padding: 7px 5px;
    }}
    .summary-table tr td:last-child {{
      text-align: right;
      color: #38625d;
      font-weight: 700;
    }}
    .controls {{
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 10px;
      margin-bottom: 14px;
    }}
    .control-box {{
      border: 1px solid rgba(160, 177, 167, 0.42);
      border-radius: 14px;
      background: rgba(248, 251, 247, 0.9);
      padding: 10px 11px;
      min-height: 165px;
      display: flex;
      flex-direction: column;
    }}
    .control-title {{
      font-size: 13px;
      color: #4a605d;
      margin-bottom: 8px;
      font-weight: 600;
    }}
    .action-list {{
      overflow: auto;
      flex: 1;
      border-top: 1px dashed rgba(132, 157, 143, 0.3);
      padding-top: 7px;
    }}
    .action-option {{
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 3px 0;
      font-size: 12px;
      color: #4f6562;
    }}
    .action-option em {{
      margin-left: auto;
      font-style: normal;
      color: #7b8c88;
      font-size: 11px;
    }}
    .heat-panel {{
      overflow: auto;
      border: 1px solid rgba(156, 174, 166, 0.35);
      border-radius: 14px;
      padding: 10px;
      background: rgba(247, 250, 246, 0.92);
      width: 100%;
      box-sizing: border-box;
    }}
    .heat-panel.hidden {{
      display: none;
    }}
    .heat-panel h3 {{
      margin: 0 0 6px;
      color: #3f5952;
      font-size: 16px;
    }}
    .heat-legend {{
      margin: 0 0 8px;
      color: #647873;
      font-size: 12px;
    }}
    .heat-canvas {{
      position: relative;
      border: 1px dashed rgba(132, 161, 146, 0.28);
      border-radius: 10px;
      background: repeating-linear-gradient(0deg, rgba(211, 224, 218, 0.15), rgba(211, 224, 218, 0.15) 24px, rgba(255, 255, 255, 0.64) 24px, rgba(255, 255, 255, 0.64) 48px);
      overflow: hidden;
      min-width: max-content;
      width: 100%;
    }}
    .heat-char {{
      position: absolute;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      color: #17332d;
      font-size: 11px;
      border-radius: 3px;
      text-shadow: 0 1px 1px rgba(255, 255, 255, 0.4);
      white-space: pre;
      transition: background 180ms ease, color 180ms ease, box-shadow 180ms ease;
      cursor: pointer;
    }}
    .compiled-text {{
      font-size: 16px;
      border: 1px solid rgba(159, 180, 170, 0.33);
      border-radius: 12px;
      background: rgba(250, 253, 250, 0.94);
      padding: 18px;
      white-space: normal;
      word-break: break-word;
    }}
    .txt-origin {{
      color: #2f4a41;
    }}
    .txt-origin-deleted {{
      color: #8f4f52;
      text-decoration: line-through;
      background: rgba(233, 184, 180, 0.45);
      border-radius: 3px;
    }}
    .txt-inserted-alive {{
      color: #205f4f;
      background: rgba(177, 221, 201, 0.52);
      border-radius: 3px;
      padding: 0 1px;
    }}
    .txt-inserted-deleted {{
      color: #6d537d;
      background: rgba(225, 198, 236, 0.52);
      text-decoration: line-through;
      border-radius: 3px;
      padding: 0 1px;
    }}
    .txt-transient-deleted {{
      color: #5e3f76;
      background: rgba(186, 151, 214, 0.55);
      text-decoration: line-through;
      border-radius: 3px;
      padding: 0 1px;
    }}
    .txt-transient-deleted.hidden-transient {{
      display: none;
    }}
    .flash-highlight {{
      outline: 3px solid rgba(246, 151, 67, 0.98);
      outline-offset: 2px;
      border-radius: 3px;
      box-shadow: 0 0 0 2px rgba(255, 211, 142, 0.55);
      animation: flashPulse 5s ease;
    }}
    @keyframes flashPulse {{
      0% {{ background-color: rgba(241, 168, 91, 0.72); }}
      100% {{ background-color: unset; }}
    }}
    .toggle-row {{
      display: flex;
      gap: 8px;
      margin-bottom: 10px;
    }}
    .toggle-btn {{
      border: 1px solid rgba(138, 160, 151, 0.46);
      border-radius: 999px;
      background: #f3f7f4;
      color: #47635f;
      padding: 7px 12px;
      font-size: 12px;
      cursor: pointer;
    }}
    .toggle-btn.active {{
      background: #dce9e2;
      color: #264a41;
      border-color: rgba(107, 139, 127, 0.62);
    }}
    .legend-explain {{
      margin-top: 8px;
      color: #60736e;
      font-size: 13px;
      background: rgba(248, 252, 249, 0.9);
      border: 1px solid rgba(161, 177, 169, 0.34);
      border-radius: 12px;
      padding: 10px 12px;
    }}
    .trend-plot-wrap {{
      margin-top: 10px;
      border: 1px solid rgba(157, 178, 169, 0.35);
      border-radius: 12px;
      background: rgba(252, 255, 253, 0.86);
      padding: 8px 10px;
    }}
    .trend-plot {{
      min-height: 360px;
      width: 100%;
    }}
    .change-popup {{
      position: fixed;
      right: 18px;
      bottom: 18px;
      width: min(560px, 42vw);
      min-height: 220px;
      border: 1px solid rgba(140, 165, 156, 0.56);
      border-radius: 14px;
      background: rgba(255, 255, 255, 0.92);
      box-shadow: 0 10px 22px rgba(88, 104, 96, 0.18);
      padding: 12px;
      z-index: 99;
      pointer-events: auto;
    }}
    .change-popup.hidden {{
      display: none;
    }}
    .change-popup h4 {{
      margin: 0 0 6px;
      color: #36544c;
      font-size: 14px;
      padding-right: 28px;
    }}
    .change-popup-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 6px;
      padding-right: 28px;
    }}
    .change-popup-actions {{
      display: inline-flex;
      gap: 6px;
      align-items: center;
    }}
    .popup-mini-btn {{
      border: 1px solid rgba(120, 150, 140, 0.48);
      border-radius: 999px;
      background: rgba(246, 252, 248, 0.94);
      color: #4f6d65;
      font-size: 11px;
      line-height: 1.2;
      padding: 4px 9px;
      cursor: pointer;
    }}
    .popup-mini-btn:hover {{
      background: rgba(237, 248, 242, 0.98);
    }}
    .popup-close {{
      position: absolute;
      top: 8px;
      right: 8px;
      width: 24px;
      height: 24px;
      border: 1px solid rgba(136, 159, 150, 0.45);
      border-radius: 50%;
      background: #f2f7f4;
      color: #54706a;
      cursor: pointer;
      font-size: 14px;
      line-height: 20px;
    }}
    .sparkline {{
      display: flex;
      flex-direction: column;
      gap: 6px;
      align-items: stretch;
      min-height: 58px;
      margin: 4px 0;
      border-top: 1px dashed rgba(126, 153, 143, 0.4);
      border-bottom: 1px dashed rgba(126, 153, 143, 0.3);
      padding: 6px 0;
    }}
    .spark-col {{
      display: block;
      height: 14px;
      min-width: 14px;
      background: rgba(103, 152, 136, 0.85);
      border-radius: 999px;
    }}
    .spark-wrap {{
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 11px;
      color: #56716b;
      flex: 0 0 auto;
    }}
    .spark-label {{
      width: 58px;
      color: #4f6961;
      font-weight: 600;
    }}
    .spark-value {{
      width: 58px;
      text-align: right;
      color: #5e736d;
    }}
    .vicinity-preview {{
      margin: 6px 0 10px;
      padding: 8px;
      border: 1px solid rgba(150, 173, 163, 0.35);
      border-radius: 10px;
      background: rgba(247, 252, 249, 0.9);
    }}
    .vicinity-caption {{
      font-size: 11px;
      color: #5d7770;
      margin-bottom: 6px;
    }}
    .vic-track {{
      position: relative;
      height: 12px;
      border-radius: 999px;
      background: rgba(203, 219, 211, 0.65);
      overflow: hidden;
    }}
    .vic-window {{
      position: absolute;
      top: 0;
      bottom: 0;
      background: rgba(116, 153, 140, 0.5);
      border-radius: 999px;
    }}
    .vic-core {{
      position: absolute;
      top: 0;
      bottom: 0;
      background: rgba(176, 128, 109, 0.86);
      border-radius: 999px;
    }}
    .vic-labels {{
      display: flex;
      justify-content: space-between;
      margin-top: 5px;
      font-size: 10px;
      color: #5f7670;
    }}
    .inline-control {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      color: #506964;
      margin: 8px 0 10px;
      padding: 6px 10px;
      border: 1px solid rgba(145, 168, 158, 0.42);
      border-radius: 999px;
      background: rgba(248, 252, 249, 0.95);
    }}
    .inline-control input {{
      width: 140px;
    }}
    .vicinity-help {{
      margin: 4px 0 12px;
      color: #56706a;
      font-size: 12px;
      line-height: 1.45;
      background: rgba(246, 250, 247, 0.9);
      border: 1px solid rgba(161, 182, 172, 0.35);
      border-radius: 10px;
      padding: 8px 10px;
    }}
    .transient-panel {{
      margin-top: 10px;
      border: 1px solid rgba(161, 132, 188, 0.35);
      border-radius: 10px;
      background: rgba(246, 238, 252, 0.82);
      padding: 10px 12px;
      color: #5e4673;
      font-size: 13px;
    }}
    .transient-panel.hidden {{
      display: none;
    }}
    .transient-items {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px 8px;
      margin-top: 6px;
    }}
    .transient-item {{
      display: inline-flex;
      align-items: center;
      max-width: 100%;
      padding: 2px 7px;
      border: 1px solid rgba(141, 108, 170, 0.32);
      border-radius: 999px;
      background: rgba(236, 224, 248, 0.72);
      color: #5a4272;
      word-break: break-word;
      font-size: 12px;
      line-height: 1.25;
    }}
    .transient-empty {{
      color: #6e5a82;
      font-size: 12px;
    }}
    .cursor-movement-wrap {{
      margin: 8px 0 12px;
      border: 1px solid rgba(145, 170, 160, 0.38);
      border-radius: 12px;
      background: rgba(248, 252, 249, 0.94);
      padding: 10px 12px;
    }}
    .cursor-movement-wrap.hidden {{
      display: none;
    }}
    .cursor-movement-title {{
      color: #48635d;
      font-size: 13px;
      margin-bottom: 6px;
      font-weight: 600;
    }}
    .cursor-movement-legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px 12px;
      font-size: 11px;
      color: #5e7771;
      margin-bottom: 8px;
    }}
    .cursor-movement-legend span {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }}
    .cursor-movement-legend i {{
      width: 10px;
      height: 10px;
      border-radius: 2px;
      display: inline-block;
    }}
    .cursor-lane {{
      margin-bottom: 8px;
    }}
    .cursor-lane-label {{
      font-size: 11px;
      color: #5a736d;
      margin-bottom: 3px;
    }}
    .cursor-lane-track {{
      position: relative;
      border: 1px solid rgba(151, 172, 164, 0.36);
      border-radius: 8px;
      background: rgba(245, 251, 248, 0.88);
      overflow: hidden;
      width: 100%;
    }}
    .cursor-path-track {{
      position: relative;
      height: 38px;
      border: 1px dashed rgba(134, 160, 149, 0.4);
      border-radius: 8px;
      background: linear-gradient(180deg, rgba(237, 246, 241, 0.7), rgba(246, 252, 248, 0.4));
      overflow: hidden;
      min-width: 100%;
    }}
    .cursor-lane-svg {{
      width: 100%;
      height: 100%;
      display: block;
    }}
    .cursor-path-svg {{
      width: 100%;
      height: 100%;
      display: block;
    }}
    .cursor-path-dot {{
      cursor: pointer;
    }}
    .cursor-axis-note {{
      margin-top: 5px;
      display: flex;
      justify-content: space-between;
      color: #68807a;
      font-size: 10px;
    }}
    .cursor-click-hit {{
      fill: rgba(0,0,0,0);
      cursor: pointer;
    }}
    .cursor-compare-tag {{
      display: inline-block;
      margin: 0 4px;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 11px;
      line-height: 1.35;
      font-weight: 700;
      vertical-align: text-top;
      box-shadow: 0 1px 4px rgba(57, 74, 68, 0.22);
    }}
    .cursor-compare-tag.point1 {{
      background: rgba(34, 146, 106, 0.24);
      color: #0f5a43;
      border: 2px solid rgba(28, 132, 96, 0.82);
    }}
    .cursor-compare-tag.point2 {{
      background: rgba(201, 94, 74, 0.24);
      color: #8b3627;
      border: 2px solid rgba(193, 74, 51, 0.82);
    }}
    .cursor-range-row {{
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 11px;
      color: #5f7872;
      margin-top: 3px;
    }}
    .cursor-point-popup {{
      position: fixed;
      left: 18px;
      top: 18px;
      width: min(420px, 42vw);
      border: 1px solid rgba(124, 154, 175, 0.48);
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.93);
      box-shadow: 0 8px 18px rgba(78, 102, 120, 0.16);
      padding: 10px 12px;
      z-index: 98;
    }}
    .cursor-point-popup.hidden {{
      display: none;
    }}
    .heat-point-popup {{
      position: fixed;
      left: 18px;
      bottom: 18px;
      width: min(360px, 36vw);
      border: 1px solid rgba(124, 154, 175, 0.48);
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.92);
      box-shadow: 0 8px 18px rgba(78, 102, 120, 0.16);
      padding: 10px 12px;
      z-index: 98;
    }}
    .heat-point-popup.hidden {{
      display: none;
    }}
    .raw-xml {{
      margin-top: 4px;
      font-family: Consolas, Monaco, monospace;
      font-size: 11px;
      color: #546a63;
      max-height: 180px;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      border-top: 1px dashed rgba(126, 153, 143, 0.35);
      padding-top: 6px;
    }}
    .popup-meta-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 6px;
      margin: 2px 0 8px;
    }}
    .popup-meta-chip {{
      border: 1px solid rgba(150, 174, 165, 0.42);
      border-radius: 8px;
      background: rgba(246, 251, 249, 0.85);
      color: #48655d;
      padding: 4px 7px;
      font-size: 11px;
    }}
    .popup-text-pair {{
      margin-top: 6px;
      display: grid;
      gap: 6px;
    }}
    .popup-text-block {{
      border: 1px solid rgba(152, 176, 166, 0.36);
      border-radius: 8px;
      background: rgba(248, 252, 249, 0.85);
      padding: 6px 8px;
      color: #4f6962;
      font-size: 12px;
      line-height: 1.45;
      word-break: break-word;
    }}
    .help-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 14px;
    }}
    .help-card {{
      border: 1px solid rgba(165, 181, 172, 0.5);
      border-radius: 12px;
      background: rgba(252, 255, 253, 0.88);
      padding: 12px;
      color: #546b64;
      font-size: 13px;
    }}
    .replay-controls {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 8px 12px;
      margin-bottom: 10px;
      align-items: end;
    }}
    .replay-row {{
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      color: #4d6560;
    }}
    .replay-row input[type="range"] {{
      flex: 1;
    }}
    .replay-time {{
      min-width: 148px;
      font-family: Consolas, Monaco, monospace;
      color: #5a706a;
      font-size: 11px;
    }}
    .replay-view {{
      border: 1px solid rgba(160, 179, 170, 0.38);
      border-radius: 12px;
      background: rgba(250, 253, 251, 0.93);
      padding: 10px;
    }}
    .replay-grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 10px;
    }}
    .replay-grid.show-source {{
      grid-template-columns: 1fr 1fr;
    }}
    .replay-pane {{
      border: 1px solid rgba(153, 174, 166, 0.35);
      border-radius: 10px;
      background: rgba(248, 252, 249, 0.92);
      padding: 8px;
    }}
    .replay-pane h4 {{
      margin: 0 0 6px;
      font-size: 13px;
      color: #415a53;
    }}
    .replay-text {{
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 14px;
      line-height: 1.6;
      color: #27413b;
      min-height: 120px;
      max-height: 360px;
      overflow: auto;
    }}
    .data-export-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 8px 14px;
      margin: 6px 0 10px;
    }}
    .data-export-item {{
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      color: #4b6560;
      border: 1px solid rgba(158, 177, 170, 0.32);
      border-radius: 8px;
      padding: 6px 8px;
      background: rgba(248, 252, 249, 0.86);
    }}
    details.vicinity-help > summary {{
      cursor: pointer;
      color: #48625b;
      user-select: none;
    }}
    .copyright {{
      margin-top: 18px;
      color: #6d7f79;
      font-size: 12px;
      text-align: right;
    }}
    @media (max-width: 1280px) {{
      .grid-two {{
        grid-template-columns: 1fr;
      }}
      .controls {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="title">Translog Post-Editing Process Visual Report</div>
    <div class="subtitle">An interactive analytical dashboard tracing the MT to PE process as logged by Translog-ii</div>
    <div class="cards">
      <div class="card"><h4>Session Time</h4><div class="value">{metrics["session_sec"]:.2f}s</div></div>
      <div class="card"><h4>MT → PE Change Rate</h4><div class="value">{metrics["change_rate"]:.2f}%</div></div>
      <div class="card"><h4>Corrections</h4><div class="value">{metrics["corrections"]}</div></div>
      <div class="card"><h4>Initial Delay</h4><div class="value">{metrics["initial_delay_sec"]:.2f}s</div></div>
      <div class="card"><h4>Source Length</h4><div class="value">{metrics["source_len"]}</div></div>
      <div class="card"><h4>MT Length</h4><div class="value">{metrics["mt_len"]}</div></div>
      <div class="card"><h4>PE Length</h4><div class="value">{metrics["pe_len"]}</div></div>
      <div class="card"><h4>Total Events</h4><div class="value">{len(events)}</div></div>
    </div>

    <div class="panel">
      <h2>Reading Speed and Edit Intensity Trends</h2>
      <div class="controls">
        <div class="control-box">
          <div class="control-title">Reading: cursor movement settings</div>
          <label class="inline-control">
            Section granularity (chars)
            <input id="readingGranularity" type="range" min="5" max="120" step="5" value="25"/>
            <span id="readingGranularityValue">25</span>
          </label>
          <label class="inline-control">
            Revisit range penalty
            <input id="revisitPenaltyWeight" type="range" min="0" max="4" step="0.1" value="1.5"/>
            <span id="revisitPenaltyWeightValue">1.5</span>
          </label>
        </div>
        <div class="control-box">
          <div class="control-title">Editing: included actions</div>
          <div class="action-list">{edit_options}</div>
        </div>
      </div>
      <details class="vicinity-help">
        <summary><strong>How is Reading Speed Score calculated?</strong></summary>
        <div style="margin-top:6px;">
          The code computes per-step score using section indices, then time-averages into each window.<br/>
          Exact equations (from implementation):<br/>
          &nbsp;&nbsp;<code>section = floor(max(0,cursor) / granularity)</code><br/>
          &nbsp;&nbsp;<code>distFromStart = currSection - startSection</code><br/>
          &nbsp;&nbsp;<code>elapsed = (currTime - startTime) / 1000</code><br/>
          &nbsp;&nbsp;<code>baseScore = distFromStart / elapsed</code><br/>
          &nbsp;&nbsp;<code>overlap = max(0, min(right, maxReachedSection) - left + 1)</code><br/>
          &nbsp;&nbsp;<code>penalty = revisitPenaltyWeight * (overlap / elapsed)</code><br/>
          &nbsp;&nbsp;<code>stepScore = baseScore - penalty</code><br/>
          where <code>left=min(prevSection,currSection)</code> and <code>right=max(prevSection,currSection)</code>.<br/><br/>
          <strong>How variables are determined (concrete)</strong><br/>
          Suppose cursor events (chars) are: <code>12 → 64 → 230 → 160</code>.<br/>
          If <code>granularity=25</code>, section mapping is:<br/>
          &nbsp;&nbsp;<code>12→0</code>, <code>64→2</code>, <code>230→9</code>, <code>160→6</code>.<br/>
          Then:<br/>
          &nbsp;&nbsp;<code>startSection = 0</code> (from first cursor=12),<br/>
          &nbsp;&nbsp;for last step <code>230→160</code>: <code>prevSection=9</code>, <code>currSection=6</code>.<br/>
          Running maximum by step: <code>0 → 2 → 9</code>, so before processing step <code>9→6</code>, <code>maxReachedSection=9</code>.<br/>
          After processing that step, it remains <code>max(9,6)=9</code>.<br/><br/>
          <strong>How Section granularity changes these values</strong><br/>
          Using the same cursor sequence <code>12 → 64 → 230 → 160</code>:<br/>
          • If <code>granularity=25</code>: sections are <code>0,2,9,6</code>.<br/>
          • If <code>granularity=50</code>: sections are <code>0,1,4,3</code>.<br/>
          So <code>startSection</code>, <code>prevSection</code>, <code>currSection</code>, and <code>maxReachedSection</code> are all derived from the same cursor path but at different section resolution.<br/><br/>
          <strong>Numeric Example</strong><br/>
          Suppose: <code>startSection=0</code>, current step moves <code>prevSection=9 → currSection=6</code>, and <code>maxReachedSection=12</code>.<br/>
          Then <code>left=6</code>, <code>right=9</code>, so:<br/>
          &nbsp;&nbsp;<code>overlap = min(9,12)-6+1 = 4</code> sections.<br/>
          If <code>currTime-startTime=20s</code>, then:<br/>
          &nbsp;&nbsp;<code>distFromStart = 6-0 = 6</code>, <code>baseScore = 6/20 = 0.3000</code>.<br/>
          With <code>revisitPenaltyWeight=1.5</code>:<br/>
          &nbsp;&nbsp;<code>penalty = 1.5*(4/20)=0.3000</code>, so <code>stepScore = 0.3000-0.3000=0.0000</code>.<br/>
          With <code>revisitPenaltyWeight=2.0</code> (same step):<br/>
          &nbsp;&nbsp;<code>penalty = 2.0*(4/20)=0.4000</code>, so <code>stepScore = 0.3000-0.4000=-0.1000</code>.<br/>
          Final trend point is the step-score time average in that window, then multiplied by 60 for display.
        </div>
      </details>
      <div class="vicinity-help">
        For Editing Intensity Heat, Translog-II may log wrong cursor positions for <strong>key:ime</strong>. Keep <strong>key:ime</strong> unchecked unless you explicitly want to include that noise.
      </div>
      <button class="button" onclick="redrawAll()">Re-draw Graph</button>
      <button id="normalizeReadingBtn" class="button" onclick="toggleReadingNormalization()">Normalize Reading Scale: On</button>
      <button class="button" onclick="clearTimeMarker()">Clear Selected Line Marker</button>
      <button class="button" onclick="exportSpeedPNG()">Export Speed PNG</button>
      <button class="button" onclick="exportSpeedCSV()">Export Graph Data CSV</button>
      <div class="trend-plot-wrap">
        <div id="speedPlotTime" class="trend-plot"></div>
      </div>
      <div class="legend-explain">
        <div><strong>Time-based Trend</strong> shows Reading Speed Score and Edit Intensity by session time windows.</div>
        <div><strong>Reading Speed Score</strong> = start→target elapsed-time speed on cursor sections (IME ignored), with revisit penalties for re-covered ranges.</div>
        <div><strong>Reading Rule</strong> = base score uses start→current elapsed time, then subtracts revisit penalties on overlapped visited ranges.</div>
        <div><strong>Edit Intensity</strong> = (sum of selected editing actions) ÷ minutes per window.</div>
        <div><strong>Normalize Reading Scale</strong> rescales reading line magnitude to the same y-range as Edit Intensity for visual comparability.</div>
        <div><strong>Window</strong> = {window_sec} seconds per point. Adjust granularity/penalty or editing actions then click <strong>Re-draw Graph</strong>.</div>
      </div>
    </div>

    <div class="panel">
      <h2>Position-based Reading Speed and Edit Intensity Trends</h2>
      <div class="controls">
        <div class="control-box">
          <div class="control-title">Reading: cursor movement settings (position-based)</div>
          <label class="inline-control">
            Section granularity (chars)
            <input id="posReadingGranularity" type="range" min="5" max="120" step="5" value="25"/>
            <span id="posReadingGranularityValue">25</span>
          </label>
          <label class="inline-control">
            Revisit range penalty
            <input id="posRevisitPenaltyWeight" type="range" min="0" max="4" step="0.1" value="1.5"/>
            <span id="posRevisitPenaltyWeightValue">1.5</span>
          </label>
        </div>
      </div>
      <button class="button" onclick="redrawAll()">Re-draw Graph</button>
      <button id="normalizePositionBtn" class="button" onclick="togglePositionReadingNormalization()">Normalize Reading Scale: On</button>
      <button class="button" onclick="clearPositionMarker()">Clear Selected Line Marker</button>
      <button class="button" onclick="exportPositionSpeedPNG()">Export Speed PNG</button>
      <button class="button" onclick="exportPositionSpeedCSV()">Export Graph Data CSV</button>
      <div class="trend-plot-wrap">
        <div id="speedPlotPosition" class="trend-plot"></div>
      </div>
      <div class="legend-explain">
        <div><strong>Position-based Trend</strong> shows Reading Speed Score and Edit Intensity along text positions (character sections).</div>
        <div><strong>X axis</strong> is character index position; each point summarizes one section controlled by <strong>Section granularity</strong>.</div>
        <div><strong>Use case</strong> compare where in the text reading/editing burden concentrates, independent of timeline order.</div>
      </div>
    </div>

    <div class="panel" id="actionSummaryPanel">
      <h2>Action Types Summary</h2>
      <button class="button" onclick="exportActionSummaryPNG()">Export Action Summary PNG</button>
      <table class="summary-table">
        <thead><tr><th>Action</th><th>Count</th><th>Percentage</th></tr></thead>
        <tbody>{action_html}</tbody>
      </table>
    </div>

    <div class="panel">
      <h2>Interactive Heat Map Overlay</h2>
      <div class="toggle-row">
        <button id="heatTextModeMT" class="toggle-btn active" onclick="switchHeatTextMode('mt')">MT Text</button>
        <button id="heatTextModePE" class="toggle-btn" onclick="switchHeatTextMode('pe')">Post-edited Text</button>
        <button id="heatModeReading" class="toggle-btn active" onclick="switchHeatMode('reading')">Reading Speed Heat</button>
        <button id="heatModeEditing" class="toggle-btn" onclick="switchHeatMode('editing')">Editing Intensity Heat</button>
        <button id="normalizeHeatBtn" class="toggle-btn active" onclick="toggleHeatNormalization()">Normalize Heat Scale: On</button>
        <button class="toggle-btn" onclick="redrawAll()">Re-draw Graph</button>
        <button class="toggle-btn" onclick="exportHeatMapPNG()">Export Heat Map PNG</button>
      </div>
      <label class="inline-control">
        Section granularity (chars)
        <input id="heatReadingGranularity" type="range" min="5" max="120" step="5" value="25"/>
        <span id="heatReadingGranularityValue">25</span>
      </label>
      <label class="inline-control">
        Revisit range penalty
        <input id="heatRevisitPenaltyWeight" type="range" min="0" max="4" step="0.1" value="1.5"/>
        <span id="heatRevisitPenaltyWeightValue">1.5</span>
      </label>
      {mt_panel}
      {pe_panel}
    </div>

    <div class="panel">
      <h2>Data Export</h2>
      <div class="replay-row">
        <button class="button" onclick="toggleAllDataExport(true)">Select All</button>
        <button class="button" onclick="toggleAllDataExport(false)">Clear All</button>
        <button class="button" onclick="exportSelectedDataCSV()">Export Selected CSV</button>
      </div>
      <div class="vicinity-help">
        Browser download restrictions may block bulk exports. Please export no more than <strong>8 CSV files</strong> at once.
      </div>
      <div class="data-export-grid">
        <label class="data-export-item"><input class="data-export-check" type="checkbox" value="session_metrics" checked/>Session metrics</label>
        <label class="data-export-item"><input class="data-export-check" type="checkbox" value="action_summary" checked/>Action summary table</label>
        <label class="data-export-item"><input class="data-export-check" type="checkbox" value="action_catalog" checked/>Action catalog counts</label>
        <label class="data-export-item"><input class="data-export-check" type="checkbox" value="time_trend" checked/>Time-based trend series</label>
        <label class="data-export-item"><input class="data-export-check" type="checkbox" value="position_trend" checked/>Position-based trend series</label>
        <label class="data-export-item"><input class="data-export-check" type="checkbox" value="cursor_timeline" checked/>Cursor movement timeline bins</label>
        <label class="data-export-item"><input class="data-export-check" type="checkbox" value="segment_meta" checked/>Change segment metadata</label>
        <label class="data-export-item"><input class="data-export-check" type="checkbox" value="replay_events" checked/>Replay source events</label>
        <label class="data-export-item"><input class="data-export-check" type="checkbox" value="activity_events" checked/>Activity events</label>
        <label class="data-export-item"><input class="data-export-check" type="checkbox" value="cursor_time_map" checked/>Cursor first-visit timeline map</label>
        <label class="data-export-item"><input class="data-export-check" type="checkbox" value="paragraph_markers" checked/>Paragraph markers</label>
        <label class="data-export-item"><input class="data-export-check" type="checkbox" value="mt_heat_map" checked/>MT heat arrays (all actions)</label>
        <label class="data-export-item"><input class="data-export-check" type="checkbox" value="pe_heat_map" checked/>PE heat arrays (all actions)</label>
        <label class="data-export-item"><input class="data-export-check" type="checkbox" value="target_char_map" checked/>Target char position map</label>
        <label class="data-export-item"><input class="data-export-check" type="checkbox" value="source_char_map" checked/>Source char position map</label>
        <label class="data-export-item"><input class="data-export-check" type="checkbox" value="final_char_map" checked/>Final char position map</label>
        <label class="data-export-item"><input class="data-export-check" type="checkbox" value="text_payloads" checked/>Source/MT/PE text payloads</label>
        <label class="data-export-item"><input class="data-export-check" type="checkbox" value="segment_detail_vicinity" checked/>Change Segment Detail (vicinity-reactive)</label>
      </div>
      <div class="replay-row">
        <button class="button" onclick="refreshPivotFieldOptions()">Refresh Pivot Fields</button>
        <button class="button" onclick="buildPivotResult()">Build Pivot Table</button>
        <button class="button" onclick="renderPivotChart()">Generate Pivot Graph</button>
        <button class="button" onclick="exportPivotCSV()">Export Pivot CSV</button>
      </div>
      <label class="inline-control">
        Pause long-gap threshold (ms)
        <input id="pauseGapThresholdMs" type="range" min="500" max="12000" step="100" value="2500"/>
        <span id="pauseGapThresholdMsValue">2500</span>
      </label>
      <div class="replay-row">
        <strong style="color:#48625b;">Quick Apply Examples:</strong>
        <button class="button pivot-mini" onclick="applyPivotPreset('cursor_revert')">Revert Timeline</button>
        <button class="button pivot-mini" onclick="applyPivotPreset('time_edit')">Time Edit Intensity</button>
        <button class="button pivot-mini" onclick="applyPivotPreset('event_dist')">Event Distribution</button>
        <button class="button pivot-mini" onclick="applyPivotPreset('heat_action_sum')">Heat by Action</button>
        <button class="button pivot-mini" onclick="applyPivotPreset('segment_type_count')">Segment Type Counts</button>
        <button class="button pivot-mini" onclick="applyPivotPreset('scatter_pause_vs_revert')">Scatter: Pause vs Revert</button>
        <button class="button pivot-mini" onclick="applyPivotPreset('scatter_span_vs_duration')">Scatter: Span vs Duration</button>
        <button class="button pivot-mini" onclick="applyPivotPreset('forward_timeline')">Forward Timeline</button>
      </div>
      <div class="replay-row">
        <button class="button pivot-mini" onclick="applyPivotPreset('pause_timeline')">Pause Timeline</button>
        <button class="button pivot-mini" onclick="applyPivotPreset('focus_loss_timeline')">Focus Loss Timeline</button>
        <button class="button pivot-mini" onclick="applyPivotPreset('segment_duration_by_type')">Segment Duration by Type</button>
        <button class="button pivot-mini" onclick="applyPivotPreset('segment_pause_ratio')">Segment Pause Ratio</button>
        <button class="button pivot-mini" onclick="applyPivotPreset('adaptive_duration_compare')">Adaptive Duration Compare</button>
        <button class="button pivot-mini" onclick="applyPivotPreset('mt_pe_action_compare')">MT vs PE Action Heat</button>
        <button class="button pivot-mini" onclick="applyPivotPreset('scatter_pause_vs_focus')">Scatter: Pause vs Focus Loss</button>
        <button class="button pivot-mini" onclick="applyPivotPreset('scatter_time_vs_cursor')">Scatter: Time vs Cursor</button>
        <button class="button pivot-mini" onclick="applyPivotPreset('progress_time_curve')">Progress Time Curve</button>
        <button class="button pivot-mini" onclick="applyPivotPreset('char_geometry_scatter')">Scatter: Char X vs Y</button>
      </div>
      <div class="replay-controls">
        <label class="replay-row">
          Row field
          <select id="pivotRowField"></select>
        </label>
        <label class="replay-row">
          Column field
          <select id="pivotColField"></select>
        </label>
        <label class="replay-row">
          Value field
          <select id="pivotValField"></select>
        </label>
        <label class="replay-row">
          Aggregation
          <select id="pivotAgg">
            <option value="count">count</option>
            <option value="sum">sum</option>
            <option value="avg">avg</option>
            <option value="min">min</option>
            <option value="max">max</option>
          </select>
        </label>
        <label class="replay-row">
          Chart type
          <select id="pivotChartType">
            <option value="bar">bar</option>
            <option value="line">line</option>
            <option value="scatter">scatter</option>
          </select>
        </label>
      </div>
      <details class="vicinity-help">
        <summary><strong>Pivot Table Help</strong></summary>
        <div style="margin-top:6px;">
        1) Select one or more datasets in the checklist (for example <code>cursor_timeline</code>, <code>segment_detail_vicinity</code>, <code>time_trend</code>).<br/>
        2) Click <strong>Refresh Pivot Fields</strong> so the Row/Column/Value fields reflect currently selected data.<br/>
        3) Choose <strong>Row field</strong> as your grouping key, optionally choose a <strong>Column field</strong> for cross-tab, then set <strong>Value field</strong> + <strong>Aggregation</strong>.<br/>
        4) Click <strong>Build Pivot Table</strong> to generate the matrix; click <strong>Generate Pivot Graph</strong> for an interactive chart; click <strong>Export Pivot CSV</strong> for the current pivot result.<br/><br/>
        <strong>Pivot control meanings</strong><br/>
        • <strong>Row field</strong>: primary grouping key (x-axis categories). Example: <code>window_index</code>.<br/>
        • <strong>Column field</strong>: secondary grouping key (creates multi-series columns). Example: <code>vicinity_adaptive</code>.<br/>
        • <strong>Value field</strong>: numeric/text field to aggregate. Example: <code>duration_ms</code>.<br/>
        • <strong>Aggregation</strong>: how grouped values are combined.<br/>
        &nbsp;&nbsp;– <code>count</code>: number of rows in the group. Example: 12 rows → result 12.<br/>
        &nbsp;&nbsp;– <code>sum</code>: arithmetic sum of numeric values. Example: 2.5, 1.0, 3.5 → 7.0.<br/>
        &nbsp;&nbsp;– <code>avg</code>: mean of numeric values. Example: (2.5+1.0+3.5)/3 = 2.3333.<br/>
        &nbsp;&nbsp;– <code>min</code>: smallest numeric value in the group. Example: min(2.5,1.0,3.5)=1.0.<br/>
        &nbsp;&nbsp;– <code>max</code>: largest numeric value in the group. Example: max(2.5,1.0,3.5)=3.5.<br/>
        &nbsp;&nbsp;Note: for non-numeric values, <code>count</code> is safest and most interpretable.<br/>
        • <strong>Chart type</strong>: visualization style for pivot result. Use line for ordered series and bar for category comparison.<br/><br/>
        <strong>Field meanings (intuitive + examples)</strong><br/>
        • <strong>dataset</strong>: source dataset name of each row. Example: <code>cursor_timeline</code>, <code>segment_detail_vicinity</code>.<br/>
        • <strong>window_index</strong>: nth cursor timeline window. Example: 1, 2, 3... used for chronological grouping.<br/>
        • <strong>minute_in_session</strong>: time bucket in minutes since start. Example: 4.5 means around minute 4:30.<br/>
        • <strong>text_position_char_index</strong>: character-index location in text. Example: 500 means around character 500 region.<br/>
        • <strong>reading_speed_score</strong>: model score of reading progression speed (higher = faster progression).<br/>
        • <strong>edit_intensity</strong>: editing activity density in the bucket/section.<br/>
        • <strong>segment_type</strong>/<strong>type</strong>: diff opcode category. Common values: <code>replace</code>, <code>insert</code>, <code>delete</code>.<br/>
        • <strong>duration_ms</strong>: estimated duration in milliseconds. Example: 3200 = ~3.2 seconds.<br/>
        • <strong>vicinity_before</strong>/<strong>vicinity_after</strong>: context chars used for duration estimation in popup logic.<br/>
        • <strong>vicinity_adaptive</strong>: 1 = adaptive vicinity enabled, 0 = fixed vicinity.<br/>
        • <strong>forward_chars</strong>/<strong>backward_chars</strong>: cursor movement magnitude in each timeline window.<br/>
        • <strong>pause_seconds</strong>: accumulated gap time where <code>gap_ms >= pause_threshold_ms</code>.<br/>
        • <strong>focus_loss_count</strong>: number of blur/out-of-focus events in that window.<br/>
        • <strong>duration_seconds</strong>: duration in seconds (window or segment, depending on dataset).<br/>
        • <strong>forward_rate_cps</strong>/<strong>backward_rate_cps</strong>: forward/backward chars per second in a timeline window.<br/>
        • <strong>net_cursor_delta</strong>: end cursor minus start cursor for a timeline window.<br/>
        • <strong>pause_ratio_pct</strong>: pause time ÷ total duration × 100 (percentage).<br/>
        • <strong>pause_per_100_chars</strong>: pause seconds normalized by segment span size.<br/>
        • <strong>active_edit_seconds</strong>: duration_seconds − pause_seconds for segment-level rows.<br/>
        • <strong>events_in_range</strong>: number of activity events inside segment vicinity/time span.<br/>
        • <strong>chars_per_sec</strong>: span or progress normalized by duration (speed-like productivity field).<br/>
        • <strong>segment_end_cursor</strong>: segment end anchor cursor (= anchor + span).<br/>
        • <strong>segment_mt_text</strong>/<strong>segment_pe_text</strong>: segment text snapshot for MT and PE.<br/>
        • <strong>segment_raw_xml</strong>: raw event XML lines linked to this segment (CSV export enrichment when Row=segment_id).<br/>
        • <strong>pause_threshold_ms</strong>: user-selected long-gap threshold used in pause calculations.<br/>
        • <strong>pause_source</strong>: pause computation source (segment raw events or vicinity activity fallback).<br/>
        • <strong>action</strong> (heat arrays): action category key that contributes to heat values.<br/>
        • <strong>value</strong> (heat arrays/char maps/events): numeric heat contribution or event payload text, depending on dataset.<br/><br/>
        <strong>pause_seconds vs duration_seconds (exact)</strong><br/>
        • <code>duration_seconds</code> is total segment/window span duration.<br/>
        • <code>pause_seconds</code> is accumulated long-gap time inside that span, where each gap satisfies <code>gap_ms >= pause_threshold_ms</code>.<br/>
        • <code>pause_threshold_ms</code> is user-configurable via <strong>Pause long-gap threshold (ms)</strong> slider in Data Export.<br/>
        • For segment-level pause metrics, gaps are computed from that segment's <code>raw_xml</code> event times (not broad vicinity activity fallback when raw exists).<br/>
        • Formula: <code>pause_ratio_pct = (pause_seconds / duration_seconds) × 100</code>.<br/>
        • Example: if <code>duration_seconds=23</code> and <code>pause_seconds=7</code>, then <code>pause_ratio_pct=30.43%</code>; active editing time is <code>23-7=16s</code>.<br/><br/>
        <strong>Recommended usage examples</strong><br/>
        • <strong>Revert Timeline</strong> — Dataset: <code>cursor_timeline</code><br/>
        &nbsp;&nbsp;Row = <code>window_index</code>, Column = (none), Value = <code>backward_chars</code>, Agg = <code>sum</code><br/>
        &nbsp;&nbsp;Goal: visualize where revert-heavy windows appear.<br/>
        • <strong>Time Edit Intensity</strong> — Dataset: <code>time_trend</code><br/>
        &nbsp;&nbsp;Row = <code>minute_in_session</code>, Column = (none), Value = <code>edit_intensity</code>, Agg = <code>avg</code><br/>
        &nbsp;&nbsp;Goal: inspect temporal editing workload profile.<br/>
        • <strong>Event Distribution</strong> — Dataset: <code>replay_events</code><br/>
        &nbsp;&nbsp;Row = <code>event_type</code>, Column = <code>tag</code>, Value = <code>time_ms</code>, Agg = <code>count</code><br/>
        &nbsp;&nbsp;Goal: compare distribution of event categories.<br/>
        • <strong>Heat by Action</strong> — Dataset: <code>mt_heat_map</code> or <code>pe_heat_map</code><br/>
        &nbsp;&nbsp;Row = <code>action</code>, Column = (none), Value = <code>value</code>, Agg = <code>sum</code><br/>
        &nbsp;&nbsp;Goal: compare total action contribution to heat values.<br/>
        • <strong>Segment Type Counts</strong> — Dataset: <code>segment_meta</code><br/>
        &nbsp;&nbsp;Row = <code>type</code>, Column = (none), Value = <code>id</code>, Agg = <code>count</code><br/>
        &nbsp;&nbsp;Goal: compare distribution of edit operation types.<br/><br/>
        • <strong>Scatter: Pause vs Revert</strong> — Dataset: <code>cursor_timeline</code><br/>
        &nbsp;&nbsp;Row = <code>pause_seconds</code>, Column = (none), Value = <code>backward_chars</code>, Agg = <code>avg</code>, Chart = <code>scatter</code><br/>
        &nbsp;&nbsp;Goal: inspect whether higher pauses correlate with stronger reverts.<br/>
        • <strong>Scatter: Span vs Duration</strong> — Dataset: <code>segment_detail_vicinity</code><br/>
        &nbsp;&nbsp;Row = <code>span_len</code>, Column = (none), Value = <code>duration_ms</code>, Agg = <code>avg</code>, Chart = <code>scatter</code><br/>
        &nbsp;&nbsp;Goal: inspect relation between segment span size and editing duration.<br/>
        • <strong>Forward Timeline</strong> — Dataset: <code>cursor_timeline</code><br/>
        &nbsp;&nbsp;Row = <code>window_index</code>, Value = <code>forward_chars</code>, Agg = <code>sum</code>.<br/>
        &nbsp;&nbsp;Goal: identify windows with strongest forward drafting progress.<br/>
        • <strong>Pause Timeline</strong> — Dataset: <code>cursor_timeline</code><br/>
        &nbsp;&nbsp;Row = <code>window_index</code>, Value = <code>pause_seconds</code>, Agg = <code>sum</code>.<br/>
        &nbsp;&nbsp;Goal: locate concentration of hesitation and planning time.<br/>
        • <strong>Focus Loss Timeline</strong> — Dataset: <code>cursor_timeline</code><br/>
        &nbsp;&nbsp;Row = <code>window_index</code>, Value = <code>focus_loss_count</code>, Agg = <code>sum</code>.<br/>
        &nbsp;&nbsp;Goal: locate likely distraction-heavy windows.<br/>
        • <strong>Segment Duration by Type</strong> — Dataset: <code>segment_meta</code><br/>
        &nbsp;&nbsp;Row = <code>type</code>, Value = <code>duration_ms</code>, Agg = <code>avg</code>.<br/>
        &nbsp;&nbsp;Goal: compare which edit operation types are slower on average.<br/>
        • <strong>Segment Pause Ratio</strong> — Dataset: <code>segment_detail_vicinity</code><br/>
        &nbsp;&nbsp;Row = <code>segment_id</code>, Value = <code>pause_ratio_pct</code>, Agg = <code>avg</code>, Chart = <code>bar</code>.<br/>
        &nbsp;&nbsp;Goal: compare pause burden per segment. Formula: <code>pause_ratio_pct = (pause_seconds / duration_seconds) × 100</code>.<br/>
        &nbsp;&nbsp;Example: duration=23s, pause=7s → <code>pause_ratio_pct = (7/23)×100 = 30.43%</code>.<br/>
        • <strong>Adaptive Duration Compare</strong> — Dataset: <code>segment_detail_vicinity</code><br/>
        &nbsp;&nbsp;Row = <code>segment_type</code>, Column = <code>vicinity_adaptive</code>, Value = <code>duration_ms</code>, Agg = <code>avg</code>.<br/>
        &nbsp;&nbsp;Goal: quantify sensitivity of duration estimates to adaptive vicinity mode.<br/>
        • <strong>MT vs PE Action Heat</strong> — Datasets: <code>mt_heat_map</code> + <code>pe_heat_map</code><br/>
        &nbsp;&nbsp;Row = <code>action</code>, Column = <code>dataset</code>, Value = <code>value</code>, Agg = <code>sum</code>.<br/>
        &nbsp;&nbsp;Goal: compare action concentration patterns between MT and PE text spaces.<br/>
        • <strong>Scatter: Pause vs Focus Loss</strong> — Dataset: <code>cursor_timeline</code><br/>
        &nbsp;&nbsp;Row = <code>pause_seconds</code>, Value = <code>focus_loss_count</code>, Agg = <code>avg</code>, Chart = <code>scatter</code>.<br/>
        &nbsp;&nbsp;Goal: inspect whether longer pauses co-occur with attention switches.<br/>
        • <strong>Scatter: Time vs Cursor</strong> — Dataset: <code>replay_events</code><br/>
        &nbsp;&nbsp;Row = <code>time_ms</code>, Value = <code>cursor</code>, Agg = <code>avg</code>, Chart = <code>scatter</code>.<br/>
        &nbsp;&nbsp;Goal: inspect trajectory shape of cursor growth over session time.<br/>
        • <strong>Progress Time Curve</strong> — Dataset: <code>paragraph_markers</code><br/>
        &nbsp;&nbsp;Row = <code>paragraph_index</code>, Value = <code>elapsed_sec</code>, Agg = <code>max</code>.<br/>
        &nbsp;&nbsp;Goal: inspect pace differences between early and late document progression.<br/>
        • <strong>Scatter: Char X vs Y</strong> — Dataset: <code>target_char_map</code><br/>
        &nbsp;&nbsp;Row = <code>x</code>, Value = <code>y</code>, Agg = <code>avg</code>, Chart = <code>scatter</code>.<br/>
        &nbsp;&nbsp;Goal: inspect spatial layout structure and wrapping geometry of target text.<br/>
        <strong>Adaptive vicinity reactivity</strong><br/>
        The dataset <code>segment_detail_vicinity</code> is reactive to <strong>Vicinity Before</strong>, <strong>Vicinity After</strong>, and <strong>Adaptive</strong> toggle in the Full Compiled Text panel.<br/>
        When these settings change, pivot fields/results/charts are refreshed automatically.
        </div>
      </details>
      <div id="pivotTableWrap" class="replay-view">
        <div id="pivotTableInner" class="raw-xml">(Build Pivot Table to view result)</div>
      </div>
      <div id="pivotPlotWrap" class="trend-plot-wrap">
        <div id="pivotPlot" class="trend-plot"></div>
      </div>
    </div>

    <div class="panel">
      <h2>Full Compiled Text with Colored Change Tags (MT → PE)</h2>
      <ul class="legend-list">
        <li><span class="txt-origin">Original MT text that remains.</span></li>
        <li><span class="txt-origin-deleted">Original MT text later removed.</span></li>
        <li><span class="txt-inserted-alive">Inserted text that survives in PE.</span></li>
        <li><span class="txt-transient-deleted">Purple transient text</span> Inserted and deleted during drafting.</li>
      </ul>
      <label class="inline-control">
        Vicinity Before (chars)
        <input id="vicinityBefore" type="range" min="5" max="120" step="5" value="30"/>
        <span id="vicinityBeforeValue">30</span>
      </label>
      <label class="inline-control">
        Vicinity After (chars)
        <input id="vicinityAfter" type="range" min="5" max="120" step="5" value="30"/>
        <span id="vicinityAfterValue">30</span>
        <span style="display:inline-flex;align-items:center;gap:4px;margin-left:8px;">
          <input id="vicinityAdaptive" type="checkbox"/>
          Adaptive
        </span>
      </label>
      <div class="vicinity-help">
        Vicinity Before/After define how much context is included on each side of the selected changed section when estimating duration.<br/>
        Example: section = cursor 520–532, before=15, after=35 → events from 505 to 567 are considered.<br/>
        Adaptive mode keeps your current slider values as preferred caps, then clamps them to nearest neighboring changes (start→previous change, end→next change) to avoid crossing unrelated edits.
      </div>
      <label class="inline-control">
        Show purple transient text inline
        <input id="showTransientInline" type="checkbox" checked/>
      </label>
      <label class="inline-control">
        Show cursor movement timeline
        <input id="showCursorMovementTimeline" type="checkbox"/>
      </label>
      <label class="inline-control">
        Compare mode
        <input id="cursorCompareMode" type="checkbox"/>
      </label>
      <button class="button" onclick="clearCursorComparePoints()">Clear Compare Points</button>
      <label class="inline-control">
        Cursor timeline window (sec)
        <input id="cursorTimelineWindowSec" type="range" min="2" max="60" step="1" value="10"/>
        <span id="cursorTimelineWindowSecValue">10</span>
      </label>
      <label class="inline-control">
        Cursor graph height
        <input id="cursorGraphHeight" type="range" min="28" max="180" step="2" value="64"/>
        <span id="cursorGraphHeightValue">64</span>
      </label>
      <button id="normalizeCursorYAxisBtn" class="button" onclick="toggleCursorYAxisNormalization()">Normalize Cursor Y Axis: Off</button>
      <div class="cursor-range-row">
        <span>View range</span>
        <input id="cursorRangeStart" type="range" min="0" max="95" step="1" value="0"/>
        <span id="cursorRangeStartValue">0%</span>
        <span>to</span>
        <input id="cursorRangeEnd" type="range" min="5" max="100" step="1" value="100"/>
        <span id="cursorRangeEndValue">100%</span>
      </div>
      <label class="inline-control">
        Use fixed time-window range
        <input id="cursorFixedWindowMode" type="checkbox"/>
      </label>
      <label class="inline-control">
        Fixed view window (sec)
        <input id="cursorViewWindowSec" type="range" min="10" max="600" step="5" value="100"/>
        <span id="cursorViewWindowSecValue">100</span>
      </label>
      <label class="inline-control">
        Move window start (sec)
        <input id="cursorViewWindowStartSec" type="range" min="0" max="0" step="1" value="0"/>
        <span id="cursorViewWindowStartSecValue">0</span>
      </label>
      <label class="inline-control">
        Export selected range only
        <input id="cursorExportSelectedOnly" type="checkbox" checked/>
      </label>
      <label class="inline-control">
        Pause long-gap threshold (ms)
        <input id="cursorPauseGapThresholdMs" type="range" min="500" max="12000" step="100" value="2500"/>
        <span id="cursorPauseGapThresholdMsValue">2500</span>
      </label>
      <button class="button" onclick="exportCursorMovementPNG()">Export Cursor Movement PNG</button>
      <button class="button" onclick="exportCursorMovementCSV()">Export Cursor Movement CSV</button>
      <div class="vicinity-help">
        Cursor movement timeline summarizes the editor path by time windows. Each window shows forward movement, backward movement, pauses and focus-loss events.<br/>
        Example: from <code>Time=1203, Cursor=0</code> to <code>Time=117687, Cursor=48</code>, net movement is forward from 0 to 48 over that elapsed period.<br/>
        Cursor-path movement ignores IME events because IME cursor entries are often noisy in Translog logs.
      </div>
      <div id="cursorMovementWrap" class="cursor-movement-wrap hidden">
        <div class="cursor-movement-title">Cursor Movement Timeline (time-based, compact view)</div>
        <div class="cursor-movement-legend">
          <span><i style="background:#4f8f75;"></i>Forward</span>
          <span><i style="background:#be7a62;"></i>Backward</span>
          <span><i style="background:#8c7eb0;"></i>Pause</span>
          <span><i style="background:#687f91;"></i>Focus Loss</span>
          <span><i style="background:#486f9f;"></i>Cursor Path</span>
        </div>
        <div id="cursorMovementBody"></div>
      </div>
      <div class="compiled-text">{compiled}</div>
      <div id="transientPanel" class="transient-panel hidden">
        <strong>Purple transient text (grouped when hidden above)</strong>
        <div id="transientPanelItems" class="transient-items"></div>
      </div>
    </div>
    <div class="panel">
      <h2>Help: Parameter and Metric Definitions</h2>
      <div class="help-grid">
        <div class="help-card"><strong>Session Time</strong><br/>Project end time − project start time; fallback: last event time − first event time.</div>
        <div class="help-card"><strong>MT → PE Change Rate</strong><br/>Levenshtein distance(TargetTextUTF8, FinalTextUTF8) ÷ length(TargetTextUTF8) × 100.</div>
        <div class="help-card"><strong>Corrections</strong><br/>Count of Key events with Type in {{delete, edit}}.</div>
        <div class="help-card"><strong>Initial Delay</strong><br/>Time from session start to first meaningful Key action: insert/delete/edit/IME input.</div>
        <div class="help-card"><strong>Reading Speed Score</strong><br/>From first valid cursor section to current section, speed is based on elapsed time to reach that section; IME ignored; revisited ranges are penalized by user setting.</div>
        <div class="help-card"><strong>Edit Intensity</strong><br/>Selected editing actions per time window.</div>
        <div class="help-card"><strong>Heat Overlay</strong><br/>Reading heat projects movement-based reading score to section ranges on selected text (MT or PE); editing heat uses selected editing actions only.</div>
        <div class="help-card"><strong>Changed Section Hover</strong><br/>Displays estimated edit time range from matched nearby edit events and raw XML snippets.</div>
      </div>
    </div>
    <div class="copyright">Report Generator Author: Jiajun Wu</div>
    <div class="copyright">Email: jiajun.aiden.wu@outlook.com</div>
  </div>
  <div id="changePopup" class="change-popup hidden">
    <button id="changePopupClose" class="popup-close" type="button">×</button>
    <div class="change-popup-head">
      <h4>Change Segment Detail</h4>
      <div class="change-popup-actions">
        <button class="popup-mini-btn" type="button" onclick="exportChangePopupPNG()">Export PNG</button>
      </div>
    </div>
    <div id="popupMeta">Hover on a changed section to inspect its editing time and XML traces.</div>
    <div id="popupVicinity" class="vicinity-preview"></div>
    <div id="popupSpark" class="sparkline"></div>
    <div id="popupRaw" class="raw-xml"></div>
  </div>
  <div id="heatPointPopup" class="heat-point-popup hidden">
    <div id="heatPointMeta">Click a heat-text character to inspect its trend position.</div>
  </div>
  <div id="cursorPointPopup" class="cursor-point-popup hidden">
    <div id="cursorPointMeta">Click a cursor movement point to inspect details.</div>
  </div>
  <script>
    const trendData = {json.dumps(trend_payload, ensure_ascii=False)};
    const segmentMeta = {json.dumps(segment_meta, ensure_ascii=False)};
    const segmentMetaMap = Object.fromEntries(segmentMeta.map((s) => [s.id, s]));
    const activityEvents = {json.dumps(activity_events, ensure_ascii=False)};
    const mtHeatData = {json.dumps(mt_heat_payload, ensure_ascii=False)};
    const peHeatData = {json.dumps(pe_heat_payload, ensure_ascii=False)};
    const cursorTimeMap = {json.dumps(cursor_time_map, ensure_ascii=False)};
    const sourceSentences = {json.dumps(source_sentences, ensure_ascii=False)};
    const replayEvents = {json.dumps(replay_events, ensure_ascii=False)};
    const reportMetrics = {json.dumps(metrics_payload, ensure_ascii=False)};
    const reportActionSummary = {json.dumps(action_summary, ensure_ascii=False)};
    const reportActionCatalog = {json.dumps(action_catalog, ensure_ascii=False)};
    const reportParagraphMarkers = {json.dumps(paragraph_markers, ensure_ascii=False)};
    const reportSourceText = {json.dumps(data["source_text"], ensure_ascii=False)};
    const reportTargetText = {json.dumps(data["target_text"], ensure_ascii=False)};
    const reportFinalText = {json.dumps(data["final_text"], ensure_ascii=False)};
    const reportTargetChars = {json.dumps(target_chars_payload, ensure_ascii=False)};
    const reportSourceChars = {json.dumps(source_chars_payload, ensure_ascii=False)};
    const reportFinalChars = {json.dumps(final_chars_payload, ensure_ascii=False)};
    const orderedSegments = [...segmentMeta].sort((a, b) => Number(a.anchor_cursor || 0) - Number(b.anchor_cursor || 0));
    const avgSegmentDuration = (() => {{
      const values = segmentMeta
        .filter((s) => s.type !== "transient")
        .map((s) => s.duration_ms || 0)
        .filter((v) => v > 0);
      if (!values.length) return 1;
      return values.reduce((a, b) => a + b, 0) / values.length;
    }})();
    let currentHeatMode = "reading";
    let currentHeatTextMode = "mt";
    let normalizeReadingScale = true;
    let normalizePositionReadingScale = true;
    let normalizeHeatScale = true;
    let trendProbeMinute = null;
    let trendProbePosition = null;
    let currentCursorTimelineModel = null;
    let cursorComparePoints = [];
    let normalizeCursorYAxis = false;
    let pivotLastExport = null;

    function getCheckedValues(cls) {{
      return Array.from(document.querySelectorAll(`.${{cls}}:checked`)).map((node) => node.value);
    }}

    function aggregateByActions(mapData, actions, length) {{
      const arr = Array(length).fill(0);
      for (const action of actions) {{
        const source = mapData[action] || [];
        for (let i = 0; i < length; i += 1) {{
          arr[i] += source[i] || 0;
        }}
      }}
      return arr;
    }}

    function robustNormalize(values) {{
      const nonZero = values.filter((v) => v > 0).sort((a, b) => a - b);
      if (!nonZero.length) return values.map(() => 0);
      const p85 = nonZero[Math.floor(nonZero.length * 0.85)] || nonZero[nonZero.length - 1];
      const cap = Math.max(1, p85);
      return values.map((v) => {{
        if (v <= 0) return 0.03;
        const clipped = Math.min(v, cap) / cap;
        const gamma = Math.pow(clipped, 0.58);
        return Math.min(1, 0.05 + gamma * 0.95);
      }});
    }}

    function toObjectRows(headers, rows) {{
      return rows.map((row) => {{
        const obj = {{}};
        headers.forEach((h, i) => {{
          obj[h] = row[i] ?? "";
        }});
        return obj;
      }});
    }}

    function computeSegmentPauseStats(seg, vicinityBefore, vicinityAfter) {{
      const pauseThresholdMs = getPauseGapThresholdMs();
      const rawTimes = (seg.raw_xml || [])
        .map((line) => {{
          const m = String(line).match(/Time="(\\d+)"/);
          return m ? Number(m[1]) : Number.NaN;
        }})
        .filter((t) => Number.isFinite(t))
        .sort((a, b) => a - b);
      if (rawTimes.length >= 1) {{
        let pauseMs = 0;
        for (let i = 1; i < rawTimes.length; i += 1) {{
          const gap = rawTimes[i] - rawTimes[i - 1];
          if (gap >= pauseThresholdMs) pauseMs += gap;
        }}
        const startMs = rawTimes[0];
        const endMs = rawTimes[rawTimes.length - 1];
        const durationMs = Math.max(1, endMs - startMs);
        const durationSec = Math.max(0.001, durationMs / 1000);
        const pauseSec = Math.max(0, pauseMs / 1000);
        const activeSec = Math.max(0, durationSec - pauseSec);
        return {{
          start_ms: startMs,
          end_ms: endMs,
          duration_ms: durationMs,
          duration_seconds: Number(durationSec.toFixed(3)),
          pause_seconds: Number(pauseSec.toFixed(3)),
          active_edit_seconds: Number(activeSec.toFixed(3)),
          pause_ratio: Number((pauseSec / durationSec).toFixed(6)),
          pause_ratio_pct: Number(((pauseSec / durationSec) * 100).toFixed(3)),
          pause_to_active_ratio: Number((pauseSec / Math.max(0.001, activeSec)).toFixed(6)),
          events_in_range: rawTimes.length,
          pause_threshold_ms: pauseThresholdMs,
          pause_source: "segment_raw_xml",
        }};
      }}
      const d = computeDurationWithVicinity(seg, vicinityBefore, vicinityAfter);
      const anchor = Number(seg.anchor_cursor || 0);
      const span = Math.max(1, Number(seg.span_len || 1));
      const leftBound = anchor - vicinityBefore;
      const rightBound = anchor + span + vicinityAfter;
      const rows = activityEvents
        .filter((e) => e.cursor >= 0 && e.cursor >= leftBound && e.cursor <= rightBound && e.time_ms >= d.start_ms && e.time_ms <= d.end_ms)
        .sort((a, b) => a.time_ms - b.time_ms);
      let pauseMs = 0;
      for (let i = 1; i < rows.length; i += 1) {{
        const gap = rows[i].time_ms - rows[i - 1].time_ms;
        if (gap >= pauseThresholdMs) pauseMs += gap;
      }}
      const durationSec = Math.max(0.001, Number(d.duration_ms || 0) / 1000);
      const pauseSec = Math.max(0, pauseMs / 1000);
      const activeSec = Math.max(0, durationSec - pauseSec);
      return {{
        ...d,
        duration_seconds: Number(durationSec.toFixed(3)),
        pause_seconds: Number(pauseSec.toFixed(3)),
        active_edit_seconds: Number(activeSec.toFixed(3)),
        pause_ratio: Number((pauseSec / durationSec).toFixed(6)),
        pause_ratio_pct: Number(((pauseSec / durationSec) * 100).toFixed(3)),
        pause_to_active_ratio: Number((pauseSec / Math.max(0.001, activeSec)).toFixed(6)),
        events_in_range: rows.length,
        pause_threshold_ms: pauseThresholdMs,
        pause_source: "vicinity_activity"
      }};
    }}

    function buildSegmentDetailVicinityRows() {{
      const totalChars = Math.max(1, document.querySelectorAll("#mtHeatPanel .heat-char").length || 1);
      const rows = [];
      (segmentMeta || []).forEach((seg) => {{
        const vicinity = getVicinityRange(seg, totalChars);
        const d = computeSegmentPauseStats(seg, vicinity.before, vicinity.after);
        const durationSec = Math.max(0.001, Number(d.duration_ms || 0) / 1000);
        const netChars = Number(seg.span_len || 0);
        rows.push({{
          segment_id: seg.id || "",
          segment_type: seg.type || "",
          anchor_cursor: Number(seg.anchor_cursor || 0),
          segment_end_cursor: Number(seg.anchor_cursor || 0) + Number(seg.span_len || 0),
          span_len: Number(seg.span_len || 0),
          mt_len: String(seg.mt_text || "").length,
          pe_len: String(seg.pe_text || "").length,
          segment_mt_text: String(seg.mt_text || ""),
          segment_pe_text: String(seg.pe_text || ""),
          segment_raw_xml: (seg.raw_xml || []).join("\\n"),
          start_ms: Number(d.start_ms || 0),
          end_ms: Number(d.end_ms || 0),
          duration_ms: Number(d.duration_ms || 0),
          duration_seconds: Number(durationSec.toFixed(3)),
          pause_seconds: Number(d.pause_seconds || 0),
          active_edit_seconds: Number(d.active_edit_seconds || 0),
          pause_ratio: Number(d.pause_ratio || 0),
          pause_ratio_pct: Number(d.pause_ratio_pct || 0),
          pause_to_active_ratio: Number(d.pause_to_active_ratio || 0),
          pause_per_100_chars: Number(((Number(d.pause_seconds || 0) / Math.max(1, netChars)) * 100).toFixed(4)),
          events_in_range: Number(d.events_in_range || 0),
          pause_threshold_ms: Number(d.pause_threshold_ms || getPauseGapThresholdMs()),
          pause_source: String(d.pause_source || ""),
          chars_per_sec: Number((netChars / durationSec).toFixed(4)),
          vicinity_before: Number(vicinity.before || 0),
          vicinity_after: Number(vicinity.after || 0),
          vicinity_span_total: Number((vicinity.before || 0) + (seg.span_len || 0) + (vicinity.after || 0)),
          vicinity_adaptive: Boolean(document.getElementById("vicinityAdaptive")?.checked) ? 1 : 0,
        }});
      }});
      return rows;
    }}

    function buildDataExportDatasets() {{
      const editActions = getCheckedValues("edit-action");
      const bins = (trendData.x || []).length;
      const totalChars = Math.max(1, getActiveHeatNodes().length);
      const trendMovement = buildReadingMovementScores(bins, trendData.window_sec || {window_sec}, totalChars);
      const timeEditing = aggregateByActions(trendData.action_counts || {{}}, editActions, bins);
      const minutes = (trendData.window_sec || {window_sec}) / 60;
      const positionMovement = buildReadingMovementScores(
        bins,
        trendData.window_sec || {window_sec},
        totalChars,
        getPositionReadingGranularity(),
        getPositionRevisitPenaltyWeight()
      );
      const positionSeries = buildPositionSeries(positionMovement.readingHeat, editActions, totalChars, positionMovement.granularity);
      const cursorModel = buildCursorMovementTimeline(getCursorTimelineWindowSec());
      return {{
        session_metrics: toObjectRows(["metric", "value"], Object.entries(reportMetrics).map(([k, v]) => [k, v])),
        action_summary: toObjectRows(["action", "count"], Object.entries(reportActionSummary).map(([k, v]) => [k, v])),
        action_catalog: (reportActionCatalog || []).map((r) => ({{ action: r.label, count: r.count }})),
        time_trend: (trendData.x || []).map((x, i) => ({{
          minute_in_session: x,
          reading_speed_score: trendMovement.readingTrend[i] || 0,
          edit_intensity: Number(((timeEditing[i] || 0) / minutes).toFixed(3))
        }})),
        position_trend: (positionSeries.x || []).map((x, i) => ({{
          text_position_char_index: x,
          reading_speed_score: positionSeries.reading[i] || 0,
          edit_intensity: positionSeries.editing[i] || 0
        }})),
        cursor_timeline: (cursorModel.bins || []).map((b, i) => ({{
          window_index: i + 1,
          start_sec: b.startMs / 1000,
          end_sec: b.endMs / 1000,
          duration_seconds: Math.max(0.001, (b.endMs - b.startMs) / 1000),
          start_cursor: b.startCursor,
          end_cursor: b.endCursor,
          net_cursor_delta: (b.endCursor || 0) - (b.startCursor || 0),
          forward_chars: b.forward,
          backward_chars: b.backward,
          forward_rate_cps: Number(((b.forward || 0) / Math.max(0.001, (b.endMs - b.startMs) / 1000)).toFixed(4)),
          backward_rate_cps: Number(((b.backward || 0) / Math.max(0.001, (b.endMs - b.startMs) / 1000)).toFixed(4)),
          movement_total_chars: Math.max(0, (b.forward || 0) + (b.backward || 0)),
          backward_ratio_pct: Number((((b.backward || 0) / Math.max(1, (b.forward || 0) + (b.backward || 0))) * 100).toFixed(3)),
          pause_seconds: b.pauseSec,
          pause_threshold_ms: getPauseGapThresholdMs(),
          pause_ratio_pct: Number((((b.pauseSec || 0) / Math.max(0.001, (b.endMs - b.startMs) / 1000)) * 100).toFixed(3)),
          focus_loss_count: b.blurCount
        }})),
        segment_meta: (segmentMeta || []).map((s) => ({{
          id: s.id || "",
          type: s.type || "",
          anchor_cursor: s.anchor_cursor || 0,
          span_len: s.span_len || 0,
          start_ms: s.start_ms || 0,
          end_ms: s.end_ms || 0,
          duration_ms: s.duration_ms || 0,
          duration_sec: Number((Number(s.duration_ms || 0) / 1000).toFixed(3)),
          mt_len: String(s.mt_text || "").length,
          pe_len: String(s.pe_text || "").length,
          text_delta_chars: String(s.pe_text || "").length - String(s.mt_text || "").length,
          abs_text_delta_chars: Math.abs(String(s.pe_text || "").length - String(s.mt_text || "").length),
          chars_per_sec: Number((Math.max(1, Number(s.span_len || 0)) / Math.max(0.001, Number(s.duration_ms || 1) / 1000)).toFixed(4)),
          mt_text: s.mt_text || "",
          pe_text: s.pe_text || ""
        }})),
        segment_detail_vicinity: buildSegmentDetailVicinityRows(),
        replay_events: (replayEvents || []).map((e) => ({{
          time_ms: e.time_ms,
          tag: e.tag,
          event_type: e.event_type,
          cursor: e.cursor,
          block: e.block || 0,
          value: e.value || "",
          text: e.text || "",
          label: e.label || ""
        }})),
        activity_events: (activityEvents || []).map((e) => ({{
          time_ms: e.time_ms,
          cursor: e.cursor,
          label: e.label || ""
        }})),
        cursor_time_map: (cursorTimeMap || []).map((t, i) => ({{ cursor: i, first_time_ms: t }})),
        paragraph_markers: (reportParagraphMarkers || []).map((p) => ({{
          label: p.label,
          paragraph_index: p.paragraph_index ?? 0,
          cursor: p.cursor,
          time_ms: p.time_ms,
          elapsed_sec: p.elapsed_sec ?? Number((Number(p.time_ms || 0) / 1000).toFixed(4)),
          minute: p.minute,
          progress_pct: p.progress_pct ?? 0
        }})),
        mt_heat_map: (() => {{
          const rows = [];
          Object.entries(mtHeatData || {{}}).forEach(([action, arr]) => (arr || []).forEach((v, i) => rows.push({{ action, cursor_index: i, value: v }})));
          return rows;
        }})(),
        pe_heat_map: (() => {{
          const rows = [];
          Object.entries(peHeatData || {{}}).forEach(([action, arr]) => (arr || []).forEach((v, i) => rows.push({{ action, cursor_index: i, value: v }})));
          return rows;
        }})(),
        target_char_map: (reportTargetChars || []).map((c) => ({{ cursor: c.cursor, value: c.value, x: c.x, y: c.y, width: c.width, height: c.height }})),
        source_char_map: (reportSourceChars || []).map((c) => ({{ cursor: c.cursor, value: c.value, x: c.x, y: c.y, width: c.width, height: c.height }})),
        final_char_map: (reportFinalChars || []).map((c) => ({{ cursor: c.cursor, value: c.value, x: c.x, y: c.y, width: c.width, height: c.height }})),
        text_payloads: [
          {{ field: "source_text", text: reportSourceText || "" }},
          {{ field: "target_text", text: reportTargetText || "" }},
          {{ field: "final_text", text: reportFinalText || "" }},
        ]
      }};
    }}

    function collectSelectedPivotRows() {{
      const selected = Array.from(document.querySelectorAll(".data-export-check:checked")).map((n) => String(n.value));
      const datasets = buildDataExportDatasets();
      const rows = [];
      selected.forEach((key) => {{
        (datasets[key] || []).forEach((r) => rows.push({{ dataset: key, ...r }}));
      }});
      return rows;
    }}

    function refreshPivotFieldOptions() {{
      const rows = collectSelectedPivotRows();
      const keys = Array.from(new Set(rows.flatMap((r) => Object.keys(r)))).sort();
      const rowSel = document.getElementById("pivotRowField");
      const colSel = document.getElementById("pivotColField");
      const valSel = document.getElementById("pivotValField");
      if (!rowSel || !colSel || !valSel) return;
      const buildOptions = (arr, includeNone = false) => {{
        const opts = [];
        if (includeNone) opts.push(`<option value="">(none)</option>`);
        arr.forEach((k) => opts.push(`<option value="${{k}}">${{k}}</option>`));
        return opts.join("");
      }};
      rowSel.innerHTML = buildOptions(keys, false);
      colSel.innerHTML = buildOptions(keys, true);
      valSel.innerHTML = buildOptions(keys.filter((k) => k !== "dataset"), false);
      if (!rowSel.value && keys.length) rowSel.value = keys.includes("dataset") ? "dataset" : keys[0];
      if (!valSel.value && keys.length) valSel.value = keys.includes("duration_ms") ? "duration_ms" : keys[0];
    }}

    function pivotAggregate(values, agg) {{
      const nums = values.map((v) => Number(v)).filter((v) => Number.isFinite(v));
      if (agg === "count") return values.length;
      if (!nums.length) return 0;
      if (agg === "sum") return nums.reduce((a, b) => a + b, 0);
      if (agg === "avg") return nums.reduce((a, b) => a + b, 0) / nums.length;
      if (agg === "min") return Math.min(...nums);
      if (agg === "max") return Math.max(...nums);
      return values.length;
    }}

    function buildPivotResult() {{
      const rows = collectSelectedPivotRows();
      const rowField = document.getElementById("pivotRowField")?.value || "dataset";
      const colField = document.getElementById("pivotColField")?.value || "";
      const valField = document.getElementById("pivotValField")?.value || rowField;
      const agg = document.getElementById("pivotAgg")?.value || "count";
      const map = new Map();
      rows.forEach((r) => {{
        const rk = String(r[rowField] ?? "(blank)");
        const ck = colField ? String(r[colField] ?? "(blank)") : "(all)";
        const k = `${{rk}}|||${{ck}}`;
        if (!map.has(k)) map.set(k, []);
        map.get(k).push(r[valField]);
      }});
      const sortMixed = (a, b) => {{
        const na = Number(a);
        const nb = Number(b);
        const aNum = Number.isFinite(na);
        const bNum = Number.isFinite(nb);
        if (aNum && bNum) return na - nb;
        return String(a).localeCompare(String(b), undefined, {{ numeric: true }});
      }};
      const rowKeys = Array.from(new Set(Array.from(map.keys()).map((k) => k.split("|||")[0]))).sort(sortMixed);
      const colKeys = Array.from(new Set(Array.from(map.keys()).map((k) => k.split("|||")[1]))).sort(sortMixed);
      const headers = [rowField, ...colKeys];
      const bodyRows = rowKeys.map((rk) => {{
        const row = [rk];
        colKeys.forEach((ck) => {{
          const v = pivotAggregate(map.get(`${{rk}}|||${{ck}}`) || [], agg);
          row.push(Number.isFinite(v) ? Number(v.toFixed(4)) : v);
        }});
        return row;
      }});
      pivotLastExport = {{ headers, rows: bodyRows, rowField, colField }};
      const tableNode = document.getElementById("pivotTableInner");
      if (tableNode) {{
        const thead = `<tr>${{headers.map((h) => `<th>${{String(h).replace(/</g, "&lt;")}}</th>`).join("")}}</tr>`;
        const tbody = bodyRows.map((r) => `<tr>${{r.map((c) => `<td>${{String(c).replace(/</g, "&lt;")}}</td>`).join("")}}</tr>`).join("");
        tableNode.innerHTML = `<table class="summary-table"><thead>${{thead}}</thead><tbody>${{tbody}}</tbody></table>`;
      }}
      return {{ rowField, colField, headers, bodyRows }};
    }}

    function renderPivotChart() {{
      const res = buildPivotResult();
      const plot = document.getElementById("pivotPlot");
      if (!plot || !res) return;
      const chartType = document.getElementById("pivotChartType")?.value || "bar";
      const x = res.bodyRows.map((r) => r[0]);
      const cols = res.headers.slice(1);
      const traces = cols.map((c, i) => {{
        const y = res.bodyRows.map((r) => Number(r[i + 1] || 0));
        return {{
          x,
          y,
          name: c,
          mode: chartType === "bar" ? undefined : "lines+markers",
          type: chartType === "line" ? "scatter" : chartType === "scatter" ? "scatter" : "bar"
        }};
      }});
      Plotly.react("pivotPlot", traces, {{
        paper_bgcolor: "rgba(255,255,255,0)",
        plot_bgcolor: "rgba(255,255,255,0)",
        title: "Pivot Graph",
        xaxis: {{ title: res.rowField }},
        yaxis: {{ title: "Aggregated value" }},
        barmode: "group",
        margin: {{ l: 45, r: 22, t: 50, b: 70 }}
      }}, {{ responsive: true, displaylogo: false }});
    }}

    function exportPivotCSV() {{
      const res = buildPivotResult();
      if (!pivotLastExport || !pivotLastExport.rows?.length || !res) return;
      let headers = [...pivotLastExport.headers];
      let rows = pivotLastExport.rows.map((r) => [...r]);
      if (res.rowField === "segment_id") {{
        const segMap = new Map((segmentMeta || []).map((s) => [String(s.id || ""), s]));
        headers = [...headers, "segment_mt_text", "segment_pe_text", "segment_raw_xml"];
        rows = rows.map((r) => {{
          const seg = segMap.get(String(r[0] ?? ""));
          const mt = String(seg?.mt_text || "");
          const pe = String(seg?.pe_text || "");
          const raw = (seg?.raw_xml || []).join("\\n");
          return [...r, mt, pe, raw];
        }});
      }}
      downloadCSV("pivot_table_export.csv", headers, rows);
    }}

    function refreshPivotReactiveSegmentData() {{
      refreshPivotFieldOptions();
      if (!pivotLastExport) return;
      buildPivotResult();
      renderPivotChart();
    }}

    function setPivotSelectValue(id, value) {{
      const node = document.getElementById(id);
      if (!node || value == null) return;
      const options = Array.from(node.options || []).map((o) => o.value);
      if (options.includes(value)) node.value = value;
    }}

    function setDataExportSelection(keys) {{
      const set = new Set(keys || []);
      document.querySelectorAll(".data-export-check").forEach((n) => {{
        n.checked = set.has(String(n.value));
      }});
    }}

    function applyPivotPreset(name) {{
      const presets = {{
        cursor_revert: {{
          datasets: ["cursor_timeline"],
          row: "window_index",
          col: "",
          val: "backward_chars",
          agg: "sum",
          chart: "bar"
        }},
        time_edit: {{
          datasets: ["time_trend"],
          row: "minute_in_session",
          col: "",
          val: "edit_intensity",
          agg: "avg",
          chart: "bar"
        }},
        event_dist: {{
          datasets: ["replay_events"],
          row: "event_type",
          col: "tag",
          val: "time_ms",
          agg: "count",
          chart: "bar"
        }},
        heat_action_sum: {{
          datasets: ["mt_heat_map"],
          row: "action",
          col: "",
          val: "value",
          agg: "sum",
          chart: "bar"
        }},
        segment_type_count: {{
          datasets: ["segment_meta"],
          row: "type",
          col: "",
          val: "id",
          agg: "count",
          chart: "bar"
        }},
        scatter_pause_vs_revert: {{
          datasets: ["cursor_timeline"],
          row: "pause_seconds",
          col: "",
          val: "backward_chars",
          agg: "avg",
          chart: "scatter"
        }},
        scatter_span_vs_duration: {{
          datasets: ["segment_detail_vicinity"],
          row: "span_len",
          col: "",
          val: "duration_ms",
          agg: "avg",
          chart: "scatter"
        }},
        forward_timeline: {{
          datasets: ["cursor_timeline"],
          row: "window_index",
          col: "",
          val: "forward_chars",
          agg: "sum",
          chart: "bar"
        }},
        pause_timeline: {{
          datasets: ["cursor_timeline"],
          row: "window_index",
          col: "",
          val: "pause_seconds",
          agg: "sum",
          chart: "bar"
        }},
        focus_loss_timeline: {{
          datasets: ["cursor_timeline"],
          row: "window_index",
          col: "",
          val: "focus_loss_count",
          agg: "sum",
          chart: "bar"
        }},
        segment_duration_by_type: {{
          datasets: ["segment_meta"],
          row: "type",
          col: "",
          val: "duration_ms",
          agg: "avg",
          chart: "bar"
        }},
        segment_pause_ratio: {{
          datasets: ["segment_detail_vicinity"],
          row: "segment_id",
          col: "",
          val: "pause_ratio_pct",
          agg: "avg",
          chart: "bar"
        }},
        adaptive_duration_compare: {{
          datasets: ["segment_detail_vicinity"],
          row: "segment_type",
          col: "vicinity_adaptive",
          val: "duration_ms",
          agg: "avg",
          chart: "bar"
        }},
        mt_pe_action_compare: {{
          datasets: ["mt_heat_map", "pe_heat_map"],
          row: "action",
          col: "dataset",
          val: "value",
          agg: "sum",
          chart: "bar"
        }},
        scatter_pause_vs_focus: {{
          datasets: ["cursor_timeline"],
          row: "pause_seconds",
          col: "",
          val: "focus_loss_count",
          agg: "avg",
          chart: "scatter"
        }},
        scatter_time_vs_cursor: {{
          datasets: ["replay_events"],
          row: "time_ms",
          col: "",
          val: "cursor",
          agg: "avg",
          chart: "scatter"
        }},
        progress_time_curve: {{
          datasets: ["paragraph_markers"],
          row: "paragraph_index",
          col: "",
          val: "elapsed_sec",
          agg: "max",
          chart: "line"
        }},
        char_geometry_scatter: {{
          datasets: ["target_char_map"],
          row: "x",
          col: "",
          val: "y",
          agg: "avg",
          chart: "scatter"
        }},
      }};
      const p = presets[name];
      if (!p) return;
      setDataExportSelection(p.datasets);
      refreshPivotFieldOptions();
      setPivotSelectValue("pivotRowField", p.row);
      setPivotSelectValue("pivotColField", p.col);
      setPivotSelectValue("pivotValField", p.val);
      setPivotSelectValue("pivotAgg", p.agg);
      setPivotSelectValue("pivotChartType", p.chart);
      buildPivotResult();
      renderPivotChart();
    }}

    function toggleAllDataExport(flag) {{
      document.querySelectorAll(".data-export-check").forEach((n) => {{
        n.checked = Boolean(flag);
      }});
    }}

    function exportSelectedDataCSV() {{
      const selected = Array.from(document.querySelectorAll(".data-export-check:checked")).map((n) => String(n.value));
      if (!selected.length) return;
      if (selected.length > 8) {{
        alert("Please export at most 8 CSV files at once due to browser download restrictions.");
        return;
      }}
      const datasets = buildDataExportDatasets();
      selected.forEach((key) => {{
        const rowsObj = datasets[key] || [];
        if (!rowsObj.length) {{
          downloadCSV(`${{key}}.csv`, ["note"], [["no rows"]]);
          return;
        }}
        const headers = Array.from(new Set(rowsObj.flatMap((r) => Object.keys(r))));
        const rows = rowsObj.map((r) => headers.map((h) => r[h] ?? ""));
        downloadCSV(`${{key}}.csv`, headers, rows);
      }});
    }}

    function editingHeatColor(v) {{
      const hue = 20 - 12 * v;
      const sat = 45 + 30 * v;
      const light = 94 - 52 * v;
      const alpha = 0.08 + 0.9 * v;
      return `hsla(${{hue}}, ${{sat}}%, ${{light}}%, ${{alpha}})`;
    }}

    function readingHeatColor(v) {{
      const hue = 4 + 214 * v;
      const sat = 74;
      const light = 56 - 8 * v;
      const alpha = 0.2 + 0.78 * v;
      return `hsla(${{hue}}, ${{sat}}%, ${{light}}%, ${{alpha}})`;
    }}

    function updateNormalizeButtonLabel() {{
      const btn = document.getElementById("normalizeReadingBtn");
      if (btn) btn.textContent = `Normalize Reading Scale: ${{normalizeReadingScale ? "On" : "Off"}}`;
    }}

    function toggleReadingNormalization() {{
      normalizeReadingScale = !normalizeReadingScale;
      updateNormalizeButtonLabel();
      redrawAll();
    }}

    function updatePositionNormalizeButtonLabel() {{
      const btn = document.getElementById("normalizePositionBtn");
      if (btn) btn.textContent = `Normalize Reading Scale: ${{normalizePositionReadingScale ? "On" : "Off"}}`;
    }}

    function togglePositionReadingNormalization() {{
      normalizePositionReadingScale = !normalizePositionReadingScale;
      updatePositionNormalizeButtonLabel();
      redrawAll();
    }}

    function clearTimeMarker() {{
      trendProbeMinute = null;
      redrawAll();
    }}

    function clearPositionMarker() {{
      trendProbePosition = null;
      redrawAll();
    }}

    function updateHeatNormalizeButtonLabel() {{
      const btn = document.getElementById("normalizeHeatBtn");
      if (!btn) return;
      btn.textContent = `Normalize Heat Scale: ${{normalizeHeatScale ? "On" : "Off"}}`;
      btn.classList.toggle("active", normalizeHeatScale);
    }}

    function toggleHeatNormalization() {{
      normalizeHeatScale = !normalizeHeatScale;
      updateHeatNormalizeButtonLabel();
      redrawAll();
    }}

    function getActiveHeatPanel() {{
      return currentHeatTextMode === "pe"
        ? document.getElementById("peHeatPanel")
        : document.getElementById("mtHeatPanel");
    }}

    function getActiveHeatNodes() {{
      return Array.from(getActiveHeatPanel()?.querySelectorAll(".heat-char") || []);
    }}

    function getActiveHeatData() {{
      return currentHeatTextMode === "pe" ? peHeatData : mtHeatData;
    }}

    function getSourceSentenceByCursor(cursor) {{
      if (!sourceSentences.length) return "";
      const activeLength = Math.max(1, getActiveHeatNodes().length);
      const c = Math.max(0, Math.min(activeLength - 1, Number(cursor || 0)));
      const totalLen = Math.max(1, sourceSentences.reduce((a, s) => a + String(s || "").length + 1, 0));
      const target = Math.floor((c / activeLength) * totalLen);
      let acc = 0;
      for (const sentence of sourceSentences) {{
        const s = String(sentence || "");
        const next = acc + s.length + 1;
        if (target < next) return s;
        acc = next;
      }}
      return String(sourceSentences[sourceSentences.length - 1] || "");
    }}

    function switchHeatTextMode(mode) {{
      currentHeatTextMode = mode;
      document.getElementById("heatTextModeMT")?.classList.toggle("active", mode === "mt");
      document.getElementById("heatTextModePE")?.classList.toggle("active", mode === "pe");
      document.getElementById("mtHeatPanel")?.classList.toggle("hidden", mode !== "mt");
      document.getElementById("peHeatPanel")?.classList.toggle("hidden", mode !== "pe");
      redrawAll();
      bindHeatClickHandlers();
    }}

    function getReadingGranularity() {{
      const node = document.getElementById("readingGranularity");
      return Math.max(1, Number(node?.value || 25));
    }}

    function getRevisitPenaltyWeight() {{
      const node = document.getElementById("revisitPenaltyWeight");
      return Math.max(0, Number(node?.value || 1.5));
    }}

    function getPositionReadingGranularity() {{
      const node = document.getElementById("posReadingGranularity");
      return Math.max(1, Number(node?.value || 25));
    }}

    function getPositionRevisitPenaltyWeight() {{
      const node = document.getElementById("posRevisitPenaltyWeight");
      return Math.max(0, Number(node?.value || 1.5));
    }}

    function getHeatReadingGranularity() {{
      const node = document.getElementById("heatReadingGranularity");
      return Math.max(1, Number(node?.value || 25));
    }}

    function getHeatRevisitPenaltyWeight() {{
      const node = document.getElementById("heatRevisitPenaltyWeight");
      return Math.max(0, Number(node?.value || 1.5));
    }}

    function getPauseGapThresholdMs() {{
      const node = document.getElementById("pauseGapThresholdMs") || document.getElementById("cursorPauseGapThresholdMs");
      return Math.max(100, Number(node?.value || 2500));
    }}

    function setPauseGapThresholdMs(value) {{
      const v = Math.max(100, Number(value || 2500));
      const nodeA = document.getElementById("pauseGapThresholdMs");
      const nodeB = document.getElementById("cursorPauseGapThresholdMs");
      if (nodeA) nodeA.value = String(v);
      if (nodeB) nodeB.value = String(v);
      updatePauseGapThresholdLabel();
    }}

    function updateReadingSettingsLabels() {{
      const granNode = document.getElementById("readingGranularityValue");
      if (granNode) granNode.textContent = String(getReadingGranularity());
      const revisitNode = document.getElementById("revisitPenaltyWeightValue");
      if (revisitNode) revisitNode.textContent = getRevisitPenaltyWeight().toFixed(1);
    }}

    function updatePositionReadingSettingsLabels() {{
      const granNode = document.getElementById("posReadingGranularityValue");
      if (granNode) granNode.textContent = String(getPositionReadingGranularity());
      const revisitNode = document.getElementById("posRevisitPenaltyWeightValue");
      if (revisitNode) revisitNode.textContent = getPositionRevisitPenaltyWeight().toFixed(1);
    }}

    function updateHeatReadingSettingsLabels() {{
      const granNode = document.getElementById("heatReadingGranularityValue");
      if (granNode) granNode.textContent = String(getHeatReadingGranularity());
      const revisitNode = document.getElementById("heatRevisitPenaltyWeightValue");
      if (revisitNode) revisitNode.textContent = getHeatRevisitPenaltyWeight().toFixed(1);
    }}

    function updatePauseGapThresholdLabel() {{
      const v = String(Math.round(getPauseGapThresholdMs()));
      const nodeA = document.getElementById("pauseGapThresholdMsValue");
      const nodeB = document.getElementById("cursorPauseGapThresholdMsValue");
      if (nodeA) nodeA.textContent = v;
      if (nodeB) nodeB.textContent = v;
    }}

    function getValidCursorEvents() {{
      return activityEvents
        .filter((e) => e.cursor >= 0 && e.label !== "key:ime")
        .sort((a, b) => a.time_ms - b.time_ms);
    }}

    function getCursorTimelineWindowSec() {{
      const node = document.getElementById("cursorTimelineWindowSec");
      return Math.max(2, Number(node?.value || 10));
    }}

    function updateCursorTimelineWindowLabel() {{
      const node = document.getElementById("cursorTimelineWindowSecValue");
      if (node) node.textContent = String(getCursorTimelineWindowSec());
    }}

    function getCursorGraphHeight() {{
      const node = document.getElementById("cursorGraphHeight");
      return Math.max(28, Number(node?.value || 64));
    }}

    function updateCursorGraphHeightLabel() {{
      const node = document.getElementById("cursorGraphHeightValue");
      if (node) node.textContent = String(getCursorGraphHeight());
    }}

    function updateNormalizeCursorYAxisButtonLabel() {{
      const btn = document.getElementById("normalizeCursorYAxisBtn");
      if (!btn) return;
      btn.textContent = `Normalize Cursor Y Axis: ${{normalizeCursorYAxis ? "On" : "Off"}}`;
    }}

    function toggleCursorYAxisNormalization() {{
      normalizeCursorYAxis = !normalizeCursorYAxis;
      updateNormalizeCursorYAxisButtonLabel();
      renderCursorMovementTimeline();
    }}

    function getCursorRangeStartPct() {{
      const node = document.getElementById("cursorRangeStart");
      return Math.max(0, Math.min(95, Number(node?.value || 0)));
    }}

    function getCursorRangeEndPct() {{
      const node = document.getElementById("cursorRangeEnd");
      return Math.max(5, Math.min(100, Number(node?.value || 100)));
    }}

    function normalizeCursorRangeInputs() {{
      const startNode = document.getElementById("cursorRangeStart");
      const endNode = document.getElementById("cursorRangeEnd");
      if (!startNode || !endNode) return;
      let s = getCursorRangeStartPct();
      let e = getCursorRangeEndPct();
      if (e - s < 2) {{
        if (document.activeElement === startNode) s = Math.max(0, e - 2);
        else e = Math.min(100, s + 2);
      }}
      startNode.value = String(s);
      endNode.value = String(e);
    }}

    function updateCursorRangeLabels() {{
      normalizeCursorRangeInputs();
      const sNode = document.getElementById("cursorRangeStartValue");
      const eNode = document.getElementById("cursorRangeEndValue");
      if (sNode) sNode.textContent = `${{Math.round(getCursorRangeStartPct())}}%`;
      if (eNode) eNode.textContent = `${{Math.round(getCursorRangeEndPct())}}%`;
    }}

    function getCursorFixedWindowMode() {{
      return Boolean(document.getElementById("cursorFixedWindowMode")?.checked);
    }}

    function getCursorViewWindowSec() {{
      const node = document.getElementById("cursorViewWindowSec");
      return Math.max(5, Number(node?.value || 100));
    }}

    function getCursorViewWindowStartSec() {{
      const node = document.getElementById("cursorViewWindowStartSec");
      return Math.max(0, Number(node?.value || 0));
    }}

    function updateCursorViewWindowSecLabel() {{
      const node = document.getElementById("cursorViewWindowSecValue");
      if (node) node.textContent = String(getCursorViewWindowSec());
    }}

    function updateCursorViewWindowStartSecLabel() {{
      const node = document.getElementById("cursorViewWindowStartSecValue");
      if (node) node.textContent = String(getCursorViewWindowStartSec());
    }}

    function updateCursorViewWindowBounds(maxTimeSec) {{
      const startNode = document.getElementById("cursorViewWindowStartSec");
      if (!startNode) return;
      const windowSec = getCursorViewWindowSec();
      const maxStart = Math.max(0, Math.floor(maxTimeSec - windowSec));
      startNode.max = String(maxStart);
      if (getCursorViewWindowStartSec() > maxStart) {{
        startNode.value = String(maxStart);
      }}
      updateCursorViewWindowStartSecLabel();
    }}

    function getCursorExportSelectedOnly() {{
      return Boolean(document.getElementById("cursorExportSelectedOnly")?.checked);
    }}

    function getCursorVisibleIndexRange(bins, forceAllRange = false) {{
      if (!bins.length || forceAllRange) return {{ sIdx: 0, eIdx: Math.max(0, bins.length - 1) }};
      let sIdx = 0;
      let eIdx = bins.length - 1;
      if (getCursorFixedWindowMode()) {{
        const windowMs = Math.max(1, getCursorTimelineWindowSec() * 1000);
        const windowSec = getCursorViewWindowSec();
        const startSec = getCursorViewWindowStartSec();
        const endSec = startSec + windowSec;
        sIdx = Math.max(0, Math.min(bins.length - 1, Math.floor((startSec * 1000) / windowMs)));
        eIdx = Math.max(sIdx + 1, Math.min(bins.length - 1, Math.ceil((endSec * 1000) / windowMs)));
      }} else {{
        const startPct = getCursorRangeStartPct();
        const endPct = getCursorRangeEndPct();
        sIdx = Math.max(0, Math.floor((bins.length - 1) * (startPct / 100)));
        eIdx = Math.max(sIdx + 1, Math.min(bins.length - 1, Math.ceil((bins.length - 1) * (endPct / 100))));
      }}
      return {{ sIdx, eIdx }};
    }}

    function getCursorCompareMode() {{
      return Boolean(document.getElementById("cursorCompareMode")?.checked);
    }}

    function buildCursorMovementTimeline(windowSec) {{
      const rows = activityEvents.filter((e) => e.label !== "key:ime").slice().sort((a, b) => a.time_ms - b.time_ms);
      if (!rows.length) return {{ bins: [], maxCursor: 0 }};
      const windowMs = Math.max(1000, Math.round(windowSec * 1000));
      const maxTime = Math.max(...rows.map((r) => Number(r.time_ms || 0)), 0);
      const binCount = Math.max(1, Math.ceil((maxTime + 1) / windowMs));
      const bins = Array.from({{ length: binCount }}, (_, i) => ({{
        forward: 0,
        backward: 0,
        pauseSec: 0,
        blurCount: 0,
        startCursor: 0,
        endCursor: 0,
        eventCount: 0,
        startMs: i * windowMs,
        endMs: (i + 1) * windowMs
      }}));
      const pauseThresholdMs = getPauseGapThresholdMs();
      let lastCursor = rows[0].cursor >= 0 ? rows[0].cursor : 0;
      let maxCursor = Math.max(0, lastCursor);
      bins[0].startCursor = lastCursor;
      for (let i = 1; i < rows.length; i += 1) {{
        const prev = rows[i - 1];
        const curr = rows[i];
        const idx = Math.min(binCount - 1, Math.max(0, Math.floor((curr.time_ms || 0) / windowMs)));
        const bin = bins[idx];
        if (bin.eventCount === 0 && bin.startCursor === 0) {{
          bin.startCursor = lastCursor;
        }}
        bin.eventCount += 1;
        const prevCursor = Number(prev.cursor ?? -1);
        const currCursor = Number(curr.cursor ?? -1);
        if (currCursor < 0 || prevCursor < 0) {{
          bin.blurCount += 1;
        }} else {{
          const delta = currCursor - prevCursor;
          if (delta > 0) bin.forward += delta;
          if (delta < 0) bin.backward += Math.abs(delta);
          lastCursor = currCursor;
          maxCursor = Math.max(maxCursor, currCursor);
        }}
        const gap = Math.max(0, Number(curr.time_ms || 0) - Number(prev.time_ms || 0));
        if (gap >= pauseThresholdMs) {{
          bin.pauseSec += gap / 1000;
        }}
        bin.endCursor = lastCursor;
      }}
      for (let i = 0; i < bins.length; i += 1) {{
        if (i > 0 && bins[i].startCursor === 0 && bins[i - 1].endCursor > 0) bins[i].startCursor = bins[i - 1].endCursor;
        if (bins[i].endCursor === 0) bins[i].endCursor = bins[i].startCursor;
      }}
      return {{ bins, maxCursor: Math.max(1, maxCursor) }};
    }}

    function removeCursorCompareTags() {{
      document.querySelectorAll(".cursor-compare-tag").forEach((n) => n.remove());
    }}

    function clearCursorComparePoints() {{
      cursorComparePoints = [];
      removeCursorCompareTags();
      const popup = document.getElementById("cursorPointPopup");
      if (popup) popup.classList.add("hidden");
    }}

    function placeCursorCompareTag(cursor, cls, label) {{
      const anchors = Array.from(document.querySelectorAll(".compiled-text span[data-cursor]"));
      if (!anchors.length) return;
      let target = anchors[anchors.length - 1];
      const c = Math.max(0, Number(cursor || 0));
      for (const node of anchors) {{
        if (Number(node.dataset.cursor || 0) >= c) {{
          target = node;
          break;
        }}
      }}
      const tag = document.createElement("span");
      tag.className = `cursor-compare-tag ${{cls}}`;
      tag.textContent = label;
      target.parentNode?.insertBefore(tag, target);
      target.scrollIntoView({{ behavior: "smooth", block: "center" }});
    }}

    function wpmFromChars(chars, sec) {{
      const s = Math.max(0.2, Number(sec || 0));
      const words = Math.max(0, Number(chars || 0)) / 5;
      return (words * 60) / s;
    }}

    function showCursorPointPopup(htmlText) {{
      const popup = document.getElementById("cursorPointPopup");
      const meta = document.getElementById("cursorPointMeta");
      if (!popup || !meta) return;
      meta.innerHTML = htmlText;
      popup.classList.remove("hidden");
    }}

    function highlightTextByCursor(cursor) {{
      const nodes = Array.from(document.querySelectorAll(".compiled-text span[data-cursor]"));
      if (!nodes.length) return;
      const target = Math.max(0, Number(cursor || 0));
      let best = nodes[nodes.length - 1];
      let bestDist = Number.POSITIVE_INFINITY;
      for (const n of nodes) {{
        const c = Number(n.dataset.cursor || 0);
        const d = Math.abs(c - target);
        if (d < bestDist) {{
          bestDist = d;
          best = n;
        }}
      }}
      best.classList.add("flash-highlight");
      best.scrollIntoView({{ behavior: "smooth", block: "center" }});
      setTimeout(() => best.classList.remove("flash-highlight"), 5000);
    }}

    function handleCursorTimelinePointClick(lane, idx) {{
      if (!currentCursorTimelineModel || !currentCursorTimelineModel.bins?.length) return;
      const bin = currentCursorTimelineModel.bins[idx];
      if (!bin) return;
      highlightTextByCursor(bin.endCursor);
      const windowSec = Math.max(1, (bin.endMs - bin.startMs) / 1000);
      const netChars = (bin.endCursor || 0) - (bin.startCursor || 0);
      const pathWpm = wpmFromChars(Math.max(0, netChars), windowSec);
      let detail = `
        <strong>Cursor timeline point</strong><br/>
        <strong>Lane:</strong> ${{lane}} | <strong>Window #:</strong> ${{idx + 1}}<br/>
        <strong>Time:</strong> ${{(bin.startMs / 1000).toFixed(2)}}s → ${{(bin.endMs / 1000).toFixed(2)}}s<br/>
        <strong>Cursor:</strong> ${{bin.startCursor}} → ${{bin.endCursor}} (Δ ${{netChars >= 0 ? "+" : ""}}${{netChars}})<br/>
        <strong>Forward:</strong> ${{Math.round(bin.forward)}} | <strong>Backward:</strong> ${{Math.round(bin.backward)}} | <strong>Pause:</strong> ${{bin.pauseSec.toFixed(2)}}s | <strong>Focus loss:</strong> ${{bin.blurCount}}<br/>
        <strong>Estimated reading speed (WPM):</strong> ${{pathWpm.toFixed(2)}}
      `;
      if (getCursorCompareMode() && lane === "path") {{
        cursorComparePoints.push({{ idx, bin }});
        if (cursorComparePoints.length > 2) cursorComparePoints = cursorComparePoints.slice(-2);
        removeCursorCompareTags();
        if (cursorComparePoints[0]) placeCursorCompareTag(cursorComparePoints[0].bin.endCursor, "point1", "[point 1]");
        if (cursorComparePoints[1]) placeCursorCompareTag(cursorComparePoints[1].bin.endCursor, "point2", "[point 2]");
        if (cursorComparePoints.length === 2) {{
          const a = cursorComparePoints[0].bin;
          const b = cursorComparePoints[1].bin;
          const dtSec = Math.max(0, (b.endMs - a.endMs) / 1000);
          const dc = (b.endCursor || 0) - (a.endCursor || 0);
          const avgWpm = wpmFromChars(Math.max(0, dc), Math.max(0.2, dtSec));
          detail += `<br/><strong>Compare [point 1 → point 2]</strong><br/><strong>ΔTime:</strong> ${{dtSec.toFixed(2)}}s | <strong>ΔCursor:</strong> ${{dc >= 0 ? "+" : ""}}${{dc}} | <strong>Avg WPM:</strong> ${{avgWpm.toFixed(2)}}`;
        }}
      }}
      showCursorPointPopup(detail);
    }}

    function renderCursorMovementTimeline(forceAllRange = false) {{
      const wrap = document.getElementById("cursorMovementWrap");
      const body = document.getElementById("cursorMovementBody");
      const enabled = Boolean(document.getElementById("showCursorMovementTimeline")?.checked);
      if (!wrap || !body) return;
      if (!enabled) {{
        wrap.classList.add("hidden");
        body.innerHTML = "";
        currentCursorTimelineModel = null;
        cursorComparePoints = [];
        removeCursorCompareTags();
        document.getElementById("cursorPointPopup")?.classList.add("hidden");
        return;
      }}
      const model = buildCursorMovementTimeline(getCursorTimelineWindowSec());
      const bins = model.bins;
      const totalSec = bins.length ? (bins[bins.length - 1].endMs / 1000) : 0;
      updateCursorViewWindowBounds(totalSec);
      if (!bins.length) {{
        wrap.classList.remove("hidden");
        body.innerHTML = "<div class='cursor-axis-note'><span>No cursor events</span><span></span></div>";
        currentCursorTimelineModel = model;
        return;
      }}
      currentCursorTimelineModel = model;
      const {{ sIdx, eIdx }} = getCursorVisibleIndexRange(bins, forceAllRange);
      const visible = bins.slice(sIdx, eIdx + 1);
      const mapToGlobal = visible.map((_, i) => sIdx + i);
      const forward = visible.map((b) => b.forward);
      const backward = visible.map((b) => b.backward);
      const pauses = visible.map((b) => b.pauseSec);
      const blurs = visible.map((b) => b.blurCount);
      const path = visible.map((b) => b.endCursor);
      const laneHeight = getCursorGraphHeight();
      const pathHeight = laneHeight + 12;
      const width = Math.max(320, body.clientWidth - 4);
      const maxCursor = Math.max(1, model.maxCursor);
      const hasDenseBins = visible.length > 120;
      const localMin = Math.min(...path);
      const localMax = Math.max(...path);
      const localSpan = Math.max(1, localMax - localMin);
      const points = path.map((c, i) => {{
        const x = visible.length <= 1 ? width / 2 : (i / (visible.length - 1)) * (width - 1);
        let y = 0;
        if (normalizeCursorYAxis) {{
          y = pathHeight - 3 - ((c - localMin) / localSpan) * Math.max(6, pathHeight - 8);
        }} else {{
          y = pathHeight - 3 - (Math.max(0, c) / maxCursor) * Math.max(6, pathHeight - 8);
        }}
        return {{ x, y, i }};
      }});
      const polyline = points.map((p) => `${{p.x.toFixed(1)}},${{p.y.toFixed(1)}}`).join(" ");
      const makeLane = (arr, laneMax, color, lane, heightPx) => {{
        const pts = arr.map((v, i) => {{
          const x = arr.length <= 1 ? width / 2 : (i / (arr.length - 1)) * (width - 1);
          const y = heightPx - 3 - ((laneMax > 0 ? v / laneMax : 0) * Math.max(6, heightPx - 8));
          return `${{x.toFixed(1)}},${{y.toFixed(1)}}`;
        }}).join(" ");
        const hits = arr.map((_, i) => {{
          const startX = (i / Math.max(1, arr.length)) * width;
          const w = width / Math.max(1, arr.length);
          return `<rect class="cursor-click-hit" data-lane="${{lane}}" data-idx="${{mapToGlobal[i]}}" x="${{startX.toFixed(1)}}" y="0" width="${{Math.max(1, w).toFixed(1)}}" height="${{heightPx}}"></rect>`;
        }}).join("");
        const dots = hasDenseBins ? "" : arr.map((v, i) => {{
          const x = arr.length <= 1 ? width / 2 : (i / (arr.length - 1)) * (width - 1);
          const y = heightPx - 3 - ((laneMax > 0 ? v / laneMax : 0) * Math.max(6, heightPx - 8));
          return `<circle class="cursor-path-dot" data-lane="${{lane}}" data-idx="${{mapToGlobal[i]}}" cx="${{x.toFixed(1)}}" cy="${{y.toFixed(1)}}" r="2.6" fill="${{color}}"></circle>`;
        }}).join("");
        return `<div class="cursor-lane-track" style="height:${{heightPx}}px;"><svg class="cursor-lane-svg" viewBox="0 0 ${{width}} ${{heightPx}}" preserveAspectRatio="none"><polyline points="${{pts}}" fill="none" stroke="${{color}}" stroke-width="2"></polyline>${{dots}}${{hits}}</svg></div>`;
      }};
      const pathHits = visible.map((_, i) => {{
        const startX = (i / Math.max(1, visible.length)) * width;
        const w = width / Math.max(1, visible.length);
        return `<rect class="cursor-click-hit" data-lane="path" data-idx="${{mapToGlobal[i]}}" x="${{startX.toFixed(1)}}" y="0" width="${{Math.max(1, w).toFixed(1)}}" height="${{pathHeight}}"></rect>`;
      }}).join("");
      const dots = hasDenseBins ? "" : points.map((p) => `<circle class="cursor-path-dot" data-lane="path" data-idx="${{mapToGlobal[p.i]}}" cx="${{p.x.toFixed(1)}}" cy="${{p.y.toFixed(1)}}" r="2.8" fill="#486f9f"/>`).join("");
      const maxForward = Math.max(...forward, 0);
      const maxBackward = Math.max(...backward, 0);
      const maxPauses = Math.max(...pauses, 0);
      const maxBlurs = Math.max(...blurs, 0);
      body.innerHTML = `
        <div class="cursor-lane">
          <div class="cursor-lane-label">Cursor path by window end (higher = later text position)</div>
          <div class="cursor-path-track" style="height:${{pathHeight}}px;">
            <svg class="cursor-path-svg" viewBox="0 0 ${{width}} ${{pathHeight}}" preserveAspectRatio="none">
              <polyline points="${{polyline}}" fill="none" stroke="#486f9f" stroke-width="2"></polyline>
              ${{dots}}
              ${{pathHits}}
            </svg>
          </div>
          <div class="cursor-axis-note"><span>session start</span><span>session end</span></div>
        </div>
        <div class="cursor-lane">
          <div class="cursor-lane-label">Forward movement (chars per window)</div>
          ${{makeLane(forward, maxForward, "#4f8f75", "forward", laneHeight)}}
        </div>
        <div class="cursor-lane">
          <div class="cursor-lane-label">Backward movement (chars per window)</div>
          ${{makeLane(backward, maxBackward, "#be7a62", "backward", laneHeight)}}
        </div>
        <div class="cursor-lane">
          <div class="cursor-lane-label">Pause time (seconds per window)</div>
          ${{makeLane(pauses, maxPauses, "#8c7eb0", "pause", laneHeight)}}
        </div>
        <div class="cursor-lane">
          <div class="cursor-lane-label">Focus-loss events (count per window)</div>
          ${{makeLane(blurs, maxBlurs, "#687f91", "blur", laneHeight)}}
        </div>
      `;
      body.querySelectorAll("[data-idx]").forEach((node) => {{
        node.addEventListener("click", () => {{
          const idx = Number(node.getAttribute("data-idx") || 0);
          const lane = String(node.getAttribute("data-lane") || "path");
          handleCursorTimelinePointClick(lane, idx);
        }});
      }});
      wrap.classList.remove("hidden");
    }}

    function buildReadingMovementScores(binCount, windowSec, totalChars, granularityOverride = null, revisitPenaltyOverride = null) {{
      const granularity = granularityOverride ?? getReadingGranularity();
      const revisitPenaltyWeight = revisitPenaltyOverride ?? getRevisitPenaltyWeight();
      const windowMs = Math.max(1, windowSec * 1000);
      const readingBins = Array(binCount).fill(0);
      const readingBinDur = Array(binCount).fill(0);
      const sectionCount = Math.max(1, Math.ceil(totalChars / granularity));
      const sectionSum = Array(sectionCount).fill(0);
      const sectionDur = Array(sectionCount).fill(0);
      const rows = getValidCursorEvents();
      if (!rows.length) {{
        return {{ readingTrend: Array(binCount).fill(0), readingHeat: Array(totalChars).fill(0), granularity, revisitPenaltyWeight }};
      }}
      const maxSection = Math.max(0, sectionCount - 1);
      const toSection = (cursor) => Math.max(0, Math.min(maxSection, Math.floor(Math.max(0, cursor) / granularity)));
      const startSection = toSection(rows[0].cursor);
      const startTimeMs = rows[0].time_ms;
      let maxReachedSection = startSection;
      for (let i = 1; i < rows.length; i += 1) {{
        const prev = rows[i - 1];
        const curr = rows[i];
        const stepElapsedSec = Math.max(0.05, (curr.time_ms - prev.time_ms) / 1000);
        if (stepElapsedSec <= 0) continue;
        const prevSection = toSection(prev.cursor);
        const currSection = toSection(curr.cursor);
        const distFromStart = Math.max(0, currSection - startSection);
        const elapsedFromStartSec = Math.max(0.05, (curr.time_ms - startTimeMs) / 1000);
        let score = distFromStart / elapsedFromStartSec;
        const left = Math.max(0, Math.min(prevSection, currSection));
        const right = Math.max(0, Math.max(prevSection, currSection));
        if (left <= maxReachedSection) {{
          const overlap = Math.max(0, Math.min(right, maxReachedSection) - left + 1);
          if (overlap > 0) {{
            score -= revisitPenaltyWeight * (overlap / elapsedFromStartSec);
          }}
        }}
        maxReachedSection = Math.max(maxReachedSection, currSection);
        const binIdx = Math.min(binCount - 1, Math.max(0, Math.floor(curr.time_ms / windowMs)));
        readingBins[binIdx] += score * stepElapsedSec;
        readingBinDur[binIdx] += stepElapsedSec;
        sectionSum[currSection] += score * stepElapsedSec;
        sectionDur[currSection] += stepElapsedSec;
      }}
      const readingTrend = readingBins.map((sum, idx) => {{
        const d = readingBinDur[idx];
        return d > 0 ? Number((sum / d * 60).toFixed(3)) : 0;
      }});
      const readingHeat = Array(totalChars).fill(0);
      for (let s = 0; s < sectionCount; s += 1) {{
        const d = sectionDur[s];
        const sectionScore = d > 0 ? sectionSum[s] / d : 0;
        const v = Math.max(0, sectionScore);
        const start = s * granularity;
        const end = Math.min(totalChars, start + granularity);
        for (let p = start; p < end; p += 1) {{
          readingHeat[p] = v;
        }}
      }}
      return {{ readingTrend, readingHeat, granularity, revisitPenaltyWeight }};
    }}

    function toggleTransientDisplay() {{
      const showInline = Boolean(document.getElementById("showTransientInline")?.checked);
      const transientSpans = document.querySelectorAll(".txt-transient-deleted");
      transientSpans.forEach((node) => node.classList.toggle("hidden-transient", !showInline));
      const panel = document.getElementById("transientPanel");
      const items = document.getElementById("transientPanelItems");
      if (!panel || !items) return;
      if (showInline) {{
        panel.classList.add("hidden");
        items.innerHTML = "";
        return;
      }}
      const transientRows = segmentMeta
        .filter((s) => s.type === "transient" && (s.pe_text || "").trim())
        .map((s) => `<div class="transient-item">${{(s.pe_text || "").replace(/</g, "&lt;")}}</div>`);
      items.innerHTML = transientRows.length ? transientRows.join("") : "<div class='transient-empty'>(No transient text found)</div>";
      panel.classList.remove("hidden");
    }}

    function getVicinityRange(seg = null, totalChars = null) {{
      const beforeSlider = document.getElementById("vicinityBefore");
      const afterSlider = document.getElementById("vicinityAfter");
      const preferredBefore = Number(beforeSlider?.value || 30);
      const preferredAfter = Number(afterSlider?.value || 30);
      const adaptive = Boolean(document.getElementById("vicinityAdaptive")?.checked);
      if (!adaptive || !seg) {{
        return {{
          before: preferredBefore,
          after: preferredAfter,
          maxBefore: preferredBefore,
          maxAfter: preferredAfter,
          adaptive: false
        }};
      }}
      const fallbackTotal = Math.max(1, totalChars ?? document.querySelectorAll("#mtHeatPanel .heat-char").length);
      const anchor = Math.max(0, Number(seg.anchor_cursor || 0));
      const span = Math.max(1, Number(seg.span_len || 1));
      const coreStart = Math.min(fallbackTotal - 1, anchor);
      const coreEnd = Math.min(fallbackTotal, anchor + span);
      let maxBefore = coreStart;
      let maxAfter = Math.max(0, fallbackTotal - coreEnd);
      const comparable = orderedSegments.filter((item) => item.type !== "transient");
      const idx = comparable.findIndex((item) => item.id === seg.id);
      if (idx >= 0) {{
        for (let i = idx - 1; i >= 0; i -= 1) {{
          const prev = comparable[i];
          const prevStart = Math.max(0, Number(prev.anchor_cursor || 0));
          const prevSpan = Math.max(1, Number(prev.span_len || 1));
          const prevEnd = prevStart + prevSpan;
          if (prevEnd <= coreStart) {{
            maxBefore = Math.max(0, coreStart - prevEnd);
            break;
          }}
        }}
        for (let i = idx + 1; i < comparable.length; i += 1) {{
          const next = comparable[i];
          const nextStart = Math.max(0, Number(next.anchor_cursor || 0));
          if (nextStart >= coreEnd) {{
            maxAfter = Math.max(0, nextStart - coreEnd);
            break;
          }}
        }}
      }}
      const before = Math.min(preferredBefore, maxBefore);
      const after = Math.min(preferredAfter, maxAfter);
      return {{ before, after, maxBefore, maxAfter, adaptive: true }};
    }}

    function updateVicinityRangeLabel(seg = null) {{
      const adaptive = Boolean(document.getElementById("vicinityAdaptive")?.checked);
      const beforeSlider = document.getElementById("vicinityBefore");
      const afterSlider = document.getElementById("vicinityAfter");
      if (beforeSlider) beforeSlider.disabled = false;
      if (afterSlider) afterSlider.disabled = false;
      const range = getVicinityRange(seg);
      const beforeNode = document.getElementById("vicinityBeforeValue");
      const afterNode = document.getElementById("vicinityAfterValue");
      if (beforeNode) {{
        beforeNode.textContent = adaptive
          ? `${{range.before}} (max ${{Math.max(0, Math.round(range.maxBefore))}})`
          : String(range.before);
      }}
      if (afterNode) {{
        afterNode.textContent = adaptive
          ? `${{range.after}} (max ${{Math.max(0, Math.round(range.maxAfter))}})`
          : String(range.after);
      }}
    }}

    function applyHeatLayout() {{
      const panel = getActiveHeatPanel();
      const canvas = panel?.querySelector(".heat-canvas");
      if (!panel || !canvas) return;
      const baseWidth = Number(canvas.dataset.baseWidth || canvas.clientWidth || 1);
      const baseHeight = Number(canvas.dataset.baseHeight || canvas.clientHeight || 1);
      const targetWidth = Math.max(baseWidth, panel.clientWidth - 24);
      const scale = targetWidth / baseWidth;
      canvas.style.width = `${{targetWidth.toFixed(1)}}px`;
      canvas.style.height = `${{Math.max(baseHeight * scale, 220).toFixed(1)}}px`;
      panel.style.overflowX = "auto";
      const chars = canvas.querySelectorAll(".heat-char");
      chars.forEach((node) => {{
        const bx = Number(node.dataset.bx || 0);
        const by = Number(node.dataset.by || 0);
        const bw = Number(node.dataset.bw || 12);
        const bh = Number(node.dataset.bh || 18);
        node.style.left = `${{(bx * scale).toFixed(2)}}px`;
        node.style.top = `${{(by * scale).toFixed(2)}}px`;
        node.style.width = `${{Math.max(10, bw * scale).toFixed(2)}}px`;
        node.style.height = `${{Math.max(15, bh * scale).toFixed(2)}}px`;
      }});
    }}

    function buildTrendProbeVisual() {{
      const shapes = [];
      const annotations = [];
      if (trendProbeMinute != null) {{
        shapes.push({{
          type: "line",
          xref: "x",
          yref: "paper",
          x0: trendProbeMinute,
          x1: trendProbeMinute,
          y0: 0,
          y1: 1,
          line: {{ color: "rgba(72,106,148,0.9)", width: 2 }}
        }});
        annotations.push({{
          x: trendProbeMinute,
          y: 1.08,
          xref: "x",
          yref: "paper",
          text: "Selected text position",
          showarrow: false,
          font: {{ size: 10, color: "#4e6b89" }}
        }});
      }}
      return {{ shapes, annotations }};
    }}

    function buildPositionProbeVisual() {{
      const shapes = [];
      const annotations = [];
      if (trendProbePosition != null) {{
        shapes.push({{
          type: "line",
          xref: "x",
          yref: "paper",
          x0: trendProbePosition,
          x1: trendProbePosition,
          y0: 0,
          y1: 1,
          line: {{ color: "rgba(72,106,148,0.9)", width: 2 }}
        }});
        annotations.push({{
          x: trendProbePosition,
          y: 1.08,
          xref: "x",
          yref: "paper",
          text: "Selected text position",
          showarrow: false,
          font: {{ size: 10, color: "#4e6b89" }}
        }});
      }}
      return {{ shapes, annotations }};
    }}

    function buildPositionSeries(readingHeat, editActions, totalChars, granularity) {{
      const sectionSize = Math.max(1, granularity);
      const sectionCount = Math.max(1, Math.ceil(totalChars / sectionSize));
      const editingChar = aggregateByActions(getActiveHeatData() || {{}}, editActions, totalChars);
      const x = [];
      const reading = [];
      const editing = [];
      for (let s = 0; s < sectionCount; s += 1) {{
        const start = s * sectionSize;
        const end = Math.min(totalChars, start + sectionSize);
        const span = Math.max(1, end - start);
        let readSum = 0;
        let editSum = 0;
        for (let i = start; i < end; i += 1) {{
          readSum += readingHeat[i] || 0;
          editSum += editingChar[i] || 0;
        }}
        x.push(start + Math.floor(span / 2));
        reading.push(Number((readSum / span).toFixed(4)));
        editing.push(Number((editSum / span).toFixed(4)));
      }}
      return {{ x, reading, editing }};
    }}

    function redrawTrend() {{
      const x = trendData.x || [];
      const bins = x.length;
      const editActions = getCheckedValues("edit-action");
      const edit = aggregateByActions(trendData.action_counts || {{}}, editActions, bins);
      const totalChars = Math.max(1, getActiveHeatNodes().length);
      const movement = buildReadingMovementScores(bins, trendData.window_sec || {window_sec}, totalChars);
      const minutes = (trendData.window_sec || {window_sec}) / 60;
      const reading = movement.readingTrend;
      const editing = edit.map((v) => Number((v / minutes).toFixed(3)));
      let readingDisplay = reading.slice();
      if (normalizeReadingScale) {{
        const readingMax = Math.max(...readingDisplay.map((v) => Math.abs(v)), 0);
        const editingMax = Math.max(...editing.map((v) => Math.abs(v)), 0);
        if (readingMax > 0) {{
          const targetMax = editingMax > 0 ? editingMax : 1;
          const scale = targetMax / readingMax;
          readingDisplay = readingDisplay.map((v) => Number((v * scale).toFixed(3)));
        }}
      }}
      const data = [
        {{
          x,
          y: readingDisplay,
          mode: "lines+markers",
          name: "Reading Speed Score",
          line: {{ color: "#6ba18f", width: 3 }},
          marker: {{ size: 5 }}
        }},
        {{
          x,
          y: editing,
          mode: "lines+markers",
          name: "Edit Intensity",
          line: {{ color: "#b18378", width: 3 }},
          marker: {{ size: 5 }}
        }}
      ];
      const trendProbe = buildTrendProbeVisual();
      const layout = {{
        paper_bgcolor: "rgba(255,255,255,0)",
        plot_bgcolor: "rgba(255,255,255,0)",
        font: {{ color: "#48625b" }},
        title: "Time-based Reading Speed and Edit Intensity Trends",
        xaxis: {{ title: "Minutes in Session", gridcolor: "rgba(130,150,140,0.18)" }},
        yaxis: {{
          title: normalizeReadingScale ? "Normalized Scale" : "Score / Minute",
          gridcolor: "rgba(130,150,140,0.18)"
        }},
        legend: {{ orientation: "h", yanchor: "bottom", y: 1.03, xanchor: "left", x: 0 }},
        margin: {{ l: 45, r: 22, t: 65, b: 40 }},
        shapes: trendProbe.shapes,
        annotations: trendProbe.annotations
      }};
      Plotly.react("speedPlotTime", data, layout, {{
        responsive: true,
        displaylogo: false,
        modeBarButtonsToRemove: ["lasso2d", "select2d"]
      }});
      const plotNode = document.getElementById("speedPlotTime");
      if (plotNode && !plotNode.__boundClick) {{
        plotNode.on("plotly_click", (evt) => {{
          const x = evt?.points?.[0]?.x;
          if (x == null) return;
          jumpToSegmentByMinute(Number(x));
        }});
        plotNode.__boundClick = true;
      }}
      const movementPosition = buildReadingMovementScores(
        bins,
        trendData.window_sec || {window_sec},
        totalChars,
        getPositionReadingGranularity(),
        getPositionRevisitPenaltyWeight()
      );
      const position = buildPositionSeries(movementPosition.readingHeat, editActions, totalChars, movementPosition.granularity);
      let posReadingDisplay = position.reading.slice();
      if (normalizePositionReadingScale) {{
        const readingMax = Math.max(...posReadingDisplay.map((v) => Math.abs(v)), 0);
        const editingMax = Math.max(...position.editing.map((v) => Math.abs(v)), 0);
        if (readingMax > 0) {{
          const scale = (editingMax > 0 ? editingMax : 1) / readingMax;
          posReadingDisplay = posReadingDisplay.map((v) => Number((v * scale).toFixed(4)));
        }}
      }}
      const posData = [
        {{
          x: position.x,
          y: posReadingDisplay,
          mode: "lines+markers",
          name: "Reading Speed Score",
          line: {{ color: "#4d83b6", width: 3 }},
          marker: {{ size: 4 }}
        }},
        {{
          x: position.x,
          y: position.editing,
          mode: "lines+markers",
          name: "Edit Intensity",
          line: {{ color: "#c0886e", width: 3 }},
          marker: {{ size: 4 }}
        }}
      ];
      const posProbe = buildPositionProbeVisual();
      const posLayout = {{
        paper_bgcolor: "rgba(255,255,255,0)",
        plot_bgcolor: "rgba(255,255,255,0)",
        font: {{ color: "#48625b" }},
        title: "Position-based Reading Speed and Edit Intensity Trends",
        xaxis: {{ title: "Text Position (character index)", gridcolor: "rgba(130,150,140,0.18)" }},
        yaxis: {{
          title: normalizePositionReadingScale ? "Normalized Scale" : "Score / Section",
          gridcolor: "rgba(130,150,140,0.18)"
        }},
        legend: {{ orientation: "h", yanchor: "bottom", y: 1.03, xanchor: "left", x: 0 }},
        margin: {{ l: 45, r: 22, t: 65, b: 40 }},
        shapes: posProbe.shapes,
        annotations: posProbe.annotations
      }};
      Plotly.react("speedPlotPosition", posData, posLayout, {{
        responsive: true,
        displaylogo: false,
        modeBarButtonsToRemove: ["lasso2d", "select2d"]
      }});
      const posPlotNode = document.getElementById("speedPlotPosition");
      if (posPlotNode && !posPlotNode.__boundClick) {{
        posPlotNode.on("plotly_click", (evt) => {{
          const x = evt?.points?.[0]?.x;
          if (x == null) return;
          jumpToSegmentByCursor(Number(x));
        }});
        posPlotNode.__boundClick = true;
      }}
      return {{
        editActions,
        readingHeat: movement.readingHeat,
        granularity: movement.granularity,
        revisitPenaltyWeight: movement.revisitPenaltyWeight
      }};
    }}

    function redrawHeat(readingHeat, editActions) {{
      const nodes = getActiveHeatNodes();
      const heatLength = nodes.length;
      const editing = aggregateByActions(getActiveHeatData() || {{}}, editActions, heatLength);
      const reading = (readingHeat || []).slice(0, heatLength);
      while (reading.length < heatLength) reading.push(0);
      const readingRaw = reading.map((v) => Math.max(0, v));
      const editingRaw = editing.map((v) => Math.max(0, v));
      const normalizeLinear = (arr) => {{
        const maxV = Math.max(...arr, 0);
        if (maxV <= 0) return arr.map(() => 0);
        return arr.map((v) => Math.min(1, v / maxV));
      }};
      const values = currentHeatMode === "reading"
        ? (normalizeHeatScale ? robustNormalize(readingRaw) : normalizeLinear(readingRaw))
        : (normalizeHeatScale ? robustNormalize(editingRaw) : normalizeLinear(editingRaw));
      const textLabel = currentHeatTextMode === "pe" ? "Post-edited text" : "MT text";
      const legendText = currentHeatMode === "reading" ? `Reading Speed Heat on ${{textLabel}}` : `Editing Intensity Heat on ${{textLabel}}`;
      const legendNode = getActiveHeatPanel()?.querySelector(".heat-legend");
      if (legendNode) legendNode.textContent = legendText;
      nodes.forEach((node, idx) => {{
        const v = values[idx] || 0;
        node.style.background = currentHeatMode === "reading" ? readingHeatColor(v) : editingHeatColor(v);
        node.style.color = v > 0.64 ? "#f7fffb" : "#12312b";
        node.style.boxShadow = v > 0.8 ? "0 0 0 1px rgba(255,255,255,0.25) inset" : "none";
      }});
      applyHeatLayout();
    }}

    function showHeatPointPopup(node) {{
      const popup = document.getElementById("heatPointPopup");
      const meta = document.getElementById("heatPointMeta");
      if (!popup || !meta) return;
      const cursor = Number(node?.dataset?.cursor || 0);
      const idx = Number(node?.dataset?.idx || 0);
      const fallbackMinute = Number(((idx / Math.max(1, getActiveHeatNodes().length)) * ((trendData.x || [0]).slice(-1)[0] || 0)).toFixed(3));
      const timeMs = cursor >= 0 && cursor < cursorTimeMap.length ? Number(cursorTimeMap[cursor] || 0) : Math.round(fallbackMinute * 60000);
      const minute = Number((timeMs / 60000).toFixed(3));
      trendProbeMinute = minute;
      trendProbePosition = cursor;
      const sourceSentence = getSourceSentenceByCursor(cursor).replace(/</g, "&lt;");
      meta.innerHTML = `<strong>Heat text point</strong><br/>Mode: ${{currentHeatTextMode === "pe" ? "Post-edited text" : "MT text"}} | Cursor: ${{cursor}} | Minute on time-based trend: ${{minute.toFixed(3)}}<br/>Position on position-based trend: ${{cursor}}<br/><strong>Source sentence:</strong> ${{sourceSentence || "(not available)"}}`;
      popup.classList.remove("hidden");
      redrawAll();
    }}

    function bindHeatClickHandlers() {{
      const nodes = getActiveHeatNodes();
      nodes.forEach((node) => {{
        if (node.__heatBound) return;
        node.addEventListener("click", () => showHeatPointPopup(node));
        node.__heatBound = true;
      }});
    }}

    function jumpToSegmentByMinute(minute) {{
      const targetMs = minute * 60000;
      let best = null;
      let bestDist = Number.POSITIVE_INFINITY;
      for (const seg of segmentMeta) {{
        const start = seg.start_ms ?? 0;
        const end = seg.end_ms ?? start;
        const center = (start + end) / 2;
        const dist = Math.abs(center - targetMs);
        if (dist < bestDist) {{
          bestDist = dist;
          best = seg;
        }}
      }}
      if (!best) return;
      flashSegment(best.id);
    }}

    function jumpToSegmentByCursor(cursor) {{
      const target = Math.max(0, Number(cursor || 0));
      let best = null;
      let bestDist = Number.POSITIVE_INFINITY;
      for (const seg of segmentMeta) {{
        const anchor = Number(seg.anchor_cursor || 0);
        const span = Math.max(1, Number(seg.span_len || 1));
        const center = anchor + span / 2;
        const dist = Math.abs(center - target);
        if (dist < bestDist) {{
          bestDist = dist;
          best = seg;
        }}
      }}
      if (!best) return;
      flashSegment(best.id);
    }}

    function flashSegment(segId) {{
      const nodes = document.querySelectorAll(`.chg-span[data-seg="${{segId}}"]`);
      if (!nodes.length) return;
      nodes.forEach((n) => n.classList.add("flash-highlight"));
      nodes[0].scrollIntoView({{ behavior: "smooth", block: "center" }});
      setTimeout(() => nodes.forEach((n) => n.classList.remove("flash-highlight")), 5000);
    }}

    function showChangePopup(segId) {{
      const seg = segmentMetaMap[segId];
      if (!seg) return;
      const popupRoot = document.getElementById("changePopup");
      popupRoot?.classList.remove("hidden");
      const popupMeta = document.getElementById("popupMeta");
      const popupVicinity = document.getElementById("popupVicinity");
      const popupSpark = document.getElementById("popupSpark");
      const popupRaw = document.getElementById("popupRaw");
      const totalChars = Math.max(1, document.querySelectorAll("#mtHeatPanel .heat-char").length);
      const vicinity = getVicinityRange(seg, totalChars);
      const segDurationData = computeDurationWithVicinity(seg, vicinity.before, vicinity.after);
      const durationSec = (segDurationData.duration_ms / 1000).toFixed(2);
      popupMeta.innerHTML = `
        <div class="popup-meta-grid">
          <div class="popup-meta-chip"><strong>Type</strong>: ${{seg.type}}</div>
          <div class="popup-meta-chip"><strong>Duration</strong>: ${{durationSec}}s</div>
          <div class="popup-meta-chip"><strong>Vicinity Before</strong>: ${{vicinity.before}} chars</div>
          <div class="popup-meta-chip"><strong>Vicinity After</strong>: ${{vicinity.after}} chars</div>
        </div>
        <div class="popup-text-pair">
          <div class="popup-text-block"><strong>MT</strong><br/>${{(seg.mt_text || "").replace(/</g, "&lt;")}}</div>
          <div class="popup-text-block"><strong>PE</strong><br/>${{(seg.pe_text || "").replace(/</g, "&lt;")}}</div>
        </div>
      `;
      const anchor = Math.max(0, Number(seg.anchor_cursor || 0));
      const span = Math.max(1, Number(seg.span_len || 1));
      const coreStart = Math.min(totalChars - 1, anchor);
      const coreEnd = Math.min(totalChars, anchor + span);
      const winStart = Math.max(0, coreStart - vicinity.before);
      const winEnd = Math.min(totalChars, coreEnd + vicinity.after);
      const windowLeftPct = (winStart / totalChars) * 100;
      const windowWidthPct = (Math.max(1, winEnd - winStart) / totalChars) * 100;
      const coreLeftPct = (coreStart / totalChars) * 100;
      const coreWidthPct = (Math.max(1, coreEnd - coreStart) / totalChars) * 100;
      popupVicinity.innerHTML = `
        <div class="vicinity-caption">Vicinity Preview: highlighted window is used for duration estimation; darker segment is the changed section.</div>
        <div class="vic-track">
          <span class="vic-window" style="left:${{windowLeftPct.toFixed(3)}}%;width:${{windowWidthPct.toFixed(3)}}%"></span>
          <span class="vic-core" style="left:${{coreLeftPct.toFixed(3)}}%;width:${{coreWidthPct.toFixed(3)}}%"></span>
        </div>
        <div class="vic-labels">
          <span>0</span>
          <span>window: ${{winStart}}–${{winEnd}} (B ${{vicinity.before}} / A ${{vicinity.after}})</span>
          <span>section: ${{coreStart}}–${{coreEnd}}</span>
          <span>${{totalChars}}</span>
        </div>
      `;
      const segDur = Math.max(1, segDurationData.duration_ms);
      const avgDur = Math.max(1, avgSegmentDuration);
      const maxDur = Math.max(segDur, avgDur);
      const segW = 24 + (segDur / maxDur) * 260;
      const avgW = 24 + (avgDur / maxDur) * 260;
      popupSpark.innerHTML = `
        <div class="spark-wrap">
          <span class="spark-label">Section</span>
          <span class="spark-col" style="width:${{segW.toFixed(1)}}px;background:rgba(118,161,141,0.9)"></span>
          <span class="spark-value">${{(segDur / 1000).toFixed(2)}}s</span>
        </div>
        <div class="spark-wrap">
          <span class="spark-label">Average</span>
          <span class="spark-col" style="width:${{avgW.toFixed(1)}}px;background:rgba(178,143,122,0.86)"></span>
          <span class="spark-value">${{(avgDur / 1000).toFixed(2)}}s</span>
        </div>
      `;
      popupRaw.textContent = (seg.raw_xml || []).join("\\n");
    }}

    function computeDurationWithVicinity(seg, vicinityBefore, vicinityAfter) {{
      const anchor = Number(seg.anchor_cursor || 0);
      const span = Math.max(1, Number(seg.span_len || 1));
      const leftBound = anchor - vicinityBefore;
      const rightBound = anchor + span + vicinityAfter;
      const candidates = activityEvents.filter((e) => e.cursor >= 0 && e.cursor >= leftBound && e.cursor <= rightBound);
      if (!candidates.length) {{
        return {{
          start_ms: Number(seg.start_ms || 0),
          end_ms: Number(seg.end_ms || seg.start_ms || 0),
          duration_ms: Math.max(1, Number(seg.duration_ms || 1))
        }};
      }}
      candidates.sort((a, b) => a.time_ms - b.time_ms);
      const seedTime = (Number(seg.start_ms || 0) + Number(seg.end_ms || seg.start_ms || 0)) / 2;
      let seedIdx = 0;
      let bestDist = Number.POSITIVE_INFINITY;
      candidates.forEach((e, i) => {{
        const d = Math.abs(e.time_ms - seedTime);
        if (d < bestDist) {{
          bestDist = d;
          seedIdx = i;
        }}
      }});
      const pauseCap = 12000;
      let left = seedIdx;
      let right = seedIdx;
      while (left > 0) {{
        if (candidates[left].time_ms - candidates[left - 1].time_ms <= pauseCap) {{
          left -= 1;
        }} else {{
          break;
        }}
      }}
      while (right + 1 < candidates.length) {{
        if (candidates[right + 1].time_ms - candidates[right].time_ms <= pauseCap) {{
          right += 1;
        }} else {{
          break;
        }}
      }}
      const startMs = candidates[left].time_ms;
      const endMs = candidates[right].time_ms;
      const cappedDuration = Math.min(300000, Math.max(1, endMs - startMs));
      return {{
        start_ms: startMs,
        end_ms: endMs,
        duration_ms: cappedDuration
      }};
    }}

    function bindChangeHover() {{
      const nodes = document.querySelectorAll(".chg-span");
      nodes.forEach((node) => {{
        node.addEventListener("mouseenter", () => {{
          const segId = node.getAttribute("data-seg");
          if (segId) showChangePopup(segId);
        }});
      }});
      const closeBtn = document.getElementById("changePopupClose");
      const popupRoot = document.getElementById("changePopup");
      if (closeBtn && popupRoot) {{
        closeBtn.addEventListener("click", () => popupRoot.classList.add("hidden"));
      }}
    }}

    function redrawAll() {{
      const selected = redrawTrend();
      const heatBins = (trendData.x || []).length;
      const heatMovement = buildReadingMovementScores(
        heatBins,
        trendData.window_sec || {window_sec},
        Math.max(1, getActiveHeatNodes().length),
        getHeatReadingGranularity(),
        getHeatRevisitPenaltyWeight()
      );
      redrawHeat(heatMovement.readingHeat, selected.editActions);
      renderCursorMovementTimeline();
    }}

    function switchHeatMode(mode) {{
      currentHeatMode = mode;
      document.getElementById("heatModeReading").classList.toggle("active", mode === "reading");
      document.getElementById("heatModeEditing").classList.toggle("active", mode === "editing");
      redrawAll();
    }}

    redrawAll();
    switchHeatTextMode("mt");
    bindChangeHover();
    bindHeatClickHandlers();
    updateNormalizeButtonLabel();
    updatePositionNormalizeButtonLabel();
    updateHeatNormalizeButtonLabel();
    updateReadingSettingsLabels();
    updatePositionReadingSettingsLabels();
    updateHeatReadingSettingsLabels();
    updateCursorTimelineWindowLabel();
    updateCursorGraphHeightLabel();
    updateNormalizeCursorYAxisButtonLabel();
    updateCursorRangeLabels();
    updateCursorViewWindowSecLabel();
    updateCursorViewWindowStartSecLabel();
    setPauseGapThresholdMs(getPauseGapThresholdMs());
    refreshPivotFieldOptions();
    updateVicinityRangeLabel();
    const readingGranularityControl = document.getElementById("readingGranularity");
    if (readingGranularityControl) {{
      readingGranularityControl.addEventListener("input", () => {{
        updateReadingSettingsLabels();
        redrawAll();
      }});
    }}
    const revisitPenaltyControl = document.getElementById("revisitPenaltyWeight");
    if (revisitPenaltyControl) {{
      revisitPenaltyControl.addEventListener("input", () => {{
        updateReadingSettingsLabels();
        redrawAll();
      }});
    }}
    const posGranularityControl = document.getElementById("posReadingGranularity");
    if (posGranularityControl) {{
      posGranularityControl.addEventListener("input", () => {{
        updatePositionReadingSettingsLabels();
        redrawAll();
      }});
    }}
    const posRevisitPenaltyControl = document.getElementById("posRevisitPenaltyWeight");
    if (posRevisitPenaltyControl) {{
      posRevisitPenaltyControl.addEventListener("input", () => {{
        updatePositionReadingSettingsLabels();
        redrawAll();
      }});
    }}
    const heatGranularityControl = document.getElementById("heatReadingGranularity");
    if (heatGranularityControl) {{
      heatGranularityControl.addEventListener("input", () => {{
        updateHeatReadingSettingsLabels();
        redrawAll();
      }});
    }}
    const heatRevisitPenaltyControl = document.getElementById("heatRevisitPenaltyWeight");
    if (heatRevisitPenaltyControl) {{
      heatRevisitPenaltyControl.addEventListener("input", () => {{
        updateHeatReadingSettingsLabels();
        redrawAll();
      }});
    }}
    const pauseGapThresholdControl = document.getElementById("pauseGapThresholdMs");
    if (pauseGapThresholdControl) {{
      pauseGapThresholdControl.addEventListener("input", () => {{
        setPauseGapThresholdMs(pauseGapThresholdControl.value);
        renderCursorMovementTimeline();
        refreshPivotReactiveSegmentData();
      }});
    }}
    const cursorPauseGapThresholdControl = document.getElementById("cursorPauseGapThresholdMs");
    if (cursorPauseGapThresholdControl) {{
      cursorPauseGapThresholdControl.addEventListener("input", () => {{
        setPauseGapThresholdMs(cursorPauseGapThresholdControl.value);
        renderCursorMovementTimeline();
        refreshPivotReactiveSegmentData();
      }});
    }}
    const vicinityBeforeControl = document.getElementById("vicinityBefore");
    if (vicinityBeforeControl) {{
      vicinityBeforeControl.addEventListener("input", () => {{
        updateVicinityRangeLabel();
        refreshPivotReactiveSegmentData();
      }});
    }}
    const vicinityAfterControl = document.getElementById("vicinityAfter");
    if (vicinityAfterControl) {{
      vicinityAfterControl.addEventListener("input", () => {{
        updateVicinityRangeLabel();
        refreshPivotReactiveSegmentData();
      }});
    }}
    const vicinityAdaptive = document.getElementById("vicinityAdaptive");
    if (vicinityAdaptive) {{
      vicinityAdaptive.addEventListener("change", () => {{
        updateVicinityRangeLabel();
        refreshPivotReactiveSegmentData();
      }});
    }}
    const transientToggle = document.getElementById("showTransientInline");
    if (transientToggle) {{
      transientToggle.addEventListener("change", toggleTransientDisplay);
    }}
    const cursorTimelineToggle = document.getElementById("showCursorMovementTimeline");
    if (cursorTimelineToggle) {{
      cursorTimelineToggle.addEventListener("change", renderCursorMovementTimeline);
    }}
    const cursorCompareToggle = document.getElementById("cursorCompareMode");
    if (cursorCompareToggle) {{
      cursorCompareToggle.addEventListener("change", clearCursorComparePoints);
    }}
    const cursorTimelineWindow = document.getElementById("cursorTimelineWindowSec");
    if (cursorTimelineWindow) {{
      cursorTimelineWindow.addEventListener("input", () => {{
        updateCursorTimelineWindowLabel();
        renderCursorMovementTimeline();
      }});
    }}
    const cursorRangeStart = document.getElementById("cursorRangeStart");
    if (cursorRangeStart) {{
      cursorRangeStart.addEventListener("input", () => {{
        updateCursorRangeLabels();
        renderCursorMovementTimeline();
      }});
    }}
    const cursorRangeEnd = document.getElementById("cursorRangeEnd");
    if (cursorRangeEnd) {{
      cursorRangeEnd.addEventListener("input", () => {{
        updateCursorRangeLabels();
        renderCursorMovementTimeline();
      }});
    }}
    const cursorFixedWindowMode = document.getElementById("cursorFixedWindowMode");
    if (cursorFixedWindowMode) {{
      cursorFixedWindowMode.addEventListener("change", renderCursorMovementTimeline);
    }}
    const cursorViewWindowSec = document.getElementById("cursorViewWindowSec");
    if (cursorViewWindowSec) {{
      cursorViewWindowSec.addEventListener("input", () => {{
        updateCursorViewWindowSecLabel();
        renderCursorMovementTimeline();
      }});
    }}
    const cursorViewWindowStartSec = document.getElementById("cursorViewWindowStartSec");
    if (cursorViewWindowStartSec) {{
      cursorViewWindowStartSec.addEventListener("input", () => {{
        updateCursorViewWindowStartSecLabel();
        renderCursorMovementTimeline();
      }});
    }}
    const cursorGraphHeight = document.getElementById("cursorGraphHeight");
    if (cursorGraphHeight) {{
      cursorGraphHeight.addEventListener("input", () => {{
        updateCursorGraphHeightLabel();
        renderCursorMovementTimeline();
      }});
    }}
    document.querySelectorAll(".data-export-check").forEach((node) => {{
      node.addEventListener("change", refreshPivotFieldOptions);
    }});
    toggleTransientDisplay();
    const heatPointPopup = document.getElementById("heatPointPopup");
    if (heatPointPopup) {{
      heatPointPopup.addEventListener("click", () => heatPointPopup.classList.add("hidden"));
    }}
    const cursorPointPopup = document.getElementById("cursorPointPopup");
    if (cursorPointPopup) {{
      cursorPointPopup.addEventListener("click", () => cursorPointPopup.classList.add("hidden"));
    }}
    window.addEventListener("resize", () => {{
      applyHeatLayout();
      renderCursorMovementTimeline();
    }});

    function buildActionNoteText(editActions) {{
      const toText = (arr) => arr.length ? arr.join(", ") : "(none)";
      return [
        `Editing included: ${{toText(editActions)}}`,
        `Reading granularity (chars): ${{getReadingGranularity()}}`,
        `Revisit range penalty: ${{getRevisitPenaltyWeight().toFixed(1)}}`,
        `Reading model: start→target elapsed with revisit penalties`,
        `IME ignored for reading cursor tracking: yes`
      ].join("<br>");
    }}

    function downloadCSV(filename, headers, rows) {{
      const esc = (v) => `"${{String(v ?? "").replace(/"/g, '""')}}"`;
      const csv = [
        headers.map(esc).join(","),
        ...rows.map((row) => row.map(esc).join(","))
      ].join("\\n");
      const bom = "\\uFEFF";
      const blob = new Blob([bom, csv], {{ type: "text/csv;charset=utf-8;" }});
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    }}

    async function exportSpeedPNG() {{
      const plotNode = document.getElementById("speedPlotTime");
      if (!plotNode || !plotNode.data || !plotNode.layout) return;
      const editActions = getCheckedValues("edit-action");
      const note = buildActionNoteText(editActions);
      const data = JSON.parse(JSON.stringify(plotNode.data));
      const layout = JSON.parse(JSON.stringify(plotNode.layout));
      layout.margin = layout.margin || {{}};
      layout.margin.b = Math.max(layout.margin.b || 40, 150);
      layout.annotations = (layout.annotations || []).concat([
        {{
          xref: "paper",
          yref: "paper",
          x: 0,
          y: -0.42,
          xanchor: "left",
          yanchor: "top",
          align: "left",
          showarrow: false,
          text: `<b>Selected Actions</b><br>${{note}}`,
          font: {{ size: 12, color: "#3f5650" }}
        }}
      ]);
      const tempId = "speedPlotExportTemp";
      let temp = document.getElementById(tempId);
      if (!temp) {{
        temp = document.createElement("div");
        temp.id = tempId;
        temp.style.position = "fixed";
        temp.style.left = "-10000px";
        temp.style.top = "0";
        temp.style.width = "1600px";
        temp.style.height = "980px";
        document.body.appendChild(temp);
      }}
      await Plotly.newPlot(temp, data, layout, {{ staticPlot: true, displayModeBar: false }});
      const url = await Plotly.toImage(temp, {{
        format: "png",
        width: 1600,
        height: 980
      }});
      const a = document.createElement("a");
      a.href = url;
      a.download = "reading_editing_speed_trends";
      a.click();
      Plotly.purge(temp);
    }}

    function exportSpeedCSV() {{
      const plotNode = document.getElementById("speedPlotTime");
      if (!plotNode || !plotNode.data || plotNode.data.length < 2) return;
      const x = plotNode.data[0].x || [];
      const reading = plotNode.data[0].y || [];
      const editing = plotNode.data[1].y || [];
      const rows = x.map((v, i) => [v, reading[i] ?? "", editing[i] ?? ""]);
      downloadCSV("time_based_reading_edit_intensity_trends.csv", [
        "minute_in_session",
        "reading_speed_score",
        "edit_intensity"
      ], rows);
    }}

    async function exportPositionSpeedPNG() {{
      const plotNode = document.getElementById("speedPlotPosition");
      if (!plotNode || !plotNode.data || !plotNode.layout) return;
      const editActions = getCheckedValues("edit-action");
      const note = [
        buildActionNoteText(editActions),
        `Position granularity (chars): ${{getPositionReadingGranularity()}}`,
        `Position revisit penalty: ${{getPositionRevisitPenaltyWeight().toFixed(1)}}`
      ].join("<br>");
      const data = JSON.parse(JSON.stringify(plotNode.data));
      const layout = JSON.parse(JSON.stringify(plotNode.layout));
      layout.margin = layout.margin || {{}};
      layout.margin.b = Math.max(layout.margin.b || 40, 150);
      layout.annotations = (layout.annotations || []).concat([
        {{
          xref: "paper",
          yref: "paper",
          x: 0,
          y: -0.42,
          xanchor: "left",
          yanchor: "top",
          align: "left",
          showarrow: false,
          text: `<b>Selected Actions</b><br>${{note}}`,
          font: {{ size: 12, color: "#3f5650" }}
        }}
      ]);
      const tempId = "speedPlotPositionExportTemp";
      let temp = document.getElementById(tempId);
      if (!temp) {{
        temp = document.createElement("div");
        temp.id = tempId;
        temp.style.position = "fixed";
        temp.style.left = "-10000px";
        temp.style.top = "0";
        temp.style.width = "1600px";
        temp.style.height = "980px";
        document.body.appendChild(temp);
      }}
      await Plotly.newPlot(temp, data, layout, {{ staticPlot: true, displayModeBar: false }});
      const url = await Plotly.toImage(temp, {{
        format: "png",
        width: 1600,
        height: 980
      }});
      const a = document.createElement("a");
      a.href = url;
      a.download = "position_reading_edit_intensity_trends";
      a.click();
      Plotly.purge(temp);
    }}

    function exportPositionSpeedCSV() {{
      const plotNode = document.getElementById("speedPlotPosition");
      if (!plotNode || !plotNode.data || plotNode.data.length < 2) return;
      const x = plotNode.data[0].x || [];
      const reading = plotNode.data[0].y || [];
      const editing = plotNode.data[1].y || [];
      const rows = x.map((v, i) => [v, reading[i] ?? "", editing[i] ?? ""]);
      downloadCSV("position_based_reading_edit_intensity_trends.csv", [
        "text_position_char_index",
        "reading_speed_score",
        "edit_intensity"
      ], rows);
    }}

    async function exportCursorMovementPNG() {{
      if (typeof html2canvas !== "function") return;
      const wrap = document.getElementById("cursorMovementWrap");
      const toggle = document.getElementById("showCursorMovementTimeline");
      if (!wrap) return;
      const selectedOnly = getCursorExportSelectedOnly();
      const wasEnabled = Boolean(toggle?.checked);
      if (toggle && !wasEnabled) toggle.checked = true;
      renderCursorMovementTimeline(!selectedOnly);
      await new Promise((resolve) => requestAnimationFrame(() => resolve(null)));
      if (wrap.classList.contains("hidden")) {{
        if (toggle && !wasEnabled) {{
          toggle.checked = false;
          renderCursorMovementTimeline();
        }}
        return;
      }}
      const canvas = await html2canvas(wrap, {{ scale: 2, backgroundColor: "#f7fdf9" }});
      const a = document.createElement("a");
      a.href = canvas.toDataURL("image/png");
      a.download = selectedOnly ? "cursor_movement_selected_range.png" : "cursor_movement_full_range.png";
      a.click();
      if (toggle && !wasEnabled) {{
        toggle.checked = false;
        renderCursorMovementTimeline();
      }} else {{
        renderCursorMovementTimeline();
      }}
    }}

    function exportCursorMovementCSV() {{
      const model = buildCursorMovementTimeline(getCursorTimelineWindowSec());
      const bins = model.bins || [];
      if (!bins.length) return;
      const selectedOnly = getCursorExportSelectedOnly();
      const {{ sIdx, eIdx }} = getCursorVisibleIndexRange(bins, !selectedOnly);
      const rows = [];
      for (let i = sIdx; i <= eIdx; i += 1) {{
        const b = bins[i];
        const winSec = Math.max(0.001, (b.endMs - b.startMs) / 1000);
        const netCursor = (b.endCursor || 0) - (b.startCursor || 0);
        rows.push([
          i + 1,
          Number((b.startMs / 1000).toFixed(3)),
          Number((b.endMs / 1000).toFixed(3)),
          b.startCursor || 0,
          b.endCursor || 0,
          netCursor,
          Math.round(b.forward || 0),
          Math.round(b.backward || 0),
          Number((b.pauseSec || 0).toFixed(3)),
          b.blurCount || 0,
          Number(wpmFromChars(Math.max(0, netCursor), winSec).toFixed(3))
        ]);
      }}
      downloadCSV(
        selectedOnly ? "cursor_movement_selected_range.csv" : "cursor_movement_full_range.csv",
        [
          "window_index",
          "start_sec",
          "end_sec",
          "start_cursor",
          "end_cursor",
          "net_cursor_delta",
          "forward_chars",
          "backward_chars",
          "pause_seconds",
          "focus_loss_count",
          "estimated_wpm"
        ],
        rows
      );
    }}

    function exportHeatMapPNG() {{
      const canvasNode = getActiveHeatPanel();
      if (!canvasNode || typeof html2canvas !== "function") return;
      html2canvas(canvasNode, {{ scale: 2, backgroundColor: "#f7fdf9" }}).then((canvas) => {{
        const a = document.createElement("a");
        a.href = canvas.toDataURL("image/png");
        const textMode = currentHeatTextMode === "pe" ? "pe" : "mt";
        a.download = currentHeatMode === "reading" ? `${{textMode}}_reading_heat_map.png` : `${{textMode}}_editing_heat_map.png`;
        a.click();
      }});
    }}

    function exportActionSummaryPNG() {{
      const panel = document.getElementById("actionSummaryPanel");
      if (!panel || typeof html2canvas !== "function") return;
      html2canvas(panel, {{ scale: 2, backgroundColor: "#f7fdf9" }}).then((canvas) => {{
        const a = document.createElement("a");
        a.href = canvas.toDataURL("image/png");
        a.download = "action_types_summary.png";
        a.click();
      }});
    }}

    function exportChangePopupPNG() {{
      const panel = document.getElementById("changePopup");
      if (!panel || panel.classList.contains("hidden") || typeof html2canvas !== "function") return;
      html2canvas(panel, {{ scale: 2, backgroundColor: "#ffffff" }}).then((canvas) => {{
        const a = document.createElement("a");
        a.href = canvas.toDataURL("image/png");
        a.download = "change_segment_detail.png";
        a.click();
      }});
    }}
  </script>
</body>
</html>
"""
    return report


def generate_report_file(
    xml_path: str | Path,
    window_sec: int,
    output_path: str | Path | None = None,
) -> str:
    """Generate report file from XML input and return absolute output path."""
    data = parse_xml(xml_path)
    report_html = build_report_html(
        data=data,
        window_sec=window_sec,
    )
    out_path: Path
    if output_path:
        out_path = Path(output_path)
    else:
        reports_dir = Path(xml_path).resolve().parent / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        out_path = reports_dir / f"translog_report_{uuid.uuid4().hex[:8]}.html"
    out_path.write_text(report_html, encoding="utf-8")
    return str(out_path.resolve())


def run_gradio() -> None:
    """Launch Gradio interface for interactive report generation."""
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError("Gradio is required for UI mode. Install dependencies with: pip install -r requirements.txt") from exc

    def handle_generate(
        xml_file: Any,
        window_sec: int,
    ) -> tuple[str, str]:
        xml_path = xml_file.name if hasattr(xml_file, "name") else str(xml_file)
        if not xml_path or not Path(xml_path).exists():
            raise gr.Error("Please upload a valid raw_log.xml file.")
        output = generate_report_file(
            xml_path=xml_path,
            window_sec=int(window_sec),
        )
        status = f"Report generated: {output}"
        return status, output

    with gr.Blocks(title="Translog Post-Editing Visual Parser", theme=gr.themes.Soft()) as demo:
        gr.Markdown(
            """
# Translog-ii Log Parser and Visual Report Generator
Upload your `raw_log.xml` and generate a minimal interactive HTML report.

Author: Jiajun Wu  
Email: jiajun.aiden.wu@outlook.com
"""
        )
        with gr.Row():
            xml_input = gr.File(label="Translog XML (raw_log.xml)", file_types=[".xml"], type="filepath")
        with gr.Row():
            window = gr.Slider(
                minimum=10,
                maximum=120,
                value=30,
                step=5,
                label="Trend window (seconds)",
            )
        generate_btn = gr.Button("Generate HTML Report", variant="primary")
        status_box = gr.Textbox(label="Status", interactive=False)
        output_file = gr.File(label="Generated Report (HTML)")
        generate_btn.click(
            fn=handle_generate,
            inputs=[xml_input, window],
            outputs=[status_box, output_file],
        )
    demo.launch()


def main() -> None:
    """Program entrypoint for CLI/GUI execution modes."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--xml", type=str, default="")
    parser.add_argument("--output", type=str, default="")
    parser.add_argument("--window-sec", type=int, default=30)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    if args.headless:
        if not args.xml:
            raise ValueError("--xml is required in --headless mode.")
        out = generate_report_file(
            xml_path=args.xml,
            window_sec=max(5, args.window_sec),
            output_path=args.output or None,
        )
        print(out)
        return
    run_gradio()


if __name__ == "__main__":
    main()
