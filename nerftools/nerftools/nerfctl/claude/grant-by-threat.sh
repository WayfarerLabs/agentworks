#!/usr/bin/env bash
# nerfctl-grant-by-threat -- Allow/deny nerf tools by threat profile
# This is a control-plane tool for operators, not for agents.

set -euo pipefail

if [[ "${BASH_VERSINFO[0]}" -lt 4 ]]; then
  echo "error: grant-by-threat requires bash 4+ (associative arrays). Found bash ${BASH_VERSION}" >&2
  echo "  hint: on macOS, install a newer bash via 'brew install bash'" >&2
  exit 1
fi

SCOPE="user"
PLUGIN_ROOT=""
READ_CEILING=""
WRITE_CEILING=""
FILTER="*"
OUTSIDE="deny"

THREAT_ORDER="none workspace machine remote admin"

usage() {
  cat >&2 <<'EOF'
Usage: nerfctl-grant-by-threat --read <level> --write <level>
       [--filter <glob>] [--outside deny|reset] [--scope user|local]
       [--plugin-root <path>]

  --read <level>         Read ceiling (none|workspace|machine|remote|admin)
  --write <level>        Write ceiling (none|workspace|machine|remote|admin)
  --filter <glob>        Only affect tools matching this name pattern (default: *)
  --outside deny|reset   Action for tools outside the box (default: deny)
  --scope user|local     Settings scope (default: user)
  --plugin-root <path>   Override plugin root (for testing; skips auto-detection)

Allows all tools within the threat box (read <= ceiling AND write <= ceiling).
Tools outside the box are denied or reset based on --outside.

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

_threat_rank() {
  local level="$1"
  local i=0
  for t in $THREAT_ORDER; do
    if [[ "$t" == "$level" ]]; then
      echo "$i"
      return
    fi
    i=$((i + 1))
  done
  echo "error: invalid threat level '$level'" >&2
  exit 1
}

_valid_threat() {
  local level="$1"
  for t in $THREAT_ORDER; do
    if [[ "$t" == "$level" ]]; then
      return 0
    fi
  done
  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --read) READ_CEILING="$2"; shift 2 ;;
    --write) WRITE_CEILING="$2"; shift 2 ;;
    --filter) FILTER="$2"; shift 2 ;;
    --outside) OUTSIDE="$2"; shift 2 ;;
    --scope) SCOPE="$2"; shift 2 ;;
    --plugin-root) PLUGIN_ROOT="$2"; shift 2 ;;
    -h|--help) usage ;;
    -*)  echo "error: unknown option: $1" >&2; usage ;;
    *)   echo "error: unexpected argument: $1" >&2; usage ;;
  esac
done

if [[ -z "$READ_CEILING" ]]; then
  echo "error: --read is required" >&2; usage
fi
if [[ -z "$WRITE_CEILING" ]]; then
  echo "error: --write is required" >&2; usage
fi
if ! _valid_threat "$READ_CEILING"; then
  echo "error: invalid read level '$READ_CEILING' (use: none, workspace, machine, remote, admin)" >&2
  exit 1
fi
if ! _valid_threat "$WRITE_CEILING"; then
  echo "error: invalid write level '$WRITE_CEILING' (use: none, workspace, machine, remote, admin)" >&2
  exit 1
fi
if [[ "$OUTSIDE" != "deny" && "$OUTSIDE" != "reset" ]]; then
  echo "error: --outside must be 'deny' or 'reset'" >&2; exit 1
fi

_require_jq

RESOLVED_ROOT="$(_resolve_plugin_root)"

READ_RANK=$(_threat_rank "$READ_CEILING")
WRITE_RANK=$(_threat_rank "$WRITE_CEILING")

# Find tools with embedded threat metadata
declare -a TOOL_PATHS=()
declare -A TOOL_READ=()
declare -A TOOL_WRITE=()

while IFS= read -r script_path; do
  [[ -z "$script_path" ]] && continue
  tool_name=$(basename "$script_path")

  # Apply filter
  case "$tool_name" in
    $FILTER) ;;
    *) continue ;;
  esac

  # Parse threat metadata from script header
  read_level=""
  write_level=""
  while IFS= read -r line; do
    case "$line" in
      "# nerf:threat:read="*) read_level="${line#*=}" ;;
      "# nerf:threat:write="*) write_level="${line#*=}" ;;
      "set -"*) break ;;
    esac
  done < "$script_path"

  if [[ -z "$read_level" || -z "$write_level" ]]; then
    continue  # skip tools without threat metadata
  fi

  TOOL_PATHS+=("$script_path")
  TOOL_READ["$script_path"]="$read_level"
  TOOL_WRITE["$script_path"]="$write_level"
