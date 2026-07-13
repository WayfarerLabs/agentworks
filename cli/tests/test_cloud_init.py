"""Tests for cloud-init generation."""

from __future__ import annotations

from agentworks.capabilities.vm_platform.cloud_init import (
    INIT_SYSTEM_PACKAGES,
    PROVISIONING_PACKAGES,
    generate_cloud_init,
)


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


def test_provisioning_packages_minimal() -> None:
    """PROVISIONING_PACKAGES contains only what is needed to bootstrap."""
    assert "openssh-server" in PROVISIONING_PACKAGES
    assert "curl" in PROVISIONING_PACKAGES
    assert "sudo" in PROVISIONING_PACKAGES
    assert "ca-certificates" in PROVISIONING_PACKAGES
    # These should NOT be in provisioning -- they belong in init
    assert "git" not in PROVISIONING_PACKAGES
    assert "tmux" not in PROVISIONING_PACKAGES
    assert "jq" not in PROVISIONING_PACKAGES


def test_init_system_packages() -> None:
    """INIT_SYSTEM_PACKAGES contains the packages installed during init."""
    assert "git" in INIT_SYSTEM_PACKAGES
    assert "tmux" in INIT_SYSTEM_PACKAGES
    assert "tmuxinator" in INIT_SYSTEM_PACKAGES
    assert "acl" in INIT_SYSTEM_PACKAGES
    assert "jq" in INIT_SYSTEM_PACKAGES
    assert "mise" in INIT_SYSTEM_PACKAGES
