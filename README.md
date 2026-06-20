# polaris-agent-improvement-loop

Event-driven **improvement-loop shim** for the polaris substrate — the sibling of
[`polaris-agent-cve-triage`](https://github.com/Germanedge/polaris-agent-cve-triage),
wired the same way.

It subscribes to `loop.run.requested` on the substrate and spawns ephemeral
runner containers that run a self-improving loop
(`discover → synthesize → resolve → codegen → grounded/governed score`) on the
requested target. The loop runs **with the runner's inherited access** — the LLM
key, the GitHub App, and the substrate tokens the shim already holds — instead of
a standalone process bringing its own. That was the whole point: stop running the
loop off to the side and run it like the other shims.

## How it works (concurrency-1)

1. **startup** — register a real subscription to `loop.run.requested` (owned by
   the shim), emit a `shim.heartbeat`, drain any backlog.
2. **on `loop.run.requested`** — idle → spawn the runner (emit
   `loop.run.accepted`); busy → emit `loop.run.deferred` (the substrate is the
   durable queue).
3. **on runner exit** — emit `loop.run.completed`, then dequeue the oldest
   unsettled deferral and spawn it.
4. **background** — a watchdog kills runners past their deadline.

The runner spawn (`src/dispatcher.py::build_docker_run_argv`) passes the shim's
`ANTHROPIC_API_KEY`, `GH_APP_*`, and the runner's scoped `SKILL_POLARIS_TOKEN`
through to the runner, and invokes the improvement-loop skill on the target
(`/e1:improvement-loop <target>` by default).

## Relationship to the collector

This consumes the subscription that
[`POST /loops` now provisions](https://github.com/Germanedge/polaris-bot-mdx)
(`register_loop` → real `DeliveryRouter` subscription, owned by the loop's
producer, with an optional `callback_url`). Together: the collector provisions the
subscription; this shim consumes it and spawns the runner — the loop becomes a
first-class consumer like cve-triage, rather than a self-polling standalone.

## Runner contract

The runner image (default `edgeone-skill-runner:latest`) must carry the
improvement-loop skill, exactly as it carries the e1 CVE-triage skill. The shim
only spawns it with the target + creds; it does not ship the loop logic itself.

## Config (env)

| var | meaning |
|---|---|
| `POLARIS_URL` | substrate base URL |
| `POLARIS_TOKEN` | the shim's emit token (file path or inline) |
| `SKILL_POLARIS_TOKEN` | the runner's scoped substrate token → `POLARIS_TOKEN` inside the runner |
| `ANTHROPIC_API_KEY` | LLM key, inherited by the runner |
| `GH_APP_ID` / `GH_APP_INSTALLATION_ID` / `GH_APP_PRIVATE_KEY_PEM` | GitHub App, inherited by the runner |
| `RUNNER_IMAGE` | runner image (default `edgeone-skill-runner:latest`) |
| `LOOP_MODEL` | model for the loop (default `claude-opus-4-8`) |
| `LOOP_TIMEOUT_MIN` | per-run watchdog deadline (default 60) |
| `LOOP_TRIGGER_EVENT` | the event to spawn on (default `loop.run.requested`) |

Deployment manifest: [`service.json`](service.json) (modeled on
`polaris-agent-cve-triage`). Deploy + secret provisioning are ops steps.

## Develop

```bash
pip install -r requirements.txt
python -m pytest tests/ -q
```
