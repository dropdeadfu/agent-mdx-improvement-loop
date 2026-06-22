"""Pure dequeue/target logic + the concurrency-1 accept/defer behavior."""
import asyncio

import main as M


def test_target_from_delta_variants():
    assert M.target_from_delta({"event": {"subject": {"loop_target": "skill:x"}}}) == "skill:x"
    assert M.target_from_delta({"subject": {"loop_target": "skill:y"}}) == "skill:y"     # bare
    assert M.target_from_delta({"payload": {"target": "skill:z"}}) == "skill:z"          # fallback
    assert M.target_from_delta({"subject": {}, "payload": {}}) is None
    # a cron-fired schedule delta names its target via subscription_name → drives the loop
    assert M.target_from_delta(
        {"kind": "schedule", "subscription_name": "env:software-factory"}) == "env:software-factory"
    assert M.target_from_delta(
        {"kind": "schedule", "subscription_name": "process:software-factory/pr-review"}
    ) == "process:software-factory/pr-review"
    # event-triggered subs (e.g. loop-run-requests) must NOT match the prefix fallback
    assert M.target_from_delta({"kind": "schedule", "subscription_name": "loop-run-requests"}) is None
    # an event WITH a target still wins over the subscription_name fallback
    assert M.target_from_delta(
        {"subscription_name": "env:x", "event": {"subject": {"loop_target": "skill:real"}}}) == "skill:real"


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


# --- ported JIT-refresh + keepwarm (the cve-triage holder pattern) -----------
import asyncio as _asyncio
import json as _json


def test_keepwarm_outcome_classifies_dead_alive_refreshed():
    from dispatcher import keepwarm_outcome
    # a 401-class marker in the holder output => refresh token dead
    assert keepwarm_outcome("... 401 Invalid authentication ...", 100.0, 100.0) == (False, "dead_refresh")
    assert keepwarm_outcome("invalid_grant", None, None)[0] is False
    # clean output, expiry advanced => refreshed
    assert keepwarm_outcome("ok", 100.0, 200.0) == (True, "refreshed")
    # clean output, expiry unchanged => alive (still had life)
    assert keepwarm_outcome("ok", 200.0, 200.0) == (True, "alive")


def test_credentials_expires_epoch(tmp_path):
    from dispatcher import _credentials_expires_epoch
    f = tmp_path / "c.json"
    f.write_text(_json.dumps({"claudeAiOauth": {"expiresAt": 1782000000000}}))
    assert _credentials_expires_epoch(str(f)) == 1782000000.0
    f.write_text("not json")
    assert _credentials_expires_epoch(str(f)) is None
    assert _credentials_expires_epoch(str(tmp_path / "missing.json")) is None


def test_maybe_refresh_holder_band_gate(tmp_path, monkeypatch):
    import dispatcher as D
    f = tmp_path / "c.json"
    pinged = []

    async def fake_ping(container):
        pinged.append(container); return "ok"
    monkeypatch.setattr(D, "_ping_holder", fake_ping)

    # token with 5h of life, 2h band => NO refresh
    f.write_text(_json.dumps({"claudeAiOauth": {"expiresAt": int((1000 + 5 * 3600) * 1000)}}))
    assert _asyncio.run(D.maybe_refresh_holder("holder", str(f), 7200, now=1000.0)) is False
    assert pinged == []
    # token with 1h of life, 2h band => refresh (ping the holder)
    f.write_text(_json.dumps({"claudeAiOauth": {"expiresAt": int((1000 + 3600) * 1000)}}))
    assert _asyncio.run(D.maybe_refresh_holder("holder", str(f), 7200, now=1000.0)) is True
    assert pinged == ["holder"]
    # no holder configured => never refresh
    assert _asyncio.run(D.maybe_refresh_holder("", str(f), 7200, now=1000.0)) is False


def test_keepwarm_holder_timeout_is_not_dead(tmp_path, monkeypatch):
    import dispatcher as D
    f = tmp_path / "c.json"
    f.write_text(_json.dumps({"claudeAiOauth": {"expiresAt": 1782000000000}}))

    async def fake_ping(container):
        return "__ping_timeout__"
    monkeypatch.setattr(D, "_ping_holder", fake_ping)
    alive, detail = _asyncio.run(D.keepwarm_holder("holder", str(f)))
    assert alive is True and detail.startswith("ping_unavailable")
    # no holder => skip
    assert _asyncio.run(D.keepwarm_holder("", str(f))) == (True, "skip:no_holder")