done < <(find "$RESOLVED_ROOT/skills" -path "*/scripts/*" -type f 2>/dev/null | sort)

if [[ ${#TOOL_PATHS[@]} -eq 0 ]]; then
  echo "No tools found matching filter '$FILTER' under $RESOLVED_ROOT/skills/*/scripts/"
  exit 0
fi

SETTINGS="$(_resolve_settings)"
_ensure_settings_file "$SETTINGS"

UPDATED=$(cat "$SETTINGS")

ALLOWED_COUNT=0
OUTSIDE_COUNT=0

for SCRIPT_PATH in "${TOOL_PATHS[@]}"; do
  TOOL_NAME=$(basename "$SCRIPT_PATH")
  ENTRY="Bash($SCRIPT_PATH:*)"
  STALE_ENTRY="Bash($SCRIPT_PATH)"

  tool_read_rank=$(_threat_rank "${TOOL_READ[$SCRIPT_PATH]}")
  tool_write_rank=$(_threat_rank "${TOOL_WRITE[$SCRIPT_PATH]}")

  # Check current status for annotations (check both new and stale entry forms)
  was=""
  if echo "$UPDATED" | jq -e --arg e "$ENTRY" --arg s "$STALE_ENTRY" \
    '(.permissions.allow // [] | (index($e) != null or index($s) != null))' > /dev/null 2>&1; then
    was="allowed"
  elif echo "$UPDATED" | jq -e --arg e "$ENTRY" --arg s "$STALE_ENTRY" \
    '(.permissions.deny // [] | (index($e) != null or index($s) != null))' > /dev/null 2>&1; then
    was="denied"
  fi

  if [[ $tool_read_rank -le $READ_RANK && $tool_write_rank -le $WRITE_RANK ]]; then
    UPDATED=$(echo "$UPDATED" | jq \
      --arg entry "$ENTRY" \
      --arg stale "$STALE_ENTRY" \
      '
        .permissions //= {}
        | .permissions.allow //= []
        | .permissions.deny //= []
        | .permissions.deny = [.permissions.deny[] | select(. != $entry and . != $stale)]
        | .permissions.allow = [.permissions.allow[] | select(. != $stale)]
        | if (.permissions.allow | index($entry)) == null
          then .permissions.allow += [$entry]
          else .
          end
      ')
    annotation=""
    [[ -n "$was" && "$was" != "allowed" ]] && annotation=" (was: $was)"
    echo "  Allowed: $TOOL_NAME  read:${TOOL_READ[$SCRIPT_PATH]}  write:${TOOL_WRITE[$SCRIPT_PATH]}$annotation"
    ALLOWED_COUNT=$((ALLOWED_COUNT + 1))
  else
    if [[ "$OUTSIDE" == "deny" ]]; then
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
      annotation=""
      [[ -n "$was" && "$was" != "denied" ]] && annotation=" (was: $was)"
      echo "  Denied:  $TOOL_NAME  read:${TOOL_READ[$SCRIPT_PATH]}  write:${TOOL_WRITE[$SCRIPT_PATH]}$annotation"
    else
      UPDATED=$(echo "$UPDATED" | jq \
        --arg entry "$ENTRY" \
        --arg stale "$STALE_ENTRY" \
        '
          .permissions //= {}
          | .permissions.allow //= []
          | .permissions.deny //= []
          | .permissions.allow = [.permissions.allow[] | select(. != $entry and . != $stale)]
          | .permissions.deny = [.permissions.deny[] | select(. != $entry and . != $stale)]
        ')
      annotation=""
      [[ -n "$was" ]] && annotation=" (was: $was)"
      echo "  Reset:   $TOOL_NAME  read:${TOOL_READ[$SCRIPT_PATH]}  write:${TOOL_WRITE[$SCRIPT_PATH]}$annotation"
    fi
    OUTSIDE_COUNT=$((OUTSIDE_COUNT + 1))
  fi
done

echo "$UPDATED" > "$SETTINGS"
echo ""
echo "Allowed $ALLOWED_COUNT tool(s), ${OUTSIDE}ed $OUTSIDE_COUNT tool(s) (scope: $SCOPE)"
