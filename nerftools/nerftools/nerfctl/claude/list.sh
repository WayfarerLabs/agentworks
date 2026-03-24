#!/usr/bin/env bash
# nerfctl-claude-list -- List nerf tool permissions in Claude Code settings
# This is a control-plane tool for operators, not for agents.

set -euo pipefail

SETTINGS_FILE=""

usage() {
  cat >&2 <<'EOF'
Usage: nerfctl-claude-list [--settings <path>]

  --settings <path>   Path to settings file (default: .claude/settings.local.json)

Lists all Bash(nerf-*) and Bash(nerfctl-*) entries from permissions.allow and
permissions.deny. Operates on .claude/settings.local.json in the current
directory by default. Use --settings to target a different file.

Requires jq.
EOF
  exit 1
}

_require_jq() {
  if ! command -v jq > /dev/null 2>&1; then
    echo "error: jq is required but not installed" >&2
    exit 1
  fi
}

_resolve_settings() {
  if [[ -n "$SETTINGS_FILE" ]]; then
    echo "$SETTINGS_FILE"
    return
  fi
  if [[ ! -d ".claude" ]]; then
    echo "error: .claude/ not found in current directory. Run from your workspace root or use --settings." >&2
    exit 1
  fi
  echo ".claude/settings.local.json"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --settings) SETTINGS_FILE="$2"; shift 2 ;;
    -h|--help) usage ;;
    -*) echo "error: unknown option: $1" >&2; usage ;;
    *) echo "error: unexpected argument: $1" >&2; usage ;;
  esac
done

_require_jq

SETTINGS="$(_resolve_settings)"

if [[ ! -f "$SETTINGS" ]]; then
  echo "No settings file found at $SETTINGS"
  exit 0
fi

# Extract nerf-* and nerfctl-* entries from allow and deny lists
ALLOW=$(jq -r '
  (.permissions.allow // [])[]
  | select(test("^Bash\\((nerf-|nerfctl-)"))
' "$SETTINGS")

DENY=$(jq -r '
  (.permissions.deny // [])[]
  | select(test("^Bash\\((nerf-|nerfctl-)"))
' "$SETTINGS")

if [[ -z "$ALLOW" && -z "$DENY" ]]; then
  echo "No nerf tool permissions found in $SETTINGS"
  exit 0
fi

echo "Settings: $SETTINGS"
echo ""

if [[ -n "$ALLOW" ]]; then
  echo "Allowed:"
  while IFS= read -r entry; do
    echo "  $entry"
  done <<< "$ALLOW"
fi

if [[ -n "$DENY" ]]; then
  echo "Denied:"
  while IFS= read -r entry; do
    echo "  $entry"
  done <<< "$DENY"
fi
