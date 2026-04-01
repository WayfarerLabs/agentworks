#!/usr/bin/env bash
# nerfctl-claude-grant -- Grant a nerf tool permission in Claude Code settings
# This is a control-plane tool for operators, not for agents.

set -euo pipefail

SCOPE="user"
TOOL=""

usage() {
  cat >&2 <<'EOF'
Usage: nerfctl-claude-grant <tool> [--scope user|local]

  <tool>              Name of the nerf tool to grant (e.g. nerf-git-commit)
  --scope user|local  Settings scope (default: user)
                        user:  ~/.claude/settings.json
                        local: .claude/settings.local.json

Adds permission entries for both the absolute path ($AGENTWORKS_NERF_BIN/<tool>)
and the bare command name, and removes any matching deny entries.

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

_ensure_settings_file() {
  local file="$1"
  local dir
  dir=$(dirname "$file")
  [[ -d "$dir" ]] || mkdir -p "$dir"
  [[ -f "$file" ]] || echo '{}' > "$file"
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
_ensure_settings_file "$SETTINGS"

ENTRY_ABS='Bash($AGENTWORKS_NERF_BIN/'"$TOOL"')'
ENTRY_BARE="Bash($TOOL)"

# Remove from deny, add to allow (both entries)
UPDATED=$(jq \
  --arg abs "$ENTRY_ABS" \
  --arg bare "$ENTRY_BARE" \
  '
    .permissions //= {}
    | .permissions.allow //= []
    | .permissions.deny //= []
    | .permissions.deny = [.permissions.deny[] | select(. != $abs and . != $bare)]
    | if (.permissions.allow | index($abs)) == null
      then .permissions.allow += [$abs]
      else .
      end
    | if (.permissions.allow | index($bare)) == null
      then .permissions.allow += [$bare]
      else .
      end
  ' "$SETTINGS")

echo "$UPDATED" > "$SETTINGS"
echo "Granted: $TOOL (scope: $SCOPE)"
echo "  $ENTRY_ABS"
echo "  $ENTRY_BARE"
