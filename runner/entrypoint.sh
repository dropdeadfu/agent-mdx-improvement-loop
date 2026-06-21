#!/usr/bin/env bash
# Improvement-loop runner entrypoint — one-shot. Provisions credentials + pulls
# the edgeone.improvement-loop skill bundle from the mdx substrate, then exec's
# whatever was passed as argv (the shim's dispatcher passes the claude invocation).
#
# Modeled on polaris-agent-cve-triage/runner/entrypoint.sh, trimmed to what a
# loop run needs: Anthropic + GitHub App + the substrate token. No Jira /
# Artifactory / stack-definition (those are CVE-specific).
#
# Expected argv (set by dispatcher.build_docker_run_argv):
#   claude --print "/e1:improvement-loop <target>"
#
# Required env — credential shape matches the polaris fleet (cve-triage):
#   ANTHROPIC_CREDENTIALS_B64    base64(credentials.json) OAuth creds (fleet
#                                default) — or ANTHROPIC_API_KEY (sk-ant-api03-*)
#   GH_APP_ID / GH_APP_INSTALLATION_ID
#   GH_APP_PRIVATE_KEY_B64       base64(PEM) (fleet default) — or GH_APP_PRIVATE_KEY_PEM
#   POLARIS_URL                  substrate base URL (emits + bundle pull)
#   POLARIS_TOKEN                the runner's scoped read+emit token
# Optional:
#   IMPROVEMENT_LOOP_SKILL_ID    default edgeone.improvement-loop
#   IMPROVEMENT_LOOP_SKILL_VERSION  default latest (pin to v0.X.Y for prod)
#
# Exit code: claude's exit code → the shim maps 0 → loop.run.completed verdict=pass.
set -uo pipefail

err() { echo "fatal: $*" >&2; exit 1; }

: "${POLARIS_URL:?POLARIS_URL required}"
: "${POLARIS_TOKEN:?POLARIS_TOKEN required}"
: "${GH_APP_ID:?GH_APP_ID required}"
: "${GH_APP_INSTALLATION_ID:?GH_APP_INSTALLATION_ID required}"

mkdir -p /etc/polaris /app /root/.claude /root/.config/github-app /root/.cache

# --- skip claude onboarding / trust prompts (headless) -------------------
cat > /root/.claude/config.json <<'JSON'
{ "hasCompletedOnboarding": true, "hasTrustDialogAccepted": true }
JSON

# --- Anthropic auth: OAuth credentials.json (fleet default) or raw API key -
# The fleet ships OAuth creds as base64(credentials.json) in
# ANTHROPIC_CREDENTIALS_B64 (same transport as polaris-agent-cve-triage); a raw
# ANTHROPIC_API_KEY (sk-ant-api03-*) is the fallback.
if [ -n "${ANTHROPIC_CREDENTIALS_B64:-}" ]; then
    printf '%s' "$ANTHROPIC_CREDENTIALS_B64" | base64 -d > /root/.claude/.credentials.json \
        || err "ANTHROPIC_CREDENTIALS_B64 is not valid base64"
    chmod 0600 /root/.claude/.credentials.json
    unset ANTHROPIC_CREDENTIALS_B64
    python3 -c 'import json; json.load(open("/root/.claude/.credentials.json"))' \
        || err "ANTHROPIC_CREDENTIALS_B64 did not decode to valid JSON"
    echo "anthropic creds: materialised /root/.claude/.credentials.json"
elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    echo "anthropic creds: using ANTHROPIC_API_KEY"
else
    err "ANTHROPIC_CREDENTIALS_B64 (base64 credentials.json) or ANTHROPIC_API_KEY required"
fi

# --- GitHub App: materialise PEM + wire the credential helper -----------
if [ -n "${GH_APP_PRIVATE_KEY_B64:-}" ]; then
    printf '%s' "$GH_APP_PRIVATE_KEY_B64" | base64 -d > /etc/polaris/gh-app.pem \
        || err "GH_APP_PRIVATE_KEY_B64 is not valid base64"
    unset GH_APP_PRIVATE_KEY_B64
elif [ -n "${GH_APP_PRIVATE_KEY_PEM:-}" ]; then
    printf '%s' "$GH_APP_PRIVATE_KEY_PEM" > /etc/polaris/gh-app.pem
    unset GH_APP_PRIVATE_KEY_PEM
