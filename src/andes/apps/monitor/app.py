"""The hosted monitor: a small FastAPI service in front of the Orchestrate traces API.

Run locally:  MONITOR_KEY=... WXO_API_KEY=... uvicorn andes.apps.monitor.app:app
Environment:
    MONITOR_KEY      access key asked on the page (required; every route needs it)
    WXO_API_KEY      Orchestrate API key (read-only use: traces)
    WXO_INSTANCE_URL Orchestrate instance URL (https://api.<region>.watson-orchestrate.cloud.ibm.com/instances/<id>)
    ANDES_AGENT_IDS  comma-separated ids of the Andes Travel agents (the instance is shared)

The traces hold what testers typed (e-mails, preferences), so nothing is served without the
key, and the page is never cached or indexed.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from andes.apps.monitor import model
from andes.apps.monitor.orchestrate import OrchestrateError, TracesClient

log = logging.getLogger("andes.monitor")
CACHE_SECONDS = 10  # conversations list: many tabs, one upstream call
STEPS_CACHE_SECONDS = 600
PAGE = (Path(__file__).parent / "page.html").read_text(encoding="utf-8")
HEADERS = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"}


def create_app(
    client: TracesClient | None = None,
    key: str | None = None,
    clock=time.monotonic,
) -> FastAPI:
    key = key if key is not None else os.environ.get("MONITOR_KEY", "")
    if client is None:
        ids = {i.strip() for i in os.environ.get("ANDES_AGENT_IDS", "").split(",") if i.strip()}
        client = TracesClient(
            os.environ.get("WXO_INSTANCE_URL", ""), os.environ.get("WXO_API_KEY", ""), ids
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await client.http.aclose()

    app = FastAPI(
        title="Andes Travel monitor",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    listing: dict = {"at": -1e9, "data": None}
    steps_cache: dict[str, tuple[float, list[dict]]] = {}
    list_lock = asyncio.Lock()

    def require_key(request: Request) -> None:
        given = request.headers.get("x-monitor-key", "")
        if not key:
            raise HTTPException(503, "MONITOR_KEY is not configured")
        if not given or not hmac.compare_digest(given.encode(), key.encode()):
            raise HTTPException(401, "invalid or missing key")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def page() -> HTMLResponse:
        # The page itself holds no data: it asks for the key, then calls /api/*.
        return HTMLResponse(PAGE, headers=HEADERS)

    @app.get("/health", include_in_schema=False)
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/conversations", dependencies=[Depends(require_key)])
    async def list_conversations() -> JSONResponse:
        async with list_lock:
            if clock() - listing["at"] > CACHE_SECONDS:
                try:
                    traces = await client.turns()
                    listing.update(
                        at=clock(),
                        data={"conversations": model.conversations(traces), "error": None},
                    )
                except OrchestrateError as exc:
                    if listing["data"] is None:
                        return JSONResponse(
                            {"conversations": [], "error": str(exc)}, headers=HEADERS
                        )
                    listing["data"] = {**listing["data"], "error": str(exc)}
                    listing["at"] = clock() - CACHE_SECONDS + 5  # retry soon, keep the last data
            data = listing["data"]
        return JSONResponse(data, headers=HEADERS)

    @app.get("/api/traces/{trace_id}/steps", dependencies=[Depends(require_key)])
    async def trace_steps(trace_id: str) -> JSONResponse:
        if not (len(trace_id) == 32 and all(c in "0123456789abcdef" for c in trace_id)):
            raise HTTPException(400, "trace id must be 32 hex characters")
        cached = steps_cache.get(trace_id)
        if cached and clock() - cached[0] < STEPS_CACHE_SECONDS:
            return JSONResponse({"steps": cached[1]}, headers=HEADERS)
        known = listing["data"] and any(
            t["trace_id"] == trace_id for c in listing["data"]["conversations"] for t in c["turns"]
        )
        if not known:  # only traces we listed (so: ours) can be opened
            raise HTTPException(404, "unknown trace")
        try:
            tree = model.step_tree(await client.steps(trace_id))
        except OrchestrateError as exc:
            raise HTTPException(502, str(exc)) from exc
        steps_cache[trace_id] = (clock(), tree)
        return JSONResponse({"steps": tree}, headers=HEADERS)

    return app


def _build() -> FastAPI:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    return create_app()


app = _build() if os.environ.get("MONITOR_KEY") else None
