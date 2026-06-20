"""Substrate HTTP client — POST /events, GET /events, /subscriptions, SSE.

Thin async wrapper around httpx (modeled on polaris-agent-cve-triage's
substrate_client). The shim uses it to emit events, register a subscription,
consume the SSE stream, and query its own emit history for the durable queue.

Env:
  POLARIS_URL    - substrate base URL (e.g. https://polaris-bot-mdx.k8s.myapp.de)
  POLARIS_TOKEN  - emit token. If the value is a file path, read it; else inline.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

logger = logging.getLogger("improvement-loop-shim.substrate")


def load_token(env_var_name: str = "POLARIS_TOKEN") -> str:
    raw = (os.environ.get(env_var_name) or "").strip()
    if not raw:
        raise RuntimeError(f"{env_var_name} env var not set")
    if os.path.isfile(raw):
        return Path(raw).read_text().strip()
    return raw


class SubstrateClient:
    def __init__(self, base_url: str, token: str, *, timeout_s: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=timeout_s, headers={"Authorization": f"Bearer {token}"})

    async def close(self) -> None:
        await self._client.aclose()

    async def post_event(self, envelope: dict[str, Any]) -> dict[str, Any]:
        r = await self._client.post(f"{self.base_url}/events", json=envelope)
        r.raise_for_status()
        return r.json()

    async def get_events(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        r = await self._client.get(f"{self.base_url}/events", params=query)
        r.raise_for_status()
        return (r.json() or {}).get("events") or []

    async def create_subscription(self, spec: dict[str, Any]) -> dict[str, Any]:
        r = await self._client.post(f"{self.base_url}/subscriptions", json=spec)
        r.raise_for_status()
        return r.json()

    async def sse_lines(self, owner: str) -> AsyncIterator[str]:
        async with self._client.stream(
            "GET", f"{self.base_url}/subscriptions/sse",
            params={"owner": owner}, headers={"Accept": "text/event-stream"},
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                yield line

    @staticmethod
    def parse_sse_event(line: str) -> dict[str, Any] | None:
        """Parse a `data:` SSE line to a delta dict; None for non-data lines."""
        if not line.startswith("data:"):
            return None
        payload = line[5:].strip()
        if not payload:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("bad SSE payload: %s", payload[:200])
            return None
