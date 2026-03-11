"""WSL2 provisioner -- imports Debian distros on Windows."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import typer

from agentworks.db import VMStatus
from agentworks.ssh import ExecTarget, WSL2Target
from agentworks.vms.base import ProvisionResult, VMProvisioner

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import VMRow

# Default install path for WSL2 distros
WSL_BASE_PATH = "%LOCALAPPDATA%\\agentworks\\wsl"

# Docker Hub OCI registry endpoints for the official Debian image
_DOCKER_AUTH_URL = (
    "https://auth.docker.io/token"
    "?service=registry.docker.io&scope=repository:library/debian:pull"
)
_DOCKER_MANIFESTS_URL = (
    "https://registry-1.docker.io/v2/library/debian/manifests/bookworm"
)
_DOCKER_BLOBS_URL = (
    "https://registry-1.docker.io/v2/library/debian/blobs"
)


def _wsl(args: list[str], *, check: bool = True) -> str:
    """Run a wsl.exe command and return stdout."""
    result = subprocess.run(["wsl", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"wsl command failed: {result.stderr.strip()}")
    return result.stdout


def _powershell(script: str, *, check: bool = True) -> str:
    """Run a PowerShell command and return stdout."""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"PowerShell failed: {result.stderr.strip()}")
    return result.stdout


def _download_debian_rootfs(tarball_path: str) -> None:
    """Download the official Debian rootfs from Docker Hub OCI registry.

    Pulls the rootfs layer from the official debian:bookworm image without
    requiring Docker to be installed. The layer is a tar.gz that works
    directly with ``wsl --import``.
    """
    # This PowerShell script:
    # 1. Gets an anonymous auth token from Docker Hub
    # 2. Fetches the image manifest for debian:bookworm (amd64)
    # 3. Downloads the rootfs layer (first layer in the manifest)
    script = f"""\
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
$token = (Invoke-RestMethod '{_DOCKER_AUTH_URL}').token
$headers = @{{
    Authorization = "Bearer $token"
    Accept = 'application/vnd.docker.distribution.manifest.v2+json'
}}
$manifest = Invoke-RestMethod -Headers $headers '{_DOCKER_MANIFESTS_URL}'
$digest = $manifest.layers[0].digest
$dlHeaders = @{{ Authorization = "Bearer $token" }}
Invoke-WebRequest -Headers $dlHeaders `
    -Uri "{_DOCKER_BLOBS_URL}/$digest" `
    -OutFile '{tarball_path}'
"""
    _powershell(script)


class WSL2Provisioner(VMProvisioner):
    """Provisions WSL2 Debian distributions on Windows."""

    def create(
        self, vm_name: str, config: Config, extra_packages: list[str] | None = None,
        *, vm_user: str = "agentworks",
    ) -> ProvisionResult:
        typer.echo(f"Creating WSL2 distro '{vm_name}'...")

        install_path = f"{WSL_BASE_PATH}\\{vm_name}"

        # Ensure install directory exists
        _powershell(f"New-Item -ItemType Directory -Force -Path '{install_path}'")

        # Download Debian rootfs if not cached
        cache_dir = f"{WSL_BASE_PATH}\\.cache"
        tarball = f"{cache_dir}\\debian-bookworm-rootfs.tar.gz"
        _powershell(f"New-Item -ItemType Directory -Force -Path '{cache_dir}'")

        # Check cache and download if needed
        check = _powershell(f"Test-Path '{tarball}'").strip()
        if check.lower() != "true":
            typer.echo("  Downloading Debian rootfs from Docker Hub...")
            _download_debian_rootfs(tarball)

        # Import the distro
        typer.echo("  Importing WSL2 distro...")
        _wsl(["--import", vm_name, install_path, tarball])

        # Create VM user
        _wsl(["--distribution", vm_name, "--user", "root", "--",
              "useradd", "-m", "-s", "/bin/bash", vm_user])
        _wsl(["--distribution", vm_name, "--user", "root", "--",
              "usermod", "-aG", "sudo", vm_user])
        _wsl(["--distribution", vm_name, "--user", "root", "--",
              "bash", "-c", f"echo '{vm_user} ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/{vm_user}"])

        # Set default user in wsl.conf
        _wsl(["--distribution", vm_name, "--user", "root", "--",
              "bash", "-c",
              f"printf '[user]\\ndefault={vm_user}\\n' > /etc/wsl.conf"])

        typer.echo(f"WSL2 distro '{vm_name}' created")
        return ProvisionResult(
            exec_target=ExecTarget(wsl2=WSL2Target(distro_name=vm_name, user=vm_user)),
            wsl_distro_name=vm_name,
        )

    def start(self, vm: VMRow) -> None:
        typer.echo(f"Starting WSL2 distro '{vm.name}'...")
        _wsl(["--distribution", vm.name, "--", "echo", "started"])
        typer.echo(f"WSL2 distro '{vm.name}' started")

    def stop(self, vm: VMRow) -> None:
        typer.echo(f"Terminating WSL2 distro '{vm.name}'...")
        _wsl(["--terminate", vm.name])
        typer.echo(f"WSL2 distro '{vm.name}' terminated")

    def delete(self, vm: VMRow) -> None:
        typer.echo(f"Unregistering WSL2 distro '{vm.name}'...")
        _wsl(["--unregister", vm.name], check=False)
        # Clean up install directory
        install_path = f"{WSL_BASE_PATH}\\{vm.name}"
        _powershell(
            f"Remove-Item -Recurse -Force -Path '{install_path}'"
            " -ErrorAction SilentlyContinue",
            check=False,
        )
        typer.echo(f"WSL2 distro '{vm.name}' deleted")

    def exec_target(self, vm: VMRow) -> ExecTarget:
        return ExecTarget(wsl2=WSL2Target(distro_name=vm.name, user=vm.vm_user))

    def status(self, vm: VMRow) -> VMStatus:
        try:
            output = _wsl(["--list", "--verbose"], check=False)
        except RuntimeError:
            return VMStatus.UNKNOWN

        for line in output.strip().splitlines():
            parts = line.split()
            # WSL --list --verbose output: [*] NAME STATE VERSION
            # Filter to find our distro
            name_candidates = [p for p in parts if p == vm.name]
            if not name_candidates:
                continue
            state_str = parts[-2].lower() if len(parts) >= 3 else ""
            if state_str == "running":
                return VMStatus.RUNNING
            if state_str == "stopped":
                return VMStatus.STOPPED
            return VMStatus.UNKNOWN
        return VMStatus.UNKNOWN
