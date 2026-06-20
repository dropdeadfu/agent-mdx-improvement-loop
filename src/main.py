"""improvement-loop-shim entrypoint.

Wires the improvement-loop into the substrate the same way polaris-agent-cve-triage
wires CVE triage: an event-driven shim that subscribes on the substrate and spawns
ephemeral runner containers — so a loop runs with the LLM key, GitHub App, and
substrate tokens the shim already holds (the "subs we have available"), instead of
a standalone process bringing its own.

Loop shape (concurrency-1):

  1. startup: register a real subscription to `loop.run.requested` (or run on a
     cron via `on: schedule`), emit a startup shim.heartbeat, drain any backlog.
  2. on `loop.run.requested` delta:
       idle → spawn runner (emit loop.run.accepted)
       busy → emit loop.run.deferred (the substrate is the durable queue)
  3. on runner exit: emit loop.run.completed, then dequeue + spawn next.
  4. background: watchdog (kill wedged runners), heartbeat when idle.

Env: POLARIS_URL, POLARIS_TOKEN (shim emit token), SKILL_POLARIS_TOKEN (the
runner's scoped token), ANTHROPIC_API_KEY, GH_APP_*, RUNNER_IMAGE, LOOP_MODEL,
LOOP_TRIGGER_EVENT (default loop.run.requested), LOOP_TIMEOUT_MIN.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from datetime import datetime, timezone

try:  # package context (deploy) / flat (tests)
    from .dispatcher import RunnerConfig, RunningInvocation, is_wedged, kill_container, spawn_runner
    from .substrate_client import SubstrateClient, load_token
except ImportError:  # pragma: no cover
    from dispatcher import RunnerConfig, RunningInvocation, is_wedged, kill_container, spawn_runner
    from substrate_client import SubstrateClient, load_token

logger = logging.getLogger("improvement-loop-shim")

STATION = "improvement-loop"
SHIM_PRODUCER = "loop:improvement-loop-shim"
TRIGGER_EVENT = os.environ.get("LOOP_TRIGGER_EVENT", "loop.run.requested")
HEARTBEAT_S = 60.0
WATCHDOG_S = 60.0


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid() -> str:
    """26-char Crockford ULID (the collector envelope requires event_id)."""
    n = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    return "".join(reversed([_CROCKFORD[(n >> (5 * i)) & 0x1F] for i in range(26)]))


# --- pure helpers (unit-tested) ---------------------------------------------

def target_from_delta(delta: dict) -> str | None:
    """Extract the loop target from an SSE delta (handles {event:…} wrappers and
    bare envelopes; subject.loop_target then payload.target)."""
    ev = delta.get("event") if isinstance(delta.get("event"), dict) else delta
    subj = ev.get("subject") or {}
    return subj.get("loop_target") or (ev.get("payload") or {}).get("target")


def next_to_process(events: list[dict]) -> str | None:
    """Durable dequeue from the shim's own emit history: a target with a
    loop.run.deferred that has no later loop.run.accepted/completed. Returns the
    oldest such target, or None."""
    deferred: dict[str, int] = {}
    settled: dict[str, int] = {}
    for i, e in enumerate(events):
        t = (e.get("subject") or {}).get("loop_target")
        if not t:
            continue
        et = e.get("event_type")
        if et == "loop.run.deferred":
            deferred[t] = i
        elif et in ("loop.run.accepted", "loop.run.completed"):
            settled[t] = i
    pending = [(i, t) for t, i in deferred.items() if settled.get(t, -1) < i]
    pending.sort()
    return pending[0][1] if pending else None


# --- orchestration ----------------------------------------------------------

class Shim:
    def __init__(self, client: SubstrateClient, cfg: RunnerConfig):
        self.client = client
        self.cfg = cfg
        self.current: RunningInvocation | None = None

    def _envelope(self, event_type: str, target: str, *, verdict: str = "n_a",
                  payload: dict | None = None) -> dict:
        return {
            "spec_version": "0.1", "event_id": _ulid(), "timestamp": _ts(),
            "subject": {"correlation_ids": [target], "loop_target": target},
            "role": "system", "station": STATION, "operation": "observation",
            "event_type": event_type, "verdict": verdict, "payload": payload or {},
        }

    async def _emit(self, event_type: str, target: str, **kw) -> None:
        try:
            await self.client.post_event(self._envelope(event_type, target, **kw))
        except Exception as e:  # noqa: BLE001 — emit is best-effort, never blocks
            logger.warning("emit %s failed: %s", event_type, e)

    async def handle_request(self, target: str) -> None:
        if self.current is not None:
            await self._emit("loop.run.deferred", target,
                             payload={"reason": "busy", "running": self.current.target})
            return
        await self._spawn(target)

    async def _spawn(self, target: str) -> None:
        name = f"loop-run-{int(time.time())}-{abs(hash(target)) % 100000}"
        self.current = await spawn_runner(self.cfg, name=name, target=target,
                                          env=dict(os.environ), now=time.time())
        await self._emit("loop.run.accepted", target, payload={"runner": name})
        asyncio.create_task(self._await_exit(self.current))

    async def _await_exit(self, inv: RunningInvocation) -> None:
        rc = await inv.proc.wait()
        verdict = "pass" if rc == 0 else "fail"
        await self._emit("loop.run.completed", inv.target, verdict=verdict,
                         payload={"runner": inv.name, "exit_code": rc})
        if self.current is inv:
            self.current = None
        await self._dequeue()

    async def _dequeue(self) -> None:
        try:
            hist = await self.client.get_events(
                {"event_type": "loop.run.deferred", "limit": 500})
            done = await self.client.get_events(
                {"event_type": "loop.run.completed", "limit": 500})
            nxt = next_to_process(hist + done)
        except Exception as e:  # noqa: BLE001
            logger.warning("dequeue query failed: %s", e)
            return
        if nxt and self.current is None:
            await self._spawn(nxt)

    async def watchdog(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_S)
            inv = self.current
            if inv is not None and is_wedged(inv, time.time()):
                logger.warning("runner %s wedged past deadline — killing", inv.name)
                await kill_container(inv.name)


async def run() -> int:  # pragma: no cover — the live loop (needs substrate+docker)
    logging.basicConfig(level=logging.INFO)
    base = os.environ["POLARIS_URL"]
    client = SubstrateClient(base, load_token("POLARIS_TOKEN"))
    cfg = RunnerConfig(
        runner_image=os.environ.get("RUNNER_IMAGE", "edgeone-skill-runner:latest"),
        model=os.environ.get("LOOP_MODEL", "claude-opus-4-8"),
        timeout_min=int(os.environ.get("LOOP_TIMEOUT_MIN", "60")),
    )
    shim = Shim(client, cfg)
    sub = await client.create_subscription({
        "name": "loop-run-requests", "on": TRIGGER_EVENT,
        "where": "", "owner": SHIM_PRODUCER})
    # The collector assigns the owner from the token (an emitter token → the
    # producer name, ignoring the body owner). Consume SSE with the owner it
    # actually used, so delivery matches regardless of token type.
    owner = sub.get("owner") or SHIM_PRODUCER
    await shim._emit("shim.heartbeat", "improvement-loop-shim",
                     payload={"status": "started", "trigger": TRIGGER_EVENT})
    asyncio.create_task(shim.watchdog())
    async for line in client.sse_lines(owner):
        delta = client.parse_sse_event(line)
        if not delta:
            continue
        target = target_from_delta(delta)
        if target:
            await shim.handle_request(target)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(run()))
