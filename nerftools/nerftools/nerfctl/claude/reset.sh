#!/usr/bin/env bash
# nerfctl-claude-reset -- Remove a nerf tool from Claude Code settings entirely
# This is a control-plane tool for operators, not for agents.

set -euo pipefail

SCOPE="user"
TOOL=""

usage() {
  cat >&2 <<'EOF'
Usage: nerfctl-claude-reset <tool> [--scope user|local]

  <tool>              Name of the nerf tool to reset (e.g. nerf-git-push-origin)
  --scope user|local  Settings scope (default: user)
                        user:  ~/.claude/settings.json
                        local: .claude/settings.local.json

Removes all permission entries (both absolute path and bare name) from both
allow and deny lists. After reset, framework defaults apply.

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
  case "$SCOPE" in
    user)  echo "$HOME/.claude/settings.json" ;;
    local)
      if [[ ! -d ".claude" ]]; then
        echo "error: .claude/ not found in current directory" >&2
        exit 1
      fi
      echo ".claude/settings.local.json"
      ;;
    *) echo "error: unknown scope '$SCOPE' (use 'user' or 'local')" >&2; exit 1 ;;
  esac
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope) SCOPE="$2"; shift 2 ;;
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
  echo "Reset: no settings file at $SETTINGS"
  exit 0
fi

ENTRY_ABS='Bash($AGENTWORKS_NERF_BIN/'"$TOOL"')'
ENTRY_BARE="Bash($TOOL)"

UPDATED=$(jq \
  --arg abs "$ENTRY_ABS" \
  --arg bare "$ENTRY_BARE" \
  '
    .permissions //= {}
    | .permissions.allow //= []
    | .permissions.deny //= []
    | .permissions.allow = [.permissions.allow[] | select(. != $abs and . != $bare)]
    | .permissions.deny = [.permissions.deny[] | select(. != $abs and . != $bare)]
  ' "$SETTINGS")

echo "$UPDATED" > "$SETTINGS"
echo "Reset: $TOOL removed from $SETTINGS (scope: $SCOPE)"
