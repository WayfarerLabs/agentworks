"""The Lima VM platform: local limactl, or limactl over SSH, whichever
the site's ``platform_config.placement`` selects."""

from __future__ import annotations

import contextlib
import json
import re
import shlex
import sys
import textwrap

# Imported at RUNTIME, not just for typing: not_ready reads an unvalidated
# placement table and has to isinstance-check it.
from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal

from pydantic import Field

from agentworks import output
from agentworks.capabilities.retired_shapes import RetiredPresenceShape
from agentworks.capabilities.vm_platform.base import ProvisionRequest, ProvisionResult, VMPlatform
from agentworks.capabilities.vm_platform.bootstrap_script import (
    REBOOT_SENTINEL_PATH,
    generate_bootstrap_script,
    parse_bootstrap_output,
)
from agentworks.capabilities.vm_platform.cloud_init import PROVISIONING_PACKAGES
from agentworks.db import VMStatus
from agentworks.errors import ProvisioningError, SensitiveDataCleanupError, StateError
from agentworks.schema import AgwModel, NonEmptyStr
from agentworks.ssh import SSHError, SSHTarget
from agentworks.ssh import run as ssh_run
from agentworks.topics import TopicProse
from agentworks.transports import LimaTransport, RemoteLimaTransport, SSHTransport

if TYPE_CHECKING:
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

_REMOTE_TEMPLATE_CLEANUP_ATTEMPTS = 3
_REMOTE_TEMPLATE_ROOT = "/tmp"
_REMOTE_TEMPLATE_PREFIX = "agentworks-lima-template."
_REMOTE_TEMPLATE_RANDOM_LENGTH = 10

# Lima template for Debian cloud VMs (values substituted at create time).
# The provision block runs the non-secret bootstrap script (user, packages,
# swap, SSH key, and Tailscale installation) as a system-level provisioner
# during limactl start. Lima retains this block in its instance YAML, so the
# resolved Tailscale auth key crosses a separate post-start stdin boundary.
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

