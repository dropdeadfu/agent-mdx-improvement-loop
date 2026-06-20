"""Runner dispatch — build the `docker run` argv and spawn an ephemeral runner.

Same shape as polaris-agent-cve-triage's dispatcher, minus the CVE-specific
OAuth-docker-cp credential dance: the improvement-loop runner is handed its
credentials as plain env (ANTHROPIC_API_KEY, GH App, POLARIS tokens) — the
`api_key` transport — which is the common case. The runner image already carries
the skill code (the improvement-loop kit), exactly as edgeone-skill-runner
carries the e1 skills; the shim only spawns it with the target + creds.

The runner inherits the shim's access — that is the whole point: the loop runs
with the LLM key, GitHub App, and substrate tokens the shim already holds,
instead of a standalone process bringing its own.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("improvement-loop-shim.dispatcher")

# Env names passed straight through from the shim's environment to the runner
# (the inherited access). Absent ones are simply skipped. Both credential
# transports are forwarded: the fleet default is base64 OAuth creds
# (ANTHROPIC_CREDENTIALS_B64) + base64 GitHub App PEM (GH_APP_PRIVATE_KEY_B64),
# with the raw ANTHROPIC_API_KEY / GH_APP_PRIVATE_KEY_PEM as fallbacks — the
# runner entrypoint accepts whichever is present.
_PASSTHROUGH_ENV = (
    "ANTHROPIC_CREDENTIALS_B64", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "GH_APP_ID", "GH_APP_INSTALLATION_ID",
    "GH_APP_PRIVATE_KEY_B64", "GH_APP_PRIVATE_KEY_PEM",
    "POLARIS_URL",
    "IMPROVEMENT_LOOP_SKILL_ID", "IMPROVEMENT_LOOP_SKILL_VERSION",
)


@dataclass
class RunnerConfig:
    runner_image: str = "edgeone-skill-runner:latest"
    # The command the runner runs. {target} is substituted with the loop target.
    # Defaults to the improvement-loop skill (sibling of /e1:edgeone.cve-triage).
    skill_command: str = "/e1:improvement-loop {target}"
    model: str = "claude-opus-4-8"
    max_turns: int = 200
    timeout_min: int = 60
    network: str = "bridge"           # runner needs egress for LLM + GitHub
    # The runner's own substrate token (read+emit for loop.* on its station).
    # Passed as POLARIS_TOKEN inside the runner; sourced from the shim env.
    skill_token_env: str = "SKILL_POLARIS_TOKEN"
    extra_env: dict = field(default_factory=dict)


def build_docker_run_argv(cfg: RunnerConfig, *, name: str, target: str,
                          env: dict) -> list[str]:
    """Build the `docker run` argv for one loop invocation. `env` is the shim's
    environment (os.environ); the passthrough creds + skill token are forwarded.
    Exposed for unit testing (the load-bearing wiring)."""
    argv = [
        "docker", "run", "--rm", "--name", name,
        "--network", cfg.network,
        # the codegen sandbox spawns its own jailed containers via the host daemon
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-e", f"LOOP_TARGET={target}",
    ]
    # the runner talks to the substrate as itself (its scoped token → POLARIS_TOKEN)
    skill_token = env.get(cfg.skill_token_env)
    if skill_token:
        argv += ["-e", f"POLARIS_TOKEN={skill_token}"]
    for key in _PASSTHROUGH_ENV:
        if env.get(key):
            argv += ["-e", f"{key}={env[key]}"]
    for k, v in cfg.extra_env.items():
        argv += ["-e", f"{k}={v}"]
    argv += [cfg.runner_image]
    # the claude invocation the runner entrypoint exec's (runs the loop skill)
    argv += ["claude", "--print", "--dangerously-skip-permissions",
             "--max-turns", str(cfg.max_turns), "--model", cfg.model,
             cfg.skill_command.format(target=target)]
    return argv


@dataclass
class RunningInvocation:
    name: str
    target: str
    proc: asyncio.subprocess.Process
    deadline_epoch: float


async def spawn_runner(cfg: RunnerConfig, *, name: str, target: str, env: dict,
                       now: float) -> RunningInvocation:
    """Spawn the ephemeral runner container. Never blocks on completion — the
    caller awaits proc.wait() (or the watchdog kills it past the deadline)."""
    argv = build_docker_run_argv(cfg, name=name, target=target, env=env)
    logger.info("spawn runner name=%s target=%s image=%s", name, target, cfg.runner_image)
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    return RunningInvocation(name=name, target=target, proc=proc,
                             deadline_epoch=now + cfg.timeout_min * 60)


def is_wedged(inv: RunningInvocation, now: float) -> bool:
    return now > inv.deadline_epoch


async def kill_container(name: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
    except Exception as e:  # noqa: BLE001 — docker hiccup, best-effort
        logger.warning("kill_container %s failed: %s", name, e)
