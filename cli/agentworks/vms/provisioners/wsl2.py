"""WSL2 provisioner -- imports Debian distros on Windows."""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import urllib.request
from pathlib import Path
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
_DOCKER_AUTH_URL = "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/debian:pull"
_DOCKER_MANIFESTS_URL = "https://registry-1.docker.io/v2/library/debian/manifests/bookworm"
_DOCKER_BLOBS_URL = "https://registry-1.docker.io/v2/library/debian/blobs"

# Map Python's platform.machine() to OCI architecture names
_ARCH_MAP = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def _oci_arch() -> str:
    """Return the OCI architecture name for the host machine."""
    machine = platform.machine().lower()
    arch = _ARCH_MAP.get(machine)
    if arch is None:
        raise RuntimeError(f"Unsupported architecture: {machine}")
    return arch


class _StripAuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Strip Authorization header when following redirects to a different host.

    Docker Hub blob requests return a 302 to a CDN. The CDN rejects the
    Bearer token with 400 Bad Request, so we must drop it on redirect.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)  # type: ignore[arg-type]
        if new_req is not None:
            new_req.remove_header("Authorization")
        return new_req


_blob_opener = urllib.request.build_opener(_StripAuthRedirectHandler)


def _wsl(args: list[str], *, check: bool = True, timeout: int = 300) -> str:
    """Run a wsl.exe command and return stdout."""
    result = subprocess.run(["wsl", *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(f"wsl command failed: {result.stderr.strip()}")
    return result.stdout


def _powershell(script: str, *, check: bool = True, timeout: int = 120) -> str:
    """Run a PowerShell command and return stdout."""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
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
    # 1. Get anonymous pull token
    typer.echo("  Authenticating with Docker Hub...")
    with urllib.request.urlopen(_DOCKER_AUTH_URL) as resp:
        token = json.loads(resp.read())["token"]

    # 2. Fetch image manifest to find the rootfs layer digest.
    #    debian:bookworm is multi-arch, so we first get the manifest list
    #    and resolve the platform-specific manifest for the host architecture.
    typer.echo("  Fetching Debian bookworm image manifest...")
    auth_header = {"Authorization": f"Bearer {token}"}

    req = urllib.request.Request(
        _DOCKER_MANIFESTS_URL,
        headers={
            **auth_header,
            "Accept": (
                "application/vnd.docker.distribution.manifest.list.v2+json, "
                "application/vnd.docker.distribution.manifest.v2+json"
            ),
        },
    )
    with urllib.request.urlopen(req) as resp:
        manifest = json.loads(resp.read())

    # If it's a manifest list, resolve the entry for the host architecture
    if "manifests" in manifest:
        arch = _oci_arch()
        match = next(
            (
                m
                for m in manifest["manifests"]
                if m.get("platform", {}).get("architecture") == arch and m.get("platform", {}).get("os") == "linux"
            ),
            None,
        )
        if match is None:
            raise RuntimeError(f"No {arch}/linux manifest found for debian:bookworm")
        platform_digest = match["digest"]
        manifest_url = f"https://registry-1.docker.io/v2/library/debian/manifests/{platform_digest}"
        req = urllib.request.Request(
            manifest_url,
            headers={
                **auth_header,
                "Accept": "application/vnd.docker.distribution.manifest.v2+json",
            },
        )
        with urllib.request.urlopen(req) as resp:
            manifest = json.loads(resp.read())

    digest = manifest["layers"][0]["digest"]
    total_bytes = manifest["layers"][0].get("size", 0)

    # 3. Download the rootfs layer with progress
    blob_url = f"{_DOCKER_BLOBS_URL}/{digest}"
    req = urllib.request.Request(blob_url, headers=auth_header)
    size_mb = f" (~{total_bytes // 1024 // 1024} MB)" if total_bytes else ""
    typer.echo(f"  Downloading Debian rootfs{size_mb}...")

    dest = Path(tarball_path)
    with _blob_opener.open(req) as resp, dest.open("wb") as f:
        downloaded = 0
        chunk_size = 256 * 1024
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if total_bytes:
                pct = downloaded * 100 // total_bytes
                mb = downloaded // 1024 // 1024
                total_mb = total_bytes // 1024 // 1024
                sys.stderr.write(f"\r  Progress: {mb}/{total_mb} MB ({pct}%)")
                sys.stderr.flush()
        if total_bytes:
            sys.stderr.write("\n")

    typer.echo("  Download complete.")


class WSL2Provisioner(VMProvisioner):
    """Provisions WSL2 Debian distributions on Windows."""

    def create(
        self,
        vm_name: str,
        config: Config,
        *,
        admin_username: str = "agentworks",
    ) -> ProvisionResult:
        typer.echo(f"Provisioning WSL2 VM '{vm_name}'...")

        install_path = f"{WSL_BASE_PATH}\\{vm_name}"
        _powershell(f"New-Item -ItemType Directory -Force -Path '{install_path}'")

        # Download Debian rootfs if not cached
        cache_dir = "%LOCALAPPDATA%\\agentworks\\cache"
        tarball = f"{cache_dir}\\debian-bookworm-{_oci_arch()}-rootfs.tar.gz"
        _powershell(f"New-Item -ItemType Directory -Force -Path '{cache_dir}'")

        check = _powershell(f"Test-Path '{tarball}'").strip()
        if check.lower() != "true":
            _download_debian_rootfs(tarball)
        else:
            typer.echo("  Using cached Debian rootfs.")

        # Import and configure the distro
        typer.echo("  Importing rootfs into WSL2...")
        _wsl(["--import", vm_name, install_path, tarball])

        # The Docker rootfs is minimal. Install packages to bring it up to
        # parity with the Lima/Azure cloud images.
        typer.echo("  Installing base packages...")
        _wsl(
            [
                "--distribution",
                vm_name,
                "--user",
                "root",
                "--",
                "bash",
                "-c",
                "apt-get update -qq"
                " && apt-get install -y -qq"
                " bash bash-completion sudo passwd"
                " openssh-server curl git ca-certificates"
                " tmux tmuxinator"
                " locales procps iproute2 iputils-ping"
                " less vim-tiny man-db"
                " > /dev/null",
            ]
        )

        # Configure swap file
        if config.vm.swap_gb > 0:
            swap_mb = config.vm.swap_gb * 1024
            typer.echo(f"  Setting up {config.vm.swap_gb} GiB swap file...")
            _wsl(
                [
                    "--distribution",
                    vm_name,
                    "--user",
                    "root",
                    "--",
                    "bash",
                    "-c",
                    f"fallocate -l {swap_mb}M /swapfile"
                    " && chmod 600 /swapfile"
                    " && mkswap /swapfile"
                    " && swapon /swapfile"
                    " && echo '/swapfile none swap sw 0 0' >> /etc/fstab",
                ]
            )

        # Create user account
        typer.echo(f"  Creating user '{admin_username}'...")
        _wsl(["--distribution", vm_name, "--user", "root", "--", "useradd", "-m", "-s", "/bin/bash", admin_username])
        _wsl(["--distribution", vm_name, "--user", "root", "--", "usermod", "-aG", "sudo", admin_username])
        import shlex

        _wsl(
            [
                "--distribution",
                vm_name,
                "--user",
                "root",
                "--",
                "bash",
                "-c",
                f"echo {shlex.quote(f'{admin_username} ALL=(ALL) NOPASSWD:ALL')}"
                f" > /etc/sudoers.d/{shlex.quote(admin_username)}",
            ]
        )

        # Configure wsl.conf: default user + systemd
        typer.echo("  Enabling systemd...")
        _wsl(
            [
                "--distribution",
                vm_name,
                "--user",
                "root",
                "--",
                "bash",
                "-c",
                f"printf '[user]\\ndefault={shlex.quote(admin_username)}"
                f"\\n\\n[boot]\\nsystemd=true\\n' > /etc/wsl.conf",
            ]
        )

        # Restart the distro so systemd takes effect
        typer.echo("  Restarting distro...")
        _wsl(["--terminate", vm_name])
        # Run a command to trigger the distro to start with systemd
        _wsl(["--distribution", vm_name, "--user", "root", "--", "bash", "-c", "echo ok"])

        typer.echo(f"  WSL2 VM '{vm_name}' provisioned.")
        return ProvisionResult(
            exec_target=ExecTarget(wsl2=WSL2Target(distro_name=vm_name, user=admin_username)),
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
            f"Remove-Item -Recurse -Force -Path '{install_path}' -ErrorAction SilentlyContinue",
            check=False,
        )
        typer.echo(f"WSL2 distro '{vm.name}' deleted")

    def exec_target(self, vm: VMRow, *, config: object | None = None) -> ExecTarget:
        return ExecTarget(wsl2=WSL2Target(distro_name=vm.name, user=vm.admin_username))

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
