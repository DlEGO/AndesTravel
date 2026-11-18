"""Live local viewer of the Andes Travel traces: a small server on 127.0.0.1 only.

    make wxo-traces-live            # then open http://127.0.0.1:8765

A background thread asks Orchestrate for new traces every INTERVAL seconds (each trace is
fetched once; foreign ones are remembered by id only and never stored) and the page polls
``/data.json`` to update itself. Traces of ours are also saved under ``build/traces/``, so
history survives a restart. Nothing leaves this machine: the server listens on loopback
and rejects any ``Host`` header that is not loopback (DNS rebinding), so another web page
open in your browser cannot read the conversations.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import trace_viewer
import wxo_traces

ROOT = Path(__file__).resolve().parent.parent
DIRECTORY = ROOT / "build" / "traces"
FOREIGN_FILE = ".foreign_ids"  # ids only, no content


class Live:
    def __init__(self, directory: Path, last: str, interval: float) -> None:
        self.directory, self.last, self.interval = directory, last, interval
        self.ours: dict[str, dict] = {}
        self.foreign: set[str] = set()
        self.lock = threading.Lock()
        self.state = {"conversations": [], "updated": "iniciando…", "error": None}
        self._load()
        self._publish(None)

    def _load(self) -> None:
        for path in self.directory.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("observations") and data.get("trace_id"):
                self.ours[data["trace_id"]] = data
        known = self.directory / FOREIGN_FILE
        if known.exists():
            self.foreign = set(known.read_text(encoding="utf-8").split())

    def _publish(self, error: str | None) -> None:
        turns = [trace_viewer.summarize(t) for t in self.ours.values()]
        state = {
            "conversations": trace_viewer.group(turns),
            "updated": datetime.now().strftime("%H:%M:%S"),
            "error": error,
        }
        with self.lock:
            self.state = state

    def snapshot(self) -> bytes:
        with self.lock:
            return json.dumps(self.state, ensure_ascii=False, default=str).encode()

    def cycle(self, names: set[str], ids: set[str]) -> None:
        changed = wxo_traces.refresh(self.last, names, ids, self.ours, self.foreign)
        self.directory.mkdir(parents=True, exist_ok=True)
        for trace_id in changed:
            wxo_traces.save(self.ours[trace_id])
        (self.directory / FOREIGN_FILE).write_text(" ".join(sorted(self.foreign)), encoding="utf-8")
        self._publish(None)

    def run(self) -> None:
        names = wxo_traces.our_agent_names()
        ids: set[str] = set()
        while True:
            try:
                ids = ids or wxo_traces.our_agent_ids(names)
                self.cycle(names, ids)
            except SystemExit as exc:  # the helpers exit with a message on CLI failures
                self._publish(_short(str(exc)))
            except Exception as exc:  # keep serving; show the problem in the page
                self._publish(_short(f"{type(exc).__name__}: {exc}"))
            time.sleep(self.interval)


def _short(message: str) -> str:
    if "token" in message.lower() and "expired" in message.lower():
        return "token de Orchestrate vencido: corre `make wxo-login` y reinicia"
    return " ".join(message.split())[:200]


def make_handler(live: Live, port: int) -> type[BaseHTTPRequestHandler]:
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
    page = trace_viewer.render([], live=True).encode()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.headers.get("Host", "") not in allowed:
                self._send(403, b"forbidden", "text/plain")
            elif self.path == "/":
                self._send(200, page, "text/html; charset=utf-8")
            elif self.path == "/data.json":
                self._send(200, live.snapshot(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def log_message(self, *_: object) -> None:  # quiet: the console is for problems
            pass

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--interval", type=float, default=15.0, help="seconds between refreshes")
    parser.add_argument("--last", default="4h", help="trace window, at most 4h (API limit)")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    live = Live(DIRECTORY, args.last, args.interval)
    threading.Thread(target=live.run, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(live, args.port))
    url = f"http://127.0.0.1:{args.port}"
    print(
        f"live viewer on {url} (Ctrl+C to stop); refreshing every {args.interval:g}s",
        file=sys.stderr,
    )
    if not args.no_open:
        webbrowser.open(url)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
