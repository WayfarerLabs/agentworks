#!/usr/bin/env bash

# ============================================================================
# Rulesync update and generate
#
# Runs version-pinned rulesync install --update and generate commands.
# The version is read from .rulesync-version (required).
#
# After each step, fixes executable permissions on skill scripts. Rulesync
# does not preserve the executable bit on install or generate, so we detect
# files with shebangs after install and propagate the bit after generate.
#
# Usage:
#   ./scripts/rulesync-upgen.sh           Install + regenerate all outputs.
#   ./scripts/rulesync-upgen.sh --check   Verify the committed shared-target
#                                         output (copilot, claudecode, codexcli)
#                                         is up to date; exit non-zero on drift.
#                                         Does not write or install.
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

source "$SCRIPT_DIR/_common.sh"
require_npm_package_runner

RULESYNC_VERSION=$(read_version_file .rulesync-version "" "$REPO_ROOT")

INSTALLED_SKILLS_DIR="$REPO_ROOT/.rulesync/skills"
CLAUDE_SKILLS_DIR="$REPO_ROOT/.claude/skills"

# The shared targets whose generated output is committed and drift-checked are
# exactly rulesync.jsonc's "targets". Derive them from that file so this script
# and the config cannot diverge (a divergence would let a newly added shared
# target's committed output escape the drift check). The array is single-line
# in rulesync.jsonc; extract its contents and strip spaces/quotes.
SHARED_TARGETS=$(sed -n 's/.*"targets"[[:space:]]*:[[:space:]]*\[\([^]]*\)\].*/\1/p' "$REPO_ROOT/rulesync.jsonc" | tr -d ' "')
if [[ -z "$SHARED_TARGETS" ]]; then
    echo "Error: could not parse \"targets\" from rulesync.jsonc (is the array on one line?)." >&2
    exit 1
fi

cd "$REPO_ROOT"

# --- Arg parsing ---
#
# `--check` mode verifies the committed shared-target output. We always pass
# `-t $SHARED_TARGETS` so the dev's personal rulesync.local.jsonc targets don't
# enter the picture; only the shared, committed output is what's checked here
# (and the same script runs the same invocation in CI, so the local and CI
# checks cannot drift).
CHECK_ONLY=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --check) CHECK_ONLY=1 ;;
        -h|--help)
            cat <<EOF
Usage: $0 [--check]

  (no flag)   Install + regenerate all outputs (including the committed
              shared-target output: copilot, claudecode, codexcli).
  --check     Verify the committed shared-target output is up to date; exit
              non-zero on drift. Does not write or install.
EOF
            exit 0
            ;;
        *)
            echo "Error: unknown argument '$1'. Run with --help for usage." >&2
            exit 1
            ;;
    esac
    shift
done

if [[ $CHECK_ONLY -eq 1 ]]; then
    echo "Checking committed output for $SHARED_TARGETS (v$RULESYNC_VERSION)..."
    run_npm_package rulesync@"$RULESYNC_VERSION" generate -t "$SHARED_TARGETS" --check
    exit
fi

# --- Install ---

echo "Running rulesync install --update (v$RULESYNC_VERSION)..."
run_npm_package rulesync@"$RULESYNC_VERSION" install --update

# Workaround: rulesync does not preserve the executable bit on installed files.
# Find files with a shebang and make them executable.
echo "Restoring executable permissions on installed skill scripts..."
if [[ -d "$INSTALLED_SKILLS_DIR" ]]; then
    while IFS= read -r file; do
        first_two=$(head -c 2 "$file")
        if [[ ! -x "$file" && "$first_two" == '#!' ]]; then
            chmod +x "$file"
            echo "  +x $file"
        fi
    done < <(find "$INSTALLED_SKILLS_DIR" -type f)
fi

# --- Generate ---

echo "Running rulesync generate (v$RULESYNC_VERSION)..."
run_npm_package rulesync@"$RULESYNC_VERSION" generate

# rulesync.local.jsonc's `targets` field replaces (not unions) the shared
# config's, so when a dev's local targets exclude a shared target, its
# committed output above does not get refreshed. Force-regenerate all shared
# targets here so the CI drift check stays green regardless of personal choices.
echo "Regenerating committed output for $SHARED_TARGETS (v$RULESYNC_VERSION)..."
run_npm_package rulesync@"$RULESYNC_VERSION" generate -t "$SHARED_TARGETS"

# Workaround: rulesync generate also drops the executable bit.
# Propagate from installed skills to generated .claude/skills output.
echo "Propagating executable permissions to generated skill scripts..."
if [[ -d "$INSTALLED_SKILLS_DIR" && -d "$CLAUDE_SKILLS_DIR" ]]; then
    while IFS= read -r src_file; do
        [[ -x "$src_file" ]] || continue
        rel="${src_file#"$INSTALLED_SKILLS_DIR"/}"
        # Installed remote skills live under .curated/ but generate flattens
        rel="${rel#.curated/}"
        target_file="$CLAUDE_SKILLS_DIR/$rel"
        if [[ -f "$target_file" && ! -x "$target_file" ]]; then
            chmod +x "$target_file"
            echo "  +x $target_file"
        fi
    done < <(find "$INSTALLED_SKILLS_DIR" -type f)
fi

echo "Done."
