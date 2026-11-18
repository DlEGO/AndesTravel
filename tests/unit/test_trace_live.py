"""Live viewer: incremental refresh (foreign traces never stored) and a loopback-only server."""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import trace_live  # noqa: E402
import wxo_traces  # noqa: E402

NOW = datetime(2026, 11, 18, 16, 0, 0, tzinfo=UTC)
OURS, THEIRS = "agent-ours", "agent-theirs"


def _row(trace_id: str, ts: str, agent: str = "-") -> dict:
    return {"id": trace_id, "ts": ts, "agent": agent, "latency": "1.0s"}


def _trace(trace_id: str, agent_id: str, extra: int = 0) -> dict:
    obs = {"metadata": {"attributes": {"agent.id": agent_id}}}
    return {"trace_id": trace_id, "observations": [obs] * (1 + extra)}


class Fake:
    """Stands in for the CLI: a search table and a trace store, counting fetches."""

    def __init__(self, rows: list[dict], traces: dict[str, dict]) -> None:
        self.rows, self.traces, self.fetched = rows, traces, []

    def fetch(self, trace_id: str, _last: str) -> dict | None:
        self.fetched.append(trace_id)
        return self.traces.get(trace_id)


def _refresh(fake: Fake, ours: dict, foreign: set, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr(wxo_traces, "search", lambda *_: fake.rows)
    return wxo_traces.refresh("4h", {"named"}, {OURS}, ours, foreign, now=NOW, fetcher=fake.fetch)


OLD, YOUNG = "11/18/2026 , 03:00:00PM", "11/18/2026 , 03:59:00PM"  # now is 16:00 UTC


def test_each_settled_trace_is_fetched_once(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Fake(
        [_row("a" * 32, OLD), _row("b" * 32, OLD)],
        {"a" * 32: _trace("a" * 32, OURS), "b" * 32: _trace("b" * 32, THEIRS)},
    )
    ours: dict = {}
    foreign: set = set()
    assert _refresh(fake, ours, foreign, monkeypatch) == ["a" * 32]
    assert set(ours) == {"a" * 32} and foreign == {"b" * 32}
    assert "b" * 32 not in json.dumps(ours)  # a foreign trace is remembered by id, never kept
    _refresh(fake, ours, foreign, monkeypatch)
    assert fake.fetched == ["a" * 32, "b" * 32]  # nothing fetched on the second pass


def test_young_trace_is_fetched_again_and_not_written_off(monkeypatch: pytest.MonkeyPatch) -> None:
    tid = "c" * 32
    fake = Fake([_row(tid, YOUNG)], {tid: _trace(tid, "not-there-yet")})
    ours: dict = {}
    foreign: set = set()
    _refresh(fake, ours, foreign, monkeypatch)
    assert foreign == set()  # too young to rule out: its agent attributes may be missing
    fake.traces[tid] = _trace(tid, OURS, extra=2)  # it settles and now shows our agent
    assert _refresh(fake, ours, foreign, monkeypatch) == [tid]
    assert len(ours[tid]["observations"]) == 3


def test_named_agent_row_is_ours_without_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    tid = "d" * 32
    fake = Fake([_row(tid, OLD, "named")], {tid: _trace(tid, "whatever")})
    ours: dict = {}
    assert _refresh(fake, ours, set(), monkeypatch) == [tid]


def test_unreadable_trace_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Fake([_row("e" * 32, OLD)], {})
    ours: dict = {}
    foreign: set = set()
    assert _refresh(fake, ours, foreign, monkeypatch) == []
    assert not ours and not foreign  # retried next cycle


@pytest.fixture
def server(tmp_path: Path):
    live = trace_live.Live(tmp_path, "4h", 15)
    live.ours["f" * 32] = _trace("f" * 32, OURS)
    live._publish(None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), trace_live.make_handler(live, 0))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = trace_live.make_handler(live, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield port
    httpd.shutdown()


def _get(port: int, path: str, host: str | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_server_serves_page_and_data_on_loopback(server: int) -> None:
    status, body = _get(server, "/")
    assert status == 200 and b"const LIVE = true" in body
    status, body = _get(server, "/data.json")
    assert status == 200 and len(json.loads(body)["conversations"]) == 1
    assert _get(server, "/other")[0] == 404


def test_server_rejects_foreign_host_header(server: int) -> None:
    assert _get(server, "/data.json", host="evil.example.com")[0] == 403  # DNS rebinding
    assert _get(server, "/", host=f"attacker.test:{server}")[0] == 403
