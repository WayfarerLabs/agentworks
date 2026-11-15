"""Tests for bootstrap script generation and parsing."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from agentworks.capabilities.vm_platform.bootstrap_script import (
    REBOOT_SENTINEL_PATH,
    generate_bootstrap_script,
    parse_bootstrap_steps,
    run_once_marker,
    run_once_script,
)
from agentworks.execution import root_script
from tests.conftest import requires_posix_shell


def test_generate_bootstrap_script_all_steps() -> None:
    """Full bootstrap script includes all expected steps."""
    script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        provisioning_packages=["curl", "git"],
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
    assert "SWAP_GB=4" in script
    assert "lima--myvm" in script


def test_generate_bootstrap_script_masks_sve_gated_on_apple() -> None:
    """The SVE mask is gated on Apple Virtualization + SVE, writes a grub
    drop-in with arm64.nosve, and drops a restart sentinel."""
    script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        provisioning_packages=["curl"],
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

    A first run that failed part way runs again on the next boot under
    ``run_once_script`` (Lima, GCE). A bare ``echo ... >> authorized_keys``
    would append a byte-identical duplicate on that rerun. The install must
    instead be guarded so the append only happens when the exact key line is
    absent.
    """
    script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        provisioning_packages=["curl"],
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
        hostname="lima--myvm",
        swap=4,
    )

    assert "if [ -f /swapfile ]; then" in script
    # Exactly one fstab append, and it is the swap line under the guard.
    assert script.count(">> /etc/fstab") == 1
    assert "echo '/swapfile none swap sw 0 0' >> /etc/fstab" in script


@requires_posix_shell
def test_generate_bootstrap_script_passes_bash_syntax_check() -> None:
    """End-to-end: the generated script, inside the root-script wrapper that
    WSL2 and Proxmox run it in, must syntactically parse as bash. Catches any
    future template change that leaks an unescaped brace, an unterminated
    heredoc, or a parenthesis the wrapper's subshell cannot hold.

    Skips on Windows: the script targets the Linux VM guest, and the
    Git-for-Windows ``bash -n`` used on the Windows controller parses these
    heredocs differently from the guest's bash. Linux CI covers this."""
    import os
    import subprocess
    import tempfile

    script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        provisioning_packages=["curl", "tmux"],
        hostname="lima--myvm",
        swap=2,
    )
    wrapped = root_script.wrap(script, nonce="0" * 32, secret=None).decode()
    # Pass the script as a real file rather than /dev/stdin, which does not
    # resolve under the git-bash used on Windows. newline="\n" keeps it LF so
    # bash never sees a stray carriage return.
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, newline="\n") as handle:
        handle.write(wrapped)
        script_path = handle.name
    try:
        result = subprocess.run(
            ["bash", "-n", script_path],
            text=True,
            capture_output=True,
        )
    finally:
        os.unlink(script_path)
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
        hostname="wsl2--myvm",
        swap=4,
    )

    assert "--userspace-networking" not in script
    # Specifically must not WRITE to /etc/default/tailscaled (the previous
    # bug). A comment mentioning the path is fine; an output redirect is not.
    assert "> /etc/default/tailscaled" not in script
    assert ">> /etc/default/tailscaled" not in script


def test_parse_bootstrap_steps_reads_each_step_and_its_markers() -> None:
    output = (
        "noise before any step\n"
        "##SUCCESS## orphaned\n"
        "##STEP## Hostname\n"
        "##SUCCESS## hostname set to vm1\n"
        "##STEP## Tailscale install\n"
        "##WARN## slow mirror\n"
        "##ERROR## curl failed\n"
    )

    steps = parse_bootstrap_steps(output)

    assert [(step.name, step.success_msg, step.warnings, step.error) for step in steps] == [
        ("Hostname", "hostname set to vm1", [], None),
        ("Tailscale install", None, ["slow mirror"], "curl failed"),
    ]


def _boot(wrapper: Path, ledger: Path) -> int:
    """Run the wrapper as a backend's boot would; the toy bootstrap records each run in ``ledger``."""
    env = {**os.environ, "LEDGER": str(ledger)}
    return subprocess.run(["bash", str(wrapper)], env=env, capture_output=True, check=False).returncode


@requires_posix_shell
def test_run_once_script_runs_the_bootstrap_on_the_first_boot_only(tmp_path: Path) -> None:
    """A boot after a successful one runs nothing: the operator's changes since then stand."""
    ledger = tmp_path / "ledger"
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(
        run_once_script(
            '#!/bin/bash\necho ran >> "$LEDGER"\n',
            stem=str(tmp_path / "state" / "bootstrap"),
        ),
        newline="\n",
    )

    assert [_boot(wrapper, ledger) for _ in range(3)] == [0, 0, 0]
    assert ledger.read_text().splitlines() == ["ran"]


@requires_posix_shell
def test_run_once_script_runs_a_failed_bootstrap_again(tmp_path: Path) -> None:
    """A first run that fails leaves no marker, so the next boot runs the bootstrap again."""
    ledger = tmp_path / "ledger"
    marker = Path(run_once_marker(str(tmp_path / "state" / "bootstrap")))
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(
        run_once_script(
            '#!/bin/bash\necho ran >> "$LEDGER"\nexit 3\n',
            stem=str(tmp_path / "state" / "bootstrap"),
        ),
        newline="\n",
    )

    assert [_boot(wrapper, ledger) for _ in range(2)] == [3, 3]
    assert ledger.read_text().splitlines() == ["ran", "ran"]
    assert not marker.exists()


def test_run_once_script_refuses_a_bootstrap_holding_its_delimiter() -> None:
    """A line equal to the heredoc delimiter would end the copy early and run the rest as the wrapper."""
    delimiter = re.search(r"<<'(\w+)'", run_once_script("", stem="/x"))
    assert delimiter is not None
    with pytest.raises(ValueError):
        run_once_script(f"echo before\n{delimiter[1]}\necho after\n", stem="/x")
