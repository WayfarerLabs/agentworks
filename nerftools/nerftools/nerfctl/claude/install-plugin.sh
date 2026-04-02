#!/usr/bin/env bash
# nerfctl-install-plugin -- Install the nerftools Claude Code plugin
# This is a control-plane tool for operators, not for agents.

set -euo pipefail

SCOPE="user"

usage() {
  cat >&2 <<'EOF'
Usage: nerfctl-install-plugin [--scope user|local]

  --scope user|local  Installation scope (default: user)

Registers the nerftools local marketplace and installs the nerftools plugin
so Claude Code can discover nerf tool skills. Locates the plugin directory
relative to this script's location.

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

# Resolve the plugin directory from this script's location
# Script is at: <plugin-root>/scripts/nerfctl-claude-install-plugin
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

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

echo "Done. Nerftools plugin installed from $PLUGIN_DIR"
