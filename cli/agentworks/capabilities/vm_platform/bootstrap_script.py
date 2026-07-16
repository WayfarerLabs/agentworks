"""Phase A bootstrap script generation and output parsing.

Generates a self-contained bash script that runs all Phase A (bootstrap)
steps on a fresh VM. The script uses structured markers in stdout so the
Python side can drive logging and console output.

Markers:
  ##STEP## <name>       - step boundary
  ##SUCCESS## <msg>     - step succeeded
  ##WARN## <msg>        - non-fatal warning
  ##ERROR## <msg>       - fatal error
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field

from agentworks.capabilities.vm_platform.skel import BASHRC, ZSHRC

# Canonical cloud-init drop-in that stops host-key regeneration on stop/start.
# By default cloud-init may delete and regenerate /etc/ssh/ssh_host_* on some
# boot events, which makes SSH clients reject the connection with a changed
# host key. This drop-in is written here during Phase A and reconciled during
# Phase B (initializer._preserve_ssh_host_keys), so VMs provisioned before it
# existed get repaired on `vm reinit`.
SSH_PRESERVE_KEYS_PATH = "/etc/cloud/cloud.cfg.d/99-preserve-ssh-keys.cfg"
SSH_PRESERVE_KEYS_LINES = ("ssh_deletekeys: false", "ssh_genkeytypes: []")
SSH_PRESERVE_KEYS_CONTENT = "".join(f"{line}\n" for line in SSH_PRESERVE_KEYS_LINES)

# Sentinel dropped by the "Mask SVE" step when arm64.nosve is configured but
# the running kernel has not picked it up yet, i.e. a restart is needed. The
# lima platform probes it after create and restarts the instance once (see
# LimaPlatform._sve_reboot_pending). It lives on tmpfs, so the restart clears
# it. Shared so the writer and the probe cannot drift apart.
SVE_REBOOT_SENTINEL_PATH = "/run/agentworks-reboot-required"

SCRIPT_TEMPLATE = """\
#!/bin/bash
set -euo pipefail

VM_USER={admin_username}
SSH_PUBLIC_KEY={ssh_public_key}
PROVISIONING_PACKAGES={provisioning_packages}
TAILSCALE_AUTH_KEY={tailscale_auth_key}
VM_HOSTNAME={vm_hostname}
SWAP_GB={swap}

# -- Step 1: Ensure user --
echo "##STEP## Ensure user"
if id "$VM_USER" >/dev/null 2>&1; then
    echo "##SUCCESS## user $VM_USER already exists"
else
    useradd -m -s /bin/bash "$VM_USER"
    echo "##SUCCESS## user $VM_USER created"
fi
usermod -aG sudo "$VM_USER"
echo "$VM_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/$VM_USER"
HOME_DIR="/home/$VM_USER"

# -- Step 1b: Default shell rc seeds for admin's home --
# One-time copy. Init / reinit also writes the same seeds to
# /etc/skel so future ``useradd -m`` (i.e. new agents) inherit them
# automatically. Operators who later install their own dotfiles win:
# agentworks never writes shell rc files into a user's home again.
echo "##STEP## Default shell rc seeds"
cat > "$HOME_DIR/.bashrc" <<'AGW_BASHRC_EOF'
{bashrc_content}AGW_BASHRC_EOF
cat > "$HOME_DIR/.zshrc" <<'AGW_ZSHRC_EOF'
{zshrc_content}AGW_ZSHRC_EOF
chown "$VM_USER:$VM_USER" "$HOME_DIR/.bashrc" "$HOME_DIR/.zshrc"
chmod 644 "$HOME_DIR/.bashrc" "$HOME_DIR/.zshrc"
echo "##SUCCESS## shell rc seeds installed"

