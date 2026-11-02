"""The Lima VM platform: local limactl, or limactl over SSH, whichever
the site's tagged platform ``placement`` selects."""

from __future__ import annotations

import contextlib
import functools
import json
import re
import shlex
import sys
import textwrap

# Imported at RUNTIME, not just for typing: not_ready reads an unvalidated
# placement table and has to isinstance-check it.
from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal

from pydantic import Field, field_validator

from agentworks import output
from agentworks.capabilities.vm_platform.base import ProvisionRequest, ProvisionResult, VMPlatform
from agentworks.capabilities.vm_platform.bootstrap_script import (
    REBOOT_SENTINEL_PATH,
    generate_bootstrap_script,
    parse_bootstrap_output,
)
from agentworks.capabilities.vm_platform.cloud_init import PROVISIONING_PACKAGES
from agentworks.capabilities.vm_platform.debian_release import (
    code_owned_release_value,
)
from agentworks.capabilities.vm_platform.lima_host import LimaCommandFailed, LimaHost
from agentworks.capabilities.vm_platform.tailscale_join import (
    CREATE_RERUN_HINT,
    join_tailscale,
    settle_tailnet_address,
)
from agentworks.db import VMStatus
from agentworks.debian import DebianRelease
from agentworks.errors import ConnectivityError, ProvisioningError, StateError
from agentworks.execution.errors import ClientUnsupported
from agentworks.naming import NAME_RE
from agentworks.schema import AgwModel, NonEmptyStr
from agentworks.topics import TopicProse
from agentworks.transports import LimaTransport, RemoteLimaTransport

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform.base import BootstrapProgress
    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.execution.root_script import RawRun
    from agentworks.resources.graph import Readiness
    from agentworks.transports import Transport

# Markers the restart-sentinel probe echoes on stdout. The probe exits 0
# either way, so an absent sentinel stays a normal result, not an exception.
_REBOOT_PENDING_MARKER = "AGW_REBOOT_PENDING"
_REBOOT_CLEAR_MARKER = "AGW_REBOOT_CLEAR"

_REMOTE_TEMPLATE_ROOT = "/tmp"
_REMOTE_TEMPLATE_PREFIX = "agentworks-lima-template."
_REMOTE_TEMPLATE_RANDOM_LENGTH = 10
_STATUS_TIMEOUT_SECONDS = 10

# Lima template for Debian cloud VMs (values substituted at create time).
# The provision block runs the non-secret bootstrap script (user, packages,
# swap, SSH key, and Tailscale installation) as a system-level provisioner
# during limactl start. Lima retains this block in its instance YAML, so the
# Tailscale auth key is never in it: create joins afterwards through the VM's
# root channel.
_LIMA_IMAGE_BLOCKS: dict[DebianRelease, str] = {
    DebianRelease.TRIXIE: """\
  - location: https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2
    arch: x86_64
  - location: https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-arm64.qcow2
    arch: aarch64""",
}

LIMA_TEMPLATE = """\
# Agentworks Debian VM template for Lima
arch: default
images:
{images}
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


# Local and SSH placement use different transports, readiness rules, and
# required fields, so each is a distinct tagged arm.
class LimaLocalPlacement(AgwModel):
    """Run limactl on this machine.

    Needs ``limactl`` installed here, and the site reports not-ready
    without it.
    """

    mode: Literal["local"]


# ``ssh`` names the transport. The enclosing placement arm supplies the
# context for the unprefixed ``host`` field.
class LimaSshPlacement(AgwModel):
    """Run limactl on another host over SSH.

    The VMs live on a shared box and the local OpenSSH client is required.
    """

    mode: Literal["ssh"]

    host: NonEmptyStr = Field(examples=["me@gpu-box"])
    """The SSH host running ``limactl`` (e.g. ``user@host``)."""

    @field_validator("host")
    @classmethod
    def _host_is_one_word(cls, value: str) -> str:
        # Boundary: operator config that becomes one ssh argument, after ``--``.
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("must not contain whitespace or control characters")
        return value


#: Where ``limactl`` runs. Distinct arms make SSH's required host visible to
#: validation and schema emission. Local is the default because ``limactl``
#: runs on the invoking machine unless told otherwise.
LimaPlacement = Annotated[LimaLocalPlacement | LimaSshPlacement, Field(discriminator="mode")]


class LimaConfig(AgwModel):
    """Where a Lima site's ``limactl`` runs."""

    name: Literal["lima"]
    """The platform this config is for."""

    placement: LimaPlacement = LimaLocalPlacement(mode="local")
    """Where ``limactl`` runs: ``{mode: local}`` on this machine, or
    ``{mode: ssh, host: ...}`` over SSH. Defaults to local."""


