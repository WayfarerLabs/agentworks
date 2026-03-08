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
# Usage: ./ops/scripts/rulesync-upgen.bash
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

source "$SCRIPT_DIR/_common.bash"
require_npm_package_runner

RULESYNC_VERSION=$(read_version_file .rulesync-version "" "$REPO_ROOT")

INSTALLED_SKILLS_DIR="$REPO_ROOT/.rulesync/skills"
CLAUDE_SKILLS_DIR="$REPO_ROOT/.claude/skills"

cd "$REPO_ROOT"

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
