"""Lima VM provisioner -- local and remote VM Host variants."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from agentworks.db import VMStatus
from agentworks.ssh import ExecTarget, LimaTarget, SSHError, SSHTarget, copy_to
from agentworks.ssh import run as ssh_run
from agentworks.vms.base import ProvisionResult, VMProvisioner

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import VMRow

# Lima template for Debian cloud VMs (values substituted at create time)
LIMA_TEMPLATE = """\
# Agentworks Debian VM template for Lima
arch: default
images:
  - location: https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2
    arch: x86_64
  - location: https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-arm64.qcow2
    arch: aarch64
cpus: {cpus}
memory: {memory}GiB
disk: {disk}GiB
ssh:
  localPort: 0
mountType: virtiofs
"""


class LimaProvisioner(VMProvisioner):
    """Provisions Lima VMs, either locally or on a remote VM Host."""

    def __init__(self, vm_host_ssh: str | None = None) -> None:
        """Initialize the Lima provisioner.

        Args:
            vm_host_ssh: SSH host for remote mode. None for local mode.
        """
        self._vm_host_ssh = vm_host_ssh

    @property
    def is_remote(self) -> bool:
        return self._vm_host_ssh is not None

    def _run_lima(self, command: str, *, check: bool = True) -> str:
        """Run a limactl command, locally or on the VM Host."""
        if self.is_remote:
            assert self._vm_host_ssh is not None
            target = SSHTarget(host=self._vm_host_ssh, user="")
            result = ssh_run(target, command, check=check)
            return result.stdout
        else:
            import subprocess

            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
            )
            if check and proc.returncode != 0:
                raise SSHError(f"limactl failed: {proc.stderr.strip()}")
            return proc.stdout

    def create(
        self,
        vm_name: str,
        config: Config,
        extra_packages: list[str] | None = None,
        *,
        cpus: int = 4,
        memory: int = 8,
        disk: int = 50,
    ) -> ProvisionResult:
        typer.echo(f"Creating Lima VM '{vm_name}' ({'remote' if self.is_remote else 'local'})...")
        typer.echo(f"  Resources: {cpus} CPUs, {memory} GiB memory, {disk} GiB disk")

        rendered = LIMA_TEMPLATE.format(cpus=cpus, memory=memory, disk=disk)

        if self.is_remote:
            assert self._vm_host_ssh is not None
            target = SSHTarget(host=self._vm_host_ssh, user="")
            # Write template to a temp file and copy to VM Host
            with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
                f.write(rendered)
                local_template = f.name
            remote_template = f"/tmp/agentworks-{vm_name}.yaml"
            copy_to(target, local_template, remote_template)
            Path(local_template).unlink()

            self._run_lima(f"limactl create --name {vm_name} --tty=false {remote_template}")
            self._run_lima(f"limactl start {vm_name}")
        else:
            # Write template locally
            with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
                f.write(rendered)
                template_path = f.name

            self._run_lima(f"limactl create --name {vm_name} --tty=false {template_path}")
            self._run_lima(f"limactl start {vm_name}")
            Path(template_path).unlink()

        typer.echo(f"Lima VM '{vm_name}' created and running")

        if self.is_remote:
            # Remote: need SSH via VM Host proxy
            ssh_info = self._get_ssh_target(vm_name)
            return ProvisionResult(exec_target=ExecTarget(ssh=ssh_info))
        else:
            # Local: use limactl shell as provisioning transport
            return ProvisionResult(exec_target=ExecTarget(lima=LimaTarget(vm_name=vm_name)))

    def start(self, vm: VMRow) -> None:
        typer.echo(f"Starting Lima VM '{vm.name}'...")
        self._run_lima(f"limactl start {vm.name}")
        typer.echo(f"Lima VM '{vm.name}' started")

    def stop(self, vm: VMRow) -> None:
        typer.echo(f"Stopping Lima VM '{vm.name}'...")
        self._run_lima(f"limactl stop {vm.name}")
        typer.echo(f"Lima VM '{vm.name}' stopped")

    def delete(self, vm: VMRow) -> None:
        typer.echo(f"Deleting Lima VM '{vm.name}'...")
        self._run_lima(f"limactl delete --force {vm.name}", check=False)
        typer.echo(f"Lima VM '{vm.name}' deleted")

    def exec_target(self, vm: VMRow) -> ExecTarget:
        if self.is_remote:
            return ExecTarget(ssh=self._get_ssh_target(vm.name))
        return ExecTarget(lima=LimaTarget(vm_name=vm.name))

    def status(self, vm: VMRow) -> VMStatus:
        try:
            output = self._run_lima(f"limactl list --json {vm.name}", check=False)
        except SSHError:
            return VMStatus.UNKNOWN

        for line in output.strip().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            raw_status = entry.get("status", "").lower()
            if raw_status == "running":
                return VMStatus.RUNNING
            if raw_status == "stopped":
                return VMStatus.STOPPED
            return VMStatus.UNKNOWN
        return VMStatus.UNKNOWN

    def _get_ssh_target(self, vm_name: str) -> SSHTarget:
        """Parse SSH connection info from limactl."""
        output = self._run_lima(f"limactl show-ssh --format=options {vm_name}")
        # Parse SSH options like: -o IdentityFile="/path" -o Port=12345 -o Hostname=127.0.0.1 -o User=user
        host = "127.0.0.1"
        user = "agentworks"
        port = None
        identity_file = None
        proxy_jump = None

        parts = output.split("-o ")
        for part in parts:
            part = part.strip().rstrip()
            if part.startswith("Hostname="):
                host = part.split("=", 1)[1].strip('"')
            elif part.startswith("Port="):
                port = int(part.split("=", 1)[1].strip('"'))
            elif part.startswith("User="):
                user = part.split("=", 1)[1].strip('"')
            elif part.startswith("IdentityFile="):
                identity_file = Path(part.split("=", 1)[1].strip('"'))

        if self.is_remote:
            # Remote mode: proxy through VM Host
            proxy_jump = self._vm_host_ssh

        return SSHTarget(
            host=host,
            user=user,
            port=port,
            identity_file=identity_file,
            proxy_jump=proxy_jump,
        )
