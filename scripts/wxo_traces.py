"""Read-only view of the native Orchestrate traces, limited to the Andes Travel agents.

The instance is shared with other projects and the trace API has no per-agent filter, so
this wrapper keeps only the traces that involve one of our agents (the names in
``orchestrate/agents/*.yaml``, resolved to their ids) and writes nothing else to disk.

The search table shows the agent name for some traces and ``-`` for others. A named row
is classified by its name; a ``-`` row is fetched into memory just to read the
``agent.id`` attributes (never its inputs or outputs) and is discarded unless one of those
ids is ours. Traces of other projects are never saved.

    make wxo-traces                      # last 4 h (the API's maximum window)
    make wxo-traces LAST=2h
    make wxo-traces TRACE=<32-hex id>    # export one of ours to build/traces/<id>.json

Run with the ADK's Python (see WXO_PY in the Makefile) after `make wxo-login`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROW = re.compile(
    r"│\s*(?P<ts>\d\d/\d\d/\d{4}\s*,\s*\d\d:\d\d:\d\d[AP]M)\s*│\s*(?P<id>[0-9a-f]{32})\s*│"
    r"\s*(?P<agent>\S+)\s*│\s*(?P<latency>[\d.]+m?s)\s*│"
)
TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
UNKNOWN_AGENT = "-"
MAX_INSPECT = 300  # unnamed traces fetched per run, newest first


def our_agent_names() -> set[str]:
    names = set()
    for path in sorted((ROOT / "orchestrate" / "agents").glob("*.yaml")):
        match = re.search(r"^name:\s*(\S+)", path.read_text(encoding="utf-8"), re.M)
        if match:
            names.add(match.group(1))
    return names


def _json_in(text: str) -> object:
    """First JSON value in CLI output (it may be preceded by log lines)."""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", text):  # a log tag such as "[INFO]" is not JSON
        try:
            return decoder.raw_decode(text[match.start() :])[0]
        except ValueError:
            continue
    raise ValueError("no JSON in output")


def our_agent_ids(names: set[str]) -> set[str]:
    done = subprocess.run(
        ["orchestrate", "agents", "list", "-v"], capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        sys.exit(f"agents list failed:\n{done.stderr[-500:]}")
    return _ids_by_name(_json_in(done.stdout), names)


def _ids_by_name(data: object, names: set[str]) -> set[str]:
    """Ids of the named agents. ``agents list -v`` groups them: {native: [...], external: [...]}."""
    if isinstance(data, dict):
        groups = [v for v in data.values() if isinstance(v, list)]
        agents = [a for group in groups for a in group]
    else:
        agents = data if isinstance(data, list) else []
    return {
        a["id"] for a in agents if isinstance(a, dict) and a.get("name") in names and a.get("id")
    }


def agent_ids_in(trace: dict) -> set[str]:
    """Every ``agent.id`` attribute in a trace: only ids, never inputs or outputs."""
    found = set()
    for obs in trace.get("observations", []):
        meta = obs.get("metadata") or {}
        for section in ("attributes", "resourceAttributes"):
            value = (meta.get(section) or {}).get("agent.id")
            if value:
                found.add(str(value))
    return found


def is_ours(trace: dict, ids: set[str]) -> bool:
    return bool(agent_ids_in(trace) & ids)


def _orchestrate(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "COLUMNS": "250", "NO_COLOR": "1"}  # wide table: names not truncated
    return subprocess.run(
        ["orchestrate", "observability", "traces", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def search(last: str, limit: int = 1000) -> list[dict[str, str]]:
    done = _orchestrate("search", "--last", last, "--limit", str(limit))
    if done.returncode != 0:
        sys.exit(f"traces search failed:\n{done.stdout}{done.stderr}")
    return [m.groupdict() for m in ROW.finditer(done.stdout)]


def fetch(trace_id: str, last: str) -> dict | None:
    """A trace in memory (nothing on disk), or None when it cannot be read."""
    done = _orchestrate("export", "-t", trace_id, "--last", last, "--no-pretty")
    if done.returncode != 0:
        return None
    try:
        data = _json_in(done.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("observations") else None


def save(trace: dict) -> Path:
    out = ROOT / "build" / "traces" / f"{trace['trace_id']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(trace, indent=2), encoding="utf-8")
    return out


def ours_in_window(last: str) -> list[tuple[dict[str, str], dict | None]]:
    """(row, trace) for each trace of ours; trace is the fetched data when it was needed."""
    names = our_agent_names()
    ids = our_agent_ids(names)
    if not ids:
        sys.exit("could not resolve the ids of the Andes Travel agents on this instance")
    rows = search(last)
    mine: list[tuple[dict[str, str], dict | None]] = []
    unnamed = [r for r in rows if r["agent"] == UNKNOWN_AGENT]
    for r in rows:
        if r["agent"] in names:
            mine.append((r, None))
    for r in unnamed[:MAX_INSPECT]:
        trace = fetch(r["id"], last)
        if trace is not None and is_ours(trace, ids):
            mine.append((r, trace))
    skipped = max(0, len(unnamed) - MAX_INSPECT)
    note = f"; {skipped} older unnamed traces not inspected" if skipped else ""
    print(
        f"{len(rows)} traces in the window; {len(mine)} belong to Andes Travel{note}",
        file=sys.stderr,
    )
    mine.sort(key=lambda m: m[0]["ts"], reverse=True)
    return mine


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--last", default="4h", help="window, at most 4h (API limit)")
    parser.add_argument("--trace", help="export this trace id if it is one of ours")
    parser.add_argument("--export-all", action="store_true", help="export every trace of ours")
    args = parser.parse_args()
    if args.trace and not TRACE_ID.match(args.trace):
        sys.exit("trace id must be 32 hex characters")

    mine = ours_in_window(args.last)

    if args.export_all or args.trace:
        chosen = [m for m in mine if not args.trace or m[0]["id"] == args.trace]
        if args.trace and not chosen:
            sys.exit(
                "that trace is not an Andes Travel trace in this window "
                "(another project's, or older than --last): not exporting it"
            )
        failed = 0
        for row, trace in chosen:
            trace = trace or fetch(row["id"], args.last)
            if trace is None:
                failed += 1
                continue
            print(f"saved {save(trace)}", file=sys.stderr)
        return 1 if failed else 0

    for r, _ in mine:
        print(f"{r['ts']}  {r['id']}  {r['agent']}  {r['latency']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
