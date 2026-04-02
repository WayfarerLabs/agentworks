#!/usr/bin/env bash
# nerfctl-claude-list -- List nerf tool permissions in Claude Code settings
# This is a control-plane tool for operators, not for agents.

set -euo pipefail

SCOPE="user"

usage() {
  cat >&2 <<'EOF'
Usage: nerfctl-claude-list [--scope user|local]

  --scope user|local  Settings scope (default: user)
                        user:  ~/.claude/settings.json
                        local: .claude/settings.local.json

Lists all nerf-related entries from permissions.allow and permissions.deny.
Matches both absolute path ($AGENTWORKS_NERF_BIN/...) and bare command entries.

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
    *) echo "error: unexpected argument: $1" >&2; usage ;;
  esac
done

_require_jq

SETTINGS="$(_resolve_settings)"

if [[ ! -f "$SETTINGS" ]]; then
  echo "No settings file at $SETTINGS"
  exit 0
fi

# Match entries containing nerf- or nerfctl- anywhere in the Bash() wrapper
ALLOW=$(jq -r '
  (.permissions.allow // [])[]
  | select(test("^Bash\\(.*nerf(ctl)?-"))
' "$SETTINGS")

DENY=$(jq -r '
  (.permissions.deny // [])[]
  | select(test("^Bash\\(.*nerf(ctl)?-"))
' "$SETTINGS")

if [[ -z "$ALLOW" && -z "$DENY" ]]; then
  echo "No nerf tool permissions found in $SETTINGS (scope: $SCOPE)"
  exit 0
fi

echo "Settings: $SETTINGS (scope: $SCOPE)"
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
