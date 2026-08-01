"""``VMPlatform``: the VM-domain capability, plus the provisioning
request/result shapes.

A VM platform is the code that runs VMs on one backend kind (lima,
wsl2, azure-vm, proxmox). Platforms register in ``VM_PLATFORM_REGISTRY``
(``agentworks.capabilities.vm_platform``) and publish as read-only ``vm-platform``
capability resources; the declarable ``vm-site`` kind exposes a
configured platform ("a place to create VMs"), and all invocation goes
through site resolution (``agentworks.vms.sites``). See ADR 0016 for
the capability/declarable split.
"""

from __future__ import annotations

from abc import abstractmethod
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from agentworks.capabilities.base import Capability, idempotent_op

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from agentworks.capabilities.base import RunContext
    from agentworks.config import Config
    from agentworks.db import VMRow, VMStatus
    from agentworks.transports import Transport


@dataclass
class ProvisionRequest:
    """All inputs a platform might need to create a VM.

    Every platform receives the same request shape; each ignores fields
    it doesn't use. Adding a platform-specific input means adding a
    field here, not changing the protocol. Units match the rest of the
    codebase (GiB), so there is no conversion seam.
    """

    vm_name: str
    # The R11 hostname ({slug}-{vm_name} or {vm_name}), computed by the
    # manager and recorded in vms.hostname; platforms bake it via their
    # bootstrap paths and tailscaled picks it up as the node name.
    hostname: str
    system_slug: str | None
    admin_username: str
    ssh_public_key: str
    # Path to the operator's SSH private key, for platforms whose
    # native transport is plain SSH during create (azure, proxmox).
    ssh_private_key: Path | None
    # None: the platform defers Tailscale bootstrap to Phase A (wsl2
    # always; lima/azure/proxmox when no key was resolvable).
    tailscale_auth_key: str | None
    cpus: int | None = None
    memory_gib: int | None = None
    disk_gib: int | None = None
    swap_gib: int | None = None


@dataclass
class ProvisionResult:
    """What a platform returns from ``create()``.

    ``platform_metadata`` is the opaque dict written verbatim to
    ``vms.platform_metadata``; the owning platform is its only reader.
    Keys are absent when there is nothing to record, never empty
    strings.
    """

    native_transport: Transport
    platform_metadata: dict[str, str] = field(default_factory=dict)
    bootstrap_complete: bool = False
    tailscale_ip: str | None = None


