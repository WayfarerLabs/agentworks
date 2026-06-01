#!/usr/bin/env bash

# ============================================================================
# lint-files.sh: run the repository's file-quality linters at the versions CI uses.
#
# Covers cspell, markdownlint-cli2, and prettier. Each is pinned in its own
# `.<tool>-version` file (`.cspell-version`, `.markdownlint-cli2-version`,
# `.prettier-version`). This script reads those same files and invokes each
# tool via the local npm package runner.
#
# This script intentionally does NOT cover rulesync drift; that's a different
# concern (generated artifact freshness, not file quality) and lives in
# `./scripts/rulesync-upgen.sh --check`.
#
# Usage:
#   ./scripts/lint-files.sh          Check only. Mirrors what CI checks.
#   ./scripts/lint-files.sh --fix    Auto-fix where each tool can, then re-check.
#                              cspell cannot auto-fix; remaining unknown
#                              words must be corrected by hand or added
#                              to .cspell.json.
# ============================================================================

# Intentionally NOT using `set -e`: we want every checker to run so contributors
# see every problem in one pass, then aggregate failures via $FAIL at the end.
# Do not "fix" this to -euo pipefail without also restructuring the check loop.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

source "$SCRIPT_DIR/_common.sh"

# --- Arg parsing ---

FIX=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --fix) FIX=1 ;;
        -h|--help)
            sed -n '/^# Usage:/,/^# ====/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//; /^====/d'
            exit 0
            ;;
        *)
            echo "Error: unknown argument '$1'. Run with --help for usage." >&2
            exit 1
            ;;
    esac
    shift
done

# --- Resolve npm package runner once ---
#
# Use an array variable so each tool invocation can splice it in cleanly.
# Mirrors `_common.sh`'s detection (kept inline instead of using the shared
# `run_npm_package` shell function for readability at the call sites).
if command -v bunx >/dev/null 2>&1; then
    PKGRUN=(bunx)
elif command -v pnpm >/dev/null 2>&1; then
    PKGRUN=(pnpm dlx)
elif command -v npx >/dev/null 2>&1; then
    PKGRUN=(npx -y)
else
    echo "Error: no npm package runner found (tried bunx, pnpm dlx, npx)." >&2
    echo "Install one of: bun, pnpm, or Node.js." >&2
    exit 1
fi

cd "$REPO_ROOT"

# read_version_file errors on missing OR empty files; explicit `|| exit 1` makes
# the failure propagate without `set -e` (we deliberately don't use -e so that all
# checks below can run and aggregate failures in $FAIL).
CSPELL_VERSION=$(read_version_file .cspell-version "" "$REPO_ROOT") || exit 1
MDLINT_VERSION=$(read_version_file .markdownlint-cli2-version "" "$REPO_ROOT") || exit 1
PRETTIER_VERSION=$(read_version_file .prettier-version "" "$REPO_ROOT") || exit 1

FAIL=0

# Prettier 3.x does NOT respect .gitignore by default (changed in 3.0), so we
# pass --ignore-path explicitly. The other tools (cspell, markdownlint-cli2)
# have gitignore: true / useGitignore: true set in their own config files.
PRETTIER_IGNORES=(--ignore-path .gitignore --ignore-path .prettierignore)
# Prettier scope: markdown plus structured-config formats it natively supports.
# Toml is intentionally omitted because prettier doesn't have a native TOML
# parser. JSON/JSONC/YAML get the same prose-wrap-style consistency markdown gets.
PRETTIER_GLOBS=('**/*.md' '**/*.json' '**/*.jsonc' '**/*.yaml' '**/*.yml')

# --- Fix pass ---

if [[ $FIX -eq 1 ]]; then
    echo "--- prettier --write ---"
    "${PKGRUN[@]}" prettier@"$PRETTIER_VERSION" --write "${PRETTIER_IGNORES[@]}" "${PRETTIER_GLOBS[@]}"

    echo ""
    echo "--- markdownlint-cli2 --fix ---"
    # Deliberate `|| true`: markdownlint exits non-zero when it encounters
    # rules it cannot auto-fix, and we want the script to continue to the
    # re-check pass below so the user sees the unfixable issues clearly
    # rather than failing here mid-fix.
    "${PKGRUN[@]}" markdownlint-cli2@"$MDLINT_VERSION" --fix '**/*.md' || true
fi

# --- Check pass (always runs) ---

echo ""
echo "=== prettier --check ==="
if "${PKGRUN[@]}" prettier@"$PRETTIER_VERSION" --check "${PRETTIER_IGNORES[@]}" "${PRETTIER_GLOBS[@]}"; then
    echo "  ok"
else
    FAIL=1
fi

echo ""
echo "=== markdownlint-cli2 ==="
if "${PKGRUN[@]}" markdownlint-cli2@"$MDLINT_VERSION" '**/*.md'; then
    echo "  ok"
else
    FAIL=1
fi

echo ""
echo "=== cspell ==="
if "${PKGRUN[@]}" cspell@"$CSPELL_VERSION" --no-progress \
    '**/*.md' '**/*.py' '**/*.yaml' '**/*.yml' '**/*.toml'; then
    echo "  ok"
else
    echo ""
    echo "  cspell flags cannot be auto-fixed. For each unknown word:"
    echo "    - correct the spelling, OR"
    echo "    - add the word to .cspell.json's \"words\" list."
    FAIL=1
fi

echo ""
if [[ $FAIL -eq 0 ]]; then
    echo "All checks passed."
else
    echo "One or more checks failed. See above."
    exit 1
fi
