"""Read-only client of the Orchestrate traces API (``/v1/agentops-v3``), no CLI needed.

Auth: the Orchestrate API key is exchanged for an IAM token (cached until near expiry).
The instance is shared with other projects and the API has no per-agent filter, so every
method keeps only traces whose ``agentId`` is one of ours; others are dropped on arrival
and never stored, logged or returned.

Traces list: ``GET /traces`` already carries ``agentId``, ``sessionId`` (the conversation),
the user's message (``input``) and the answer (``output``). The steps of a turn are one
call each (``GET /observations``, 4 per minute), so they are fetched on demand and cached.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

IAM_URL = "https://iam.cloud.ibm.com/identity/token"
MAX_WINDOW = timedelta(hours=4)  # the API refuses wider windows
PAGE = 100
STEP_CALLS_PER_MINUTE = 4  # documented limit of /observations


class OrchestrateError(Exception):
    """A failure to read Orchestrate, with a message that is safe to show in the page."""


class TracesClient:
    def __init__(
        self,
        instance_url: str,
        api_key: str,
        agent_ids: set[str],
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base = instance_url.rstrip("/")
        self.api_key = api_key
        self.agent_ids = agent_ids
        self.http = http or httpx.AsyncClient(timeout=30)
        self.clock = clock
        self._token = ""
        self._token_until = 0.0
        self._step_calls: list[float] = []
        self._lock = asyncio.Lock()

    async def _bearer(self) -> str:
        if self._token and self.clock() < self._token_until:
            return self._token
        resp = await self.http.post(
            IAM_URL,
            data={"grant_type": "urn:ibm:params:oauth:grant-type:apikey", "apikey": self.api_key},
        )
        if resp.status_code != 200:
            raise OrchestrateError("no se pudo autenticar con Orchestrate (¿WXO_API_KEY vigente?)")
        body = resp.json()
        self._token = body["access_token"]
        self._token_until = self.clock() + max(60, int(body.get("expires_in", 3600)) - 120)
        return self._token

    async def _get(self, path: str, params: dict) -> dict:
        headers = {"Authorization": f"Bearer {await self._bearer()}"}
        resp = await self.http.get(
            f"{self.base}/v1/agentops-v3{path}", params=params, headers=headers
        )
        if resp.status_code == 401:
            self._token = ""  # expired early: one retry with a fresh token
            headers = {"Authorization": f"Bearer {await self._bearer()}"}
            resp = await self.http.get(
                f"{self.base}/v1/agentops-v3{path}", params=params, headers=headers
            )
        if resp.status_code == 429:
            raise OrchestrateError("Orchestrate limitó las consultas; reintenta en un minuto")
        if resp.status_code != 200:
            raise OrchestrateError(f"Orchestrate respondió {resp.status_code}")
        return resp.json()

    async def turns(self, since: timedelta = MAX_WINDOW) -> list[dict]:
        """Our turns (one ``POST /orchestrate/runs`` trace each) in the window, newest first."""
        now = datetime.now(UTC)
        stamp = "%Y-%m-%dT%H:%M:%SZ"
        out: list[dict] = []
        page = 1
        while True:
            body = await self._get(
                "/traces",
                {
                    "limit": PAGE,
                    "page": page,
                    "fromTimestamp": (now - min(since, MAX_WINDOW)).strftime(stamp),
                    "toTimestamp": now.strftime(stamp),
                    "orderBy": "timestamp.desc",
                },
            )
            out += [t for t in body.get("data", []) if t.get("agentId") in self.agent_ids]
            if page >= (body.get("meta") or {}).get("totalPages", 1):
                return out
            page += 1

    async def steps(self, trace_id: str, since: timedelta = MAX_WINDOW) -> list[dict]:
        """Every observation of one trace, paginated; spaced to respect 4 calls per minute."""
        now = datetime.now(UTC)
        stamp = "%Y-%m-%dT%H:%M:%SZ"
        out: list[dict] = []
        page = 1
        while True:
            await self._wait_for_step_slot()
            body = await self._get(
                "/observations",
                {
                    "limit": 1000,
                    "traceId": trace_id,
                    "page": page,
                    "fromStartTime": (now - min(since, MAX_WINDOW)).strftime(stamp),
                    "toStartTime": now.strftime(stamp),
                },
            )
            out += body.get("data", [])
            if page >= (body.get("meta") or {}).get("totalPages", 1):
                return out
            page += 1

    async def _wait_for_step_slot(self) -> None:
        async with self._lock:
            now = self.clock()
            self._step_calls = [t for t in self._step_calls if now - t < 60]
            if len(self._step_calls) >= STEP_CALLS_PER_MINUTE:
                await asyncio.sleep(60 - (now - self._step_calls[0]) + 0.5)
                now = self.clock()
                self._step_calls = [t for t in self._step_calls if now - t < 60]
            self._step_calls.append(now)
