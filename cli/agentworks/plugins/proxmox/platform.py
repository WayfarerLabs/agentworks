"""The Proxmox VE VM platform: clone + cloud-init + guest-agent bootstrap
via the Proxmox REST API."""

from __future__ import annotations

import functools
import time
import urllib.parse
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal

from pydantic import Field, model_validator

from agentworks import output
from agentworks.capabilities.vm_platform.base import ProvisionRequest, ProvisionResult, VMPlatform
from agentworks.capabilities.vm_platform.bootstrap_script import run_bootstrap
from agentworks.capabilities.vm_platform.debian_release import (
    operator_owned_release_value,
)
from agentworks.capabilities.vm_platform.tailscale_join import (
    CREATE_RERUN_HINT,
    join_tailscale,
    settle_tailnet_address,
)
from agentworks.db import VMStatus
from agentworks.debian import DebianRelease
from agentworks.errors import ConnectivityError, ProvisioningError, StateError
from agentworks.execution import root_script
from agentworks.plugins.proxmox import guest_agent
from agentworks.plugins.proxmox.api import ProxmoxAPI, ProxmoxAPIError
from agentworks.plugins.proxmox.teardown import (
    rollback_create_on_interrupt,
    rollback_partial_create,
    stop_and_delete_vm,
)
from agentworks.schema import AgwModel, NonEmptyStr, PositiveInt, SecretRef
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agentworks.capabilities.base import RunContext
    from agentworks.db import VMRow
    from agentworks.execution.root_script import RawRun

BOOTSTRAP_DEADLINE_SECONDS = 600
CLOUD_INIT_DEADLINE_SECONDS = guest_agent.AGENT_WAIT_SECONDS + 300
"""The guest agent's wait, then cloud-init's own 300 s: the one root-channel call draws on both."""


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

    contract_version: ClassVar[int] = 2
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

        QEMU Guest Agent provides Tailscale-independent administrative execution for
        bootstrap and recovery. Interactive native shell remains unavailable; use the
        Proxmox web UI's serial console for manual access.

        Creation runs the bootstrap, the Tailscale join and the address read as root through
        QEMU Guest Agent, with the auth key only on the join script's stdin. An address
        that cannot be read logs the node out. Core then checks the live Debian release over
        SSH at the returned address. A bootstrap, join or address failure rolls the
        cloned VM back; a later core mismatch retains an addressable failed VM for explicit
        deletion.

        Ships as the opt-in `proxmox` system plugin, so a site stays not-ready until
        `[plugins] system` lists it.
        """,
    )
    # QGA reaches the guest without its sshd.
    recovery_needs_guest_sshd: ClassVar[bool] = False
    native_shell_unavailable_hint: ClassVar[str] = (
        "Proxmox QEMU Guest Agent does not provide an interactive native shell. "
        "Use the Proxmox web UI's serial console "
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

                # 6. Wait for cloud-init to finish (releases apt lock), then run the
                # bootstrap, join and read the address through the guest agent,
                # the VM's root channel
                run_as_root = functools.partial(guest_agent.run_as_root, self._api(ctx), node, newid)
                output.detail("Waiting for cloud-init...")
                _wait_for_cloud_init(run_as_root, newid)
                run_bootstrap(
                    run_as_root,
                    admin_username=request.admin_username,
                    ssh_public_key=request.ssh_public_key,
                    hostname=request.hostname,
                    swap=request.swap_gib,
                    deadline=BOOTSTRAP_DEADLINE_SECONDS,
                    progress=request.progress,
                )
                join_tailscale(run_as_root, request.tailscale_auth_key, node=request.hostname)
                tailnet_address = settle_tailnet_address(
                    run_as_root, node=request.hostname, rerun_hint=CREATE_RERUN_HINT
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
            platform_metadata={"vmid": str(newid), "node": node},
            tailnet_address=tailnet_address,
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
            result = self._api(ctx).vm_status(self._vm_node(vm), self._vmid(vm), timeout=10)
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

    def run_as_root(self, vm: VMRow, ctx: RunContext, script: bytes, deadline: float) -> RawRun:
        """Run ``script`` as root through the QEMU Guest Agent, which needs no guest sshd."""
        return guest_agent.run_as_root(self._api(ctx), self._vm_node(vm), self._vmid(vm), script, deadline)

    # -- Helpers ---------------------------------------------------------------


def _wait_for_cloud_init(run_as_root: Callable[[bytes, float], RawRun], vmid: int) -> None:
    """Wait for cloud-init to finish inside the VM; status 2 is recoverable warnings.

    A channel failure is asked again until :data:`CLOUD_INIT_DEADLINE_SECONDS`
    runs out: cloud-init can restart the guest agent mid-run (Proxmox's
    ``ciupgrade``), and ``cloud-init status --wait`` changes nothing, so
    running it again is safe.
    """
    expires = time.monotonic() + CLOUD_INIT_DEADLINE_SECONDS
    while True:
        try:
            result = root_script.run(
                run_as_root, "cloud-init status --wait", deadline=max(expires - time.monotonic(), 0.1)
            )
            break
        except ConnectivityError:
            remaining = expires - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(guest_agent.POLL_INTERVAL_SECONDS, remaining))
    if result.status == 2:
        output.warn(f"cloud-init completed with recoverable warnings on Proxmox VMID {vmid}; continuing")
    elif result.status != 0:
        outcome = "an unknown outcome" if result.status is None else f"exit {result.status}"
        raise ProvisioningError(f"cloud-init did not complete on Proxmox VMID {vmid} ({outcome})")
