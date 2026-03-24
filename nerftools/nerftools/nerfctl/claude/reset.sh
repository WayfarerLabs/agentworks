#!/usr/bin/env bash
# nerfctl-claude-reset -- Remove a nerf tool from Claude Code settings entirely
# This is a control-plane tool for operators, not for agents.

set -euo pipefail

SETTINGS_FILE=""
TOOL=""

usage() {
  cat >&2 <<'EOF'
Usage: nerfctl-claude-reset <tool> [--settings <path>]

  <tool>              Name of the nerf tool to reset (e.g. nerf-git-push-origin)
  --settings <path>   Path to settings file (default: .claude/settings.local.json)

Removes Bash(<tool>) from both permissions.allow and permissions.deny. After
reset the tool has no explicit permission entry -- framework defaults apply.

Operates on .claude/settings.local.json in the current directory by default.
Use --settings to target a different file.

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

if [[ ! -f "$SETTINGS" ]]; then
  echo "Reset: Bash($TOOL) not found in $SETTINGS (file does not exist)"
  exit 0
fi

ENTRY="Bash($TOOL)"

UPDATED=$(jq \
  --arg entry "$ENTRY" \
  '
    .permissions //= {}
    | .permissions.allow //= []
    | .permissions.deny //= []
    | .permissions.allow = [.permissions.allow[] | select(. != $entry)]
    | .permissions.deny = [.permissions.deny[] | select(. != $entry)]
  ' "$SETTINGS")

echo "$UPDATED" > "$SETTINGS"
echo "Reset: $ENTRY removed from $SETTINGS"
