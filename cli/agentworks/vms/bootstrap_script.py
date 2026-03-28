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

SCRIPT_TEMPLATE = """\
#!/bin/bash
set -euo pipefail

VM_USER={admin_username}
SSH_PUBLIC_KEY={ssh_public_key}
SYSTEM_PACKAGES={system_packages}
TAILSCALE_AUTH_KEY={tailscale_auth_key}
VM_HOSTNAME={vm_hostname}
TS_EXTRA_FLAGS={ts_extra_flags}
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

# -- Step 2: System packages --
echo "##STEP## System packages"
apt-get update -qq
# shellcheck disable=SC2086
apt-get install -y -qq $SYSTEM_PACKAGES
echo "##SUCCESS## system packages installed"

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

# -- Step 6: Install Tailscale --
echo "##STEP## Tailscale install"
if command -v tailscale >/dev/null 2>&1; then
    echo "##SUCCESS## tailscale already installed"
else
    curl -fsSL https://tailscale.com/install.sh | sh
    echo "##SUCCESS## tailscale installed"
fi

# -- Step 7: Join Tailscale --
echo "##STEP## Tailscale join"
# shellcheck disable=SC2086
tailscale up --auth-key "$TAILSCALE_AUTH_KEY" $TS_EXTRA_FLAGS
TS_IP=$(tailscale ip -4)
echo "##SUCCESS## tailscale-ip=$TS_IP"
"""


def vm_hostname(platform: str, vm_name: str) -> str:
    """Build a consistent VM hostname: <platform>--<vm_name>."""
    return f"{platform}--{vm_name}"


def generate_bootstrap_script(
    *,
    admin_username: str,
    ssh_public_key: str,
    system_packages: list[str],
    tailscale_auth_key: str,
    hostname: str,
    swap: int = 0,
    is_wsl2: bool = False,
) -> str:
    """Generate the Phase A bootstrap script with parameters baked in."""
    ts_extra_flags = "--userspace-networking" if is_wsl2 else ""

    return SCRIPT_TEMPLATE.format(
        admin_username=shlex.quote(admin_username),
        ssh_public_key=shlex.quote(ssh_public_key),
        system_packages=shlex.quote(" ".join(system_packages)),
        tailscale_auth_key=shlex.quote(tailscale_auth_key),
        vm_hostname=shlex.quote(hostname),
        ts_extra_flags=shlex.quote(ts_extra_flags),
        swap=swap,
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
