"""Tests for bootstrap script generation and parsing."""

from __future__ import annotations

from agentworks.capabilities.vm_platform.bootstrap_script import (
    REBOOT_SENTINEL_PATH,
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


def test_generate_bootstrap_script_can_defer_tailscale_join_without_key() -> None:
    """Lima's retained bootstrap shape installs Tailscale but omits the key."""
    sentinel = "tskey-persistence-sentinel"
    keyed_script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        provisioning_packages=["curl", "git"],
        tailscale_auth_key=sentinel,
        hostname="lima--myvm",
        swap=4,
    )
    script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        provisioning_packages=["curl", "git"],
        tailscale_auth_key=None,
        hostname="lima--myvm",
        swap=4,
    )

    assert sentinel in keyed_script
    assert "TAILSCALE_AUTH_KEY=''" in script
    assert "Tailscale join deferred to platform" in script
    assert "tailscale up --auth-key" in script
    assert sentinel not in script


def test_generate_bootstrap_script_masks_sve_gated_on_apple() -> None:
    """The SVE mask is gated on Apple Virtualization + SVE, writes a grub
    drop-in with arm64.nosve, and drops a restart sentinel."""
    script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        provisioning_packages=["curl"],
        tailscale_auth_key="tskey-auth-test123",
        hostname="lima--myvm",
        swap=4,
    )

    # Self-gated: only Apple Virtualization guests advertising SVE act.
    assert "apple virtualization" in script
    assert "/sys/class/dmi/id/product_name" in script
    assert "/proc/cpuinfo" in script
    # The fix and its host-side restart signal.
    assert "arm64.nosve" in script
    assert "/etc/default/grub.d/99-agentworks-nosve.cfg" in script
    assert "update-grub" in script
    assert f"touch {REBOOT_SENTINEL_PATH}" in script


def test_sve_gate_matches_sve_and_sve2_as_whole_words() -> None:
    """The cpuinfo half of the SVE gate fires on SVE or SVE2, words only.

    SVE2 implies SVE, so a guest advertising SVE without SVE2 hits the same
    unusable-HWCAP trap and must mask too. The SVE2-only sub-features
    (sveaes, svesha3, ...) must not trigger it on their own.

    Asserting on the grep's spelling would only restate the source, so pull
    the real pattern out of the generated script and run grep with it.
    """
    import re
    import subprocess

    script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        provisioning_packages=["curl"],
        tailscale_auth_key="tskey-auth-test123",
        hostname="lima--myvm",
        swap=4,
    )

    match = re.search(r"grep (-\S+) '([^']+)' /proc/cpuinfo", script)
    assert match, "SVE gate's /proc/cpuinfo probe not found in generated script"
    flags, pattern = match.group(1), match.group(2)

    def fires(features: str) -> bool:
        proc = subprocess.run(["grep", flags, pattern], input=features, text=True, capture_output=True)
        return proc.returncode == 0

    # Apple vz advertising the full SVE2 set, and SVE without SVE2.
    assert fires("Features\t: fp asimd sve sve2 sveaes svesha3 bti")
    assert fires("Features\t: fp asimd sve asimdfhm bf16 bti")
    # Post-mask (arm64.nosve strips SVE from HWCAP): must not re-fire, which
    # is what stops the create flow from restarting the VM in a loop.
    assert not fires("Features\t: fp asimd aes pmull crc32 atomics bti")
    # Sub-features alone are not the SVE HWCAP.
    assert not fires("Features\t: fp asimd sveaes svepmull bti")


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
        swap=4,
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
        swap=4,
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


def test_authorized_keys_install_is_idempotent() -> None:
    """The operator key install must not append a duplicate on re-run.

    This bootstrap is a Lima provision.system provisioner, which Lima
    re-executes on every guest boot. A bare ``echo ... >> authorized_keys``
    would append a byte-identical duplicate on every stop/start (unbounded
    managed-file growth). The install must instead be guarded so the append
    only happens when the exact key line is absent.
    """
    script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        provisioning_packages=["curl"],
        tailscale_auth_key="tskey-auth-test123",
        hostname="lima--myvm",
        swap=4,
    )

    # Guarded append: whole-line fixed-string match, then append only if absent.
    # (The template's line continuation collapses in Python, like the SVE gate,
    # so the guard renders as one shell line; assert on its parts, not spacing.)
    assert 'grep -qxF "$SSH_PUBLIC_KEY" "$HOME_DIR/.ssh/authorized_keys" 2>/dev/null' in script
    assert '|| echo "$SSH_PUBLIC_KEY" >> "$HOME_DIR/.ssh/authorized_keys"' in script
    # And specifically NOT the unguarded bare append that caused the growth.
    assert 'mkdir -p "$HOME_DIR/.ssh"\necho "$SSH_PUBLIC_KEY" >>' not in script
    # Exactly one authorized_keys append site, and it is the guarded one.
    assert script.count('>> "$HOME_DIR/.ssh/authorized_keys"') == 1


def test_ssh_key_install_is_skipped_when_the_key_is_empty() -> None:
    """A platform that installs the admin key out of band (EC2 via the
    cloud-init users block, to avoid embedding the key literal a second time in
    a size-capped user-data) passes an empty key; the install step then no-ops,
    and the key literal never appears in the payload.
    """
    script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="",
        provisioning_packages=["curl"],
        tailscale_auth_key="tskey-auth-test123",
        hostname="lima--myvm",
        swap=4,
    )
    # The step is present but gated on a non-empty key, and the skip branch runs.
    assert 'if [ -n "$SSH_PUBLIC_KEY" ]; then' in script
    assert "SSH key provisioned out of band" in script
    # No key literal is embedded (empty quotes), so it is not double-counted.
    assert "SSH_PUBLIC_KEY=''" in script

    # A non-empty key still embeds and installs, unchanged for every other
    # platform.
    with_key = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        provisioning_packages=["curl"],
        tailscale_auth_key="tskey-auth-test123",
        hostname="lima--myvm",
        swap=4,
    )
    assert "ssh-ed25519 AAAA testkey" in with_key


def test_swap_fstab_append_is_guarded_against_re_execution() -> None:
    """The fstab swap entry must not duplicate when the bootstrap re-runs.

    ``/swapfile`` is a persistent on-disk file, so the surrounding
    ``if [ -f /swapfile ]`` guard sends every re-run down the "already exists"
    branch, skipping both swap creation and the fstab append. Assert that the
    single ``>> /etc/fstab`` append lives under that guard rather than at the
    top level of the step.
    """
    script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        provisioning_packages=["curl"],
        tailscale_auth_key="tskey-auth-test123",
        hostname="lima--myvm",
        swap=4,
    )

    assert "if [ -f /swapfile ]; then" in script
    # Exactly one fstab append, and it is the swap line under the guard.
    assert script.count(">> /etc/fstab") == 1
    assert "echo '/swapfile none swap sw 0 0' >> /etc/fstab" in script


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
        swap=4,
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