# -- Step 2: Provisioning packages --
echo "##STEP## Provisioning packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
timeout 600 apt-get dist-upgrade -y -qq -o Dpkg::Options::="--force-confnew"
# shellcheck disable=SC2086
apt-get install -y -qq -o Dpkg::Options::="--force-confnew" $PROVISIONING_PACKAGES
echo "##SUCCESS## provisioning packages installed"

# -- Step 2b: Preserve SSH host keys across reboots --
# By default, cloud-init may delete and regenerate SSH host keys on certain
# boot events (e.g., VM stop/start). This causes SSH clients to reject the
# connection due to a changed host key. Tell cloud-init to preserve existing keys.
# The same drop-in is reconciled during Phase B (initializer._preserve_ssh_host_keys)
# so VMs created before this step existed get repaired on `vm reinit`.
echo "##STEP## Preserve SSH host keys"
mkdir -p /etc/cloud/cloud.cfg.d
cat > {ssh_preserve_path} <<'CLOUDCFG'
{ssh_preserve_content}CLOUDCFG
echo "##SUCCESS## SSH host key preservation configured"

# -- Step 3: SSH public key --
echo "##STEP## SSH public key"
HOME_DIR="/home/$VM_USER"
mkdir -p "$HOME_DIR/.ssh"
echo "$SSH_PUBLIC_KEY" >> "$HOME_DIR/.ssh/authorized_keys"
chown -R "$VM_USER:$VM_USER" "$HOME_DIR/.ssh"
chmod 700 "$HOME_DIR/.ssh"
chmod 600 "$HOME_DIR/.ssh/authorized_keys"
echo "##SUCCESS## SSH key installed"

# -- Step 4: Swap file --
echo "##STEP## Swap file"
if [ "$SWAP_GB" -gt 0 ]; then
    if [ -f /swapfile ]; then
        echo "##SUCCESS## swap file already exists"
    else
        SWAP_MB=$((SWAP_GB * 1024))
        fallocate -l "${{SWAP_MB}}M" /swapfile
        chmod 600 /swapfile
        mkswap /swapfile
        swapon /swapfile
        echo '/swapfile none swap sw 0 0' >> /etc/fstab
        echo "##SUCCESS## ${{SWAP_GB}} GiB swap file created"
    fi
else
    echo "##SUCCESS## swap disabled"
fi

# -- Step 5: Set hostname --
echo "##STEP## Hostname"
hostnamectl set-hostname "$VM_HOSTNAME" 2>/dev/null || hostname "$VM_HOSTNAME"
echo "##SUCCESS## hostname set to $VM_HOSTNAME"

# -- Step 5b: Mask broken SVE on Apple Virtualization guests --
# Apple's Virtualization.framework advertises SVE/SVE2 in the guest HWCAP
# that the guest cannot actually execute; the first SVE instruction traps
# as SIGILL (seen in OpenSSL, and therefore git-over-https and Python
# cryptography). Disable SVE at the kernel cmdline via a grub drop-in so
# no library selects an SVE routine. This needs a reboot to take effect,
# and rebooting inside a provision step is unreliable (lima-vm/lima#4867),
# so the platform restarts the instance from the host when it sees the
# sentinel dropped below. Self-gated: a no-op on every non-Apple host.
echo "##STEP## Mask SVE"
if grep -qi 'apple virtualization' /sys/class/dmi/id/product_name 2>/dev/null \
    && grep -qi sve2 /proc/cpuinfo 2>/dev/null; then
    mkdir -p /etc/default/grub.d
    cat > /etc/default/grub.d/99-agentworks-nosve.cfg <<'AGW_NOSVE_EOF'
# agentworks: Apple Virtualization advertises SVE the guest cannot run.
GRUB_CMDLINE_LINUX="$GRUB_CMDLINE_LINUX arm64.nosve"
AGW_NOSVE_EOF
    if update-grub >/dev/null 2>&1; then
        if grep -qw arm64.nosve /proc/cmdline; then
            echo "##SUCCESS## SVE already masked (arm64.nosve active)"
        else
            touch {sve_reboot_sentinel}
            echo "##SUCCESS## SVE masked via arm64.nosve (restart pending)"
        fi
    else
        echo "##WARN## update-grub failed; SVE not masked"
    fi
