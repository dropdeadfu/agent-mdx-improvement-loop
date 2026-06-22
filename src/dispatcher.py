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
import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field

logger = logging.getLogger("improvement-loop-shim.dispatcher")

# Substrings in the holder's claude-cli output that mean the refresh token itself
# is dead (interactive re-auth needed) — distinct from a transient/network blip.
_DEAD_REFRESH_MARKERS = ("401", "Invalid authentication", "invalid_grant", "OAuth")
# JIT-refresh band: if the OAuth token expires within this many seconds at spawn
# time, ping the holder's claude-cli to roll it forward. 7200s = 2h (tokens ~8h).
DEFAULT_REFRESH_BAND_S = int(os.environ.get("LOOP_REFRESH_BAND_S", "7200"))
KEEPWARM_PING_TIMEOUT_S = int(os.environ.get("LOOP_KEEPWARM_PING_TIMEOUT_S", "120"))


def _read_credentials_b64(path: str) -> str | None:
    """Read a live credentials.json → base64, or None if absent/unreadable.

    The fleet refreshes the OAuth credentials.json IN PLACE (a keepalive sidecar
    renews the token well before its ~8h expiry). The shim therefore re-reads it
    at every spawn so each runner gets a currently-valid token — forwarding the
    blob captured once at the shim's own startup would 401 within hours."""
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except OSError:
        return None


# --- OAuth holder JIT-refresh + keep-warm (ported from polaris-agent-cve-triage)
# The holder's OAuth refresh token dies after ~8h UNUSED (idle-lapse), not from
# use — claude-cli self-refreshes it whenever exercised. So we (1) JIT-refresh at
# spawn if the token is within its refresh band of expiry, and (2) keep-warm on a
# timer to roll the refresh forward inside the idle window.

def _credentials_expires_epoch(path: str) -> float | None:
    """Parse claudeAiOauth.expiresAt (epoch seconds) from the creds file, or None."""
    try:
        with open(path, "rb") as f:
            doc = json.loads(f.read().decode("utf-8"))
        exp_ms = (doc.get("claudeAiOauth") or {}).get("expiresAt")
        if isinstance(exp_ms, (int, float)) and exp_ms > 0:
            return float(exp_ms) / 1000.0
    except (OSError, ValueError, UnicodeDecodeError, AttributeError):
        return None
    return None


async def _ping_holder(container: str) -> str:
    """Trivial Haiku invocation inside the holder — forces the OAuth flow to
    refresh + persist a new token if expiry is near. Returns the cli output (for
    dead-refresh classification) or a sentinel on timeout/error."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", container,
            "claude", "-p", "reply with: ok", "--model", "claude-haiku-4-5",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=KEEPWARM_PING_TIMEOUT_S)
        return (out or b"").decode("utf-8", "replace")
    except asyncio.TimeoutError:
        return "__ping_timeout__"
    except Exception as e:  # noqa: BLE001 — docker hiccup, not a creds verdict
        return f"__ping_error__:{type(e).__name__}"


async def maybe_refresh_holder(container: str, credentials_file: str,
                               refresh_band_s: int, now: float | None = None) -> bool:
    """JIT refresh: if the holder's OAuth token expires within refresh_band_s, exec
    claude-cli in the holder to roll it forward (it persists to the holder volume,
    which credentials_file reads). Returns True iff a refresh was attempted (caller
    re-reads the file either way). No-op when no holder or plenty of life left."""
    if not container or not credentials_file:
        return False
    exp = _credentials_expires_epoch(credentials_file)
    if exp is None:
        return False
    now = now if now is not None else time.time()
    if exp - now >= refresh_band_s:
        return False  # plenty of life — skip
    await _ping_holder(container)
    return True


def keepwarm_outcome(output_text: str, before_exp: float | None,
                     after_exp: float | None) -> tuple[bool, str]:
    """Pure: classify a keep-warm ping. alive=False ⇒ refresh token dead (401-class
    marker) → needs re-auth. alive=True: 'refreshed' if expiry advanced, else 'alive'."""
    if any(m in output_text for m in _DEAD_REFRESH_MARKERS):
        return False, "dead_refresh"
    moved = (before_exp is not None and after_exp is not None and after_exp > before_exp)
    return True, ("refreshed" if moved else "alive")


async def keepwarm_holder(container: str, credentials_file: str) -> tuple[bool, str]:
    """Unconditionally exercise the holder's refresh token (beat the ~8h idle-lapse),
    then report (alive, detail). A timeout/error is treated as NOT-dead (transient),
    so only an explicit 401-class marker raises a re-auth signal."""
    if not container:
        return True, "skip:no_holder"
    before = _credentials_expires_epoch(credentials_file)
    text = await _ping_holder(container)
    if text.startswith("__ping_timeout__") or text.startswith("__ping_error__"):
        return True, "ping_unavailable:" + text.strip("_")
    after = _credentials_expires_epoch(credentials_file)
    return keepwarm_outcome(text, before, after)

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
    # Sibling of /e1:edgeone.cve-triage — the command is /e1:<skill-dir>, i.e.
    # /e1:edgeone.improvement-loop (the skill lives at skills/edgeone.improvement-loop).
    skill_command: str = "/e1:edgeone.improvement-loop {target}"
    model: str = "claude-opus-4-8"
    max_turns: int = 200
    timeout_min: int = 60
    network: str = "bridge"           # runner needs egress for LLM + GitHub
    # The runner's own substrate token (read+emit for loop.* on its station).
    # Passed as POLARIS_TOKEN inside the runner; sourced from the shim env.
    skill_token_env: str = "SKILL_POLARIS_TOKEN"
    # Live OAuth credentials.json path (mounted into the shim from the holder's
    # volume). When set + present, re-read at each spawn and injected as a FRESH
    # ANTHROPIC_CREDENTIALS_B64 (overrides any stale env one).
    credentials_file: str = ""
    # The OAuth holder container backing credentials_file. When set, the shim
    # JIT-refreshes the token (exec claude-cli in the holder if near expiry) at
    # each spawn + keep-warms it on a timer — the cve-triage pattern, so the token
    # never goes stale/idle-lapses regardless of the holder's own keepalive.
    holder_container: str = ""
    refresh_band_s: int = DEFAULT_REFRESH_BAND_S
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
    # fresh OAuth creds re-read at spawn (the env-captured blob goes stale); when
    # present it supersedes the passthrough ANTHROPIC_CREDENTIALS_B64.
    fresh_creds = _read_credentials_b64(cfg.credentials_file) if cfg.credentials_file else None
    for key in _PASSTHROUGH_ENV:
        if key == "ANTHROPIC_CREDENTIALS_B64" and fresh_creds:
            continue  # injected fresh below
        if env.get(key):
            argv += ["-e", f"{key}={env[key]}"]
    if fresh_creds:
        argv += ["-e", f"ANTHROPIC_CREDENTIALS_B64={fresh_creds}"]
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
