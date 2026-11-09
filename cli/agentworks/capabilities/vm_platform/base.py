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

import ipaddress
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
    from agentworks.execution.errors import FeatureUnavailable
    from agentworks.execution.root_script import RawRun
    from agentworks.execution.trust import HostKey, TrustBinding


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
    # The operator's SSH private key, for a platform whose create path reaches
    # the VM over SSH. Create always passes the configured one.
    ssh_private_key: Path
    # Host-key trust for a platform whose create path reaches the VM over its
    # public address before the VM is in the host-key store: an UNVERIFIED
    # binding whose file the manager owns and deletes when the create ends.
    provisioning_trust: TrustBinding
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

    ``tailnet_address`` is the joined node's address, read over the
    platform's root channel with ``tailscale_join.settle_tailnet_address``;
    core validates it again before recording it, and reaches the VM over SSH
    there for everything after create. ``platform_metadata`` is the opaque
    dict written verbatim to
    ``vms.platform_metadata``; the owning platform is its only reader.
    Keys are absent when there is nothing to record, never empty
    strings.
    """

    tailnet_address: str
    platform_metadata: dict[str, str] = field(default_factory=dict)


def parse_tailnet_address(text: object) -> str:
    """The VM's tailnet address in canonical form, or ``ValueError``.

    Validates at two boundaries: each address read over a platform's root
    channel (guest output), and the address a platform returns in
    :class:`ProvisionResult` before core records it (a plugin's value). The
    address is the node's IPv4 one (what ``tailscale ip -4`` prints), and an
    address no tailnet assigns (loopback, unspecified, multicast, link-local
    or reserved) is refused. No tailnet range is assumed, so a self-hosted
    tailnet with another prefix still parses.
    """
    if not isinstance(text, str):
        raise ValueError("a tailnet address must be text")
    address = ipaddress.IPv4Address(text.strip())
    if (
        address.is_loopback
        or address.is_unspecified
        or address.is_multicast
        or address.is_link_local
        or address.is_reserved
    ):
        raise ValueError(
            "a tailnet address cannot be a loopback, unspecified, multicast, link-local or reserved address"
        )
    return str(address)


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

    owner_kind: str = "vm-site"

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

    # Guidance for a platform that offers no native shell (it does not
    # override :meth:`native_shell`): where its operator reaches a VM by hand.
    native_shell_unavailable_hint: ClassVar[str | None] = None

    # Whether ``run_as_root``'s channel goes through the guest's sshd, so a
    # broken sshd can leave no root script a way in (recover's failure hint
    # says so). Declared by every platform (a descriptor requirement), never
    # defaulted here.
    recovery_needs_guest_sshd: ClassVar[bool]

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
        - Complete create-time bootstrap, then join Tailscale once with
          ``tailscale_join.join_tailscale`` (the clouds through
          ``complete_cloud_bootstrap``) and read the node's address with
          ``settle_tailnet_address`` over :meth:`run_as_root`, all inside the
          rollback window: an address that cannot be read logs the node out
          and fails create like any other step.
        - Roll back partial backend state before letting a failure OR
          an operator interrupt (``KeyboardInterrupt``) propagate: the
          caller's unwind deletes only the DB row, so any backend
          resource left behind is orphaned with nothing to target it.
          All in-tree platforms implement both arms: Azure (#338),
          proxmox (#343), lima and wsl2 (#340/#344), and EC2.
          If a platform cannot confirm that rollback completed, it raises
          ``RetainedProvisioningError`` with the identifiers needed by its
          ``delete`` op. Core persists them and retains the failed VM row.
        - Return ``ProvisionResult`` with the required ``tailnet_address``
          the settle read and ``platform_metadata`` capturing whatever
          identifiers subsequent ops need, without relying on live
          configuration (e.g. proxmox records the node alongside the vmid).
          Core reaches the VM over SSH at that address from then on, and
          attests the live Debian release there itself.
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

    @abstractmethod
    def run_as_root(self, vm: VMRow, ctx: RunContext, script: bytes, deadline: float) -> RawRun:
        """Run ``/bin/bash -s`` as root through the platform's own channel, with ``script`` on its stdin.

        The channel works without the VM's Tailscale state. Write ``script``,
        close stdin, and return everything the channel printed on each
        stream when it ends or ``deadline`` seconds pass; stop waiting at the
        deadline and never claim the guest command stopped. Run it once:
        never retry, parse the output or report an exit code (``RawRun`` has
        no field for one, because no channel's status is trusted; core reads
        the status from markers in the streams). ``script`` can carry a
        secret, so it never reaches an argument, a log, an exception or a
        file. A channel that waits for the VM to answer before it dispatches
        (the clouds' public SSH, Proxmox's guest agent) raises
        ``root_script.ChannelUnreachable`` when that wait runs out, since
        nothing was dispatched: callers stop there, and do not try a logout
        they could not deliver. Raise another ``AgentworksError`` when the
        channel cannot start or a provider call fails; core treats that like
        an unknown outcome and never dispatches again. ``ctx`` is the
        op-start context, for a platform whose channel needs a credential.
        """

    def native_shell(self, vm: VMRow, ctx: RunContext) -> int:
        """Open an interactive login shell on the VM through the platform's own channel.

        Optional: a platform offers one by overriding this, and the default
        offers none. Core checks for the override with
        :func:`require_native_shell` before the operation boundary, so a
        platform without one is refused before any credential resolves. The
        override runs the shell on the operator's terminal, without the VM's
        Tailscale, and returns its exit status; core owns the terminal guard
        around it. ``ctx`` is the op-start context, for a platform whose
        channel needs a credential.
        """
        raise _native_shell_unavailable(self, vm.name)

    def read_host_keys(self, vm: VMRow, ctx: RunContext) -> tuple[HostKey, ...]:
        """The VM's public host keys, read through :meth:`run_as_root` by core's fixed script.

        Recovery shows them to the operator, who compares them with the VM's
        console before anything is recorded. A platform whose root channel
        verifies the host key it is reading overrides this to read under an
        Unverified provisioning file instead.
        """
        from functools import partial

        from agentworks.execution import root_script

        return root_script.read_host_keys(partial(self.run_as_root, vm, ctx))

    def close_provisioning_access(self, vm: VMRow, ctx: RunContext) -> None:  # noqa: B027  # intentional concrete no-op
        """Close whatever ingress the platform opened for bootstrap; Phase A calls it once, however it ends.

        Default no-op. The clouds override it to remove their bootstrap SSH
        allow (scoped to the operator's egress prefixes; the deny-all-inbound
        baseline remains), leaving the VM with zero inbound exposure. Unlike a
        cloud's route around each :meth:`run_as_root` call, this ingress is
        not one scope: it opens inside :meth:`create` (cloud-init needs
        inbound SSH) and closes when Phase A (``phase_a.bootstrap``) ends,
        whether the VM contract passed, Phase A failed (the row is marked
        FAILED) or the operator interrupted it (the row stays incomplete). A
        create that fails before it returns rolls back its own ingress
        inside :meth:`create` and never reaches this hook. The clouds' base
        (``public_ssh.PublicAddressPlatform``) calls it again after each
        root-channel route, so an access an earlier close left open is
        closed then, and a close with nothing to remove must be silent.

        Implementations must not assume the VM is reachable or complete, and
        the callers treat the hook as best effort
        (:func:`close_provisioning_access_or_warn`): a failure warns with what
        it raised, so an error should name what may remain open, and an error or
        interrupt already propagating keeps going. The
        ordinary-command gate refuses a VM Phase A did not complete,
        ``vm shell --platform`` included, so debugging such a VM goes
        through the platform's console outside the firewalled path.

        ``ctx`` is the op-start :class:`RunContext` (see :meth:`create`):
        closing the ingress is a backend call and reads any credential it
        needs from it. The caller passes the create op's own scoped context,
        whose secrets were resolved before Phase A began, so even the
        interrupt path never resolves a secret here for the first time; with
        a service principal or access key configured there is no ambient
        fallback.
        """

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

        No ``ctx`` here: every hold that exists is local (wsl2 runs
        ``wsl.exe``), so none makes a backend call and none needs a
        credential. A platform whose hold does (a cloud API "keep this
        instance awake") would take ``ctx`` as the ops do.
        """
        return nullcontext()


def offers_native_shell(platform: VMPlatform) -> bool:
    """Whether the platform overrides :meth:`VMPlatform.native_shell`; reads only the class."""
    return type(platform).native_shell is not VMPlatform.native_shell


def require_native_shell(platform: VMPlatform, vm_name: str) -> None:
    """Refuse a platform that does not override :meth:`VMPlatform.native_shell`.

    Reads only the class, so a command calls it before its operation boundary
    resolves anything.
    """
    if not offers_native_shell(platform):
        raise _native_shell_unavailable(platform, vm_name)


def close_provisioning_access_or_warn(platform: VMPlatform, vm: VMRow, ctx: RunContext) -> None:
    """Run :meth:`VMPlatform.close_provisioning_access` to its end, warning rather than raising when it fails.

    An interrupt that arrives meanwhile waits for the close to finish and is
    raised then (:func:`agentworks.interrupts.interrupts_deferred`), since a
    close cut short before its provider call leaves the allow open. A failure
    warns with what the platform said, which names the rule, and the command
    that finishes the close: any later root-channel operation, of which
    ``vm recover`` is the one a complete VM always allows, or ``vm delete``
    for a VM that never completed provisioning.
    """
    from agentworks import output
    from agentworks.db import ProvisioningStatus
    from agentworks.interrupts import interrupts_deferred

    with interrupts_deferred():
        try:
            platform.close_provisioning_access(vm, ctx)
        except Exception as error:
            if vm.provisioning_status == ProvisioningStatus.COMPLETE.value:
                remedy = f"run 'agw vm recover {vm.name}' to close it"
            else:
                remedy = f"run 'agw vm delete {vm.name}' to remove it with the VM"
            output.warn(f"could not close the provisioning access of VM '{vm.name}': {error}; {remedy}")


def _native_shell_unavailable(platform: VMPlatform, vm_name: str) -> FeatureUnavailable:
    from agentworks.execution.errors import FeatureUnavailable

    return FeatureUnavailable(vm_name, platform.name, "a native shell", platform.native_shell_unavailable_hint)
