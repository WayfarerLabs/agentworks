"""``VMPlatform``: the VM-domain capability, plus the provisioning
request/result shapes.

A VM platform is the code that runs VMs on one backend kind (lima,
wsl2, azure, proxmox). Platforms register in ``VM_PLATFORM_REGISTRY``
(``agentworks.vms.platforms``) and publish as read-only ``vm-platform``
capability resources; the declarable ``vm-site`` kind exposes a
configured platform ("a place to create VMs"), and all invocation goes
through site resolution (``agentworks.vms.sites``). See ADR 0016 and
``docs/sdd/2026-07-01-vm-sites/``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from agentworks.config import Config
    from agentworks.db import VMRow, VMStatus
    from agentworks.resources.reference import ConfigReference
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
    azure_vm_size: str | None = None


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


class VMPlatform(ABC):
    """Capability: the code that runs VMs on one backend kind.

    Registered in ``VM_PLATFORM_REGISTRY`` and published as a read-only
    ``vm-platform`` capability resource; invoked only through site
    resolution (``agentworks.vms.sites``). Instances are constructed by
    the site layer as ``cls(site_name, platform_config, secret_values)``
    -- the platform bound to one declared site.

    Class-level contract (consumed by the vm-site kind decoder, the
    capability publisher, the R4 slug nudge, and the DB migration):
    ``name``, ``description``, ``validate_config``, ``shared_backend``,
    and ``legacy_platform_metadata``.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    # Operator guidance shown when native_transport returns None (the
    # transports factory embeds it in the StateError hint). Platforms
    # that opt out of a native transport override with prose naming
    # their actual escape hatch.
    no_native_transport_hint: ClassVar[str] = (
        "This platform has no interactive native transport."
    )

    def __init__(
        self,
        site_name: str,
        platform_config: Mapping[str, object],
        secret_values: Mapping[str, str] | None = None,
    ) -> None:
        self.site_name = site_name
        self.platform_config = platform_config
        self.secret_values: Mapping[str, str] = secret_values or {}

    @classmethod
    def validate_config(
        cls, owner: str, config: Mapping[str, object]
    ) -> tuple[ConfigReference, ...]:
        """Validate ``config`` (the ``platform_config`` blob owned by
        ``owner``) and return the resource references it implies.

        Invoked at each source's blob boundary (manifest decode with
        ``file:line`` framing; the legacy TOML loader) and by
        ``VMSiteDecl.referenced_resources()`` at finalize; MUST be pure.
        ``owner`` is display context for error messages.

        Base behavior: accepts no configuration. Subclasses with config
        override wholesale.

        NOTE: this invoked-validation API may be deprecated in favor of
        capabilities pushing a declarative config schema definition at
        registration time (fields typed as resource references to
        specific kinds, with usage information), letting the core
        engine validate and derive references without invoking the
        capability.
        """
        if config:
            display = getattr(cls, "name", cls.__name__)
            raise ConfigError(
                f"{owner}: the {display} platform accepts no "
                f"configuration; got {sorted(config)}"
            )
        return ()

    @classmethod
    def shared_backend(cls, platform_config: Mapping[str, object]) -> bool:
        """True when multiple agentworks installs plausibly share this
        site's backend (drives the R4 deferred slug nudge). Cloud and
        cluster platforms return a constant True; lima computes from
        ``vm_host`` presence. Default False (single-workstation
        backends).
        """
        return False

    @classmethod
    def legacy_platform_metadata(
        cls, row: Mapping[str, Any], legacy: Mapping[str, Any]
    ) -> dict[str, str]:
        """Map a pre-SDD ``vms`` row's legacy column values to this
        platform's ``platform_metadata`` conventions.

        Pure over its two inputs: ``row`` is the sqlite row mapping;
        ``legacy`` is the migration context's best-effort parse of the
        config file's legacy TOML sections (possibly empty; nothing may
        depend on it). Keys with nothing to record are omitted, never
        empty strings. Consumed only by the one-shot DB migration.
        """
        return {}

    @abstractmethod
    def create(self, request: ProvisionRequest) -> ProvisionResult:
        """Create the backend-side VM.

        Responsibilities:

        - Construct a backend-side name, using ``request.system_slug``
          as the namespacing token when set (else ``request.vm_name``).
        - Pre-flight collision check (SDD R9): raise ``StateError`` with
          clear guidance when a resource with the intended name already
          exists (all four in-tree platforms; soft-name backends may
          auto-suffix instead).
        - Create the resource(s).
        - Return ``ProvisionResult`` with ``platform_metadata``
          capturing whatever identifiers subsequent ops need, without
          relying on live configuration (e.g. proxmox records the node
          alongside the vmid).
        """

    @abstractmethod
    def start(self, vm: VMRow) -> None:
        """Start a stopped VM. Reads ``vm.platform_metadata``."""

    @abstractmethod
    def stop(self, vm: VMRow) -> None:
        """Stop a running VM. Reads ``vm.platform_metadata``."""

    @abstractmethod
    def delete(self, vm: VMRow) -> None:
        """Delete a VM and clean up backend resources. Reads
        ``vm.platform_metadata``."""

    @abstractmethod
    def status(self, vm: VMRow) -> VMStatus:
        """Query the live observed status. Reads ``vm.platform_metadata``."""

    @abstractmethod
    def display_backend_name(self, vm: VMRow) -> str:
        """Short human-readable identifier for the backend-side resource,
        for ``agw vm describe`` and error messages (azure returns the
        VM-name portion of the resource ID; wsl2 the distro name;
        proxmox ``vmid@node``). Reads ``vm.platform_metadata``.
        """

    def native_transport(
        self, vm: VMRow, *, config: Config | None = None
    ) -> Transport | None:
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

    def post_tailscale_ready(self, vm: VMRow) -> None:  # noqa: B027 -- intentional concrete no-op
        """Hook called once the VM's Tailscale node is up during create.

        Default no-op. Azure overrides to detach the cloud-init public
        IP at the moment Tailscale becomes reachable, minimizing the
        window the VM is exposed to the internet. The asymmetry vs.
        :meth:`transient_route` is genuine: the matching attach lives
        inside :meth:`create` (cloud-init bootstrap needs the IP) and
        the detach fires at an async Tailscale-ready point inside
        ``initialize_vm``, neither of which is an ExitStack-shaped
        lifecycle.
        """

    def transient_route(self, vm: VMRow) -> AbstractContextManager[None]:
        """Hold any platform-native transient network state while the
        native transport is in use.

        Default no-op (:func:`contextlib.nullcontext`) for platforms
        whose native transport works without setup (lima, wsl2). Azure
        overrides to attach a public IP on enter and detach on exit so
        the transient state is bounded by the caller's
        :class:`contextlib.ExitStack` scope.
        """
        return nullcontext()

    def vm_active(
        self, vm: VMRow, *, config: Config | None = None
    ) -> AbstractContextManager[None]:
        """Hold the VM against the backend's own idle-shutdown mechanism
        for the duration of the context.

        Callers gate with the manager's ``ensure_active`` first, so on
        entry the VM is either running or was just started. Default
        no-op for platforms without an idle-shutdown mechanism (lima,
        azure, proxmox); wsl2 overrides to anchor the distro against
        ``vmIdleTimeout``. ``config`` carries operator settings (wsl2
        builds the Tailscale transport for its reconnect wait).
        """
        return nullcontext()
