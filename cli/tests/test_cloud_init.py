"""Tests for cloud-init generation."""

from __future__ import annotations

from agentworks.vms.cloud_init import SYSTEM_PACKAGES, generate_cloud_init


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


def test_system_packages_constant() -> None:
    """SYSTEM_PACKAGES contains the expected base packages."""
    assert "openssh-server" in SYSTEM_PACKAGES
    assert "curl" in SYSTEM_PACKAGES
    assert "git" in SYSTEM_PACKAGES
    assert "sudo" in SYSTEM_PACKAGES
    assert "acl" in SYSTEM_PACKAGES
    assert "jq" in SYSTEM_PACKAGES
