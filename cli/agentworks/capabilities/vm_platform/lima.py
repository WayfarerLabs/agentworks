"""The Lima VM platform: local limactl, or limactl over SSH when the
site's ``platform_config`` declares a ``vm_host``."""

from __future__ import annotations

import contextlib
import json
import shlex
import tempfile
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import Field

from agentworks import output
from agentworks.capabilities.vm_platform.base import ProvisionRequest, ProvisionResult, VMPlatform
from agentworks.capabilities.vm_platform.bootstrap_script import (
    REBOOT_SENTINEL_PATH,
    generate_bootstrap_script,
    parse_bootstrap_output,
)
from agentworks.capabilities.vm_platform.cloud_init import PROVISIONING_PACKAGES
from agentworks.db import VMStatus
from agentworks.errors import StateError
from agentworks.schema import AgwModel, NonEmptyStr
from agentworks.ssh import SSHError, SSHTarget, copy_to
from agentworks.ssh import run as ssh_run
from agentworks.topics import TopicProse
from agentworks.transports import LimaTransport, RemoteLimaTransport, SSHTransport

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.capabilities.base import RunContext
    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.resources.graph import Readiness
    from agentworks.ssh import SSHLogger
    from agentworks.transports import Transport

# Markers the restart-sentinel probe echoes on stdout. The probe exits 0
# either way, so an absent sentinel stays a normal result, not an exception.
_REBOOT_PENDING_MARKER = "AGW_REBOOT_PENDING"
_REBOOT_CLEAR_MARKER = "AGW_REBOOT_CLEAR"

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
# No host file sharing: agentworks VMs are self-contained. An explicit empty
# list guarantees no mounts regardless of Lima's host defaults or _config
# overrides. (mountType is moot with zero mounts, so it is omitted.)
mounts: []
provision:
  # Cap oversized subordinate uid/gid ranges. Lima's rootless-base boot
  # script grants the host-matched user a 1 GiB (1073741824) subuid/subgid
  # range, which overruns login.defs SUB_UID_MAX and starves agent-user
  # creation (each new agent needs a free 65536 block). 65536 is the standard
  # rootless allocation and is sufficient. Idempotent, and the boot script's
  # `grep -qw <user>` guard means this correction sticks across reboots (the
  # user stays present, so the range is not re-added).
  - mode: system
    script: |
      #!/bin/sh
      set -eu
      for f in /etc/subuid /etc/subgid; do
        [ -e "$f" ] || continue
        # Atomic replace: write the capped copy to a sibling temp with the
        # same mode/owner, then rename over the original. A mid-stream awk
        # failure leaves the original intact rather than truncated, and the
        # rename is atomic within /etc (never a partially written subid file).
        awk -F: 'BEGIN{{OFS=":"}} $3+0>65536{{$3=65536}} {{print}}' "$f" >"$f.agw"
        chmod --reference="$f" "$f.agw"
        chown --reference="$f" "$f.agw"
        mv "$f.agw" "$f"
      done
  - mode: system
    script: |
{provision_script}
"""


class LimaConfig(AgwModel):
    """Where a Lima site's ``limactl`` runs."""

    name: Literal["lima"]
    """The platform this config is for."""

    vm_host: NonEmptyStr | None = Field(default=None, examples=["me@gpu-box"])
    """The SSH host running ``limactl`` for a REMOTE-Lima site (e.g.
    ``user@host``). Omit for a local site, which needs ``limactl``
    installed here."""


