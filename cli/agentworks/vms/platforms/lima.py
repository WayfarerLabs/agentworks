"""The Lima VM platform -- local limactl, or limactl over SSH when the
site's ``platform_config`` declares a ``vm_host``."""

from __future__ import annotations

import json
import shlex
import tempfile
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from agentworks import output
from agentworks.db import VMStatus
from agentworks.errors import ConfigError, StateError
from agentworks.ssh import SSHError, SSHTarget, copy_to
from agentworks.ssh import run as ssh_run
from agentworks.transports import LimaTransport, RemoteLimaTransport, SSHTransport
from agentworks.vms.base import ProvisionRequest, ProvisionResult, VMPlatform
from agentworks.vms.bootstrap_script import generate_bootstrap_script, parse_bootstrap_output
from agentworks.vms.cloud_init import PROVISIONING_PACKAGES

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.resources.reference import ConfigReference
    from agentworks.transports import Transport

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


class LimaPlatform(VMPlatform):
    """Runs VMs via limactl, locally or on a remote host over SSH."""

    name: ClassVar[str] = "lima"
    description: ClassVar[str] = "Lima VMs (local, or on a remote host via SSH)"

    @classmethod
    def validate_config(
        cls, owner: str, config: Mapping[str, object]
    ) -> tuple[ConfigReference, ...]:
        vm_host = config.get("vm_host")
        if vm_host is not None and (not isinstance(vm_host, str) or not vm_host):
            raise ConfigError(
                f"{owner}.vm_host must be a non-empty SSH host string "
                f"(e.g. 'user@host'), got {vm_host!r}"
            )
        unknown = sorted(set(config) - {"vm_host"})
        if unknown:
            raise ConfigError(
                f"{owner}: unknown lima platform field(s): {', '.join(unknown)}"
            )
        return ()

    @classmethod
    def shared_backend(cls, platform_config: Mapping[str, object]) -> bool:
        # A remote Lima box can be shared between installs; local Lima
        # collides only within one workstation user.
        return "vm_host" in platform_config

    @classmethod
    def legacy_platform_metadata(
        cls, row: Mapping[str, Any], legacy: Mapping[str, Any]
    ) -> dict[str, str]:
        # Legacy Lima ops keyed off vm.name directly; the instance name
        # IS the VM name for every existing row.
        return {"instance_name": str(row["name"])}

    @property
    def _vm_host_ssh(self) -> str | None:
        vm_host = self.platform_config.get("vm_host")
        return str(vm_host) if vm_host else None

    @property
    def is_remote(self) -> bool:
        return self._vm_host_ssh is not None

    def _instance_name(self, vm: VMRow) -> str:
        name = vm.platform_metadata.get("instance_name")
        if not name:
            raise StateError(
                f"VM '{vm.name}' has no lima instance_name in its platform "
                f"metadata; the DB row is incomplete",
                entity_kind="vm",
                entity_name=vm.name,
            )
        return str(name)

    def _run_lima(self, command: str, *, check: bool = True) -> str:
        """Run a limactl command, locally or on the site's vm_host."""
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

    def create(self, request: ProvisionRequest) -> ProvisionResult:
        if not self.is_remote:
            import shutil

            if not shutil.which("limactl"):
                from agentworks.errors import ConnectivityError

                # Mirrors the 'tailscale' / 'tailscale status' precedent in
                # initializer.py: a required local CLI tool is missing or
                # unreachable, which is a transport-level problem rather
                # than a state mismatch on a managed entity.
                raise ConnectivityError(
                    "'limactl' not found. Lima is not installed on this machine.",
                    hint=(
                        "For remote Lima VMs, declare a vm-site with "
                        "platform_config.vm_host and pass it via --site."
                    ),
                )

        cpus = request.cpus if request.cpus is not None else 4
        memory = request.memory_gib if request.memory_gib is not None else 8
        disk = request.disk_gib if request.disk_gib is not None else 50
        swap = request.swap_gib if request.swap_gib is not None else 0

        # The platform owns the backend-side name; the slug is
        # the namespacing token. Pre-flight collision check (lima
        # instance names are the primary identifier -- error, never
        # suffix).
        instance_name = (
            f"{request.system_slug}-{request.vm_name}"
            if request.system_slug
            else request.vm_name
        )
        if self._instance_exists(instance_name):
            raise StateError(
                f"a Lima instance named '{instance_name}' already exists"
                + (f" on '{self._vm_host_ssh}'" if self.is_remote else ""),
                entity_kind="vm",
                entity_name=request.vm_name,
                hint=(
                    "delete it first (limactl delete) or pick a different "
                    "VM name"
                ),
            )

        if self.is_remote:
            output.info(f"Connecting to VM host '{self._vm_host_ssh}'...")
        output.info(
            f"Creating Lima VM '{instance_name}' "
            f"({'remote' if self.is_remote else 'local'})..."
        )
        output.detail(f"Resources: {cpus} CPUs, {memory} GiB memory, {disk} GiB disk")
        if swap > 0:
            output.detail(f"Swap: {swap} GiB")

        # Generate the full bootstrap script and embed in the Lima provision block.
        # This handles user creation, system packages, swap, SSH key, and Tailscale.
        if request.tailscale_auth_key:
            provision_script = generate_bootstrap_script(
                admin_username=request.admin_username,
                ssh_public_key=request.ssh_public_key,
                provisioning_packages=PROVISIONING_PACKAGES,
                tailscale_auth_key=request.tailscale_auth_key,
                hostname=request.hostname,
                swap=swap,
            )
        else:
            # No Tailscale key -- provision block is a no-op.
            # Phase A bootstrap will handle everything separately.
            provision_script = (
                "#!/bin/bash\necho '##STEP## Provision'\necho '##SUCCESS## no-op (deferred to Phase A)'\n"
            )

        # Indent the provision script for YAML embedding (6 spaces)
        indented_script = textwrap.indent(provision_script, "      ")
        rendered = LIMA_TEMPLATE.format(
            cpus=cpus, memory=memory, disk=disk, provision_script=indented_script
        )

        if self.is_remote:
            self._create_remote(instance_name, rendered)
        else:
            self._create_local(instance_name, rendered)

        output.detail(f"Lima VM '{instance_name}' created.")

        # If Tailscale was provisioned via the provision block, extract the IP
        tailscale_ip = None
        bootstrap_complete = False
        if request.tailscale_auth_key:
            output.detail("Retrieving Tailscale IP...")
            try:
                ip_output = self._run_lima(
                    f"limactl shell {instance_name} sudo tailscale ip -4"
                )
                tailscale_ip = ip_output.strip()
                bootstrap_complete = True
                output.detail(f"Tailscale IP: {tailscale_ip}")
            except SSHError as e:
                output.warn(f"could not retrieve Tailscale IP: {e}")
                output.warn("Tailscale will be set up during Phase A bootstrap.")

        return ProvisionResult(
            native_transport=self._transport_for(instance_name),
            platform_metadata={"instance_name": instance_name},
            bootstrap_complete=bootstrap_complete,
            tailscale_ip=tailscale_ip,
        )

    def _instance_exists(self, instance_name: str) -> bool:
        """Pre-flight: does a Lima instance with this name exist?"""
        try:
            listing = self._run_lima(
                f"limactl list --json {instance_name}", check=False
            )
        except SSHError:
            return False
        for line in listing.strip().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("name") == instance_name:
                return True
        return False

    def _transport_for(self, instance_name: str) -> Transport:
        if self.is_remote:
            assert self._vm_host_ssh is not None
            return RemoteLimaTransport(
                vm_name=instance_name, vm_host_ssh=self._vm_host_ssh
            )
        return LimaTransport(vm_name=instance_name)

    def _create_local(self, instance_name: str, lima_yaml: str) -> None:
        """Create and start a Lima VM locally."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(lima_yaml)
            template_path = f.name

        try:
            self._run_lima(
                f"limactl create --name {instance_name} --tty=false {template_path}"
            )
            self._run_lima(f"limactl start {instance_name}")
        except SSHError:
            self._log_provision_errors(instance_name)
            raise
        finally:
            Path(template_path).unlink(missing_ok=True)

    def _create_remote(self, instance_name: str, lima_yaml: str) -> None:
        """Create and start a Lima VM on the site's vm_host."""
        assert self._vm_host_ssh is not None
        target = SSHTarget(host=self._vm_host_ssh, user=None)

        # Write Lima YAML locally and copy to VM Host
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(lima_yaml)
            local_template = f.name

        remote_template = f"/tmp/agentworks-{instance_name}.yaml"
        copy_to(target, local_template, remote_template)
        Path(local_template).unlink()

        # Run limactl create + start as a single detached operation
        from agentworks.remote_exec import run_detached
        from agentworks.ssh import SSHLogger

        ssh_logger = SSHLogger(instance_name, "vm-provision")
        host_target = SSHTransport(
            host=self._vm_host_ssh,
            user=None,
            login_shell=True,
            logger=ssh_logger,
        )
        lima_cmd = (
            f"limactl create --name {instance_name} --tty=false {remote_template} "
            f"&& limactl start {instance_name}"
        )
        output.detail("Starting and provisioning VM via Lima (this may take several minutes)...")
        # reuse_completed=False: creation is one-shot, so a leftover
        # status file can only be stale garbage from an interrupted
        # attempt -- consuming it would report a phantom result for a
        # limactl run that never happened.
        result = run_detached(
            host_target,
            lima_cmd,
            label=f"Lima ({instance_name})",
            base_path=f"/tmp/agentworks-lima-{instance_name}",
            timeout=600,
            quiet=True,
            reuse_completed=False,
        )
        try:
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
        finally:
            # Clean up the remote temp file on success AND failure (these
            # were accumulating in /tmp on the VM host after failures).
            ssh_run(target, f"rm -f {remote_template}", check=False)

    def _log_provision_errors(self, instance_name: str) -> None:
        """Attempt to surface provision script errors from Lima logs."""
        try:
            log_output = self._run_lima(
                f"limactl shell {instance_name} cat /var/log/cloud-init-output.log 2>/dev/null || true",
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
        self._run_lima(f"limactl start {self._instance_name(vm)}")
        output.info(f"Lima VM '{vm.name}' started")

    def stop(self, vm: VMRow) -> None:
        output.info(f"Stopping Lima VM '{vm.name}'...")
        self._run_lima(f"limactl stop {self._instance_name(vm)}")
        output.info(f"Lima VM '{vm.name}' stopped")

    def delete(self, vm: VMRow) -> None:
        output.info(f"Deleting Lima VM '{vm.name}'...")
        self._run_lima(
            f"limactl delete --force {self._instance_name(vm)}", check=False
        )
        output.info(f"Lima VM '{vm.name}' deleted")

    def display_backend_name(self, vm: VMRow) -> str:
        instance = str(vm.platform_metadata.get("instance_name", vm.name))
        if self.is_remote:
            return f"{instance}@{self._vm_host_ssh}"
        return instance

    def native_transport(
        self, vm: VMRow, *, config: Config | None = None,
    ) -> Transport | None:
        return self._transport_for(self._instance_name(vm))

    def status(self, vm: VMRow) -> VMStatus:
        instance_name = self._instance_name(vm)
        try:
            listing = self._run_lima(
                f"limactl list --json {instance_name}", check=False
            )
        except SSHError:
            return VMStatus.UNKNOWN

        for line in listing.strip().splitlines():
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
