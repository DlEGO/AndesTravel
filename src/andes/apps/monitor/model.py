"""Shape the Orchestrate payloads into what the page shows: conversations, turns, step trees.

Unlike the local viewer (which infers the tree from time containment), the API gives each
step its ``parentObservationId`` and ``level``, so the tree and the errors are exact.
"""

from __future__ import annotations

from datetime import datetime


def _millis(value: str | None) -> float | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000


def _text(value: object, limit: int = 4000) -> str:
    """The user-facing text of a trace input/output (they come as str, or a message dict)."""
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, dict):
        content = value.get("content")
        if isinstance(content, list):
            parts = [c.get("text") for c in content if isinstance(c, dict) and c.get("text")]
            if parts:
                return " ".join(parts)[:limit]
        if isinstance(content, str):
            return content[:limit]
    return ""


def turn_summary(trace: dict) -> dict:
    """One turn (a ``POST /orchestrate/runs`` trace) for the list: no steps yet."""
    return {
        "trace_id": trace["id"],
        "started": trace.get("timestamp", ""),
        "latency_ms": round(float(trace.get("latency") or 0) * 1000, 1),
        "user": _text(trace.get("input")),
        "answer": _text(trace.get("output")),
        "agent_id": trace.get("agentId", ""),
        "steps": len(trace.get("observations") or []),
    }


def conversations(traces: list[dict]) -> list[dict]:
    """Group turns by ``sessionId`` (a trace without one is a conversation of its own)."""
    groups: dict[str, list[dict]] = {}
    for trace in traces:
        key = trace.get("sessionId") or trace["id"]
        groups.setdefault(key, []).append(turn_summary(trace))
    out = []
    for key, turns in groups.items():
        turns.sort(key=lambda t: t["started"])
        out.append(
            {
                "id": key,
                "started": turns[0]["started"],
                "turns": turns,
                "latency_ms": round(sum(t["latency_ms"] for t in turns), 1),
            }
        )
    out.sort(key=lambda c: c["started"], reverse=True)
    return out


def step_tree(observations: list[dict]) -> list[dict]:
    """Observations as a depth-first list with exact depth (via parent ids) and offsets."""
    by_parent: dict[str | None, list[dict]] = {}
    ids = {o["id"] for o in observations}
    for obs in observations:
        parent = obs.get("parentObservationId")
        by_parent.setdefault(parent if parent in ids else None, []).append(obs)
    for children in by_parent.values():
        children.sort(key=lambda o: _millis(o.get("startTime")) or 0)
    starts = [_millis(o.get("startTime")) for o in observations if o.get("startTime")]
    t0 = min(starts) if starts else 0.0
    out: list[dict] = []

    def walk(parent: str | None, depth: int) -> None:
        for obs in by_parent.get(parent, []):
            start = _millis(obs.get("startTime")) or t0
            end = _millis(obs.get("endTime")) or start
            level = (obs.get("level") or "DEFAULT").upper()
            out.append(
                {
                    "id": obs["id"],
                    "name": obs.get("name", "?"),
                    "type": obs.get("type", "SPAN"),
                    "depth": depth,
                    "start_ms": round(start - t0, 1),
                    "dur_ms": round(end - start, 1),
                    "model": obs.get("model"),
                    "tokens": int(obs.get("totalTokens") or 0),
                    "error": level == "ERROR",
                    "message": obs.get("statusMessage"),
                    "input": obs.get("input"),
                    "output": obs.get("output"),
                }
            )
            walk(obs["id"], depth + 1)

    walk(None, 0)
    return out