class LimaPlatform(VMPlatform):
    """Runs VMs via limactl, locally or on a remote host over SSH."""

    contract_version: ClassVar[int] = 1
    name: ClassVar[str] = "lima"
    description: ClassVar[str] = "Lima VMs (local, or on a remote host via SSH)"
    config_model: ClassVar[type[LimaConfig]] = LimaConfig
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="Lima",
        overview="""
        Lima runs Linux VMs through `limactl`. A site with no `vm_host` runs them on
        this machine, which is the zero-config starting point and what the built-in
        `lima-local` site is; a site WITH one runs `limactl` on that host over SSH, so
        the VMs live on a shared box and nothing but SSH is needed here.

        Local sites need `limactl` installed here and report not-ready without it.
        Remote sites need nothing locally.
        """,
    )
    # No unsupported_reason override: the platform is supported on
    # every host, because remote-Lima sites run limactl on the vm_host
    # over SSH and need nothing locally.

    @classmethod
    def not_ready(cls, config: Mapping[str, object]) -> Readiness:
        """A LOCAL Lima site (no ``vm_host``) is pointless without a
        local ``limactl``. This covers the bundled ``lima-local``
        site and any operator-declared local site alike; a host that
        later installs Lima enables them on the next look. Remote
        sites need nothing here.

        Non-constructing (LLD c): reads ``config`` fields directly, never
        builds an instance, so the readiness fold stays total over
        unvalidated ``platform_config``."""
        from agentworks.resources.graph import Readiness

        if config.get("vm_host"):
            return Readiness.ready()
        import shutil

        if not shutil.which("limactl"):
            return Readiness.blocked("limactl not installed")
        return Readiness.ready()

    @property
    def config(self) -> LimaConfig:
        """This site's validated lima config."""
        return self._config_as(LimaConfig)

    @classmethod
    def legacy_platform_metadata(cls, row: Mapping[str, Any], legacy: Mapping[str, Any]) -> dict[str, str]:
        # Legacy Lima ops keyed off vm.name directly; the instance name
        # IS the VM name for every existing row.
        return {"instance_name": str(row["name"])}

    @property
    def _vm_host_ssh(self) -> str | None:
        return self.config.vm_host

    @property
    def is_remote(self) -> bool:
        return self._vm_host_ssh is not None

    def _instance_name(self, vm: VMRow) -> str:
        name = vm.platform_metadata.get("instance_name")
        if not name:
            raise StateError(
                f"VM '{vm.name}' has no lima instance_name in its platform metadata; the DB row is incomplete",
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

    def preflight(self, ctx: RunContext) -> None:
        """Local sites: ``limactl`` must be on PATH. Remote sites defer
        to the ops (probing the vm_host over SSH is a real round trip;
        the first op's error is already clear). No config secrets, so
        the operation sweep's central prediction has nothing to check.

        The limactl check ordinarily never fires here: a limactl-less
        local site is not-ready (``not_ready``) before any op reaches
        preflight. It stays as defense for directly-constructed
        instances, not as a disagreement about whose check this is."""
        super().preflight(ctx)
        if not self.is_remote:
            self._ensure_limactl()

    def _ensure_limactl(self) -> None:
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
                    "For remote Lima VMs, declare a vm-site whose "
                    "`platform: {name: lima, vm_host: ...}` names the host, and pass it via --site."
                ),
            )

    def create(self, request: ProvisionRequest, ctx: RunContext) -> ProvisionResult:
        if not self.is_remote:
            # Preflight re-runs the same check at the composition root;
            # keeping it here too costs one PATH scan and keeps the op's
            # error clear for direct callers.
            self._ensure_limactl()

        cpus = request.cpus
        memory = request.memory_gib
        disk = request.disk_gib
        swap = request.swap_gib

        # The platform owns the backend-side name; the slug is
        # the namespacing token. Pre-flight collision check (lima
        # instance names are the primary identifier: error, never
        # suffix).
        instance_name = f"{request.system_slug}-{request.vm_name}" if request.system_slug else request.vm_name
        if self._instance_exists(instance_name):
            raise StateError(
                f"a Lima instance named '{instance_name}' already exists"
                + (f" on '{self._vm_host_ssh}'" if self.is_remote else ""),
                entity_kind="vm",
                entity_name=request.vm_name,
                hint=("delete it first (limactl delete) or pick a different VM name"),
            )

        if self.is_remote:
            output.info(f"Connecting to VM host '{self._vm_host_ssh}'...")
        output.info(f"Creating Lima VM '{instance_name}' ({'remote' if self.is_remote else 'local'})...")
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
            # No Tailscale key: provision block is a no-op.
            # Phase A bootstrap will handle everything separately.
            provision_script = (
                "#!/bin/bash\necho '##STEP## Provision'\necho '##SUCCESS## no-op (deferred to Phase A)'\n"
            )

        # Indent the provision script for YAML embedding (6 spaces)
        indented_script = textwrap.indent(provision_script, "      ")
        rendered = LIMA_TEMPLATE.format(
            cpus=cpus,
            memory=memory,
            disk=disk,
            provision_script=indented_script,
        )

        # Rollback: spans every step from the first backend mutation
        # (limactl create) through the post-start steps (restart
        # sentinel, Tailscale IP). The caller's unwind deletes only the
        # DB row on failure OR interrupt, so an instance left behind
        # here would be orphaned with nothing to target it (#340; the
        # azure precedent is #338). Everything before this try mutates
        # nothing, so no arm ever fires a cleanup call for an instance
        # that was never made.
        try:
            try:
                if self.is_remote:
                    self._create_remote(instance_name, rendered)
                else:
                    self._create_local(instance_name, rendered)

                output.detail(f"Lima VM '{instance_name}' created.")

                # Some bootstrap steps (currently the Apple-vz SVE mask, see
                # bootstrap_script) only take effect after a reboot, and rebooting
                # mid-provision is unreliable (lima-vm/lima#4867). Such steps drop a
                # restart sentinel; restart the instance from the host when we see it.
                # The probe stays generic: the host cannot cheaply tell which guest
                # shape it is, so a bare failure is phrased for what it does know.
                try:
                    restart_pending = self._restart_sentinel_present(instance_name)
                except SSHError as e:
                    output.warn(
                        f"could not check whether '{instance_name}' needs a restart to finish provisioning: {e}"
                    )
                    output.warn(
                        f"if the VM misbehaves, 'limactl restart {instance_name}' "
                        "reapplies any deferred bootstrap step."
                    )
                    restart_pending = False
                if restart_pending:
                    output.detail(f"A bootstrap step needs a reboot; restarting '{instance_name}'...")
                    self._run_lima(f"limactl restart {instance_name}")

                # If Tailscale was provisioned via the provision block, extract the IP
                tailscale_ip = None
                bootstrap_complete = False
                if request.tailscale_auth_key:
                    output.detail("Retrieving Tailscale IP...")
                    try:
                        ip_output = self._run_lima(f"limactl shell {instance_name} sudo tailscale ip -4")
                        tailscale_ip = ip_output.strip()
                        bootstrap_complete = True
                        output.detail(f"Tailscale IP: {tailscale_ip}")
                    except SSHError as e:
                        output.warn(f"could not retrieve Tailscale IP: {e}")
                        output.warn("Tailscale will be set up during Phase A bootstrap.")
            except Exception:
                # Lima's error convention holds: SSHError / StateError
                # propagate unwrapped; the only new obligation is the
                # teardown. _create_local has already surfaced the
                # provision log by the time this runs, so nothing here
                # reads from the instance after it is gone.
                output.detail(f"Cleaning up the partial Lima instance '{instance_name}'...")
                self._cleanup_partial_create(instance_name)
                raise
        except KeyboardInterrupt:
            self._rollback_create_on_interrupt(instance_name)
            raise

        return ProvisionResult(
            native_transport=self._transport_for(instance_name),
            platform_metadata={"instance_name": instance_name},
            bootstrap_complete=bootstrap_complete,
            tailscale_ip=tailscale_ip,
        )

    def _restart_sentinel_present(self, instance_name: str) -> bool:
        """True if a bootstrap step left the restart sentinel in the guest.

        A bootstrap step that needs a reboot to take effect touches
        ``REBOOT_SENTINEL_PATH`` (see ``bootstrap_script``); currently only
        the Apple-vz SVE mask does. The sentinel lives on tmpfs, so it clears
        itself on the restart. The probe stays deliberately why-agnostic: the
        host restarts on the sentinel without needing to know which step set
        it.

        The probe reports its answer on stdout and exits 0 whether or not the
        sentinel is there, so a raised ``SSHError`` means a genuine shell or
        transport failure and never a merely absent sentinel.
        """
        probe = self._run_lima(
            f"limactl shell {instance_name} sh -c "
            f"'test -f {REBOOT_SENTINEL_PATH} "
            f"&& echo {_REBOOT_PENDING_MARKER} || echo {_REBOOT_CLEAR_MARKER}'"
        )
        if _REBOOT_PENDING_MARKER in probe:
            return True
        if _REBOOT_CLEAR_MARKER in probe:
            return False
        raise SSHError(f"unrecognized restart-sentinel probe output: {probe.strip()!r}")

    def _instance_exists(self, instance_name: str) -> bool:
        """Pre-flight: does a Lima instance with this name exist?"""
        try:
            listing = self._run_lima(f"limactl list --json {instance_name}", check=False)
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
            return RemoteLimaTransport(vm_name=instance_name, vm_host_ssh=self._vm_host_ssh)
        return LimaTransport(vm_name=instance_name)

    def _create_local(self, instance_name: str, lima_yaml: str) -> None:
        """Create and start a Lima VM locally."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(lima_yaml)
            template_path = f.name

        try:
            self._run_lima(f"limactl create --name {instance_name} --tty=false {template_path}")
            self._run_lima(f"limactl start {instance_name}")
        except SSHError:
            self._log_provision_errors(instance_name)
            raise
        finally:
            Path(template_path).unlink(missing_ok=True)

    def _host_transport(self, logger: SSHLogger | None = None) -> SSHTransport:
        """An exec transport to the site's vm_host (remote sites only):
        the create path's run_detached target, and the interrupt
        rollback's kill target."""
        assert self._vm_host_ssh is not None
        return SSHTransport(
            host=self._vm_host_ssh,
            user=None,
            login_shell=True,
            logger=logger,
        )

    @staticmethod
    def _remote_base_path(instance_name: str) -> str:
        """run_detached's file identity for this instance's create; the
        interrupt rollback derives the same path to kill the detached
        limactl before deleting the instance."""
        return f"/tmp/agentworks-lima-{instance_name}"

    def _create_remote(self, instance_name: str, lima_yaml: str) -> None:
        """Create and start a Lima VM on the site's vm_host."""
        assert self._vm_host_ssh is not None
        target = SSHTarget(host=self._vm_host_ssh, user=None)

        # Write Lima YAML locally and copy to VM Host
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(lima_yaml)
            local_template = f.name

        remote_template = f"/tmp/agentworks-{instance_name}.yaml"
        try:
            copy_to(target, local_template, remote_template)
        finally:
            Path(local_template).unlink(missing_ok=True)

        # Run limactl create + start as a single detached operation
        from agentworks.remote_exec import run_detached
        from agentworks.ssh import SSHLogger

        ssh_logger = SSHLogger(instance_name, "vm-provision")
        host_target = self._host_transport(logger=ssh_logger)
        lima_cmd = (
            f"limactl create --name {instance_name} --tty=false {remote_template} && limactl start {instance_name}"
        )
        output.detail("Starting and provisioning VM via Lima (this may take several minutes)...")
        try:
            # reuse_completed=False: creation is one-shot, so a leftover
            # status file can only be stale garbage from an interrupted
            # attempt; consuming it would report a phantom result for a
            # limactl run that never happened.
            result = run_detached(
                host_target,
                lima_cmd,
                label=f"Lima ({instance_name})",
                base_path=self._remote_base_path(instance_name),
                timeout=600,
                quiet=True,
                reuse_completed=False,
            )
            if result.exit_code != 0:
                # Parse structured markers from provision script output if present
                bootstrap = parse_bootstrap_output(result.output, result.exit_code)
                for step in bootstrap.steps:
                    if step.error:
                        ssh_logger.log_error(f"Provision step '{step.name}': {step.error}")

                ssh_logger.log_error(f"limactl failed (exit {result.exit_code})")
                ssh_logger.log_error(result.output)
                raise SSHError(
                    f"limactl create/start failed (exit {result.exit_code})\n"
                    f"SSH log: {ssh_logger.path}\n"
                    f"Last output:\n{result.output[-1000:]}"
                )
        finally:
            # Exactly-once close, covering the paths where run_detached
            # itself raises (a transport failure, or the interrupt from
            # the poll) that used to skip it and leave the per-op log
            # without its footer. close() is not idempotent (each call
            # appends a footer), hence one call here rather than one per
            # branch; called with an exception in flight it also lands
            # the traceback in the per-op log (its documented behavior).
            # Suppressed so a local log-write failure (disk full,
            # permissions) cannot skip the remote rm below or mask the
            # original error.
            with contextlib.suppress(OSError):
                ssh_logger.close()
            # Clean up the remote temp file on success, failure, AND
            # interrupt (these were accumulating in /tmp on the VM host
            # after failures; an interrupt inside run_detached used to
            # skip this entirely). Suppressed so a transport hiccup on
            # the unwind can never mask the original error or interrupt.
            with contextlib.suppress(SSHError):
                ssh_run(target, f"rm -f {remote_template}", check=False)

    def _cleanup_partial_create(self, instance_name: str) -> None:
        """Best-effort teardown of the instance a failed ``create`` made
        (only ever an instance this create named: the pre-flight
        collision check guarantees the name was free when we started).

        Never raises a cleanup failure over the original error; it
        warns with the manual removal command instead. An operator's
        second Ctrl-C (``KeyboardInterrupt``) deliberately escapes so
        :meth:`_rollback_create_on_interrupt` can abandon the cleanup.
        """
        try:
            self._delete_instance(instance_name)
        except Exception as e:
            output.warn(f"could not clean up the partial Lima instance '{instance_name}': {e}")
            output.warn(self._manual_removal_hint(instance_name))

    def _rollback_create_on_interrupt(self, instance_name: str) -> None:
        """Roll back the partially created instance after an operator
        interrupt inside :meth:`create` (the azure precedent:
        ``rollback_create_on_interrupt``, #338).

        A SECOND interrupt during the cleanup abandons it cleanly
        instead of wedging, warning with the exact removal command; it
        is absorbed so the caller re-raises the ORIGINAL interrupt,
        which then reaches ``create_vm``, whose unwind deletes the DB
        row it no longer needs."""
        output.warn(
            f"Interrupted: cleaning up the partial Lima instance '{instance_name}', "
            "please wait (Ctrl-C again to abandon it)..."
        )
        try:
            if self.is_remote:
                # This locally raised interrupt stopped nothing on the
                # vm_host: run_detached nohups the remote limactl
                # precisely so it survives this process. Kill the
                # detached wrapper before deleting, or the delete races
                # a create/start still mutating the same instance. The
                # kill targets the wrapper PID, not the process group,
                # so an in-flight limactl child may briefly survive it;
                # severing the wrapper stops the && chain from
                # advancing, and the limactl delete --force below stops
                # the instance's own processes. (Local creates need no
                # equivalent: the terminal delivers the SIGINT to the
                # foreground limactl itself.)
                from agentworks.remote_exec import kill_detached

                kill_detached(self._host_transport(), self._remote_base_path(instance_name))
            self._cleanup_partial_create(instance_name)
        except KeyboardInterrupt:
            output.warn(
                f"Cleanup abandoned: the Lima instance '{instance_name}' may remain; "
                + self._manual_removal_hint(instance_name)
            )

    def _manual_removal_hint(self, instance_name: str) -> str:
        where = f" on '{self._vm_host_ssh}'" if self.is_remote else ""
        return f"remove it manually with 'limactl delete --force {instance_name}'{where}."

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

    def start(self, vm: VMRow, ctx: RunContext) -> None:
        # Idempotency guard (the ABC flags start): `limactl start` on a
        # running instance is not reliably a no-op, so land in the
        # running state ourselves.
        if self.status(vm, ctx) == VMStatus.RUNNING:
            output.detail(f"Lima VM '{vm.name}' is already running")
            return
        output.info(f"Starting Lima VM '{vm.name}'...")
        self._run_lima(f"limactl start {self._instance_name(vm)}")
        output.info(f"Lima VM '{vm.name}' started")

    def stop(self, vm: VMRow, ctx: RunContext) -> None:
        # Idempotency guard (the ABC flags stop): `limactl stop` on a
        # stopped instance errors rather than no-ops.
        if self.status(vm, ctx) == VMStatus.STOPPED:
            output.detail(f"Lima VM '{vm.name}' is already stopped")
            return
        output.info(f"Stopping Lima VM '{vm.name}'...")
        self._run_lima(f"limactl stop {self._instance_name(vm)}")
        output.info(f"Lima VM '{vm.name}' stopped")

    def _delete_instance(self, instance_name: str) -> None:
        """The one place the teardown command lives: shared by the
        delete op and the create rollback. ``--force`` stops a running
        instance first; ``check=False`` makes it a no-op for an
        instance that is already gone or only partially exists
        (created but never started)."""
        self._run_lima(f"limactl delete --force {instance_name}", check=False)

    def delete(self, vm: VMRow, ctx: RunContext) -> None:
        output.info(f"Deleting Lima VM '{vm.name}'...")
        self._delete_instance(self._instance_name(vm))
        output.info(f"Lima VM '{vm.name}' deleted")

    def display_backend_name(self, vm: VMRow) -> str:
        instance = str(vm.platform_metadata.get("instance_name", vm.name))
        if self.is_remote:
            return f"{instance}@{self._vm_host_ssh}"
        return instance

    def native_transport(
        self,
        vm: VMRow,
        ctx: RunContext,
        *,
        config: Config | None = None,
    ) -> Transport | None:
        # ctx is unused: limactl (local or over the vm_host SSH hop)
        # needs no backend credential.
        return self._transport_for(self._instance_name(vm))

    def status(self, vm: VMRow, ctx: RunContext) -> VMStatus:
        instance_name = self._instance_name(vm)
        try:
            listing = self._run_lima(f"limactl list --json {instance_name}", check=False)
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
