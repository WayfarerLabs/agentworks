#!/usr/bin/env bash
# nerfctl-grant-deny -- Deny nerf tools entirely
# This is a control-plane tool for operators, not for agents.

set -euo pipefail

SCOPE="user"
PLUGIN_ROOT=""
PATTERN=""

usage() {
  cat >&2 <<'EOF'
Usage: nerfctl-grant-deny <pattern> [--scope user|local] [--plugin-root <path>]

  <pattern>              Tool name or glob pattern (e.g. nerf-git-commit or nerf-git-*)
  --scope user|local     Settings scope (default: user)
  --plugin-root <path>   Override plugin root (for testing; skips auto-detection)

Finds all matching tool scripts under the plugin root and adds deny entries
for each, removing any matching allow entries.

The plugin root is auto-detected from CLAUDE_PLUGIN_ROOT or the script's
own location. Use --plugin-root only for testing.

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

_resolve_plugin_root() {
  if [[ -n "$PLUGIN_ROOT" ]]; then
    echo "$PLUGIN_ROOT"
    return
  fi

  local resolved=""

  if [[ -n "${CLAUDE_PLUGIN_ROOT:-}" ]]; then
    local canonical
    canonical=$(realpath "$CLAUDE_PLUGIN_ROOT" 2>/dev/null || echo "")
    if [[ -n "$canonical" && "$canonical" == "$HOME/.claude/plugins/"* ]]; then
      resolved="$canonical"
    else
      echo "warning: CLAUDE_PLUGIN_ROOT '$CLAUDE_PLUGIN_ROOT' is not under ~/.claude/plugins/; deriving from script location" >&2
    fi
  else
    echo "warning: CLAUDE_PLUGIN_ROOT not set; deriving from script location" >&2
  fi

  if [[ -z "$resolved" ]]; then
    local script_dir
    script_dir=$(cd "$(dirname "$(realpath "$0")")" && pwd)
    resolved=$(cd "$script_dir/.." && pwd)
  fi

  if [[ "$resolved" != "$HOME/.claude/plugins/"* ]]; then
    echo "error: resolved plugin root '$resolved' is not under ~/.claude/plugins/" >&2
    echo "  hint: if testing, use --plugin-root to override" >&2
    exit 1
  fi

  if [[ ! -d "$resolved/.claude-plugin" ]]; then
    echo "error: '$resolved' does not contain .claude-plugin/ -- not a valid plugin root" >&2
    exit 1
  fi
  if [[ ! -d "$resolved/skills" ]]; then
    echo "error: '$resolved' does not contain skills/ -- not a valid plugin root" >&2
    exit 1
  fi

  echo "$resolved"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope) SCOPE="$2"; shift 2 ;;
    --plugin-root) PLUGIN_ROOT="$2"; shift 2 ;;
    -h|--help) usage ;;
    -*) echo "error: unknown option: $1" >&2; usage ;;
    *)
      if [[ -z "$PATTERN" ]]; then
        PATTERN="$1"; shift
      else
        echo "error: unexpected argument: $1" >&2; usage
      fi
      ;;
  esac
done

if [[ -z "$PATTERN" ]]; then
  echo "error: <pattern> is required" >&2; usage
fi

_require_jq

RESOLVED_ROOT="$(_resolve_plugin_root)"

mapfile -t MATCHES < <(find "$RESOLVED_ROOT/skills" -path "*/scripts/$PATTERN" -type f 2>/dev/null | sort)

if [[ ${#MATCHES[@]} -eq 0 ]]; then
  echo "error: no tools matching '$PATTERN' found under $RESOLVED_ROOT/skills/*/scripts/" >&2
  echo "hint: use 'nerf-git-*' to match a package, or check tool names in the nerf skills" >&2
  exit 1
fi

SETTINGS="$(_resolve_settings)"
_ensure_settings_file "$SETTINGS"

UPDATED=$(cat "$SETTINGS")
for SCRIPT_PATH in "${MATCHES[@]}"; do
  TOOL_NAME=$(basename "$SCRIPT_PATH")
  ENTRY="Bash($SCRIPT_PATH:*)"
  STALE_ENTRY="Bash($SCRIPT_PATH)"

  UPDATED=$(echo "$UPDATED" | jq \
    --arg entry "$ENTRY" \
    --arg stale "$STALE_ENTRY" \
    '
      .permissions //= {}
      | .permissions.allow //= []
      | .permissions.deny //= []
      | .permissions.allow = [.permissions.allow[] | select(. != $entry and . != $stale)]
      | .permissions.deny = [.permissions.deny[] | select(. != $stale)]
      | if (.permissions.deny | index($entry)) == null
        then .permissions.deny += [$entry]
        else .
        end
    ')
  echo "  Denied: $TOOL_NAME"
  echo "    $ENTRY"
done

echo "$UPDATED" > "$SETTINGS"
echo ""
echo "Denied ${#MATCHES[@]} tool(s) (scope: $SCOPE)"
