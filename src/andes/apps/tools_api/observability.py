"""Observability for the tools API: JSON request logs, request ids and in-process metrics.

- One JSON log line per request (``andes.access``): request id, method, route template,
  status, latency, and the domain outcome (``ok`` or the error code). Never the body, the
  query string or the raw traveler id (an e-mail or phone): a short hash stands in for it.
- ``X-Request-ID`` is accepted from the caller (or generated) and echoed back, so a tool
  call seen in Orchestrate traces can be found in the Render logs.
- Counters and latency buckets live in memory (one process, resets on restart) and are
  served as Prometheus text by ``GET /metrics`` (API key required).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock

from starlette.types import ASGIApp, Message, Receive, Scope, Send

access_log = logging.getLogger("andes.access")

BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


@dataclass
class RequestInfo:
    """Mutable per-request record: handlers fill it in, the middleware logs it."""

    request_id: str
    outcome: str = "ok"
    traveler: str | None = None
    extra: dict = field(default_factory=dict)


_current: ContextVar[RequestInfo | None] = ContextVar("andes_request", default=None)


def current() -> RequestInfo | None:
    return _current.get()


def mark_error(code: str) -> None:
    """Record a domain failure (the API answers 200 with ok=false, so status can't show it)."""
    info = _current.get()
    if info is not None:
        info.outcome = code


def mark_traveler(traveler_id: str | None) -> None:
    info = _current.get()
    if info is not None and traveler_id:
        info.traveler = hash_traveler(traveler_id)


def hash_traveler(traveler_id: str) -> str:
    """Stable, non-reversible tag to correlate one traveler's calls without storing PII."""
    return hashlib.sha256(traveler_id.strip().lower().encode()).hexdigest()[:10]


class Metrics:
    def __init__(self) -> None:
        self._lock = Lock()
        self.requests: dict[tuple[str, str, str, str], int] = defaultdict(int)
        self.buckets: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0] * len(BUCKETS))
        self.sum: dict[tuple[str, str], float] = defaultdict(float)
        self.count: dict[tuple[str, str], int] = defaultdict(int)
        self.guard: dict[str, int] = defaultdict(int)

    def observe(self, method: str, route: str, status: int, outcome: str, seconds: float) -> None:
        key = (method, route)
        with self._lock:
            self.requests[(method, route, str(status), outcome)] += 1
            self.sum[key] += seconds
            self.count[key] += 1
            for i, bound in enumerate(BUCKETS):
                if seconds <= bound:
                    self.buckets[key][i] += 1

    def guard_event(self, result: str) -> None:
        with self._lock:
            self.guard[result] += 1

    def render(self) -> str:
        out: list[str] = []
        with self._lock:
            out += ["# TYPE andes_http_requests_total counter"]
            for (m, r, s, o), n in sorted(self.requests.items()):
                lbl = f'method="{m}",route="{r}",status="{s}",outcome="{o}"'
                out.append(f"andes_http_requests_total{{{lbl}}} {n}")
            out += ["# TYPE andes_http_request_seconds histogram"]
            for (m, r), counts in sorted(self.buckets.items()):
                lbl = f'method="{m}",route="{r}"'
                for bound, n in zip(BUCKETS, counts, strict=True):
                    out.append(f'andes_http_request_seconds_bucket{{{lbl},le="{bound}"}} {n}')
                out.append(
                    f'andes_http_request_seconds_bucket{{{lbl},le="+Inf"}} {self.count[(m, r)]}'
                )
                out.append(f"andes_http_request_seconds_sum{{{lbl}}} {self.sum[(m, r)]:.6f}")
                out.append(f"andes_http_request_seconds_count{{{lbl}}} {self.count[(m, r)]}")
            out += ["# TYPE andes_price_guard_total counter"]
            for result, n in sorted(self.guard.items()):
                out.append(f'andes_price_guard_total{{result="{result}"}} {n}')
        return "\n".join(out) + "\n"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        info = _current.get()
        if info is not None:
            data["request_id"] = info.request_id
        extra = getattr(record, "fields", None)
        if extra:
            data.update(extra)
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    """JSON lines on stdout for the ``andes`` loggers (Render collects stdout)."""
    root = logging.getLogger("andes")
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    root.propagate = False


class ObservabilityMiddleware:
    """Pure ASGI (not BaseHTTPMiddleware) so handlers share the per-request context."""

    def __init__(self, app: ASGIApp, metrics: Metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-request-id", b"").decode("latin-1")
        info = RequestInfo(incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex)
        token = _current.set(info)
        start = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                message["headers"].append((b"x-request-id", info.request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            info.outcome = "exception"
            access_log.exception("unhandled error", extra={"fields": {"path": scope["path"]}})
            raise
        finally:
            elapsed = time.perf_counter() - start
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            outcome = info.outcome if status < 400 else f"http_{status}"
            if route != "/health":
                self.metrics.observe(scope["method"], route, status, outcome, elapsed)
                level = logging.ERROR if status >= 500 else logging.INFO
                access_log.log(
                    level,
                    "request",
                    extra={
                        "fields": {
                            "method": scope["method"],
                            "route": route,
                            "status": status,
                            "outcome": outcome,
                            "ms": round(elapsed * 1000, 1),
                            **({"traveler": info.traveler} if info.traveler else {}),
                            **info.extra,
                        }
                    },
                )
            _current.reset(token)
