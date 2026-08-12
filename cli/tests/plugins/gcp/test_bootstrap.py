"""GCE retained startup wrapper boundaries."""

from __future__ import annotations

import pytest

from agentworks.errors import ConfigError
from agentworks.plugins.gcp.bootstrap import (
    GCE_BOOTSTRAP_MARKER,
    GCE_STARTUP_SCRIPT_LIMIT_BYTES,
    build_startup_script,
)


def test_wrapper_checks_marker_before_mutation_and_marks_only_after_success() -> None:
    script = build_startup_script("#!/bin/bash\necho bootstrap", instance_name="vm-a")
    marker_check = script.index('if test -f "$MARKER"')
    first_mutation = script.index("install -d")
    bootstrap_run = script.index('"$BOOTSTRAP"\n')
    marker_move = script.index('mv -f "$MARKER_TMP" "$MARKER"')
    assert f"MARKER={GCE_BOOTSTRAP_MARKER}" in script
    assert marker_check < first_mutation < bootstrap_run < marker_move
    assert "TAILSCALE" not in script.upper()


def test_utf8_size_gate_accepts_exact_limit_and_rejects_plus_one() -> None:
    empty = build_startup_script("", instance_name="vm-a")
    overhead = len(empty.encode("utf-8"))
    exact = build_startup_script("x" * (GCE_STARTUP_SCRIPT_LIMIT_BYTES - overhead), instance_name="vm-a")
    assert len(exact.encode("utf-8")) == GCE_STARTUP_SCRIPT_LIMIT_BYTES
    with pytest.raises(ConfigError, match="262144-byte metadata limit"):
        build_startup_script("x" * (GCE_STARTUP_SCRIPT_LIMIT_BYTES - overhead + 1), instance_name="vm-a")


def test_utf8_gate_counts_encoded_bytes_not_characters() -> None:
    empty = build_startup_script("", instance_name="vm-a")
    overhead = len(empty.encode("utf-8"))
    count = (GCE_STARTUP_SCRIPT_LIMIT_BYTES - overhead) // len("é".encode())
    accepted = build_startup_script("é" * count, instance_name="vm-a")
    assert len(accepted.encode("utf-8")) <= GCE_STARTUP_SCRIPT_LIMIT_BYTES
    with pytest.raises(ConfigError):
        build_startup_script("é" * (count + 1), instance_name="vm-a")