else
    echo "##SUCCESS## SVE mask not needed"
fi

# -- Step 6: Install Tailscale --
# tailscaled is configured by its Debian package's systemd unit, which reads
# /etc/default/tailscaled for the FLAGS env var. We leave both at their
# package defaults across all platforms (Lima, Azure, Proxmox, WSL2) so
# tailscaled runs in kernel-tun mode -- modern WSL2 kernels (5.10+) ship
# /dev/net/tun, just like every other Debian-based VM we target.
#
# If WSL2 ever needs userspace networking (e.g. a stripped kernel without
# tun): drop a systemd unit override that appends to FLAGS, e.g.:
#   /etc/systemd/system/tailscaled.service.d/10-userspace.conf
#     [Service]
#     Environment="FLAGS=--tun=userspace-networking"
# Do NOT overwrite /etc/default/tailscaled -- it sets PORT too, and an
# empty PORT makes tailscaled refuse to start with INVALIDARGUMENT.
echo "##STEP## Tailscale install"
if command -v tailscale >/dev/null 2>&1; then
    echo "##SUCCESS## tailscale already installed"
else
    curl -fsSL https://tailscale.com/install.sh | sh
    echo "##SUCCESS## tailscale installed"
fi

# -- Step 7: Join Tailscale --
echo "##STEP## Tailscale join"
tailscale up --auth-key "$TAILSCALE_AUTH_KEY"
TS_IP=$(tailscale ip -4)
echo "##SUCCESS## tailscale-ip=$TS_IP"
"""


def generate_bootstrap_script(
    *,
    admin_username: str,
    ssh_public_key: str,
    provisioning_packages: list[str],
    tailscale_auth_key: str,
    hostname: str,
    swap: int = 0,
) -> str:
    """Generate the Phase A bootstrap script with parameters baked in."""
    return SCRIPT_TEMPLATE.format(
        admin_username=shlex.quote(admin_username),
        ssh_public_key=shlex.quote(ssh_public_key),
        provisioning_packages=shlex.quote(" ".join(provisioning_packages)),
        tailscale_auth_key=shlex.quote(tailscale_auth_key),
        vm_hostname=shlex.quote(hostname),
        swap=swap,
        ssh_preserve_path=SSH_PRESERVE_KEYS_PATH,
        ssh_preserve_content=SSH_PRESERVE_KEYS_CONTENT,
        sve_reboot_sentinel=SVE_REBOOT_SENTINEL_PATH,
        bashrc_content=BASHRC,
        zshrc_content=ZSHRC,
    )


@dataclass
class StepResult:
    name: str
    success_msg: str | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class BootstrapResult:
    exit_code: int
    tailscale_ip: str | None = None
    steps: list[StepResult] = field(default_factory=list)
    raw_output: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.tailscale_ip is not None


def parse_bootstrap_output(stdout: str, exit_code: int) -> BootstrapResult:
    """Parse structured markers from bootstrap script output."""
    result = BootstrapResult(exit_code=exit_code, raw_output=stdout)
    current_step: StepResult | None = None

    for line in stdout.splitlines():
        if line.startswith("##STEP## "):
            current_step = StepResult(name=line[9:])
            result.steps.append(current_step)
        elif line.startswith("##SUCCESS## "):
            msg = line[12:]
            if current_step is not None:
                current_step.success_msg = msg
            if msg.startswith("tailscale-ip="):
                result.tailscale_ip = msg.split("=", 1)[1].strip()
        elif line.startswith("##WARN## "):
            if current_step is not None:
                current_step.warnings.append(line[9:])
        elif line.startswith("##ERROR## "):
            if current_step is not None:
                current_step.error = line[10:]

    return result
