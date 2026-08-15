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
export USERPROFILE="$WORKDIR"
mkdir -p "$HOME/.config/agentworks" "$HOME/.ssh"
printf 'ssh-ed25519 AAAA...\n' >"$HOME/.ssh/id_ed25519.pub"
printf '%s\n' '-----BEGIN OPENSSH PRIVATE KEY-----' >"$HOME/.ssh/id_ed25519"
chmod 600 "$HOME/.ssh/id_ed25519"

# Start with valid settings so the paired drive proves that adding the retired
# resource root, rather than an unrelated settings defect, changes the
# Configuration state.
cat >"$HOME/.config/agentworks/config.toml" <<'EOF'
[operator]
ssh_public_key = "~/.ssh/id_ed25519.pub"
ssh_private_key = "~/.ssh/id_ed25519"
EOF

cd "$CLI_DIR"

configuration_state() {
    local expected="$1"
    uv run python -c '
import json
import sys

document = json.load(sys.stdin)
assert document["schema_version"] == 1
assert document["command"] == "doctor"
configuration = next(group for group in document["data"]["groups"] if group["name"] == "Configuration")
checks = configuration["checks"]
failures = [check for check in checks if check["status"] == "fail"]
if sys.argv[1] == "valid":
    assert not failures
else:
    assert len(failures) == 1
' "$expected"
}

echo "--- driving agw doctor against valid settings ---"
OUTPUT="$(uv run agw doctor --output json 2>/dev/null || true)"
echo "$OUTPUT"
printf '%s' "$OUTPUT" | configuration_state valid

# Add an OLD-shaped legacy inline `[azure]` vm-site section, the pre-ADR-0022
# way of declaring a resource directly in config.toml.
cat >>"$HOME/.config/agentworks/config.toml" <<'EOF'

[azure]
subscription_id = "example"
EOF

echo "--- driving agw doctor against an old-shaped config.toml ---"
OUTPUT="$(uv run agw doctor --output json 2>/dev/null || true)"
echo "$OUTPUT"

echo ""
echo "--- asserting the breaking change fails LOUDLY, not silently ---"
printf '%s' "$OUTPUT" | configuration_state invalid
echo "  ok: the legacy section changes Configuration from valid to failed"
