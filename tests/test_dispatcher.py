"""The runner spawn argv — the load-bearing 'inherit the shim's access' wiring."""
import dispatcher as D


def test_runner_inherits_creds_and_invokes_the_skill_on_the_target():
    cfg = D.RunnerConfig(runner_image="edgeone-skill-runner:latest",
                         model="claude-opus-4-8")
    env = {
        "ANTHROPIC_API_KEY": "sk-ant-xxx",
        "GH_APP_ID": "123", "GH_APP_INSTALLATION_ID": "456",
        "GH_APP_PRIVATE_KEY_PEM": "-----BEGIN-----",
        "SKILL_POLARIS_TOKEN": "ge_emit_skill",
        "POLARIS_URL": "https://collector",
    }
    argv = D.build_docker_run_argv(cfg, name="loop-run-1",
                                   target="skill:edgeone.cve-triage", env=env)
    j = " ".join(argv)
    # ephemeral + named
    assert "--rm" in argv and argv[argv.index("--name") + 1] == "loop-run-1"
    # inherited LLM + GitHub creds passed through to the runner
    assert "ANTHROPIC_API_KEY=sk-ant-xxx" in argv
    assert "GH_APP_ID=123" in argv and "GH_APP_PRIVATE_KEY_PEM=-----BEGIN-----" in argv
    # the runner talks to the substrate as itself (skill token → POLARIS_TOKEN)
    assert "POLARIS_TOKEN=ge_emit_skill" in argv
    # target threaded + the skill invoked
    assert "LOOP_TARGET=skill:edgeone.cve-triage" in argv
    assert cfg.runner_image in argv
    assert "/e1:edgeone.improvement-loop skill:edgeone.cve-triage" in j


def test_absent_creds_are_skipped():
    cfg = D.RunnerConfig()
    argv = D.build_docker_run_argv(cfg, name="n", target="t", env={})
    assert not any("ANTHROPIC_API_KEY=" in a for a in argv)
    assert not any(a.startswith("POLARIS_TOKEN=") for a in argv)  # no skill token → none


def test_fresh_credentials_reread_at_spawn_supersede_stale_env(tmp_path):
    creds = tmp_path / "credentials.json"
    creds.write_text('{"claudeAiOauth":{"accessToken":"FRESH"}}')
    cfg = D.RunnerConfig(credentials_file=str(creds))
    # the shim env carries a STALE blob captured at its own startup
    env = {"ANTHROPIC_CREDENTIALS_B64": "U1RBTEU="}  # base64("STALE")
    argv = D.build_docker_run_argv(cfg, name="n", target="t", env=env)
    creds_args = [a for a in argv if a.startswith("ANTHROPIC_CREDENTIALS_B64=")]
    assert len(creds_args) == 1, "exactly one creds env, the fresh one"
    import base64
    got = base64.b64decode(creds_args[0].split("=", 1)[1]).decode()
    assert "FRESH" in got and "STALE" not in creds_args[0]


def test_missing_credentials_file_falls_back_to_env_blob(tmp_path):
    cfg = D.RunnerConfig(credentials_file=str(tmp_path / "absent.json"))
    env = {"ANTHROPIC_CREDENTIALS_B64": "U1RBTEU="}
    argv = D.build_docker_run_argv(cfg, name="n", target="t", env=env)
    assert "ANTHROPIC_CREDENTIALS_B64=U1RBTEU=" in argv  # unreadable file → keep env
    assert "LOOP_TARGET=t" in argv and cfg.runner_image in argv


def test_extra_env_is_forwarded():
    cfg = D.RunnerConfig(extra_env={"FOO": "bar"})
    argv = D.build_docker_run_argv(cfg, name="n", target="t", env={})
    assert "FOO=bar" in argv


def test_wedged_past_deadline():
    inv = D.RunningInvocation(name="n", target="t", proc=None, deadline_epoch=100.0)
    assert D.is_wedged(inv, 101.0) is True
    assert D.is_wedged(inv, 99.0) is False
