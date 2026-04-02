#!/usr/bin/env bash
# nerfctl-grant-list -- List nerf tool permissions in Claude Code settings
# This is a control-plane tool for operators, not for agents.

set -euo pipefail

SCOPE=""

usage() {
  cat >&2 <<'EOF'
Usage: nerfctl-grant-list [--scope user|local]

  --scope user|local  Show only this scope (default: show all scopes)

Lists all nerf-related entries from permissions.allow and permissions.deny.
Shows all scopes unless a specific scope is requested.

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

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope) SCOPE="$2"; shift 2 ;;
    -h|--help) usage ;;
    -*) echo "error: unknown option: $1" >&2; usage ;;
    *) echo "error: unexpected argument: $1" >&2; usage ;;
  esac
done

_require_jq

_list_scope() {
  local scope_name="$1"
  local settings_file="$2"

  if [[ ! -f "$settings_file" ]]; then
    return
  fi

  local allow deny
  allow=$(jq -r '
    (.permissions.allow // [])[]
    | select(test("^Bash\\(.*nerf(ctl)?-"))
  ' "$settings_file" 2>/dev/null)

  deny=$(jq -r '
    (.permissions.deny // [])[]
    | select(test("^Bash\\(.*nerf(ctl)?-"))
  ' "$settings_file" 2>/dev/null)

  if [[ -z "$allow" && -z "$deny" ]]; then
    return
  fi

  echo "$scope_name ($settings_file):"
  if [[ -n "$allow" ]]; then
    echo "  Allowed:"
    while IFS= read -r entry; do
      echo "    $entry"
    done <<< "$allow"
  fi
  if [[ -n "$deny" ]]; then
    echo "  Denied:"
    while IFS= read -r entry; do
      echo "    $entry"
    done <<< "$deny"
  fi
  echo ""
}

FOUND=0

if [[ -z "$SCOPE" || "$SCOPE" == "user" ]]; then
  BEFORE=$FOUND
  output=$(_list_scope "user" "$HOME/.claude/settings.json")
  if [[ -n "$output" ]]; then
    echo "$output"
    FOUND=1
  fi
fi

if [[ -z "$SCOPE" || "$SCOPE" == "local" ]]; then
  if [[ -d ".claude" ]]; then
    output=$(_list_scope "local" ".claude/settings.local.json")
    if [[ -n "$output" ]]; then
      echo "$output"
      FOUND=1
    fi
  fi
fi

if [[ "$FOUND" -eq 0 ]]; then
  if [[ -n "$SCOPE" ]]; then
    echo "No nerf tool permissions found (scope: $SCOPE)"
  else
    echo "No nerf tool permissions found in any scope"
  fi
fi
