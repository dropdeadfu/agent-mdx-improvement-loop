"""Pure dequeue/target logic + the concurrency-1 accept/defer behavior."""
import asyncio

import main as M


def test_target_from_delta_variants():
    assert M.target_from_delta({"event": {"subject": {"loop_target": "skill:x"}}}) == "skill:x"
    assert M.target_from_delta({"subject": {"loop_target": "skill:y"}}) == "skill:y"     # bare
    assert M.target_from_delta({"payload": {"target": "skill:z"}}) == "skill:z"          # fallback
    assert M.target_from_delta({"subject": {}, "payload": {}}) is None


def test_next_to_process_dequeues_unsettled_deferrals_oldest_first():
    def ev(et, t):
        return {"event_type": et, "subject": {"loop_target": t}}
    events = [
        ev("loop.run.deferred", "A"),     # 0
        ev("loop.run.deferred", "B"),     # 1
        ev("loop.run.completed", "A"),    # 2  → A settled
    ]
    assert M.next_to_process(events) == "B"     # A done, B still pending


def test_next_to_process_none_when_all_settled():
    def ev(et, t):
        return {"event_type": et, "subject": {"loop_target": t}}
    events = [ev("loop.run.deferred", "A"), ev("loop.run.accepted", "A")]
    assert M.next_to_process(events) is None


class _FakeClient:
    def __init__(self):
        self.emitted = []
    async def post_event(self, env):
        self.emitted.append(env)
        return {"event_id": "e"}
    async def get_events(self, q):
        return []


def test_busy_shim_defers_instead_of_spawning():
    client = _FakeClient()
    shim = M.Shim(client, M.RunnerConfig())
    shim.current = M.RunningInvocation(name="running", target="skill:busy",
                                       proc=None, deadline_epoch=1e18)
    asyncio.run(shim.handle_request("skill:new"))
    types = [e["event_type"] for e in client.emitted]
    assert types == ["loop.run.deferred"]
    assert client.emitted[0]["subject"]["loop_target"] == "skill:new"
    assert client.emitted[0]["payload"]["running"] == "skill:busy"


def test_idle_shim_spawns(monkeypatch):
    client = _FakeClient()
    shim = M.Shim(client, M.RunnerConfig())

    class _P:
        async def wait(self):
            return 0

    async def fake_spawn(cfg, *, name, target, env, now):
        return M.RunningInvocation(name=name, target=target, proc=_P(),
                                   deadline_epoch=now + 3600)

    monkeypatch.setattr(M, "spawn_runner", fake_spawn)

    async def drive():
        await shim.handle_request("skill:go")
        # let the _await_exit task run to completion
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(drive())
    types = [e["event_type"] for e in client.emitted]
    assert "loop.run.accepted" in types
