"""Tests for bootstrap script generation and parsing."""

from __future__ import annotations

from agentworks.capabilities.vm_platform.bootstrap_script import (
    generate_bootstrap_script,
    parse_bootstrap_output,
)


def test_generate_bootstrap_script_all_steps() -> None:
    """Full bootstrap script includes all expected steps."""
    script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        provisioning_packages=["curl", "git"],
        tailscale_auth_key="tskey-auth-test123",
        hostname="lima--myvm",
        swap=4,
    )

    assert script.startswith("#!/bin/bash\n")
    assert "set -euo pipefail" in script
    assert "##STEP## Ensure user" in script
    assert "##STEP## Provisioning packages" in script
    assert "##STEP## SSH public key" in script
    assert "##STEP## Swap file" in script
    assert "##STEP## Hostname" in script
    assert "##STEP## Mask SVE" in script
    assert "##STEP## Tailscale install" in script
    assert "##STEP## Tailscale join" in script
    assert "tskey-auth-test123" in script
    assert "SWAP_GB=4" in script
    assert "lima--myvm" in script


def test_generate_bootstrap_script_masks_sve_gated_on_apple() -> None:
    """The SVE mask is gated on Apple Virtualization + SVE, writes a grub
    drop-in with arm64.nosve, and drops a restart sentinel."""
    script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        provisioning_packages=["curl"],
        tailscale_auth_key="tskey-auth-test123",
        hostname="lima--myvm",
    )

    # Self-gated: only Apple Virtualization guests advertising SVE act.
    assert "apple virtualization" in script
    assert "/sys/class/dmi/id/product_name" in script
    assert "sve2 /proc/cpuinfo" in script
    # The fix and its host-side restart signal.
    assert "arm64.nosve" in script
    assert "/etc/default/grub.d/99-agentworks-nosve.cfg" in script
    assert "update-grub" in script
    assert "touch /run/agentworks-reboot-required" in script


def test_generate_bootstrap_script_preserves_ssh_host_keys() -> None:
    """Bootstrap writes the cloud-init drop-in that pins SSH host keys.

    Guards against drift between the bootstrap template and the constants
    reused by the Phase B reconcile step (initializer._preserve_ssh_host_keys).
    """
    from agentworks.capabilities.vm_platform.bootstrap_script import (
        SSH_PRESERVE_KEYS_LINES,
        SSH_PRESERVE_KEYS_PATH,
    )

    script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        provisioning_packages=["curl"],
        tailscale_auth_key="tskey-auth-test123",
        hostname="lima--myvm",
    )

    assert "##STEP## Preserve SSH host keys" in script
    assert f"cat > {SSH_PRESERVE_KEYS_PATH} <<'CLOUDCFG'" in script
    for line in SSH_PRESERVE_KEYS_LINES:
        assert line in script


def test_generate_bootstrap_script_swap_disabled() -> None:
    """swap=0 still includes the step but skips creation."""
    script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        provisioning_packages=["curl", "git"],
        tailscale_auth_key="tskey-auth-test123",
        hostname="azure--myvm",
        swap=0,
    )

    assert "##STEP## Swap file" in script
    assert "SWAP_GB=0" in script


def test_generate_bootstrap_script_writes_shell_rc_seeds() -> None:
    """Bootstrap inlines the shell rc seeds into admin's home so the
    very first interactive login has a sane bash AND zsh setup (no
    Debian /etc/skel/.zshrc means a fresh zsh user has no rc otherwise).

    The seeds are written via a single-quoted heredoc so bash doesn't
    expand ``${AGENTWORKS_AGENT:-admin}`` at provision time -- the
    identity vars only get substituted when the operator opens an
    interactive shell, after init populates /etc/profile.d/.
    """
    from agentworks.capabilities.vm_platform.skel import BASHRC, ZSHRC

    script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        provisioning_packages=["curl"],
        tailscale_auth_key="tskey-auth-test123",
        hostname="lima--myvm",
    )

    assert "##STEP## Default shell rc seeds" in script
    # Single-quoted heredoc so bash leaves the AGENTWORKS_* refs literal.
    assert "cat > \"$HOME_DIR/.bashrc\" <<'AGW_BASHRC_EOF'" in script
    assert "cat > \"$HOME_DIR/.zshrc\" <<'AGW_ZSHRC_EOF'" in script
    # Seed content is verbatim from the shared module.
    assert BASHRC in script
    assert ZSHRC in script
    # Ownership flips back to admin after root writes the files.
    assert 'chown "$VM_USER:$VM_USER" "$HOME_DIR/.bashrc" "$HOME_DIR/.zshrc"' in script


def test_generate_bootstrap_script_passes_bash_syntax_check() -> None:
    """End-to-end: the generated script must syntactically parse as
    bash. Catches any future template change that leaks an unescaped
    brace, an unterminated heredoc, etc."""
    import subprocess

    script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        provisioning_packages=["curl", "tmux"],
        tailscale_auth_key="tskey-auth-test123",
        hostname="lima--myvm",
        swap=2,
    )
    result = subprocess.run(
        ["bash", "-n", "/dev/stdin"],
        input=script,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_generate_bootstrap_script_no_platform_specific_tailscale_config() -> None:
    """The bootstrap script must not carry platform-specific Tailscale flags.

    Regression guard for two stacked historical bugs:
      1. ``--userspace-networking`` appended to ``tailscale up`` (it's a
         daemon-only flag; the client rejects it with exit 2).
      2. Overwriting ``/etc/default/tailscaled`` to set FLAGS, which also
         clobbered PORT and made tailscaled refuse to start.
    Tailscale runs in its package-default kernel-tun mode on every platform.
    If we ever need WSL2-specific config, do it via a systemd drop-in (see
    bootstrap_script.py comment), NOT by overwriting /etc/default/tailscaled.
    """
    script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        provisioning_packages=["curl"],
        tailscale_auth_key="tskey-auth-test123",
        hostname="wsl2--myvm",
    )

    assert "--userspace-networking" not in script
    # Specifically must not WRITE to /etc/default/tailscaled (the previous
    # bug). A comment mentioning the path is fine; an output redirect is not.
    assert "> /etc/default/tailscaled" not in script
    assert ">> /etc/default/tailscaled" not in script
    assert "tailscale up --auth-key" in script


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
