"""``VMPlatform``: the VM-domain capability, plus the provisioning
request/result shapes.

A VM platform is the code that runs VMs on one backend kind (lima,
wsl2, azure-vm, aws-ec2, gcp-gce, proxmox). Platforms register in ``VM_PLATFORM_REGISTRY``
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
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from agentworks.capabilities.base import Capability, idempotent_op
from agentworks.errors import ProvisioningError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from agentworks.capabilities.base import RunContext
    from agentworks.config import Config
    from agentworks.db import VMRow, VMStatus
    from agentworks.debian import DebianRelease
    from agentworks.transports import Transport


class BootstrapProgress(Protocol):
    """Value-free progress sink for create-time VM bootstrap.

    Platforms can report the bootstrap transcript through this narrow surface
    without depending on the VM manager or the concrete logger that owns the
    durable operation log. The manager retains construction and lifecycle
    ownership of that logger.
    """

    def step(self, name: str) -> None: ...

    def output(self, text: str) -> None: ...

    def warning(self, msg: str) -> None: ...

    def log_error(self, msg: str) -> None: ...


@dataclass
class ProvisionRequest:
    """All inputs a platform might need to create a VM.

    Every platform receives the same request shape; each ignores fields
    it doesn't use. Adding a platform-specific input means adding a
    field here, not changing the protocol. Units match the rest of the
    codebase (GiB), so there is no conversion seam.

    The hardware fields are REQUIRED and non-optional, which is FR15
    made structural: the vm-template's model layer resolves them (see
    ``ResolvedVMTemplate``, which declares the defaults exactly once),
    so by the time a platform reads one it is a number. Nothing here
    carries a default of its own, because a default here would be a
    second declaration of the same value, free to drift from the first;
    the ``swap`` fallbacks this replaced had already drifted, each
    platform substituting 0 against the template layer's 4.
    """

    vm_name: str
    # Core owns release policy. Platforms translate this concrete value
    # through their own artifact map and never infer "current" themselves.
    debian_release: DebianRelease
    # The R11 hostname ({slug}-{vm_name} or {vm_name}), computed by the
    # manager and recorded in vms.hostname; platforms bake it via their
    # bootstrap paths and tailscaled picks it up as the node name.
    hostname: str
    system_slug: str | None
    admin_username: str
    ssh_public_key: str
    # Path to the operator's SSH private key when a platform's create path
    # constructs an SSH transport.
    ssh_private_key: Path | None
    # Domain-owned operation input: the vm-template declares the secret name
    # and the VM manager resolves its value before platform dispatch. This is
    # intentionally separate from platform-config secrets delivered by ctx.
    tailscale_auth_key: str
    # Value-free sink for platform-owned create-time bootstrap progress. The
    # VM manager owns its construction, redactions, and lifecycle.
    progress: BootstrapProgress
    cpus: int
    memory_gib: int
    disk_gib: int
    swap_gib: int


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
    tailscale_ip: str | None = None


class RetainedProvisioningError(ProvisioningError):
    """Create failed after backend resources became unsafe to forget.

    The manager persists ``platform_metadata`` on the provisional VM row and
    leaves that row in failed state so the owning platform can target the
    surviving resources through an explicit ``vm delete`` retry.
    """

    def __init__(
        self,
        message: str,
        *,
        platform_metadata: dict[str, str],
        entity_name: str,
        hint: str,
    ) -> None:
        super().__init__(message, entity_kind="vm", entity_name=entity_name, hint=hint)
        self.platform_metadata = dict(platform_metadata)


class VMPlatform(Capability):
    """Capability: the code that runs VMs on one backend kind.

    Registered in ``VM_PLATFORM_REGISTRY`` and published as a read-only
    ``vm-platform`` capability resource; invoked only through site
    resolution (``agentworks.vms.sites``). Instances are constructed by
    the site layer as ``cls(site_name, platform_config)``, which the
    base validates into this platform's declared model: the platform
    bound to one declared site, never resolved secret values (see the
    ``Capability`` lifecycle). The declared config secrets join an
    operation's boundary union through the holding node's
    ``secret_refs`` and their values arrive per op call through the
    context (``ctx.secret``).

    Class-level contract (consumed by registration conformance, the
    capability publisher, the core's config validation and reference
    extraction, and the DB migration): ``name``, ``description``,
    ``contract_version``, ``config_model``, and
    ``legacy_platform_metadata``.

    Idempotency: ops flagged with ``@idempotent_op`` (``start``,
    ``stop``, ``delete``) must land in the same place run twice as run
    once (``reinit`` re-applies everything and failed commands are
    retried). ``create`` is deliberately unflagged: it is one-shot per
    VM. A platform must collision-check its intended backend name and
    either fail loudly or select and persist a different collision-free
    name, never target or replace the existing resource.
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
        everywhere because ssh-placed sites run ``limactl`` on the placement host over
        SSH, but a local-Lima site without a local ``limactl`` is not-ready).
        Default: supported everywhere.
        """
        return None

    # Operator guidance shown when native_transport returns None.
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

    def validate_create_release(self, release: DebianRelease) -> None:
        """Validate create-time release inputs before secret resolution.

        This operation-specific preflight is pure and offline. Platforms whose
        operator-owned configuration maps releases to artifacts override it so
        a missing entry fails before ``runup`` authenticates to the backend.
        The default is a no-op because code-owned catalogs ship with the
        implementation and remain covered by conformance tests and ``create``.

        ``create`` still resolves the same value before mutation. This early
        boundary improves failure ordering without making a current-release
        artifact a load-time requirement for sites used only by existing VMs.
        """

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
        - Pre-flight collision check: when a resource with the intended
          name already exists, either raise ``StateError`` with clear
          guidance (all six in-tree platforms) or choose a different,
          collision-free name. A platform that chooses a name must persist
          the exact backend identifier in ``platform_metadata`` so every
          later operation targets the created resource.
        - Create the resource(s).
        - Complete create-time bootstrap, including the Tailscale join, before
          returning. A successful join may return without an IP when discovery
          failed; the manager retries only ``tailscale ip -4`` in that case.
        - Roll back partial backend state before letting a failure OR
          an operator interrupt (``KeyboardInterrupt``) propagate: the
          caller's unwind deletes only the DB row, so any backend
          resource left behind is orphaned with nothing to target it.
          All in-tree platforms implement both arms: Azure (#338),
          proxmox (#343), lima and wsl2 (#340/#344), and EC2.
          If a platform cannot confirm that rollback completed, it raises
          ``RetainedProvisioningError`` with the identifiers needed by its
          ``delete`` op. Core persists them and retains the failed VM row.
        - Return ``ProvisionResult`` with ``platform_metadata``
          capturing whatever identifiers subsequent ops need, without
          relying on live configuration (e.g. proxmox records the node
          alongside the vmid), and a transport through which core can attest
          the live Debian release.
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
        half-cleaned backends; a second run finishes the job).

        Failure contract (#329): a delete that cannot remove the
        backend VM must raise a typed error rather than return. The
        caller (``delete_vm``) deletes the DB row only after this op
        returns, so a swallowed backend failure orphans a surviving VM
        with nothing left to target it; a raise keeps the row for a
        retry. Best-effort warn-and-continue is acceptable only for auxiliary
        resources an operator can still find and remove (azure's NIC/IP/NSG/disk
        sweep); the VM itself is the gate,
        which azure enforces with a post-teardown existence probe."""

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

    def native_transport(self, vm: VMRow, ctx: RunContext, *, config: Config | None = None) -> Transport | None:
        """Platform-native :class:`Transport` for Tailscale recovery and
        ``vm shell --platform``.

        The contract requires this transport to work independently of the VM's
        Tailscale state. The optional return and default ``None`` temporarily
        preserve the current non-compliant Proxmox behavior (#727); they do not
        make the transport optional for an implementation.

        Callers reach this through the
        :func:`agentworks.transports.native_transport` factory, which
        wraps the call in :meth:`transient_route`, applies the
        reachability probe, and raises a typed ``StateError`` (with the
        platform's console hint) on ``None``.

        ``ctx`` is the op-start :class:`RunContext`, exactly as the ops
        receive it (see :meth:`create`): building a native transport is
        backend work like any other, and a platform whose backend API
        needs a credential reads it here via ``ctx.secret(name)``. Azure
        needs it (its network/compute clients resolve the transient
        public IP); lima and wsl2 accept and ignore it.

        ``config`` carries OPERATOR settings (azure needs
        ``config.operator.ssh_private_key`` for the public-IP path),
        distinct from the bound ``platform_config``.
        """
        return None

    def post_tailscale_ready(self, vm: VMRow, ctx: RunContext) -> None:  # noqa: B027  # intentional concrete no-op
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

        ``ctx`` is the op-start :class:`RunContext` (see
        :meth:`create`): closing the ingress is a backend call and
        reads any credential it needs from it. With a service principal
        configured there is no ambient fallback, so the caller must hand
        the real scoped context (the create op's own).
        """

    def secure_failed_vm(self, vm: VMRow, ctx: RunContext) -> None:  # noqa: B027  # intentional concrete no-op
        """Hook called when a created VM is kept after Phase A fails:
        post-create Tailscale verification died (the row is marked FAILED) or
        the operator interrupted verification (the row keeps its in-flight
        status), and the VM is kept for debugging rather than rolled back.

        Create-time bootstrap failures never reach this hook. They raise from
        :meth:`create` while the platform's backend rollback window is open.

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

        ``ctx`` is the op-start :class:`RunContext` (see
        :meth:`create`): closing the ingress is a backend call and
        reads any credential it needs from it. The caller passes the
        create op's own scoped context, whose secrets are already
        resolved (the boundary resolve ran before Phase A began), so
        even the interrupt path never resolves a secret here for the
        first time; with a service principal configured there is no
        ambient fallback.
        """

    def transient_route(
        self, vm: VMRow, ctx: RunContext, *, config: Config | None = None
    ) -> AbstractContextManager[None]:
        """Hold any platform-native transient network state while the
        native transport is in use.

        Default no-op (:func:`contextlib.nullcontext`) for platforms
        whose native transport works without setup (lima, wsl2). Azure
        overrides to open a scoped SSH route on enter (heal a missing
        public IP, poke an ephemeral NSG allow scoped to the operator's
        egress prefixes) and delete the allow on exit so the transient
        state is bounded by the caller's :class:`contextlib.ExitStack`
        scope.

        ``ctx`` is the op-start :class:`RunContext` (see
        :meth:`create`): opening and closing the route are backend calls
        and read any credential they need from it, with no ambient
        fallback when a service principal is configured. ``config``
        carries OPERATOR settings, mirroring :meth:`native_transport`
        and :meth:`vm_active` (azure reads
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

        No ``ctx`` here, unlike :meth:`transient_route`: every hold that
        exists is local (wsl2 runs ``wsl.exe``), so none makes a backend
        call and none needs a credential. A platform whose hold does
        (a cloud API "keep this instance awake") threads ``ctx`` in the
        same way the transport hooks did.
        """
        return nullcontext()
