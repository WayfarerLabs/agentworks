#!/usr/bin/env bash
# nerfctl-claude-deny -- Deny a nerf tool permission in Claude Code settings
# This is a control-plane tool for operators, not for agents.

set -euo pipefail

SETTINGS_FILE=""
TOOL=""

usage() {
  cat >&2 <<'EOF'
Usage: nerfctl-claude-deny <tool> [--settings <path>]

  <tool>              Name of the nerf tool to deny (e.g. nerf-git-push-origin)
  --settings <path>   Path to settings file (default: .claude/settings.local.json)

Adds Bash(<tool>) to permissions.deny and removes it from permissions.allow
if present. Operates on .claude/settings.local.json in the current directory
by default. Use --settings to target a different file.

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

_ensure_settings_file() {
  local file="$1"
  if [[ ! -f "$file" ]]; then
    echo '{}' > "$file"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --settings) SETTINGS_FILE="$2"; shift 2 ;;
    -h|--help) usage ;;
    -*) echo "error: unknown option: $1" >&2; usage ;;
    *)
      if [[ -z "$TOOL" ]]; then
        TOOL="$1"; shift
      else
        echo "error: unexpected argument: $1" >&2; usage
      fi
      ;;
  esac
done

if [[ -z "$TOOL" ]]; then
  echo "error: <tool> is required" >&2; usage
fi

_require_jq

SETTINGS="$(_resolve_settings)"
_ensure_settings_file "$SETTINGS"

ENTRY="Bash($TOOL)"

# Remove from allow if present, then add to deny if not already there
UPDATED=$(jq \
  --arg entry "$ENTRY" \
  '
    .permissions //= {}
    | .permissions.allow //= []
    | .permissions.deny //= []
    | .permissions.allow = [.permissions.allow[] | select(. != $entry)]
    | if (.permissions.deny | index($entry)) == null
      then .permissions.deny += [$entry]
      else .
      end
  ' "$SETTINGS")

echo "$UPDATED" > "$SETTINGS"
echo "Denied: $ENTRY in $SETTINGS"