class LimaPlatform(VMPlatform):
    """Runs VMs via limactl, locally or on a remote host over SSH."""

    contract_version: ClassVar[int] = 1
    name: ClassVar[str] = "lima"
    description: ClassVar[str] = "Lima VMs (local, or on a remote host via SSH)"
    config_model: ClassVar[type[LimaConfig]] = LimaConfig
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="Lima",
        overview="""
        Lima runs Linux VMs through `limactl`. `placement` says where, and defaults
        to `{mode: local}`: `limactl` runs on this machine, which is what the
        built-in `lima-local` site is. `placement: {mode: ssh, host: me@gpu-box}`
        runs `limactl` on that host over SSH, so the VMs live on a shared box and
        only the local OpenSSH client is needed here.

        Creation retains a credential-free provision script, then sends the required
        Tailscale key through one fixed guest command on stdin. It returns only after
        the join succeeds, or raises after removing the partial instance.

        Local sites need `limactl` installed here and report not-ready without it.
        Remote sites need `ssh` installed locally.
        """,
    )
    # No unsupported_reason override: the platform is supported on
    # every host, because remote-Lima sites run limactl on the placement
    # host and need only the portable OpenSSH client locally.

    # `limactl shell` is SSH to the guest's sshd with Lima's own key.
    recovery_needs_guest_sshd: ClassVar[bool] = True

    @classmethod
    def not_ready(cls, config: Mapping[str, object]) -> Readiness:
        """Require local ``limactl`` only for local placement.

        Reads raw config without constructing the model so readiness stays
        total. A malformed written placement is left to validation; an
        omitted placement uses the model's declared default.
        """
        from agentworks.resources.graph import Readiness

        if "placement" in config:
            placement = config.get("placement")
            local = isinstance(placement, Mapping) and placement.get("mode") == "local"
            remote = isinstance(placement, Mapping) and placement.get("mode") == "ssh"
        else:
            local = isinstance(LimaConfig.model_fields["placement"].default, LimaLocalPlacement)
            remote = False
        import shutil

        if not local:
            if remote and not shutil.which("ssh"):
                return Readiness.blocked("ssh not installed")
            return Readiness.ready()
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
    def _remote_host(self) -> str | None:
        """The SSH host ``limactl`` runs on, or ``None`` for a local site.

        Read off the placement ARM rather than off a nullable field, so
        "there is no host" and "this site is local" are the same fact
        rather than two that could disagree.
        """
        placement = self.config.placement
        return placement.host if isinstance(placement, LimaSshPlacement) else None

    @property
    def is_remote(self) -> bool:
        return self._remote_host is not None

    @property
    def _host(self) -> LimaHost:
        """Where this site's ``limactl`` runs."""
        return LimaHost(self._remote_host)

    def _instance_name(self, vm: VMRow) -> str:
        name = vm.platform_metadata.get("instance_name")
        if not name:
            raise StateError(
                f"VM '{vm.name}' has no lima instance_name in its platform metadata; the DB row is incomplete",
                entity_kind="vm",
                entity_name=vm.name,
            )
        # This value normally comes from create(), but backup/restore and
        # legacy migration make the database a trust boundary. Every Lima
        # command interpolates the instance name into a shell command (and
        # remote placement adds another login-shell hop), so reject stored
        # values outside the resource-name character grammar before any
        # transport sees them. Length is not part of this shell-safety
        # boundary; imposing today's cap here could reject historical rows.
        if not isinstance(name, str) or NAME_RE.fullmatch(name) is None:
            raise StateError(
                f"VM '{vm.name}' has an invalid lima instance_name in its platform metadata",
                entity_kind="vm",
                entity_name=vm.name,
            )
        return name

    def preflight(self, ctx: RunContext) -> None:
        """Local sites: ``limactl`` must be on PATH. Remote sites defer
        to the ops (probing the placement host over SSH is a real round trip;
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
                    "`platform: {name: lima, placement: {mode: ssh, host: ...}}` "
                    "names the host, and pass it via --site."
                ),
            )

    def create(self, request: ProvisionRequest, ctx: RunContext) -> ProvisionResult:
        images = code_owned_release_value(
            _LIMA_IMAGE_BLOCKS,
            request.debian_release,
            platform_name=self.name,
        )
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
                + (f" on '{self._remote_host}'" if self.is_remote else ""),
                entity_kind="vm",
                entity_name=request.vm_name,
                hint=("delete it first (limactl delete) or pick a different VM name"),
            )

        if self.is_remote:
            output.info(f"Connecting to VM host '{self._remote_host}'...")
        output.info(f"Creating Lima VM '{instance_name}' ({'remote' if self.is_remote else 'local'})...")
        output.detail(f"Resources: {cpus} CPUs, {memory} GiB memory, {disk} GiB disk")
        if swap > 0:
            output.detail(f"Swap: {swap} GiB")

        # Lima persists its provision scripts in the instance YAML and can
        # render them through `limactl list --json`. The bootstrap holds no
        # secret; the Tailscale key reaches the guest only in the join below,
        # through the root channel.
        provision_script = generate_bootstrap_script(
            admin_username=request.admin_username,
            ssh_public_key=request.ssh_public_key,
            provisioning_packages=PROVISIONING_PACKAGES,
            hostname=request.hostname,
            swap=swap,
        )

        # Indent the provision script for YAML embedding (6 spaces)
        indented_script = textwrap.indent(provision_script, "      ")
        rendered = LIMA_TEMPLATE.format(
            images=images,
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
            if self.is_remote:
                self._create_remote(instance_name, rendered, progress=request.progress)
            else:
                self._create_local(instance_name, rendered)

            output.detail(f"Lima VM '{instance_name}' created.")

            run_as_root = functools.partial(self._host.run_as_root, instance_name)
            output.detail("Joining Tailscale...")
            join_tailscale(run_as_root, request.tailscale_auth_key, node=request.hostname)

            # Some bootstrap steps (currently the Apple-vz SVE mask, see
            # bootstrap_script) only take effect after a reboot, and rebooting
            # mid-provision is unreliable (lima-vm/lima#4867). Such steps drop a
            # restart sentinel; restart the instance from the host when we see it.
            # The probe stays generic: the host cannot cheaply tell which guest
            # shape it is, so a bare failure is phrased for what it does know.
            try:
                restart_pending = self._restart_sentinel_present(instance_name)
            except LimaCommandFailed as e:
                output.warn(f"could not check whether '{instance_name}' needs a restart to finish provisioning: {e}")
                output.warn(
                    f"if the VM misbehaves, 'limactl restart {instance_name}' reapplies any deferred bootstrap step."
                )
                restart_pending = False
            if restart_pending:
                output.detail(f"A bootstrap step needs a reboot; restarting '{instance_name}'...")
                self._host.run(f"limactl restart {instance_name}")

            # The read follows any restart, and its retries cover the guest
            # coming back up.
            output.detail("Retrieving Tailscale IP...")
            tailscale_ip = settle_tailnet_address(run_as_root, node=request.hostname, rerun_hint=CREATE_RERUN_HINT)

            transport = self._transport_for(instance_name)
        except KeyboardInterrupt:
            self._rollback_create_on_interrupt(instance_name)
            raise
        except Exception:
            output.detail(f"Cleaning up the partial Lima instance '{instance_name}'...")
            self._cleanup_partial_create(instance_name)
            raise

        return ProvisionResult(
            native_transport=transport,
            platform_metadata={"instance_name": instance_name},
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
        sentinel is there, so a raised ``LimaCommandFailed`` means a genuine
        shell or connection failure and never a merely absent sentinel.
        """
        probe = self._host.run(
            f"limactl shell {instance_name} sh -c "
            f"'test -f {REBOOT_SENTINEL_PATH} "
            f"&& echo {_REBOOT_PENDING_MARKER} || echo {_REBOOT_CLEAR_MARKER}'"
        )
        if _REBOOT_PENDING_MARKER in probe:
            return True
        if _REBOOT_CLEAR_MARKER in probe:
            return False
        raise LimaCommandFailed(f"unrecognized restart-sentinel probe output: {probe.strip()!r}")

    def _instance_exists(self, instance_name: str) -> bool:
        """Pre-flight: does a Lima instance with this name exist?"""
        try:
            listing = self._host.run(f"limactl list --json {instance_name}", check=False)
        except LimaCommandFailed:
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
            assert self._remote_host is not None
            return RemoteLimaTransport(vm_name=instance_name, vm_host_ssh=self._remote_host)
        return LimaTransport(vm_name=instance_name)

    def _create_local(self, instance_name: str, lima_yaml: str) -> None:
        """Create and start a local Lima VM without persisting its template."""
        try:
            # Lima's documented ``-`` template source consumes stdin. The
            # provider configuration therefore never needs a local filesystem
            # path, including on Windows where unlinking an open temp file is
            # not reliable.
            self._host.run(
                f"limactl create --name {instance_name} --tty=false -",
                input=lima_yaml,
            )
            self._host.run(f"limactl start {instance_name}")
        except LimaCommandFailed:
            self._log_provision_errors(instance_name)
            raise

    @staticmethod
    def _remote_base_path(instance_name: str) -> str:
        """run_detached's file identity for this instance's create; the
        interrupt rollback derives the same path to kill the detached
        limactl before deleting the instance."""
        return f"/var/tmp/agentworks-lima-{instance_name}"

    def _create_remote(
        self,
        instance_name: str,
        lima_yaml: str,
        *,
        progress: BootstrapProgress,
    ) -> None:
        """Create and start a Lima VM on the site's placement host.

        ``lima_yaml`` is the exact provider-persisted configuration and must
        contain no resolved secret. The create and start run detached, since
        ``limactl create`` may download the base image, and their output goes
        to ``progress``, the create's own log.
        """
        host = self._host
        remote_template_dir = self._allocate_remote_template_dir()
        remote_template = f"{remote_template_dir}/template.yaml"
        try:
            # Stream directly into the private directory allocated above, with
            # mode 0600; the YAML travels on stdin, never in an argument.
            host.upload(remote_template, lima_yaml)
            lima_cmd = (
                f"limactl create --name {instance_name} --tty=false {remote_template} && limactl start {instance_name}"
            )
            output.detail("Starting and provisioning VM via Lima (this may take several minutes)...")
            result = host.run_detached(lima_cmd, base_path=self._remote_base_path(instance_name), timeout=600)
            progress.step(f"limactl create and start on '{self._remote_host}'")
            progress.output(result.output)
            if result.exit_code != 0:
                # Parse structured markers from provision script output if present
                bootstrap = parse_bootstrap_output(result.output, result.exit_code)
                for step in bootstrap.steps:
                    if step.error:
                        progress.log_error(f"Provision step '{step.name}': {step.error}")
                progress.log_error(f"limactl failed (exit {result.exit_code})")
                raise LimaCommandFailed(
                    f"limactl create/start failed on '{self._remote_host}' (exit {result.exit_code}); "
                    "its output is in the create's log"
                )
        finally:
            # Cleanup runs after the primary span, but a second Ctrl-C cannot
            # replace an active failure. A standalone cleanup interrupt
            # remains visible.
            active_failure = sys.exc_info()[1]
            try:
                self._remove_remote_template_dir(remote_template_dir)
            except (LimaCommandFailed, ConnectivityError, ClientUnsupported, OSError):
                pass
            except KeyboardInterrupt:
                if active_failure is None:
                    raise

    def _allocate_remote_template_dir(self) -> str:
        """Atomically allocate and validate a private remote staging directory."""
        path_template = f"{_REMOTE_TEMPLATE_ROOT}/{_REMOTE_TEMPLATE_PREFIX}{'X' * _REMOTE_TEMPLATE_RANDOM_LENGTH}"
        remote_template_dir = self._host.run(f"umask 077 && mktemp -d {shlex.quote(path_template)}").strip()
        expected = (
            rf"{re.escape(_REMOTE_TEMPLATE_ROOT)}/{re.escape(_REMOTE_TEMPLATE_PREFIX)}"
            rf"[A-Za-z0-9]{{{_REMOTE_TEMPLATE_RANDOM_LENGTH}}}"
        )
        if re.fullmatch(expected, remote_template_dir) is None:
            raise ProvisioningError(
                "VM host returned an invalid temporary directory for Lima provisioning",
                entity_kind="vm",
            )
        return remote_template_dir

    def _remove_remote_template_dir(self, remote_template_dir: str) -> None:
        """Remove the remote Lima template staging directory."""
        quoted_dir = shlex.quote(remote_template_dir)
        self._host.run(f"rm -rf -- {quoted_dir} && test ! -e {quoted_dir}")

    def _cleanup_partial_create(self, instance_name: str) -> None:
        """Best-effort teardown of the instance a failed ``create`` made
        (only ever an instance this create named: the pre-flight
        collision check guarantees the name was free when we started).

        Never raises a cleanup failure over the original error; it
        warns with the manual removal command instead. An operator's
        second Ctrl-C (``KeyboardInterrupt``) deliberately escapes so
        :meth:`_rollback_create_on_interrupt` can abandon the cleanup.
        """
        if self.is_remote:
            # A failed or interrupted detached create may still be alive on the
            # VM host and never reached its normal artifact cleanup. Stop it
            # and erase its output, wrapper, PID, and status before deleting
            # the instance it was mutating.
            with contextlib.suppress(Exception):
                self._host.kill_detached(self._remote_base_path(instance_name))
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
            self._cleanup_partial_create(instance_name)
        except KeyboardInterrupt:
            output.warn(
                f"Cleanup abandoned: the Lima instance '{instance_name}' may remain; "
                + self._manual_removal_hint(instance_name)
            )

    def _manual_removal_hint(self, instance_name: str) -> str:
        where = f" on '{self._remote_host}'" if self.is_remote else ""
        return f"remove it manually with 'limactl delete --force {instance_name}'{where}."

    def _log_provision_errors(self, instance_name: str) -> None:
        """Attempt to surface provision script errors from Lima logs."""
        try:
            log_output = self._host.run(
                f"limactl shell {instance_name} cat /var/log/cloud-init-output.log 2>/dev/null || true",
                check=False,
            )
            if log_output.strip():
                bootstrap = parse_bootstrap_output(log_output, 1)
                for step in bootstrap.steps:
                    if step.error:
                        output.warn(f"Provision error ({step.name}): {step.error}")
        except LimaCommandFailed:
            pass

    def start(self, vm: VMRow, ctx: RunContext) -> None:
        # Idempotency guard (the ABC flags start): `limactl start` on a
        # running instance is not reliably a no-op, so land in the
        # running state ourselves.
        if self.status(vm, ctx) == VMStatus.RUNNING:
            output.detail(f"Lima VM '{vm.name}' is already running")
            return
        output.info(f"Starting Lima VM '{vm.name}'...")
        self._host.run(f"limactl start {self._instance_name(vm)}")
        output.info(f"Lima VM '{vm.name}' started")

    def stop(self, vm: VMRow, ctx: RunContext) -> None:
        # Idempotency guard (the ABC flags stop): `limactl stop` on a
        # stopped instance errors rather than no-ops.
        if self.status(vm, ctx) == VMStatus.STOPPED:
            output.detail(f"Lima VM '{vm.name}' is already stopped")
            return
        output.info(f"Stopping Lima VM '{vm.name}'...")
        self._host.run(f"limactl stop {self._instance_name(vm)}")
        output.info(f"Lima VM '{vm.name}' stopped")

    def _delete_instance(self, instance_name: str) -> None:
        """The one place the teardown command lives: shared by the
        delete op and the create rollback. ``--force`` stops a running
        instance first; ``check=False`` makes it a no-op for an
        instance that is already gone or only partially exists
        (created but never started)."""
        self._host.run(f"limactl delete --force {instance_name}", check=False)

    def delete(self, vm: VMRow, ctx: RunContext) -> None:
        output.info(f"Deleting Lima VM '{vm.name}'...")
        self._delete_instance(self._instance_name(vm))
        output.info(f"Lima VM '{vm.name}' deleted")

    def display_backend_name(self, vm: VMRow) -> str:
        instance = self._instance_name(vm)
        if self.is_remote:
            return f"{instance}@{self._remote_host}"
        return instance

    def run_as_root(self, vm: VMRow, ctx: RunContext, script: bytes, deadline: float) -> RawRun:
        """Run ``script`` as root through ``limactl shell``, locally or on the placement host.

        ``ctx`` is unused: limactl needs no backend credential.
        """
        return self._host.run_as_root(self._instance_name(vm), script, deadline)

    def native_transport(
        self,
        vm: VMRow,
        ctx: RunContext,
        *,
        config: Config | None = None,
    ) -> Transport:
        # ctx is unused: limactl (local or over the placement host SSH hop)
        # needs no backend credential.
        return self._transport_for(self._instance_name(vm))

    def status(self, vm: VMRow, ctx: RunContext) -> VMStatus:
        instance_name = self._instance_name(vm)
        try:
            listing = self._host.run(
                f"limactl list --json {instance_name}",
                check=False,
                timeout=_STATUS_TIMEOUT_SECONDS,
            )
        except (LimaCommandFailed, ConnectivityError, ClientUnsupported):
            # A host that cannot be asked, or no limactl or ssh to ask it with.
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
