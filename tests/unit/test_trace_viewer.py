"""The trace viewer: inferred span tree, totals, and a page that stays self-contained."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import trace_viewer  # noqa: E402


def _obs(i: str, name: str, start: str, end: str, type_: str = "SPAN", **extra) -> dict:
    return {
        "id": i,
        "traceId": "a" * 32,
        "name": name,
        "type": type_,
        "startTime": f"2026-11-18T15:00:{start}Z",
        "endTime": f"2026-11-18T15:00:{end}Z",
        **extra,
    }


TRACE = {
    "trace_id": "a" * 32,
    "observations": [
        _obs("3", "tool", "01.200", "01.800", "SPAN"),
        _obs("1", "POST /runs", "00.000", "03.000"),
        _obs("2", "llm", "00.500", "01.000", "GENERATION", usage={"totalTokens": 42}),
        _obs("4", "answer", "02.000", "02.500", metadata={"level": "ERROR"}),
    ],
}


def test_tree_is_inferred_from_time_containment() -> None:
    t = trace_viewer.summarize(TRACE)
    assert [(s["name"], s["depth"]) for s in t["spans"]] == [
        ("POST /runs", 0),
        ("llm", 1),
        ("tool", 1),
        ("answer", 1),
    ]
    assert t["total_ms"] == 3000.0
    assert t["generations"] == 1 and t["tokens"] == 42 and t["errors"] == 1


def test_page_embeds_data_without_closing_the_script_block() -> None:
    evil = {**TRACE, "observations": [_obs("1", "x", "00.000", "01.000", input="</script><b>")]}
    page = trace_viewer.render([trace_viewer.summarize(evil)])
    assert "</script><b>" not in page
    assert "http://" not in page and "https://" not in page  # no external requests


def test_load_skips_broken_and_empty_files(tmp_path: Path) -> None:
    (tmp_path / "ok.json").write_text(json.dumps(TRACE))
    (tmp_path / "bad.json").write_text("{nope")
    (tmp_path / "empty.json").write_text(json.dumps({"error": "No trace data available"}))
    assert len(trace_viewer.load(tmp_path)) == 1


def _turn(trace_id: str, minute: str, thread: str | None) -> dict:
    meta = {"attributes": {"thread.id": thread}} if thread else None
    first = _obs("1", "POST /runs", "00.000", "02.000", metadata=meta)
    first["startTime"] = f"2026-11-18T15:{minute}:00.000Z"
    first["endTime"] = f"2026-11-18T15:{minute}:02.000Z"
    return {"trace_id": trace_id, "observations": [first]}


def test_turns_are_grouped_by_thread_newest_conversation_first() -> None:
    turns = [
        trace_viewer.summarize(_turn("t1", "01", "A")),
        trace_viewer.summarize(_turn("t3", "03", "B")),
        trace_viewer.summarize(_turn("t2", "02", "A")),
        trace_viewer.summarize(_turn("t4", "04", None)),
    ]
    convs = trace_viewer.group(turns)
    assert [c["id"] for c in convs] == ["t4", "B", "A"]  # no thread: a conversation of one
    a = convs[2]
    assert [t["trace_id"] for t in a["turns"]] == ["t1", "t2"]  # turns in order
    assert a["total_ms"] == 4000.0
