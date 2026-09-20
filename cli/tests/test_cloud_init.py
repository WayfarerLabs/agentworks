"""Tests for cloud-init generation."""

from __future__ import annotations

import shlex

import pytest

from agentworks.capabilities.vm_platform.bootstrap_script import generate_bootstrap_script
from agentworks.capabilities.vm_platform.cloud_init import (
    INIT_SYSTEM_PACKAGES,
    PROVISIONING_PACKAGES,
    generate_cloud_init,
)


def _render_bootstrap() -> str:
    return generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        provisioning_packages=PROVISIONING_PACKAGES,
        tailscale_auth_key=None,
        hostname="test-vm",
        swap=0,
    )


def _rendered_provisioning_operation(payload: str) -> tuple[list[str], list[str]]:
    lines = [line.strip() for line in payload.splitlines()]
    assignment = next(line for line in lines if line.startswith("PROVISIONING_PACKAGES="))
    words = shlex.split(assignment.partition("=")[2])
    assert len(words) == 1
    packages = words[0].split()
    installs = [line for line in lines if line.startswith("apt-get install ")]
    return packages, installs


def test_generate_cloud_init_wraps_script() -> None:
    """Cloud-init wraps a bootstrap script in write_files + runcmd."""
    script = "#!/bin/bash\necho hello"
    result = generate_cloud_init(script)

    assert result.startswith("#cloud-config\n")
    assert "write_files:" in result
    assert "/tmp/agentworks-bootstrap.sh" in result
    assert "echo hello" in result
    assert "runcmd:" in result
    assert '"/bin/bash"' in result


def test_generate_cloud_init_preserves_script_content() -> None:
    """The script content is embedded verbatim."""
    script = "#!/bin/bash\nset -euo pipefail\napt-get update\n"
    result = generate_cloud_init(script)

    assert "set -euo pipefail" in result
    assert "apt-get update" in result


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_render_bootstrap(), id="native-bootstrap"),
        pytest.param(generate_cloud_init(_render_bootstrap()), id="cloud-init"),
    ],
)
def test_provisioning_packages_render_through_existing_apt_operation(payload: str) -> None:
    """Early prerequisites share the bootstrap's single apt install."""
    packages, installs = _rendered_provisioning_operation(payload)

    assert "python3" in packages
    assert {"openssh-server", "curl", "sudo", "ca-certificates"} <= set(packages)
    assert {"git", "tmux", "jq"}.isdisjoint(packages)
    assert len(installs) == 1
    assert shlex.split(installs[0])[-1] == "$PROVISIONING_PACKAGES"


def test_init_system_packages() -> None:
    """INIT_SYSTEM_PACKAGES contains the packages installed during init."""
    assert "python3" in INIT_SYSTEM_PACKAGES
    assert "git" in INIT_SYSTEM_PACKAGES
    assert "tmux" in INIT_SYSTEM_PACKAGES
    assert "tmuxinator" in INIT_SYSTEM_PACKAGES
    assert "acl" in INIT_SYSTEM_PACKAGES
    assert "jq" in INIT_SYSTEM_PACKAGES
    assert "mise" in INIT_SYSTEM_PACKAGES
