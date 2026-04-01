#!/usr/bin/env bash
# nerfctl-claude-install-plugin -- Install the nerftools Claude Code plugin
# This is a control-plane tool for operators, not for agents.

set -euo pipefail

SCOPE="user"

usage() {
  cat >&2 <<'EOF'
Usage: nerfctl-claude-install-plugin [--scope user|local]

  --scope user|local  Installation scope (default: user)

Registers the nerftools local marketplace and installs the nerftools plugin
so Claude Code can discover nerf tool skills. Uses the AGENTWORKS_NERF_HOME
environment variable to locate the plugin.

Requires the claude CLI.
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope) SCOPE="$2"; shift 2 ;;
    -h|--help) usage ;;
    -*) echo "error: unknown option: $1" >&2; usage ;;
    *) echo "error: unexpected argument: $1" >&2; usage ;;
  esac
done

if [[ -z "${AGENTWORKS_NERF_HOME:-}" ]]; then
  echo "error: AGENTWORKS_NERF_HOME is not set" >&2
  echo "hint: nerf tools must be installed via 'agentworks vm init' first" >&2
  exit 1
fi

PLUGIN_DIR="$AGENTWORKS_NERF_HOME"

if [[ ! -f "$PLUGIN_DIR/.claude-plugin/marketplace.json" ]]; then
  echo "error: no marketplace manifest at $PLUGIN_DIR/.claude-plugin/marketplace.json" >&2
  echo "hint: reinit the VM to generate the plugin manifest" >&2
  exit 1
fi

if ! command -v claude > /dev/null 2>&1; then
  echo "error: claude CLI is required but not installed" >&2
  exit 1
fi

# Add the local marketplace (idempotent -- claude handles duplicates)
echo "Adding nerftools marketplace..."
claude plugin marketplace add "$PLUGIN_DIR"

# Install the plugin
echo "Installing nerftools plugin (scope: $SCOPE)..."
claude plugin install "nerftools@agentworks-nerf-local" --scope "$SCOPE"

echo "Done. Nerftools plugin installed."
