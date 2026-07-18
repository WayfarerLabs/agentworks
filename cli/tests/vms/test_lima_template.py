"""Guardrails for the Lima instance template.

Two Lima-specific hardening rules are baked into ``LIMA_TEMPLATE`` and are
easy to regress silently, so they get source-level tripwires here:

1. No host file sharing (``mounts: []``): agentworks VMs are self-contained.
2. Subordinate uid/gid ranges are capped to 65536: Lima's rootless-base boot
   script grants the host-matched user a 1 GiB range that overruns
   ``SUB_UID_MAX`` and starves agent-user creation.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest
import yaml

from agentworks.capabilities.vm_platform.lima import LIMA_TEMPLATE


def _render() -> dict:
    provision = "      #!/bin/bash\n      set -euo pipefail\n      echo provisioned"
    rendered = LIMA_TEMPLATE.format(cpus=4, memory=8, disk=50, provision_script=provision)
    return yaml.safe_load(rendered)


def test_no_host_mounts() -> None:
    """Host file sharing is off. An explicit empty list (not an omitted key)
    guarantees it regardless of Lima defaults; the moot ``mountType`` is gone."""
    doc = _render()
    assert doc["mounts"] == []
    assert "mountType" not in doc


def test_subuid_cap_step_present_and_first() -> None:
    """A ``mode: system`` provision step caps oversized subid ranges, and it
    runs before the bootstrap step (which creates the admin user and would
    otherwise allocate into a starved range)."""
    doc = _render()
    provision = doc["provision"]
    assert len(provision) == 2
    cap = provision[0]
    assert cap["mode"] == "system"
    assert "/etc/subuid" in cap["script"]
    assert "/etc/subgid" in cap["script"]
    assert "awk -F:" in cap["script"]


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("awk") is None,
    reason="needs a POSIX shell + awk (the environment the VMs actually run in)",
)
def test_cap_awk_caps_giant_range_and_preserves_normal() -> None:
    """The cap expression rewrites only entries above 65536, in place, and is
    idempotent. Exercised as the real shell/awk, not a Python reimplementation."""
    doc = _render()
    # Pull the awk expression out of the rendered cap script.
    awk_line = next(
        line.strip() for line in doc["provision"][0]["script"].splitlines() if "awk -F:" in line
    )
    # Isolate the standalone `awk -F: '...'` program from the surrounding
    # redirect so we can pipe sample content through it directly.
    program = awk_line.split(">", 1)[0].strip()

    sample = (
        "agentworks:100000:65536\n"
        "agt--wm-cda:165536:65536\n"
        "scot:524288:1073741824\n"
        "agt-agw-bp1:427680:65536\n"
    )
    expected = (
        "agentworks:100000:65536\n"
        "agt--wm-cda:165536:65536\n"
        "scot:524288:65536\n"
        "agt-agw-bp1:427680:65536\n"
    )

    first = subprocess.run(
        program, input=sample, shell=True, capture_output=True, text=True, check=True
    ).stdout
    assert first == expected
    # Idempotent: a second pass over already-capped content is a no-op.
    second = subprocess.run(
        program, input=first, shell=True, capture_output=True, text=True, check=True
    ).stdout
    assert second == expected
