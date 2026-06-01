"""Lima VM provisioner -- local and remote VM Host variants."""

from __future__ import annotations

import json
import shlex
import tempfile
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.db import VMStatus
from agentworks.ssh import ExecTarget, LimaTarget, RemoteLimaTarget, SSHError, SSHTarget, copy_to
from agentworks.ssh import run as ssh_run
from agentworks.vms.base import ProvisionResult, VMProvisioner
from agentworks.vms.bootstrap_script import generate_bootstrap_script, parse_bootstrap_output, vm_hostname
from agentworks.vms.cloud_init import PROVISIONING_PACKAGES

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import VMRow

# Lima template for Debian cloud VMs (values substituted at create time).
# The provision block runs the full bootstrap script (user, packages, swap,
# SSH key, Tailscale) as a system-level provisioner during limactl start.
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
provision:
  - mode: system
    script: |
{provision_script}
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
            target = SSHTarget(host=self._vm_host_ssh, user=None, login_shell=True)
            result = ssh_run(target, command, check=check)
            return result.stdout
        else:
            import subprocess

            proc = subprocess.run(
                shlex.split(command),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if check and proc.returncode != 0:
                raise SSHError(f"limactl failed: {proc.stderr.strip()}")
            return proc.stdout

    def create(
        self,
        vm_name: str,
        config: Config,
        *,
        cpus: int = 4,
        memory: int = 8,
        disk: int = 50,
        tailscale_auth_key: str | None = None,
    ) -> ProvisionResult:
        if not self.is_remote:
            import shutil

            if not shutil.which("limactl"):
                from agentworks.errors import StateError

                raise StateError(
                    "'limactl' not found. Lima is not installed on this machine.",
                    hint=(
                        "For remote Lima VMs, set defaults.vm_host in your config "
                        "or pass --vm-host."
                    ),
                )

        if self.is_remote:
            output.info(f"Connecting to VM host '{self._vm_host_ssh}'...")
        output.info(f"Creating Lima VM '{vm_name}' ({'remote' if self.is_remote else 'local'})...")
        output.detail(f"Resources: {cpus} CPUs, {memory} GiB memory, {disk} GiB disk")
        if config.vm.swap > 0:
            output.detail(f"Swap: {config.vm.swap} GiB")

        # Generate the full bootstrap script and embed in the Lima provision block.
        # This handles user creation, system packages, swap, SSH key, and Tailscale.
        if tailscale_auth_key:
            ssh_pub_key = config.operator.ssh_public_key.read_text().strip()
            provision_script = generate_bootstrap_script(
                admin_username=config.admin.username,
                ssh_public_key=ssh_pub_key,
                provisioning_packages=PROVISIONING_PACKAGES,
                tailscale_auth_key=tailscale_auth_key,
                hostname=vm_hostname("lima", vm_name),
                swap=config.vm.swap,
            )
        else:
            # No Tailscale key -- provision block is a no-op.
            # Phase A bootstrap will handle everything separately.
            provision_script = (
                "#!/bin/bash\necho '##STEP## Provision'\necho '##SUCCESS## no-op (deferred to Phase A)'\n"
            )

        # Indent the provision script for YAML embedding (6 spaces)
        indented_script = textwrap.indent(provision_script, "      ")
        rendered = LIMA_TEMPLATE.format(cpus=cpus, memory=memory, disk=disk, provision_script=indented_script)

        if self.is_remote:
            self._create_remote(vm_name, rendered)
        else:
            self._create_local(vm_name, rendered)

        output.detail(f"Lima VM '{vm_name}' created.")

        # If Tailscale was provisioned via the provision block, extract the IP
        tailscale_ip = None
        bootstrap_complete = False
        if tailscale_auth_key:
            output.detail("Retrieving Tailscale IP...")
            try:
                ip_output = self._run_lima(f"limactl shell {vm_name} sudo tailscale ip -4")
                tailscale_ip = ip_output.strip()
                bootstrap_complete = True
                output.detail(f"Tailscale IP: {tailscale_ip}")
            except SSHError as e:
                output.warn(f"could not retrieve Tailscale IP: {e}")
                output.warn("Tailscale will be set up during Phase A bootstrap.")

        if self.is_remote:
            assert self._vm_host_ssh is not None
            return ProvisionResult(
                admin_exec_target=ExecTarget(
                    remote_lima=RemoteLimaTarget(vm_name=vm_name, vm_host_ssh=self._vm_host_ssh),
                ),
                bootstrap_complete=bootstrap_complete,
                tailscale_ip=tailscale_ip,
            )
        else:
            return ProvisionResult(
                admin_exec_target=ExecTarget(lima=LimaTarget(vm_name=vm_name)),
                bootstrap_complete=bootstrap_complete,
                tailscale_ip=tailscale_ip,
            )

    def _create_local(self, vm_name: str, lima_yaml: str) -> None:
        """Create and start a Lima VM locally."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(lima_yaml)
            template_path = f.name

        try:
            self._run_lima(f"limactl create --name {vm_name} --tty=false {template_path}")
            self._run_lima(f"limactl start {vm_name}")
        except SSHError:
            self._log_provision_errors(vm_name)
            raise
        finally:
            Path(template_path).unlink(missing_ok=True)

    def _create_remote(self, vm_name: str, lima_yaml: str) -> None:
        """Create and start a Lima VM on a remote VM Host."""
        assert self._vm_host_ssh is not None
        target = SSHTarget(host=self._vm_host_ssh, user=None)

        # Write Lima YAML locally and copy to VM Host
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(lima_yaml)
            local_template = f.name

        remote_template = f"/tmp/agentworks-{vm_name}.yaml"
        copy_to(target, local_template, remote_template)
        Path(local_template).unlink()

        # Run limactl create + start as a single detached operation
        from agentworks.remote_exec import run_detached
        from agentworks.ssh import SSHLogger

        ssh_logger = SSHLogger(vm_name, "vm-provision")
        host_target = ExecTarget(
            ssh=SSHTarget(host=self._vm_host_ssh, user=None, login_shell=True),
            logger=ssh_logger,
        )
        lima_cmd = f"limactl create --name {vm_name} --tty=false {remote_template} && limactl start {vm_name}"
        output.detail("Starting and provisioning VM via Lima (this may take several minutes)...")
        result = run_detached(
            host_target,
            lima_cmd,
            label=f"Lima ({vm_name})",
            base_path=f"/tmp/agentworks-lima-{vm_name}",
            timeout=600,
            quiet=True,
        )
        if result.exit_code != 0:
            # Parse structured markers from provision script output if present
            bootstrap = parse_bootstrap_output(result.output, result.exit_code)
            for step in bootstrap.steps:
                if step.error:
                    ssh_logger.log_error(f"Provision step '{step.name}': {step.error}")

            ssh_logger.log_error(f"limactl failed (exit {result.exit_code})")
            ssh_logger.log_error(result.output)
            ssh_logger.close()
            raise SSHError(
                f"limactl create/start failed (exit {result.exit_code})\n"
                f"SSH log: {ssh_logger.path}\n"
                f"Last output:\n{result.output[-1000:]}"
            )
        ssh_logger.close()

        # Clean up remote temp file
        ssh_run(target, f"rm -f {remote_template}", check=False)

    def _log_provision_errors(self, vm_name: str) -> None:
        """Attempt to surface provision script errors from Lima logs."""
        try:
            log_output = self._run_lima(
                f"limactl shell {vm_name} cat /var/log/cloud-init-output.log 2>/dev/null || true",
                check=False,
            )
            if log_output.strip():
                bootstrap = parse_bootstrap_output(log_output, 1)
                for step in bootstrap.steps:
                    if step.error:
                        output.warn(f"Provision error ({step.name}): {step.error}")
        except SSHError:
            pass

    def start(self, vm: VMRow) -> None:
        output.info(f"Starting Lima VM '{vm.name}'...")
        self._run_lima(f"limactl start {vm.name}")
        output.info(f"Lima VM '{vm.name}' started")

    def stop(self, vm: VMRow) -> None:
        output.info(f"Stopping Lima VM '{vm.name}'...")
        self._run_lima(f"limactl stop {vm.name}")
        output.info(f"Lima VM '{vm.name}' stopped")

    def delete(self, vm: VMRow) -> None:
        output.info(f"Deleting Lima VM '{vm.name}'...")
        self._run_lima(f"limactl delete --force {vm.name}", check=False)
        output.info(f"Lima VM '{vm.name}' deleted")

    def admin_exec_target(self, vm: VMRow, *, config: object | None = None) -> ExecTarget:
        if self.is_remote:
            assert self._vm_host_ssh is not None
            return ExecTarget(
                remote_lima=RemoteLimaTarget(vm_name=vm.name, vm_host_ssh=self._vm_host_ssh),
            )
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
