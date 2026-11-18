"""Hosted monitor: key required everywhere, foreign traces never leave, exact step tree."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from andes.apps.monitor import model
from andes.apps.monitor.app import create_app
from andes.apps.monitor.orchestrate import IAM_URL, OrchestrateError, TracesClient

OURS, THEIRS = "agent-ours", "agent-theirs"
KEY = "monitor-key"
H = {"X-Monitor-Key": KEY}
T1, T2, T3 = "a" * 32, "b" * 32, "c" * 32


def _trace(trace_id: str, agent: str, session: str | None, ts: str, user: str = "hola") -> dict:
    return {
        "id": trace_id,
        "agentId": agent,
        "sessionId": session,
        "timestamp": ts,
        "latency": 1.5,
        "input": {"role": "user", "content": [{"text": user}]},
        "output": "respuesta",
        "observations": ["o1", "o2"],
    }


def _obs(oid: str, parent: str | None, start: str, end: str, **extra) -> dict:
    return {
        "id": oid,
        "parentObservationId": parent,
        "name": oid,
        "type": "SPAN",
        "startTime": f"2026-11-18T16:00:{start}Z",
        "endTime": f"2026-11-18T16:00:{end}Z",
        **extra,
    }


class FakeOrchestrate:
    """Plays the IAM and agentops-v3 endpoints over httpx.MockTransport."""

    def __init__(self, traces: list[dict], observations: list[dict] | None = None) -> None:
        self.traces, self.observations = traces, observations or []
        self.calls: list[str] = []
        self.observed_traces: list[str] = []  # trace ids asked of /observations
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if request.url.path.endswith("/observations"):
            self.observed_traces.append(request.url.params.get("traceId", ""))
        if str(request.url).startswith(IAM_URL):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if self.status != 200:
            return httpx.Response(self.status)
        if request.url.path.endswith("/traces"):
            return httpx.Response(200, json={"data": self.traces, "meta": {"totalPages": 1}})
        return httpx.Response(200, json={"data": self.observations, "meta": {"totalPages": 1}})


def _client(fake: FakeOrchestrate, now: float = 0.0) -> TracesClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return TracesClient(
        "https://wxo.test/instances/x", "apikey", {OURS}, http=http, clock=lambda: now
    )


@pytest.fixture
def fake() -> FakeOrchestrate:
    return FakeOrchestrate(
        [
            _trace(T1, OURS, "s1", "2026-11-18T16:00:00Z", "mi correo es ana@example.com"),
            _trace(T2, OURS, "s1", "2026-11-18T16:01:00Z"),
            _trace(T3, THEIRS, "other", "2026-11-18T16:02:00Z", "secreto de otro proyecto"),
        ],
        [_obs("root", None, "00", "03"), _obs("child", "root", "01", "02", level="ERROR")],
    )


@pytest.fixture
def web(fake: FakeOrchestrate) -> TestClient:
    return TestClient(create_app(client=_client(fake), key=KEY))


def test_every_data_route_needs_the_key(web: TestClient) -> None:
    for path in ("/api/conversations", f"/api/traces/{T1}/steps"):
        assert web.get(path).status_code == 401
        assert web.get(path, headers={"X-Monitor-Key": "wrong"}).status_code == 401
    assert web.get("/").status_code == 200 and web.get("/health").status_code == 200


def test_page_holds_no_data_and_is_not_cacheable(web: TestClient) -> None:
    page = web.get("/")
    assert "ana@example.com" not in page.text
    assert page.headers["cache-control"] == "no-store" and "noindex" in page.headers["x-robots-tag"]


def test_unconfigured_key_closes_everything(fake: FakeOrchestrate) -> None:
    closed = TestClient(create_app(client=_client(fake), key=""))
    assert closed.get("/api/conversations", headers=H).status_code == 503


def test_only_our_agents_are_listed_and_grouped_by_session(web: TestClient) -> None:
    body = web.get("/api/conversations", headers=H).json()
    assert [c["id"] for c in body["conversations"]] == ["s1"]
    assert [t["trace_id"] for t in body["conversations"][0]["turns"]] == [T1, T2]
    assert "secreto de otro proyecto" not in web.get("/api/conversations", headers=H).text
    assert body["conversations"][0]["turns"][0]["user"] == "mi correo es ana@example.com"


def test_a_foreign_trace_cannot_be_opened(web: TestClient, fake: FakeOrchestrate) -> None:
    web.get("/api/conversations", headers=H)
    assert web.get(f"/api/traces/{T3}/steps", headers=H).status_code == 404
    assert T3 not in fake.observed_traces  # never even asked upstream (prefetch is ours only)


def test_trace_id_is_validated(web: TestClient) -> None:
    assert web.get("/api/traces/not-hex/steps", headers=H).status_code == 400


def test_steps_come_as_an_exact_tree_and_are_cached(web: TestClient, fake: FakeOrchestrate) -> None:
    web.get("/api/conversations", headers=H)
    steps = web.get(f"/api/traces/{T1}/steps", headers=H).json()["steps"]
    assert [(s["name"], s["depth"], s["error"]) for s in steps] == [
        ("root", 0, False),
        ("child", 1, True),
    ]
    web.get(f"/api/traces/{T1}/steps", headers=H)
    # T1 and T2 once each (opened + prefetched share the same cache), not once per request
    assert sum(p.endswith("/observations") for p in fake.calls) <= 2


def test_listing_is_cached_between_tabs(web: TestClient, fake: FakeOrchestrate) -> None:
    web.get("/api/conversations", headers=H)
    web.get("/api/conversations", headers=H)
    assert sum(p.endswith("/traces") for p in fake.calls) == 1


def test_upstream_failure_is_reported_not_raised(fake: FakeOrchestrate) -> None:
    fake.status = 500
    web = TestClient(create_app(client=_client(fake), key=KEY))
    body = web.get("/api/conversations", headers=H).json()
    assert body["conversations"] == [] and "500" in body["error"]


def test_token_is_reused_until_it_expires(fake: FakeOrchestrate) -> None:
    import asyncio

    client = _client(fake)
    asyncio.run(client.turns())
    asyncio.run(client.turns())
    assert sum(p.endswith("/token") for p in fake.calls) == 1


def test_steps_are_not_throttled_client_side() -> None:
    """Measured against the real API: no 4-per-minute limit, so downloads must not wait."""
    import asyncio
    import time

    fake = FakeOrchestrate([], [_obs("o", None, "00", "01")])
    client = _client(fake, now=100.0)

    async def many() -> None:
        await asyncio.gather(*(client.steps(f"{i:032x}") for i in range(10)))

    started = time.monotonic()
    asyncio.run(many())
    assert time.monotonic() - started < 2
    assert sum(p.endswith("/observations") for p in fake.calls) == 10


def test_a_real_429_pauses_the_next_download() -> None:
    import asyncio

    fake = FakeOrchestrate([], [_obs("o", None, "00", "01")])
    client = _client(fake, now=100.0)
    fake.status = 429
    with pytest.raises(OrchestrateError):
        asyncio.run(client.steps(T1))
    assert client._paused_until > 100.0


def test_opening_a_turn_after_the_list_is_served_from_the_prefetch(fake: FakeOrchestrate) -> None:
    import asyncio

    from httpx import ASGITransport

    web_app = create_app(client=_client(fake), key=KEY)

    async def run() -> int:
        transport = ASGITransport(app=web_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            await c.get("/api/conversations", headers=H)
            await asyncio.sleep(0.3)  # the background prefetch finishes
            before = sum(p.endswith("/observations") for p in fake.calls)
            replies = await asyncio.gather(
                *(c.get(f"/api/traces/{T1}/steps", headers=H) for _ in range(5))
            )
            assert all(r.status_code == 200 for r in replies)
            return sum(p.endswith("/observations") for p in fake.calls) - before

    assert asyncio.run(run()) == 0  # five opens, no new download


def test_model_text_extraction_and_orphans() -> None:
    assert (
        model._text({"role": "user", "content": [{"text": "a"}, {"x": 1}, {"text": "b"}]}) == "a b"
    )
    assert model._text(None) == "" and model._text("plain") == "plain"
    tree = model.step_tree([_obs("lost", "missing-parent", "00", "01")])
    assert [(s["name"], s["depth"]) for s in tree] == [("lost", 0)]  # orphan: shown as a root


def test_orchestrate_error_is_a_plain_exception() -> None:
    assert str(OrchestrateError("x")) == "x"


def test_without_configured_agent_ids_nothing_is_shown(fake: FakeOrchestrate) -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    empty = TracesClient("https://wxo.test/instances/x", "apikey", set(), http=http)
    web = TestClient(create_app(client=empty, key=KEY))
    assert web.get("/api/conversations", headers=H).json()["conversations"] == []


def test_usage_is_shown_only_for_turns_whose_steps_are_known() -> None:
    steps = [
        {"type": "GENERATION", "tokens": 100, "error": False},
        {"type": "GENERATION", "tokens": 50, "error": True},
        {"type": "SPAN", "tokens": 0, "error": False},
    ]
    convs = model.conversations(
        [
            _trace(T1, OURS, "s1", "2026-11-18T16:00:00Z"),
            _trace(T2, OURS, "s1", "2026-11-18T16:01:00Z"),
        ]
    )
    partial = model.with_usage(convs, lambda tid: steps if tid == T1 else None)[0]
    assert partial["turns"][0]["usage"] == {"tokens": 150, "llm_calls": 2, "errors": 1}
    assert partial["turns"][1]["usage"] is None  # unknown is not zero
    assert partial["usage"] is None  # a total over unknown turns would be wrong
    full = model.with_usage(convs, lambda _: steps)[0]
    assert full["usage"] == {"tokens": 300, "llm_calls": 4, "errors": 2}


def test_listing_reports_tokens_once_the_prefetch_has_run(fake: FakeOrchestrate) -> None:
    import asyncio

    from httpx import ASGITransport

    fake.observations = [_obs("g", None, "00", "01", type="GENERATION", totalTokens=77)]
    web_app = create_app(client=_client(fake), key=KEY)

    async def run() -> tuple[dict, dict]:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=web_app), base_url="http://t"
        ) as c:
            first = (await c.get("/api/conversations", headers=H)).json()
            await asyncio.sleep(0.3)  # background prefetch
            second = (await c.get("/api/conversations", headers=H)).json()
            return first, second

    first, second = asyncio.run(run())
    assert first["conversations"][0]["usage"] is None
    assert second["conversations"][0]["usage"] == {"tokens": 154, "llm_calls": 2, "errors": 0}
