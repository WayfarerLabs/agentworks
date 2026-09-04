"""The Proxmox VE VM platform: clone + cloud-init + guest-agent bootstrap
via the Proxmox REST API."""

from __future__ import annotations

import sys
import time
import urllib.parse
import uuid
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal

from pydantic import Field, model_validator

from agentworks import output
from agentworks.capabilities.vm_platform.base import ProvisionRequest, ProvisionResult, VMPlatform
from agentworks.capabilities.vm_platform.bootstrap_script import generate_bootstrap_script
from agentworks.capabilities.vm_platform.cloud_init import PROVISIONING_PACKAGES
from agentworks.capabilities.vm_platform.debian_release import (
    operator_owned_release_value,
)
from agentworks.db import VMStatus
from agentworks.debian import DebianRelease
from agentworks.errors import ProvisioningError, StateError
from agentworks.plugins.proxmox.api import ProxmoxAPI, ProxmoxAPIError
from agentworks.plugins.proxmox.teardown import (
    rollback_create_on_interrupt,
    rollback_partial_create,
    stop_and_delete_vm,
)
from agentworks.schema import AgwModel, NonEmptyStr, PositiveInt, SecretRef
from agentworks.topics import TopicProse
from agentworks.transports import SSHTransport

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.capabilities.base import RunContext
    from agentworks.db import VMRow


def _warn_bootstrap_file_residue() -> None:
    """Warn about guest residue without replacing active control flow."""
    try:  # noqa: SIM105 - warning failures must not replace the active provisioning failure
        output.warn(
            "could not verify removal of the Proxmox bootstrap staging file; "
            "primary failure unchanged; plaintext may remain"
        )
    except (Exception, KeyboardInterrupt, SystemExit, GeneratorExit):
        pass


class ProxmoxConfig(AgwModel):
    """A Proxmox VE cluster, as one vm-site points at it."""

    name: Literal["proxmox"]
    """The platform this config is for."""

    api_url: NonEmptyStr = Field(examples=["https://pve.example.net:8006"])
    """The cluster's API endpoint (e.g. ``https://pve.example:8006``)."""

    node: NonEmptyStr = Field(examples=["pve1"])
    """The cluster node new VMs are cloned on."""

    token_id: NonEmptyStr = Field(examples=["agentworks@pam!agw"])
    """The API token's id (``user@realm!tokenname``)."""

    template_vmids: dict[DebianRelease, PositiveInt] = Field(
        default_factory=dict,
        examples=[{"trixie": 9001}],
    )
    """Template VMIDs keyed by Debian release. Core chooses the release;
    this site supplies the corresponding template."""

    template_vmid: PositiveInt | None = Field(default=None, examples=[9000], exclude=True)
    """Legacy Bookworm-only template scalar. Use ``template_vmids`` for creation."""

    token_secret: Annotated[
        NonEmptyStr,
        SecretRef(usage="the Proxmox API token", default_template="proxmox-token"),
    ]
    """The secret containing the API token. The default maps to
    ``AW_SECRET_PROXMOX_TOKEN`` in the env-var backend."""

    storage: NonEmptyStr = "local-lvm"
    """The storage new VMs' disks are created on."""

    bridge: NonEmptyStr | None = None
    """The network bridge new VMs attach to. Omit for the cluster's own
    default."""

    pool: NonEmptyStr = "agentworks"
    """The resource pool new VMs join."""

    verify_ssl: bool = True
    """Whether to verify the cluster's TLS certificate. Write booleans
    unquoted; quoted strings such as ``"no"`` are invalid."""

    @model_validator(mode="before")
    @classmethod
    def _adapt_legacy_bookworm_template(cls, value: object) -> object:
        """Normalize manifest keys and load the scalar as Bookworm only."""
        if not isinstance(value, dict):
            return value
        adapted = dict(value)
        raw_template_vmids = adapted.get("template_vmids")
        if raw_template_vmids is None:
            raw_template_vmids = {}
        if isinstance(raw_template_vmids, dict):
            releases_by_value = {release.value: release for release in DebianRelease}
            template_vmids = {
                releases_by_value.get(key, key) if isinstance(key, str) else key: vmid
                for key, vmid in raw_template_vmids.items()
            }
            if "template_vmid" in adapted:
                template_vmids.setdefault(DebianRelease.BOOKWORM, adapted["template_vmid"])
            adapted["template_vmids"] = template_vmids
        return adapted


