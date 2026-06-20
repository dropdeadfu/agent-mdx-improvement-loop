# Improvement-loop runner — spawned ephemerally per loop.run.requested by the
# shim's dispatcher. Built ON TOP OF the shared skill-runner base (claude-code
# CLI, gh, docker CLI, python3, the git-credential-github-app helper) — the same
# base the other skill runners use. Adds only the loop entrypoint, which
# provisions creds, pulls the edgeone.improvement-loop skill bundle from mdx, and
# exec's `claude --print "/e1:improvement-loop <target>"`.
#
# If the base image name differs in your registry, override at build time:
#   docker build --build-arg SKILL_RUNNER_BASE=<registry>/edgeone-skill-runner:latest .
ARG SKILL_RUNNER_BASE=edgeone-skill-runner:latest
FROM ${SKILL_RUNNER_BASE}

# claude refuses --dangerously-skip-permissions under root unless the runtime
# signals it is already sandboxed (we're an ephemeral container).
ENV IS_SANDBOX=1

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /app
ENTRYPOINT ["/entrypoint.sh"]
