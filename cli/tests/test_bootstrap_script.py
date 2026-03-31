"""Tests for bootstrap script generation and parsing."""

from __future__ import annotations

from agentworks.vms.bootstrap_script import (
    generate_bootstrap_script,
    parse_bootstrap_output,
    vm_hostname,
)


def test_vm_hostname() -> None:
    """Hostname uses <platform>--<vm_name> format."""
    assert vm_hostname("lima", "my-vm") == "lima--my-vm"
    assert vm_hostname("azure", "test") == "azure--test"
    assert vm_hostname("wsl2", "dev-box") == "wsl2--dev-box"


def test_generate_bootstrap_script_all_steps() -> None:
    """Full bootstrap script includes all expected steps."""
    script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        system_packages=["curl", "git"],
        tailscale_auth_key="tskey-auth-test123",
        hostname="lima--myvm",
        swap=4,
    )

    assert script.startswith("#!/bin/bash\n")
    assert "set -euo pipefail" in script
    assert "##STEP## Ensure user" in script
    assert "##STEP## System packages" in script
    assert "##STEP## SSH public key" in script
    assert "##STEP## Swap file" in script
    assert "##STEP## Hostname" in script
    assert "##STEP## Tailscale install" in script
    assert "##STEP## Tailscale join" in script
    assert "tskey-auth-test123" in script
    assert "SWAP_GB=4" in script
    assert "lima--myvm" in script


def test_generate_bootstrap_script_swap_disabled() -> None:
    """swap=0 still includes the step but skips creation."""
    script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        system_packages=["curl", "git"],
        tailscale_auth_key="tskey-auth-test123",
        hostname="azure--myvm",
        swap=0,
    )

    assert "##STEP## Swap file" in script
    assert "SWAP_GB=0" in script


def test_generate_bootstrap_script_wsl2() -> None:
    """WSL2 mode adds --userspace-networking."""
    script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        system_packages=["curl"],
        tailscale_auth_key="tskey-auth-test123",
        hostname="wsl2--myvm",
        is_wsl2=True,
    )

    assert "--userspace-networking" in script


def test_generate_bootstrap_script_not_wsl2() -> None:
    """Non-WSL2 mode does not add --userspace-networking."""
    script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        system_packages=["curl"],
        tailscale_auth_key="tskey-auth-test123",
        hostname="lima--myvm",
        is_wsl2=False,
    )

    assert "--userspace-networking" not in script


def test_parse_bootstrap_output_success() -> None:
    """Parse output from a successful bootstrap."""
    output = (
        "##STEP## Tailscale install\n"
        "##SUCCESS## tailscale installed\n"
        "##STEP## Tailscale join\n"
        "##SUCCESS## tailscale-ip=100.64.0.5\n"
    )

    result = parse_bootstrap_output(output, 0)

    assert result.ok
    assert result.tailscale_ip == "100.64.0.5"
    assert len(result.steps) == 2
    assert result.steps[0].name == "Tailscale install"
    assert result.steps[0].success_msg == "tailscale installed"
    assert result.steps[1].name == "Tailscale join"
    assert result.steps[1].success_msg == "tailscale-ip=100.64.0.5"


def test_parse_bootstrap_output_failure() -> None:
    """Parse output from a failed bootstrap."""
    output = "##STEP## Tailscale install\n##ERROR## curl failed\n"

    result = parse_bootstrap_output(output, 1)

    assert not result.ok
    assert result.tailscale_ip is None
    assert len(result.steps) == 1
    assert result.steps[0].error == "curl failed"
