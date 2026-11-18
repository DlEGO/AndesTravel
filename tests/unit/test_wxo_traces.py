"""Ownership rules of the trace wrapper: only traces that involve our agents are kept."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import wxo_traces  # noqa: E402


def _trace(*agent_ids: str) -> dict:
    return {
        "observations": [
            {"metadata": {"attributes": {"agent.id": i}, "resourceAttributes": {}}}
            for i in agent_ids
        ]
        + [{"metadata": None}, {"metadata": {"attributes": None}}]
    }


def test_trace_with_our_agent_id_is_ours() -> None:
    assert wxo_traces.is_ours(_trace("sup", "booking"), {"sup", "other-of-ours"})


def test_collaborator_only_trace_is_ours() -> None:
    assert wxo_traces.is_ours(_trace("booking"), {"sup", "booking"})


def test_foreign_or_empty_trace_is_not_ours() -> None:
    assert not wxo_traces.is_ours(_trace("someone-else"), {"sup"})
    assert not wxo_traces.is_ours(_trace(), {"sup"})


def test_json_is_found_after_log_lines() -> None:
    assert wxo_traces._json_in('[INFO] hi\n{"a": [1]}\ntrailing') == {"a": [1]}


def test_table_row_with_unknown_agent_is_parsed() -> None:
    line = "│ 11/18/2026 , 03:49:29PM │ " + "a" * 32 + " │ -          │  1.959s │"
    row = wxo_traces.ROW.search(line)
    assert row and row["agent"] == "-" and row["id"] == "a" * 32


def test_agent_ids_come_from_the_grouped_listing() -> None:
    listing = {
        "native": [{"id": "1", "name": "ours"}, {"id": "2", "name": "theirs"}],
        "external": [{"id": "3", "name": "ours-too"}],
        "assistant": [],
    }
    assert wxo_traces._ids_by_name(listing, {"ours", "ours-too"}) == {"1", "3"}
    assert wxo_traces._ids_by_name([{"id": "9", "name": "ours"}], {"ours"}) == {"9"}
    assert wxo_traces._ids_by_name("garbage", {"ours"}) == set()
