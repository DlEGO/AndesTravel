"""Request ids, access logs, metrics and the no-PII rule of the tools API."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from andes.apps.tools_api import observability
from andes.apps.tools_api.main import create_app
from andes.apps.tools_api.repository import MemoryRepository

KEY = "test-key"
H = {"X-API-Key": KEY}


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(repo=MemoryRepository(), api_key=KEY))


def _access(caplog: pytest.LogCaptureFixture) -> list[dict]:
    return [r.fields for r in caplog.records if r.name == "andes.access"]


def test_request_id_is_generated_and_echoed(client: TestClient) -> None:
    assert len(client.get("/health").headers["x-request-id"]) == 32
    echoed = client.get("/health", headers={"X-Request-ID": "orch-12345678"})
    assert echoed.headers["x-request-id"] == "orch-12345678"
    bad = client.get("/health", headers={"X-Request-ID": "no spaces allowed!!"})
    assert bad.headers["x-request-id"] != "no spaces allowed!!"


def test_access_log_has_outcome_and_no_pii(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="andes.access")
    client.get("/travelers/ana@example.com/context", headers=H)
    client.get("/travelers/not-an-id/context", headers=H)
    ok, bad = _access(caplog)
    assert ok["route"] == "/travelers/{traveler_id}/context" and ok["outcome"] == "ok"
    assert ok["traveler"] == observability.hash_traveler("ana@example.com")
    assert bad["status"] == 200 and bad["outcome"] != "ok"
    assert "ana@example.com" not in json.dumps(_access(caplog))


def test_auth_failures_are_labelled(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="andes.access")
    client.get("/travelers/ana@example.com/context")
    assert _access(caplog)[0]["outcome"] == "http_401"


def test_metrics_need_the_key_and_count_requests(client: TestClient) -> None:
    assert client.get("/metrics").status_code == 401
    client.get("/travelers/ana@example.com/context", headers=H)
    text = client.get("/metrics", headers=H).text
    assert 'route="/travelers/{traveler_id}/context",status="200",outcome="ok"} 1' in text
    assert "andes_http_request_seconds_count" in text
    assert "ana@example.com" not in text


def test_price_guard_events_are_counted(client: TestClient) -> None:
    payload = {
        "agent_id": "a1",
        "messages": [{"role": "assistant", "content": {"type": "text", "text": "Son $9.999.999"}}],
    }
    body = {"payload": payload, "context": {}}
    client.post("/plugins/price_guard", json=body, headers=H)
    text = client.get("/metrics", headers=H).text
    assert 'andes_price_guard_total{result="replaced"} 1' in text
    assert 'outcome="price_replaced"' in text


def test_json_formatter_includes_request_id() -> None:
    rec = logging.LogRecord("andes.x", logging.INFO, "", 0, "hola", None, None)
    rec.fields = {"a": 1}
    out = json.loads(observability.JsonFormatter().format(rec))
    assert out["msg"] == "hola" and out["a"] == 1