class VMPlatform(Capability):
    """Capability: the code that runs VMs on one backend kind.

    Registered in ``VM_PLATFORM_REGISTRY`` and published as a read-only
    ``vm-platform`` capability resource; invoked only through site
    resolution (``agentworks.vms.sites``). Instances are constructed by
    the site layer as ``cls(site_name, platform_config)``: the platform
    bound to one declared site, never resolved secret values (see the
    ``Capability`` lifecycle). The declared config secrets join an
    operation's boundary union through the holding node's
    ``secret_refs`` and their values arrive per op call through the
    context (``ctx.secret``).

    Class-level contract (consumed by the vm-site kind decoder, the
    capability publisher, and the DB migration): ``name``,
    ``description``, ``dependencies`` / ``validate``, and
    ``legacy_platform_metadata``.

    Idempotency: ops flagged with ``@idempotent_op`` (``start``,
    ``stop``, ``delete``) must land in the same place run twice as run
    once (``reinit`` re-applies everything and failed commands are
    retried). ``create`` is deliberately unflagged: it is one-shot per
    VM, and its collision check makes a re-run a loud error rather than
    a silent second resource.
    """

    owner_kind: ClassVar[str] = "vm-site"

    @classmethod
    def unsupported_reason(cls) -> str | None:
        """Why this platform cannot run on this host AT ALL, or ``None``
        when it can. A non-None reason makes every ``vm-platform`` node for
        it not-ready (host-support is READINESS, not absence, R13): the row
        still publishes, every site referencing it is not-ready with this
        reason folded into its chain, and doctor lists the platform as
        installed-but-not-ready.

        This is the config-INDEPENDENT half of readiness: a pure, fast
        classmethod with no config, no instance, and no secrets, run at
        every registry build and consumed by the readiness fold (LLD c) as
        the platform node's own verdict. It answers "could any configuration
        of this platform ever work here" (wsl2 off Windows: no), not "is this
        configured site ready" (that is preflight) and not "is a tool merely
        missing but installable" (that is the config-dependent
        :meth:`Capability.not_ready`: lima the platform is supported
        everywhere because remote sites run ``limactl`` on the vm_host over
        SSH, but a local-Lima site without a local ``limactl`` is not-ready).
        Default: supported everywhere.
        """
        return None

    # Operator guidance shown when native_transport returns None (the
    # transports factory embeds it in the StateError hint). Platforms
    # that opt out of a native transport override with prose naming
    # their actual escape hatch.
    no_native_transport_hint: ClassVar[str] = "This platform has no interactive native transport."

    # Operator guidance warned when every reachability probe of the
    # native transport fails (the transports factory emits it just
    # before the SSHError propagates). None means no extra guidance;
    # platforms whose route setup can succeed while the transport still
    # cannot connect override with prose naming the likely cause (azure:
    # the ephemeral SSH allow is scoped to the DETECTED egress IP, which
    # may not be where the operator's SSH traffic actually leaves).
    probe_failure_hint: ClassVar[str | None] = None

    @property
    def site_name(self) -> str:
        """The bound site's name (the capability-generic ``owner_name``,
        under the domain's vocabulary)."""
        return self.owner_name

    @property
    def platform_config(self) -> Mapping[str, object]:
        """The bound site's validated config blob (the
        capability-generic ``config``, under the domain's vocabulary)."""
        return self.config

    @classmethod
    def legacy_platform_metadata(cls, row: Mapping[str, Any], legacy: Mapping[str, Any]) -> dict[str, str]:
        """Map a pre-v27 ``vms`` row's legacy column values to this
        platform's ``platform_metadata`` conventions.

        Pure over its two inputs: ``row`` is the sqlite row mapping;
        ``legacy`` is the migration context's best-effort parse of the
        config file's legacy TOML sections (possibly empty; nothing may
        depend on it). Keys with nothing to record are omitted, never
        empty strings. Consumed only by the one-shot DB migration.
        """
        return {}

    @abstractmethod
    def create(self, request: ProvisionRequest, ctx: RunContext) -> ProvisionResult:
        """Create the backend-side VM.

        ``ctx`` is the op-start :class:`RunContext`; an op that needs a
        resolved config secret (proxmox's API token) reads it via
        ``ctx.secret(name)``, the declare/receive contract's delivery
        surface. Platforms without op secrets ignore it. The same
        applies to every power op below: when the orchestrator drives
        the op it hands a context scoped over the site's declared
        names; when the activation gate drives it, the context wraps
        the gate's own scoped reader.

        Responsibilities:

        - Construct a backend-side name, using ``request.system_slug``
          as the namespacing token when set (else ``request.vm_name``).
        - Pre-flight collision check: raise ``StateError`` with
          clear guidance when a resource with the intended name already
          exists (all four in-tree platforms; soft-name backends may
          auto-suffix instead).
        - Create the resource(s).
        - Return ``ProvisionResult`` with ``platform_metadata``
          capturing whatever identifiers subsequent ops need, without
          relying on live configuration (e.g. proxmox records the node
          alongside the vmid).
        """

    @idempotent_op
    @abstractmethod
    def start(self, vm: VMRow, ctx: RunContext) -> None:
        """Start a stopped VM. Reads ``vm.platform_metadata`` (and any
        op secret via ``ctx``; see :meth:`create`).

        Idempotent by contract: starting an already-running VM must
        land in the running state, not error."""

    @idempotent_op
    @abstractmethod
    def stop(self, vm: VMRow, ctx: RunContext) -> None:
        """Stop a running VM. Reads ``vm.platform_metadata`` (and any
        op secret via ``ctx``; see :meth:`create`).

        Idempotent by contract: stopping an already-stopped VM must
        land in the stopped state, not error."""

    @idempotent_op
    @abstractmethod
    def delete(self, vm: VMRow, ctx: RunContext) -> None:
        """Delete a VM and clean up backend resources. Reads
        ``vm.platform_metadata`` (and any op secret via ``ctx``; see
        :meth:`create`).

        Idempotent by contract: deleting resources that are already
        gone must succeed (``vm delete`` is retried against
        half-cleaned backends; a second run finishes the job)."""

    @abstractmethod
    def status(self, vm: VMRow, ctx: RunContext) -> VMStatus:
        """Query the live observed status. Reads
        ``vm.platform_metadata`` (and any op secret via ``ctx``; see
        :meth:`create`)."""

    @abstractmethod
    def display_backend_name(self, vm: VMRow) -> str:
        """Short human-readable identifier for the backend-side resource,
        for ``agw vm describe`` and error messages (azure returns the
        VM-name portion of the resource ID; wsl2 the distro name;
        proxmox ``vmid@node``). Reads ``vm.platform_metadata``.
        """

    def native_transport(self, vm: VMRow, *, config: Config | None = None) -> Transport | None:
        """Platform-native :class:`Transport` for bootstrap and
        ``vm shell --platform``, or ``None`` when the platform has no
        interactive native transport (proxmox: one-shot QEMU guest-agent
        exec can't host a shell).

        Callers reach this through the
        :func:`agentworks.transports.native_transport` factory, which
        wraps the call in :meth:`transient_route`, applies the
        reachability probe, and raises a typed ``StateError`` (with the
        platform's console hint) on ``None``.

        ``config`` carries OPERATOR settings (azure needs
        ``config.operator.ssh_private_key`` for the public-IP path),
        distinct from the bound ``platform_config``.
        """
        return None

    def post_tailscale_ready(self, vm: VMRow) -> None:  # noqa: B027  # intentional concrete no-op
        """Hook called once the VM's Tailscale node is up during create.

        Default no-op. The hook's contract is "close provisioning
        access": whatever ingress the platform opened for bootstrap is
        no longer needed once Tailscale carries the traffic. Azure
        overrides to delete the ephemeral bootstrap SSH allow rule
        (scoped to the operator's egress prefixes; a permanent
        deny-all-inbound baseline remains), leaving the VM with zero
        inbound exposure. The asymmetry vs. :meth:`transient_route` is
        genuine: the bootstrap ingress opens inside :meth:`create`
        (cloud-init needs inbound SSH) and closes at an async
        Tailscale-ready point inside ``bootstrap_vm`` (Phase A),
        neither of which is an ExitStack-shaped lifecycle.
        """

    def secure_failed_vm(self, vm: VMRow) -> None:  # noqa: B027  # intentional concrete no-op
        """Hook called when a create is kept without completing Phase A:
        the bootstrap or Tailscale verification died (the row is marked
        FAILED) or the operator interrupted it mid-bootstrap (the row
        keeps its in-flight status), and the VM is kept for debugging
        rather than rolled back.

        Default no-op. Same contract as :meth:`post_tailscale_ready`
        (which only fires on success): close provisioning access. Azure
        overrides to delete the ephemeral bootstrap SSH allow rule, so a
        failed VM defaults to zero inbound exposure rather than keeping
        its bootstrap ingress indefinitely. Implementations must not
        assume the VM is reachable, and the caller treats the hook as
        best-effort: the original failure keeps propagating regardless.
        Operator debugging survives the fail-closed posture: ``vm shell
        --platform`` pokes a fresh transient allow via
        :meth:`transient_route`, and platform consoles outside the
        firewalled path (Azure's serial console) are unaffected.
        """

    def transient_route(self, vm: VMRow, *, config: Config | None = None) -> AbstractContextManager[None]:
        """Hold any platform-native transient network state while the
        native transport is in use.

        Default no-op (:func:`contextlib.nullcontext`) for platforms
        whose native transport works without setup (lima, wsl2). Azure
        overrides to open a scoped SSH route on enter (heal a missing
        public IP, poke an ephemeral NSG allow scoped to the operator's
        egress prefixes) and delete the allow on exit so the transient
        state is bounded by the caller's :class:`contextlib.ExitStack`
        scope.

        ``config`` carries OPERATOR settings, mirroring
        :meth:`native_transport` and :meth:`vm_active` (azure reads
        ``config.operator.ssh_allow_cidrs`` to widen the allow's scope),
        distinct from the bound ``platform_config``.
        """
        return nullcontext()

    def vm_active(self, vm: VMRow, *, config: Config | None = None) -> AbstractContextManager[None]:
        """Hold the VM against the backend's own idle-shutdown mechanism
        for the duration of the context.

        A pure power-hold on every platform: it holds power, and does no
        connectivity verification or retry (that is handled uniformly by
        the shared paths, which run inside this hold). Callers converge
        the power state first (the orchestrated activation gate, or a
        create's just-provisioned VM), so on entry the VM is either
        running or was just started. Because no platform's hold retries
        connectivity, a gate that finds the target not confirmed-active
        yet not observed-stopped either (``ensure_active`` skips
        ``auto_start`` when a status probe reports RUNNING while
        tailscaled is mid-reattach) proceeds into the hold and the op and
        surfaces a plain SSHError, uniformly, rather than a WSL2-only
        reachability retry. Default no-op for platforms without
        an idle-shutdown mechanism (lima, azure, proxmox); wsl2 overrides
        to anchor the distro against ``vmIdleTimeout``. ``config`` is
        reserved operator settings, available to a platform whose hold
        needs them (none does today).
        """
        return nullcontext()
