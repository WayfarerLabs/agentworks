#!/usr/bin/env bash
# Example harness: ISOLATED-HOME CLI drive.
#
# Pattern: run the real `agw` CLI end to end against a throwaway HOME, so it
# never touches an operator's real ~/.config/agentworks (config.toml,
# resources/, agentworks.db). Every agentworks path is derived from
# Path.home(), so pointing HOME at a fresh temp directory is enough to
# sandbox a full run: first-run behavior, `agw config init`, resource
# creation, etc. can all be exercised without state-mutation risk.
#
# This is an illustrative example, not a maintained test suite. Adapt the
# command sequence to whatever CLI surface you are actually driving.
set -euo pipefail

# Run from the cli/ directory of an agentworks checkout so `uv run agw`
# resolves against that checkout's editable install.
CLI_DIR="${AGW_CLI_DIR:-$(pwd)}"

WORKDIR="$(mktemp -d)"
cleanup() {
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

export HOME="$WORKDIR"
echo "isolated HOME: $HOME"

cd "$CLI_DIR"

echo "--- agw config init (first run) ---"
uv run agw config init

echo "--- verify config landed under the isolated HOME ---"
test -f "$HOME/.config/agentworks/config.toml"
echo "  ok: $HOME/.config/agentworks/config.toml exists"

echo "--- example: exercise a read-only surface against the fresh config ---"
uv run agw --help >/dev/null
echo "  ok: agw runs against the isolated HOME with no operator state present"

# Extend from here: create a scratch resource under
# $HOME/.config/agentworks/resources/, run the command under test, and
# assert on its observed output. The temp HOME is discarded automatically
# on exit, so there is nothing to roll back.