class ProxmoxPlatform(VMPlatform):
    """Runs VMs on a Proxmox VE cluster."""

    contract_version: ClassVar[int] = 1
    name: ClassVar[str] = "proxmox"
    description: ClassVar[str] = "Proxmox VE cluster VMs (clone + cloud-init)"
    config_model: ClassVar[type[ProxmoxConfig]] = ProxmoxConfig
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="Proxmox VE",
        overview="""
        Clones a prepared template VM on a Proxmox VE cluster and configures it through
        cloud-init. The release-keyed template (`template_vmids.<release>`) has to exist on the node already;
        agentworks does not build it. A create missing the core-selected release fails during
        preflight, before the API token is delivered to the platform or authenticated.

        The API token value is a secret, `proxmox-token` unless the site names another,
        resolved through the configured source chain like any other secret. The token id is not a
        secret and is written in the document.

        Proxmox does not currently implement the required native administrative transport
        that works independently of the VM's Tailscale state. The QEMU guest agent's exec
        interface provides the needed non-interactive foundation; issue #727 tracks the
        transport correction. Until then, use the Proxmox web UI's serial console for manual
        access.

        Creation passes the required Tailscale key through a private guest-agent staging
        file and verifies removal of that file before returning a Tailscale-backed transport.
        Core checks the live Debian release through the returned transport. A bootstrap
        failure rolls the cloned VM back; a later core mismatch retains an addressable failed
        VM for explicit deletion.

        Ships as the opt-in `proxmox` system plugin, so a site stays not-ready until
        `[plugins] system` lists it.
        """,
    )
    no_native_transport_hint: ClassVar[str] = (
        "Proxmox does not yet implement the required native administrative "
        "transport (#727). Use the Proxmox web UI's serial console "
        "(VM > Console in the Proxmox VE web UI) for manual access."
    )

    def __init__(self, owner_name: str, config: Mapping[str, object]) -> None:
        super().__init__(owner_name, config)
        # The op client, built on FIRST need by :meth:`_api` and reused
        # for the instance's remaining ops.
        self._api_cached: ProxmoxAPI | None = None

    @property
    def config(self) -> ProxmoxConfig:
        """This site's validated proxmox config."""
        return self._config_as(ProxmoxConfig)

    @classmethod
    def legacy_platform_metadata(cls, row: Mapping[str, Any], legacy: Mapping[str, Any]) -> dict[str, str]:
        metadata: dict[str, str] = {}
        if row["proxmox_vmid"]:
            metadata["vmid"] = str(row["proxmox_vmid"])
        # Best-effort: the node comes from the legacy [proxmox] section
        # when the migration context could parse one. When absent, ops
        # fall back to the bound site's platform_config node.
        proxmox_section = legacy.get("proxmox")
        if isinstance(proxmox_section, dict) and proxmox_section.get("node"):
            metadata["node"] = str(proxmox_section["node"])
        return metadata

    def _build_api(self, token_value: str) -> ProxmoxAPI:
        """Construct an API client for a resolved token. Shared by the op
        client and ``runup``, so the two stages build the client the same
        way from the same value."""
        from agentworks.secrets.line_safety import (
            LineOrientedSecretUse,
            require_line_safe_secret,
        )

        token_value = require_line_safe_secret(
            token_value,
            use=LineOrientedSecretUse.PROXMOX_API,
            secret_name=self.config.token_secret,
        )
        return ProxmoxAPI(
            api_url=self.config.api_url,
            token_id=self.config.token_id,
            token_secret=token_value,
            verify_ssl=self.config.verify_ssl,
        )

    def _api(self, ctx: RunContext) -> ProxmoxAPI:
        """The op client, built on first need from the context's scoped
        secret delivery (``ctx.secret``, the declare/receive contract:
        the instance never holds a resolver or a raw reader, only the
        client derived from the delivered value) and cached for the
        instance's remaining ops. A context assembled without resolved
        secrets (inspection?) is the accessor's typed ``ConfigError``;
        an undeclared or unresolved name is scoped delivery's own typed
        refusal."""
        api = self._api_cached
        if api is None:
            token_secret = self.config.token_secret
            api = self._build_api(ctx.secret(token_secret))
            self._api_cached = api
        return api

    def runup(self, ctx: RunContext) -> None:
        """Provisioning-phase runup: authenticate the API token with a
        cheap read (next available VMID) before ``create`` mutates
        anything, so a bad or unauthorized token fails cleanly here
        rather than mid-provision. A 401/403 is a definitive rejection
        (fatal, before any VM exists); anything else (a transient error
        or an unreachable host) warns and continues unverified, so an
        outage never blocks work a valid token would have done.

        Post-resolve and read-only: the token comes from the context's
        resolved secrets (``ctx.secret(name)``), the same read the op
        client makes at op time. runup builds a throwaway
        client for the check and leaves ``self`` untouched. A context
        with no resolved secrets at all (inspection only?) is the
        accessor's typed ``ConfigError``, so every capability's runup
        fails that case the same way."""
        from agentworks import output
        from agentworks.errors import TokenRejectedError

        token_secret = self.config.token_secret
        # Read the token before announcing the check, so a context with
        # no resolved secrets fails before the banner (the old guard's
        # error-path ordering).
        from agentworks.secrets.line_safety import (
            LineOrientedSecretUse,
            require_line_safe_secret,
        )

        token = require_line_safe_secret(
            ctx.secret(token_secret),
            use=LineOrientedSecretUse.PROXMOX_API,
            secret_name=token_secret,
        )
        output.detail(f"Performing runup test for vm-site/{self.site_name}...")
        api = self._build_api(token)
        try:
            api.next_id()
        except ProxmoxAPIError as e:
            if e.code in (401, 403):
                raise TokenRejectedError(
                    f"Proxmox rejected the API token for vm-site '{self.site_name}' (secret '{token_secret}')",
                    entity_kind="vm-site",
                    entity_name=self.site_name,
                    hint=("Check token_id and the token secret's value and permissions on the Proxmox host."),
                ) from e
            output.warn(f"could not verify the Proxmox API token for '{self.site_name}' ({e}); continuing unverified")
        except OSError as e:
            output.warn(f"could not reach Proxmox for '{self.site_name}' (network: {e}); continuing unverified")

    def _vm_node(self, vm: VMRow) -> str:
        # Prefer the recorded node (decouples existing VMs from config
        # edits); fall back to the site's node for rows migrated without
        # a parseable legacy [proxmox] section.
        node = vm.platform_metadata.get("node") or self.config.node
        if not node:
            raise StateError(
                f"VM '{vm.name}' has no proxmox node in its platform metadata or site configuration",
                entity_kind="vm",
                entity_name=vm.name,
            )
        return str(node)

    def _vmid(self, vm: VMRow) -> int:
        vmid = vm.platform_metadata.get("vmid")
        if not vmid:
            raise StateError(
                f"VM '{vm.name}' has no proxmox vmid in its platform metadata; the DB row is incomplete",
                entity_kind="vm",
                entity_name=vm.name,
            )
        return int(vmid)

    def _template_vmid(self, release: DebianRelease) -> int:
        return operator_owned_release_value(
            self.config.template_vmids,
            release,
            site_name=self.site_name,
            field="template_vmids",
        )

    def validate_create_release(self, release: DebianRelease) -> None:
        self._template_vmid(release)

    def create(self, request: ProvisionRequest, ctx: RunContext) -> ProvisionResult:
        node = self.config.node
        template_vmid = self._template_vmid(request.debian_release)
        pool = self.config.pool
        storage = self.config.storage

        # The platform owns the backend-side name. PVE names are
        # soft (the vmid identifies), but a duplicate name is operator
        # confusion worth surfacing.
        backend_name = f"{request.system_slug}-{request.vm_name}" if request.system_slug else request.vm_name
        if self._name_exists(node, backend_name, ctx):
            raise StateError(
                f"a Proxmox VM named '{backend_name}' already exists on node {node}",
                entity_kind="vm",
                entity_name=request.vm_name,
                hint="delete it first or pick a different VM name",
            )

        output.info(f"Provisioning Proxmox VM '{backend_name}' on node {node}...")

        # 1. Get next VMID
        newid = self._api(ctx).next_id()
        output.detail(f"Allocated VMID: {newid}")

        # Rollback spans everything from the clone POST through the
        # bootstrap wait: the clone is the first call that mutates PVE
        # state, and the caller's unwind deletes only the DB row, so a
        # cloned VM left behind on ANY unwind (failure or interrupt)
        # would be orphaned with nothing to target it (the create
        # contract; #340). The arms nest like azure's: the outer
        # interrupt arm wraps the inner failure arm, so a Ctrl-C landing
        # DURING the failure arm's rollback still gets the full
        # interrupt treatment. ``pending_upid`` tracks the task whose
        # wait_for_task has not returned, so the rollback can cancel an
        # in-flight clone instead of waiting it out; an interrupt inside
        # the clone POST itself, before the UPID returns, is an accepted
        # sub-second window (azure's begin_* poller race is the analog)
        # where the rollback cannot cancel a task it never learned of
        # and falls back to the best-effort delete alone.
        pending_upid: str | None = None
        try:
            try:
                # 2. Clone template into the agentworks pool
                output.detail(f"Cloning template {template_vmid}...")
                pending_upid = self._api(ctx).clone_vm(
                    node,
                    template_vmid,
                    newid,
                    backend_name,
                    storage=storage,
                    pool=pool,
                )
                self._api(ctx).wait_for_task(node, pending_upid)
                pending_upid = None
                output.detail("Clone complete")

                # 3. Configure VM resources
                vm_config: dict[str, object] = {
                    "cores": request.cpus,
                    "memory": request.memory_gib * 1024,  # GiB -> MiB
                }

                # Cloud-init: user, SSH key, network
                vm_config["ciuser"] = request.admin_username
                vm_config["sshkeys"] = urllib.parse.quote(request.ssh_public_key, safe="")
                vm_config["ipconfig0"] = "ip=dhcp"

                # Boot order, guest agent, and CPU type (host passthrough exposes
                # AVX/AVX2 which tools like Bun require)
                vm_config["boot"] = "order=scsi0"
                vm_config["agent"] = "enabled=1"
                vm_config["cpu"] = "host"

                output.detail("Configuring VM...")
                self._api(ctx).configure_vm(node, newid, **vm_config)

                # 4. Resize the clone's disk to the template's size
                output.detail(f"Resizing disk to {request.disk_gib}G...")
                self._api(ctx).resize_disk(node, newid, "scsi0", f"{request.disk_gib}G")

                # 5. Start VM
                output.detail("Starting VM...")
                pending_upid = self._api(ctx).start_vm(node, newid)
                self._api(ctx).wait_for_task(node, pending_upid)
                pending_upid = None

                # 6. Wait for guest agent and get VM IP
                output.detail("Waiting for guest agent...")
                ip = self._wait_for_guest_ip(node, newid, ctx)
                output.detail(f"VM IP: {ip}")

                # 7. Wait for cloud-init to finish (releases apt lock)
                output.detail("Waiting for cloud-init...")
                self._wait_for_cloud_init(node, newid, ctx)

                # 8. Run bootstrap script via guest agent
                output.detail("Running bootstrap via guest agent...")
                bootstrap = generate_bootstrap_script(
                    admin_username=request.admin_username,
                    ssh_public_key=request.ssh_public_key,
                    provisioning_packages=PROVISIONING_PACKAGES,
                    tailscale_auth_key=request.tailscale_auth_key,
                    hostname=request.hostname,
                    swap=request.swap_gib,
                )
                tailscale_ip = self._run_bootstrap_via_agent(node, newid, bootstrap, ctx)
                if not tailscale_ip:
                    raise ProvisioningError("Proxmox bootstrap did not return a Tailscale IP")
                output.detail(f"Tailscale IP: {tailscale_ip}")

                target = SSHTransport(
                    host=tailscale_ip,
                    user=request.admin_username,
                    identity_file=request.ssh_private_key,
                    force_tty=sys.platform == "win32",
                )
            except Exception:
                # Re-raised unwrapped after rollback: the manager preserves
                # typed configuration/state failures and maps other backend
                # exceptions at the create boundary, so wrapping here would
                # only add a redundant provider layer.
                output.detail("Cleaning up the partial VM...")
                rollback_partial_create(self._api(ctx), node, newid, pending_upid=pending_upid)
                raise
        except KeyboardInterrupt:
            rollback_create_on_interrupt(self._api(ctx), node, newid, pending_upid=pending_upid)
            raise

        return ProvisionResult(
            native_transport=target,
            platform_metadata={"vmid": str(newid), "node": node},
            tailscale_ip=tailscale_ip,
        )

    def _name_exists(self, node: str, backend_name: str, ctx: RunContext) -> bool:
        """Pre-flight: does a VM with this name exist on the node?"""
        try:
            existing = self._api(ctx).list_vms(node)
        except ProxmoxAPIError:
            return False
        return any(entry.get("name") == backend_name for entry in existing)

    def start(self, vm: VMRow, ctx: RunContext) -> None:
        # Idempotency guard (the ABC flags start): the Proxmox
        # status/start endpoint errors on an already-running VM.
        if self.status(vm, ctx) == VMStatus.RUNNING:
            output.detail(f"Proxmox VM '{vm.name}' is already running")
            return
        node = self._vm_node(vm)
        upid = self._api(ctx).start_vm(node, self._vmid(vm))
        self._api(ctx).wait_for_task(node, upid)

    def stop(self, vm: VMRow, ctx: RunContext) -> None:
        # Idempotency guard (the ABC flags stop): stopping an
        # already-stopped VM must land in the stopped state, not error.
        if self.status(vm, ctx) == VMStatus.STOPPED:
            output.detail(f"Proxmox VM '{vm.name}' is already stopped")
            return
        node = self._vm_node(vm)
        upid = self._api(ctx).stop_vm(node, self._vmid(vm))
        self._api(ctx).wait_for_task(node, upid)

    def delete(self, vm: VMRow, ctx: RunContext) -> None:
        # The stop-then-delete sequence lives in the teardown module,
        # shared with create's rollback arms.
        stop_and_delete_vm(self._api(ctx), self._vm_node(vm), self._vmid(vm))

    def status(self, vm: VMRow, ctx: RunContext) -> VMStatus:
        try:
            result = self._api(ctx).vm_status(self._vm_node(vm), self._vmid(vm))
        except ProxmoxAPIError:
            return VMStatus.UNKNOWN
        pve_status = result.get("status", "")
        if pve_status == "running":
            return VMStatus.RUNNING
        if pve_status == "stopped":
            return VMStatus.STOPPED
        return VMStatus.UNKNOWN

    def display_backend_name(self, vm: VMRow) -> str:
        vmid = vm.platform_metadata.get("vmid", "?")
        node = vm.platform_metadata.get("node") or self.config.node
        return f"{vmid}@{node}"

    # native_transport: inherited None default. This is a known contract
    # violation (#727), not an optional capability. The transports factory
    # raises the typed StateError with the web-console hint.

    # -- Helpers ---------------------------------------------------------------

    def _wait_for_cloud_init(self, node: str, vmid: int, ctx: RunContext, *, timeout: int = 300) -> None:
        """Wait for cloud-init to finish inside the VM."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = self._api(ctx).guest_agent_exec_wait(
                    node,
                    vmid,
                    "/usr/bin/cloud-init",
                    ["status", "--wait"],
                    timeout=60,
                )
                if result is not None and result.get("exitcode", -1) == 0:
                    return
            except ProxmoxAPIError:
                pass
            time.sleep(5)
        raise ProvisioningError(
            f"Timed out waiting for cloud-init on Proxmox VMID {vmid}; "
            "the template must have cloud-init and the QEMU guest agent installed and enabled"
        )

    def _wait_for_guest_ip(self, node: str, vmid: int, ctx: RunContext, *, timeout: int = 120) -> str:
        """Poll the guest agent until it reports a non-loopback IPv4 address."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                interfaces = self._api(ctx).guest_agent_network(node, vmid)
                for iface in interfaces:
                    if iface.get("name") == "lo":
                        continue
                    for addr in iface.get("ip-addresses", []):
                        if addr.get("ip-address-type") == "ipv4":
                            ip = addr["ip-address"]
                            if not ip.startswith("127."):
                                return str(ip)
            except ProxmoxAPIError:
                pass  # guest agent not ready yet
            time.sleep(3)
        # ProvisioningError, not ProxmoxAPIError: every API call above
        # succeeded (or was tolerated), so this is the backend VM failing
        # to reach readiness during provisioning, not an API failure.
        raise ProvisioningError(f"Timed out waiting for guest agent IP on VMID {vmid}")

    def _run_bootstrap_via_agent(self, node: str, vmid: int, script: str, ctx: RunContext) -> str:
        """Write and run the bootstrap script via the guest agent.

        Returns the Tailscale IP if bootstrap succeeds. A timeout, unsuccessful
        execution, or missing valid Tailscale IP raises so create rolls back.
        """
        from agentworks.capabilities.vm_platform.bootstrap_script import parse_bootstrap_output

        script_path = f"/tmp/agentworks-bootstrap-{uuid.uuid4().hex}.sh"
        api = self._api(ctx)
        try:
            prepared = api.guest_agent_exec_wait(
                node,
                vmid,
                "/usr/bin/install",
                ["-m", "600", "/dev/null", script_path],
            )
            if prepared is None or prepared.get("exitcode", -1) != 0:
                raise ProvisioningError("could not create the private Proxmox bootstrap staging file")
            write_failed = False
            try:
                api.guest_agent_file_write(node, vmid, script_path, script)
            except ProxmoxAPIError:
                write_failed = True
            if write_failed:
                raise ProxmoxAPIError("Proxmox guest-agent bootstrap file write failed") from None

            # Bash is invoked explicitly so the private script does not need
            # an executable bit. The path, never its content, is guest argv.
            result = api.guest_agent_exec_wait(
                node,
                vmid,
                "/bin/bash",
                [script_path],
                timeout=600,
            )
        finally:
            active_failure = sys.exc_info()[1]
            try:
                removed = api.guest_agent_exec_wait(
                    node,
                    vmid,
                    "/bin/sh",
                    [
                        "-c",
                        'rm -f -- "$1" && test ! -e "$1"',
                        "agentworks-bootstrap-cleanup",
                        script_path,
                    ],
                )
                if removed is None or removed.get("exitcode", -1) != 0:
                    raise ProxmoxAPIError("Proxmox guest-agent bootstrap staging file removal failed")
            except (ProxmoxAPIError, KeyboardInterrupt, SystemExit, GeneratorExit) as cleanup_failure:
                if active_failure is None:
                    if not isinstance(cleanup_failure, ProxmoxAPIError):
                        raise
                    raise ProxmoxAPIError("could not verify removal of the Proxmox bootstrap staging file") from None
                _warn_bootstrap_file_residue()

        if result is None:
            raise ProvisioningError("Proxmox guest-agent bootstrap timed out")

        exit_code = result.get("exitcode", -1)
        stdout = result.get("out-data", "")
        parsed = parse_bootstrap_output(stdout, exit_code)

        if parsed.ok:
            assert parsed.tailscale_ip is not None
            return parsed.tailscale_ip

        raise ProvisioningError(f"Proxmox guest-agent bootstrap failed (exit {exit_code})")
