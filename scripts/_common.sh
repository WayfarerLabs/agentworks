#!/usr/bin/env bash

# ============================================================================
# Shared helpers for workspace scripts.
# Source this file, do not execute it directly.
#
# Usage: source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
# ============================================================================

# Check that a supported npm package runner is available.
# Call this early to fail fast with a clear message.
require_npm_package_runner() {
    if command -v bunx &>/dev/null; then return 0; fi
    if command -v pnpm &>/dev/null; then return 0; fi
    if command -v npx &>/dev/null; then return 0; fi
    echo "Error: no npm package runner found (tried bunx, pnpm dlx, npx)." >&2
    echo "Install one of: bun, pnpm, or Node.js." >&2
    return 1
}

# Run an npm package via the best available package runner.
# Detects bunx, pnpm dlx, or npx (in that order).
#
# Usage: run_npm_package <package[@version]> [args...]
# Example: run_npm_package "rulesync@7.14.0" generate
run_npm_package() {
    if command -v bunx &>/dev/null; then
        bunx "$@"
    elif command -v pnpm &>/dev/null; then
        pnpm dlx "$@"
    elif command -v npx &>/dev/null; then
        npx -y "$@"
    else
        echo "Error: no npm package runner found (tried bunx, pnpm dlx, npx)." >&2
        echo "Install one of: bun, pnpm, or Node.js." >&2
        return 1
    fi
}

# Read a version string from a file.
# Searches from the given directory upward to the filesystem root.
# If required (default), fails when the file is not found. Pass a default
# value as the second argument to fall back instead.
#
# Usage: read_version_file <filename> [default] [start_dir]
# Example: read_version_file .rulesync-version          # required, fails if missing
# Example: read_version_file .node-version 22.0.0       # optional, falls back
read_version_file() {
    local filename="$1"
    local default="${2:-}"
    local dir="${3:-$PWD}"

    while [[ "$dir" != "/" ]]; do
        if [[ -f "$dir/$filename" ]]; then
            local content
            content=$(head -1 "$dir/$filename" | tr -d '[:space:]')
            if [[ -z "$content" ]]; then
                echo "Error: $dir/$filename is empty." >&2
                return 1
            fi
            echo "$content"
            return 0
        fi
        dir="$(dirname "$dir")"
    done

    if [[ -n "$default" ]]; then
        echo "Warning: $filename not found. Defaulting to $default." >&2
        echo "$default"
        return 0
    fi

    echo "Error: $filename not found (searched upward from ${3:-$PWD})." >&2
    return 1
}