else
    err "GH_APP_PRIVATE_KEY_B64 (base64 PEM) or GH_APP_PRIVATE_KEY_PEM required"
fi
chmod 0400 /etc/polaris/gh-app.pem

# The helper (git-credential-github-app) sources this config.env.
cat > /root/.config/github-app/config.env <<EOF
APP_ID=${GH_APP_ID}
INSTALLATION_ID=${GH_APP_INSTALLATION_ID}
PRIVATE_KEY=/etc/polaris/gh-app.pem
EOF
chmod 0400 /root/.config/github-app/config.env
git config --global credential.helper "/usr/local/bin/git-credential-github-app"
# Also materialise /app/.git-credentials in the netrc-shaped form some skills
# expect (https://x-access-token:<token>@github.com).
if GH_TOKEN=$(python3 /opt/runner/mint_gh_app_token.py 2>/dev/null); then
    export GH_TOKEN
    umask 077
    printf 'https://x-access-token:%s@github.com\n' "$GH_TOKEN" > /app/.git-credentials
    chmod 0600 /app/.git-credentials
else
    echo "warn: GH App token mint failed at startup; the helper will retry on first git op" >&2
fi

# --- pull the skill bundle from mdx (specs/skill-bundle/v0.1) -----------
export CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1
SKILL_ID="${IMPROVEMENT_LOOP_SKILL_ID:-edgeone.improvement-loop}"
SKILL_VERSION="${IMPROVEMENT_LOOP_SKILL_VERSION:-latest}"
MP=/opt/marketplace/local
mkdir -p "$MP/.claude-plugin" "$MP/plugins/e1/.claude-plugin" "$MP/plugins/e1/skills/${SKILL_ID}"

cat > "$MP/.claude-plugin/marketplace.json" <<JSON
{ "\$schema": "https://anthropic.com/claude-code/marketplace.schema.json",
  "name": "germanedge-ai-plugins",
  "description": "Substrate-served skill bundles (mdx-pulled at runtime)",
  "owner": { "name": "polaris-bot-mdx" },
  "plugins": [ { "name": "e1", "source": "./plugins/e1", "description": "E1 team skills", "category": "development" } ] }
JSON
cat > "$MP/plugins/e1/.claude-plugin/plugin.json" <<JSON
{ "name": "e1", "version": "1.0.0", "description": "E1 team skills (substrate-served)", "skills": "./skills" }
JSON

echo "pulling skill bundle ${SKILL_ID}@${SKILL_VERSION} from ${POLARIS_URL} ..."
MANIFEST=$(curl -sS --fail --max-time 15 -H "Authorization: Bearer ${POLARIS_TOKEN}" \
    "${POLARIS_URL}/agent-skills/${SKILL_ID}/${SKILL_VERSION}") \
    || err "mdx manifest fetch failed for ${SKILL_ID}/${SKILL_VERSION}"
RESOLVED=$(echo "$MANIFEST" | python3 -c 'import sys,json; print(json.load(sys.stdin)["version"])')
echo "  resolved version: ${RESOLVED}"
echo "$MANIFEST" | python3 -c 'import sys,json; [print(f["path"]) for f in json.load(sys.stdin)["files"]]' \
| while IFS= read -r rel; do
    [ -z "$rel" ] && continue
    dest="$MP/plugins/e1/skills/${SKILL_ID}/${rel}"
    mkdir -p "$(dirname "$dest")"
    curl -sS --fail --max-time 30 -H "Authorization: Bearer ${POLARIS_TOKEN}" \
        "${POLARIS_URL}/agent-skills/${SKILL_ID}/${RESOLVED}/${rel}" -o "$dest" \
        || err "bundle file fetch failed: ${rel}"
done
# Adding the marketplace is not enough — the plugin must be INSTALLED for its
# skills to register as /e1:<skill> commands in headless --print mode (this is
# the step a marketplace-add-only entrypoint misses; mirrors cve-triage).
if ! claude plugin list 2>/dev/null | grep -q "e1@germanedge-ai-plugins"; then
    claude plugin marketplace add "$MP" 2>&1 | tail -3 || true
    claude plugin install e1@germanedge-ai-plugins 2>&1 | tail -3 || true
fi

# --- exec the claude invocation the shim handed us ----------------------
exec "$@"
