#!/usr/bin/env bash
# Maintained harness: BREAKING-CHANGE loader drive.
#
# Pattern: when a change makes an on-disk format (config, a resource shape,
# a DB schema) incompatible with what it replaces, drive the real loader
# against a fixture written in the OLD shape and assert on the OBSERVED
# outcome: either a clean migration, or a loud, precise failure. A silent
# wrong answer (the old file loads but is silently ignored or
# misinterpreted) is the one outcome that must never happen; a crash with a
# clear message is an acceptable, honest failure for a breaking change.
#
# This harness drives config.toml's ADR-0022 resource-section sunset:
# config.toml is settings only now, and a config that still declares a
# legacy inline resource section (e.g. the old `[azure]` vm-site shape)
# must fail loudly rather than have that section silently dropped. See
# `agentworks/config/load.py`'s unexpected-top-level validation for the
# loader logic this exercises.
#
# See docs/testing/harnesses/README.md for the maintenance contract. Adapt
# the fixture and the assertion to whatever breaking change you are
# verifying.
set -euo pipefail

CLI_DIR="${AGW_CLI_DIR:-$(pwd)}"

WORKDIR="$(mktemp -d)"
cleanup() {
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

export HOME="$WORKDIR"
mkdir -p "$HOME/.config/agentworks"

# An OLD-shaped config.toml: a legacy inline `[azure]` vm-site section,
# the pre-ADR-0022 way of declaring a resource directly in config.toml.
cat >"$HOME/.config/agentworks/config.toml" <<'EOF'
[azure]
subscription_id = "example"
EOF

cd "$CLI_DIR"

echo "--- driving agw doctor against an old-shaped config.toml ---"
OUTPUT="$(uv run agw doctor 2>&1 || true)"
echo "$OUTPUT"

echo ""
echo "--- asserting the breaking change fails LOUDLY, not silently ---"
if echo "$OUTPUT" | grep -q 'unexpected top-level keys in config: azure'; then
    echo "  ok: the legacy section is rejected by ordinary top-level validation"
else
    echo "  VIOLATION: expected a loud rejection of the legacy [azure] section;" >&2
    echo "  the loader may now be silently ignoring or mishandling it instead." >&2
    exit 1
fi
