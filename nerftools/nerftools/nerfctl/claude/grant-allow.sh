#!/usr/bin/env bash
# nerfctl-grant-allow -- Allow nerf tools without prompting
# This is a control-plane tool for operators, not for agents.

set -euo pipefail

SCOPE="user"
PLUGIN_ROOT=""
PATTERN=""

usage() {
  cat >&2 <<'EOF'
Usage: nerfctl-grant-allow <plugin-root> <pattern> [--scope user|local]

  <plugin-root>       Absolute path to the plugin root (passed by the skill)
  <pattern>           Tool name or glob pattern (e.g. nerf-git-commit or nerf-git-*)
  --scope user|local  Settings scope (default: user)

Finds all matching tool scripts under the plugin root and adds permission
entries for each to the allow list.

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
      if [[ -z "$PLUGIN_ROOT" ]]; then
        PLUGIN_ROOT="$1"; shift
      elif [[ -z "$PATTERN" ]]; then
        PATTERN="$1"; shift
      else
        echo "error: unexpected argument: $1" >&2; usage
      fi
      ;;
  esac
done

if [[ -z "$PLUGIN_ROOT" || -z "$PATTERN" ]]; then
  echo "error: <plugin-root> and <pattern> are required" >&2; usage
fi

_require_jq

# Find matching tool scripts
mapfile -t MATCHES < <(find "$PLUGIN_ROOT/skills" -path "*/scripts/$PATTERN" -type f 2>/dev/null | sort)

if [[ ${#MATCHES[@]} -eq 0 ]]; then
  echo "error: no tools matching '$PATTERN' found under $PLUGIN_ROOT/skills/*/scripts/" >&2
  echo "hint: use 'nerf-git-*' to match a family, or check tool names in the nerf skills" >&2
  exit 1
fi

SETTINGS="$(_resolve_settings)"
_ensure_settings_file "$SETTINGS"

UPDATED=$(cat "$SETTINGS")
for SCRIPT_PATH in "${MATCHES[@]}"; do
  TOOL_NAME=$(basename "$SCRIPT_PATH")
  ENTRY="Bash($SCRIPT_PATH)"

  UPDATED=$(echo "$UPDATED" | jq \
    --arg entry "$ENTRY" \
    '
      .permissions //= {}
      | .permissions.allow //= []
      | .permissions.deny //= []
      | .permissions.deny = [.permissions.deny[] | select(. != $entry)]
      | if (.permissions.allow | index($entry)) == null
        then .permissions.allow += [$entry]
        else .
        end
    ')
  echo "  Allowed: $TOOL_NAME"
  echo "    $ENTRY"
done

echo "$UPDATED" > "$SETTINGS"
echo ""
echo "Allowed ${#MATCHES[@]} tool(s) (scope: $SCOPE)"