_TAILSCALE_JOIN_STDIN_COMMAND = "sudo -n /bin/bash -c " + shlex.quote(
    'IFS= read -r TAILSCALE_AUTH_KEY && test -n "$TAILSCALE_AUTH_KEY" && tailscale up --auth-key "$TAILSCALE_AUTH_KEY"'
)


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

    The VMs live on a shared box and nothing but SSH is needed here.
    """

    mode: Literal["ssh"]

    host: NonEmptyStr = Field(examples=["me@gpu-box"])
    """The SSH host running ``limactl`` (e.g. ``user@host``)."""


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
    # A lima site that WROTE ``vm_host`` (or wrote it null) crosses this
    # break and gets its exact rewrite; the zero-config local site that
    # wrote nothing lands on the local default and was never broken.
    # Release-scoped.
    retired_shape: ClassVar[RetiredPresenceShape | None] = RetiredPresenceShape(
        retired_field="vm_host",
        union_field="placement",
        present_mode="ssh",
        absent_mode="local",
        scalar_field="host",
    )
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="Lima",
        overview="""
        Lima runs Linux VMs through `limactl`. `placement` says where, and defaults
        to `{mode: local}`: `limactl` runs on this machine, which is what the
        built-in `lima-local` site is. `placement: {mode: ssh, host: me@gpu-box}`
        runs `limactl` on that host over SSH, so the VMs live on a shared box and
        nothing but SSH is needed here.

        Local sites need `limactl` installed here and report not-ready without it.
        Remote sites need nothing locally.
        """,
    )
    # No unsupported_reason override: the platform is supported on
    # every host, because remote-Lima sites run limactl on the placement
    # host over SSH and need nothing locally.

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
        else:
            local = isinstance(LimaConfig.model_fields["placement"].default, LimaLocalPlacement)
        if not local:
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

    def _instance_name(self, vm: VMRow) -> str:
        name = vm.platform_metadata.get("instance_name")
        if not name:
            raise StateError(
                f"VM '{vm.name}' has no lima instance_name in its platform metadata; the DB row is incomplete",
                entity_kind="vm",
                entity_name=vm.name,
            )
        return str(name)

    def _run_lima(self, command: str, *, check: bool = True, input_text: str | None = None) -> str:
        """Run a limactl command, locally or on the site's placement host."""
        if self.is_remote:
            assert self._remote_host is not None
            target = SSHTarget(host=self._remote_host, user=None, login_shell=True)
            result = None
            try:
                result = ssh_run(target, command, check=check, input_text=input_text)
                return result.stdout
            finally:
                input_text = None
                result = None
        else:
            import subprocess

            proc = None
            try:
                proc = subprocess.run(
                    shlex.split(command),
                    input=input_text,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if check and proc.returncode != 0:
                    if input_text is not None:
                        # A parser may echo template lines to stderr. Keep the
                        # secret-bearing stdin and raw result out of this
                        # diagnostic and its retained traceback frame.
                        returncode = proc.returncode
                        proc = None
                        raise SSHError(f"limactl stdin command failed (exit {returncode}): {command}")
                    raise SSHError(f"limactl failed: {proc.stderr.strip()}")
                # Sensitive stdin commands do not expose their output either:
                # an arbitrary program can reflect stdin to either stream.
                return "" if input_text is not None else proc.stdout
            finally:
                input_text = None
                proc = None

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
        # render them through `limactl list --json`. Embed only the non-secret
        # bootstrap shape; the resolved Tailscale key crosses the post-start
        # stdin boundary below and never enters this provider configuration.
        if request.tailscale_auth_key:
            provision_script = generate_bootstrap_script(
                admin_username=request.admin_username,
                ssh_public_key=request.ssh_public_key,
                provisioning_packages=PROVISIONING_PACKAGES,
                tailscale_auth_key=None,
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
            if self.is_remote:
                redactions = (request.tailscale_auth_key,) if request.tailscale_auth_key else ()
                self._create_remote(instance_name, rendered, redactions=redactions)
            else:
                self._create_local(instance_name, rendered)

            output.detail(f"Lima VM '{instance_name}' created.")

            tailscale_ip = None
            bootstrap_complete = False
            if request.tailscale_auth_key:
                output.detail("Joining Tailscale...")
                self._join_tailscale_ephemerally(instance_name, request.tailscale_auth_key)
                # The secret-bearing operation is complete. A later discovery
                # failure must not select the Phase A bootstrap script, which
                # would deliver the key a second time through a different
                # boundary. Phase A can rediscover the IP without the key.
                bootstrap_complete = True

            # Some bootstrap steps (currently the Apple-vz SVE mask, see
            # bootstrap_script) only take effect after a reboot, and rebooting
            # mid-provision is unreliable (lima-vm/lima#4867). Such steps drop a
            # restart sentinel; restart the instance from the host when we see it.
            # The probe stays generic: the host cannot cheaply tell which guest
            # shape it is, so a bare failure is phrased for what it does know.
            try:
                restart_pending = self._restart_sentinel_present(instance_name)
            except SSHError as e:
                output.warn(f"could not check whether '{instance_name}' needs a restart to finish provisioning: {e}")
                output.warn(
                    f"if the VM misbehaves, 'limactl restart {instance_name}' reapplies any deferred bootstrap step."
                )
                restart_pending = False
            if restart_pending:
                output.detail(f"A bootstrap step needs a reboot; restarting '{instance_name}'...")
                self._run_lima(f"limactl restart {instance_name}")

            # If Tailscale was joined through the ephemeral boundary,
            # extract the IP without retaining the key in provider state.
            if request.tailscale_auth_key:
                output.detail("Retrieving Tailscale IP...")
                try:
                    ip_output = self._run_lima(f"limactl shell {instance_name} sudo tailscale ip -4")
                    tailscale_ip = ip_output.strip()
                    output.detail(f"Tailscale IP: {tailscale_ip}")
                except SSHError as e:
                    output.warn(f"could not retrieve Tailscale IP: {e}")
                    output.warn("Tailscale is joined; Phase A will retry IP discovery without the auth key.")
        except KeyboardInterrupt:
            self._rollback_create_on_interrupt(instance_name)
            raise
        except BaseException:
            # SystemExit and GeneratorExit carry the same orphan-prevention
            # obligation as ordinary failures. Keep KeyboardInterrupt separate
            # so a second Ctrl-C retains the established abandon semantics.
            output.detail(f"Cleaning up the partial Lima instance '{instance_name}'...")
            try:
                self._cleanup_partial_create(instance_name)
            except KeyboardInterrupt:
                # Preserve the pre-existing behavior for a Ctrl-C that lands
                # during an ordinary/nonordinary failure cleanup: retry under
                # the interrupt-aware boundary, then propagate that interrupt.
                self._rollback_create_on_interrupt(instance_name)
                raise
            raise

        return ProvisionResult(
            native_transport=self._transport_for(instance_name),
            platform_metadata={"instance_name": instance_name},
            bootstrap_complete=bootstrap_complete,
            tailscale_ip=tailscale_ip,
        )

    def _join_tailscale_ephemerally(self, instance_name: str, auth_key: str) -> None:
        """Join without putting ``auth_key`` in Lima state or host argv.

        Lima stores provision scripts in its instance YAML. The fixed command
        reads exactly one key from stdin inside the guest, then invokes
        Tailscale. Non-interactive Bash does not write history, and neither the
        generated Lima config nor the host-side command contains the value.
        The guest ``tailscale`` process necessarily receives its CLI auth-key
        argument transiently; this boundary makes no guest-process-table claim.
        """
        key_input = f"{auth_key}\n"
        try:
            self._run_lima(
                f"limactl shell {instance_name} {_TAILSCALE_JOIN_STDIN_COMMAND}",
                input_text=key_input,
            )
        finally:
            key_input = ""

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
            self._run_lima(
                f"limactl create --name {instance_name} --tty=false -",
                input_text=lima_yaml,
            )
            self._run_lima(f"limactl start {instance_name}")
        except SSHError:
            self._log_provision_errors(instance_name)
            raise

    def _host_transport(self, logger: SSHLogger | None = None) -> SSHTransport:
        """An exec transport to the site's placement host (remote sites only):
        the create path's run_detached target, and the interrupt
        rollback's kill target."""
        assert self._remote_host is not None
        return SSHTransport(
            host=self._remote_host,
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

    def _create_remote(
        self,
        instance_name: str,
        lima_yaml: str,
        *,
        redactions: tuple[str, ...],
    ) -> None:
        """Create and start a Lima VM on the site's placement host.

        ``lima_yaml`` is the exact provider-persisted configuration and must
        contain no resolved secret. ``redactions`` remains defense in depth
        for provider output, but is not permission to put resolved secrets in
        the submitted template.
        """
        assert self._remote_host is not None
        target = SSHTarget(host=self._remote_host, user=None)

        remote_template_dir = self._allocate_remote_template_dir(target)
        remote_template = f"{remote_template_dir}/template.yaml"
        operation_failure: BaseException | None = None
        try:
            # Stream directly into the private directory allocated above.
            # ``umask 077`` creates mode 0600. The input is carried only on
            # subprocess stdin and is never included in argv or logger
            # surfaces.
            ssh_run(
                target,
                f"umask 077 && cat > {shlex.quote(remote_template)}",
                input_text=lima_yaml,
            )

            # Run limactl create + start as a single detached operation
            from agentworks.remote_exec import run_detached
            from agentworks.ssh import SSHLogger

            ssh_logger = SSHLogger(instance_name, "vm-provision", redactions=redactions)
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
                        f"Sanitized output is in SSH log: {ssh_logger.display_path}"
                    )
            finally:
                # Exactly-once close, covering the paths where run_detached
                # itself raises (a transport failure, or the interrupt from
                # the poll) that used to skip it and leave the per-op log
                # without its footer. close() is not idempotent (each call
                # appends a footer), hence one call here rather than one per
                # branch; called with an exception in flight it also lands
                # the traceback in the per-op log (its documented behavior).
                # An OSError cannot skip the remote cleanup. A second Ctrl-C
                # during close also cannot replace a provisioning failure or
                # the operator's first Ctrl-C, but remains visible when close
                # itself is the operation that was interrupted.
                active_failure = sys.exc_info()[1]
                try:
                    ssh_logger.close()
                except OSError:
                    pass
                except KeyboardInterrupt:
                    if active_failure is None:
                        raise
        except BaseException as exc:
            operation_failure = exc

        cleanup_failure: SensitiveDataCleanupError | None = None
        try:
            self._remove_remote_template_dir(target, remote_template_dir)
        except SensitiveDataCleanupError as exc:
            cleanup_failure = exc

        if cleanup_failure is not None:
            if operation_failure is None:
                raise cleanup_failure
            # Residue risk takes precedence over the earlier failure. Do not
            # chain provider or transport text into the diagnostic: the safe
            # combined error tells the operator both facts and how to remove
            # the sensitive path.
            raise self._remote_template_cleanup_error(remote_template_dir, operation_failed=True) from None

        if operation_failure is not None:
            raise operation_failure

    def _allocate_remote_template_dir(self, target: SSHTarget) -> str:
        """Atomically allocate and validate a private remote staging directory."""
        path_template = f"{_REMOTE_TEMPLATE_ROOT}/{_REMOTE_TEMPLATE_PREFIX}{'X' * _REMOTE_TEMPLATE_RANDOM_LENGTH}"
        result = ssh_run(target, f"umask 077 && mktemp -d {shlex.quote(path_template)}")
        remote_template_dir = result.stdout.strip()
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

    def _remove_remote_template_dir(self, target: SSHTarget, remote_template_dir: str) -> None:
        """Retry and verify removal of the remote provider-input directory."""
        quoted_dir = shlex.quote(remote_template_dir)
        command = f"rm -rf -- {quoted_dir} && test ! -e {quoted_dir}"
        for _attempt in range(_REMOTE_TEMPLATE_CLEANUP_ATTEMPTS):
            try:
                ssh_run(target, command)
                return
            except (SSHError, OSError, KeyboardInterrupt):
                continue
        raise self._remote_template_cleanup_error(remote_template_dir, operation_failed=False) from None

    def _remote_template_cleanup_error(
        self,
        remote_template_dir: str,
        *,
        operation_failed: bool,
    ) -> SensitiveDataCleanupError:
        prefix = "Lima provisioning failed and " if operation_failed else ""
        assert self._remote_host is not None
        return SensitiveDataCleanupError(
            prefix + "removal of sensitive Lima provisioning input could not be confirmed",
            entity_kind="vm",
            hint=(
                f"On VM host '{self._remote_host}', recursively remove directory "
                f"'{remote_template_dir}' before retrying. It may contain sensitive operator configuration."
            ),
        )

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
            # A failed or interrupted run_detached may still be alive on the
            # VM host and never reached its normal artifact cleanup. Stop it
            # and erase its output, wrapper, PID, and status before deleting
            # the instance it was mutating.
            from agentworks.remote_exec import kill_detached

            with contextlib.suppress(Exception):
                kill_detached(self._host_transport(), self._remote_base_path(instance_name))
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
            return f"{instance}@{self._remote_host}"
        return instance

    def native_transport(
        self,
        vm: VMRow,
        ctx: RunContext,
        *,
        config: Config | None = None,
    ) -> Transport | None:
        # ctx is unused: limactl (local or over the placement host SSH hop)
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
