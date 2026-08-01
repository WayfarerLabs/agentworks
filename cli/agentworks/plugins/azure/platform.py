"""The Azure VM platform: creates and manages VMs via the Azure SDK."""

from __future__ import annotations

import base64
import contextlib
import logging
import os
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple, Protocol

from agentworks import output
from agentworks.capabilities.vm_platform.base import ProvisionRequest, ProvisionResult, VMPlatform
from agentworks.capabilities.vm_platform.bootstrap_script import generate_bootstrap_script
from agentworks.capabilities.vm_platform.cloud_init import PROVISIONING_PACKAGES, generate_cloud_init
from agentworks.db import VMStatus
from agentworks.errors import ConfigError, NotFoundError, StateError

# The network-resource plumbing (public IP, the NSG exposure rules,
# egress-IP discovery, cleanup) and the shared SDK-error wrapper live in
# the sibling network module; this module keeps the VMPlatform
# capability surface.
from agentworks.plugins.azure.network import (
    BOOTSTRAP_ALLOW_RULE_NAME,
    VNET_NAME_SUFFIX,
    cleanup_vm_resources,
    config_allow_cidrs,
    converge_nsg,
    delete_vm_and_resources,
    ensure_public_ip,
    get_vm_public_ip,
    initial_security_rules,
    operator_ssh_prefixes,
    poke_ssh_allow,
    remove_ssh_allow,
    rollback_create_on_interrupt,
    wrap_azure_error,
)
from agentworks.ssh import SSHError
from agentworks.transports import SSHTransport

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.network import NetworkManagementClient
    from azure.mgmt.resource.resources import ResourceManagementClient

    from agentworks.capabilities.base import RunContext
    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.resources.reference import ConfigReference
    from agentworks.transports import Transport


class _HasSubscriptionId(Protocol):
    """Structural protocol for anything with subscription_id (AzureConfig or _MinimalAzureConfig)."""

    @property
    def subscription_id(self) -> str: ...


# The azure-identity logger tree. Its ChainedTokenCredential emits a WARNING
# ("DefaultAzureCredential failed to retrieve a token from the included
# credentials...") when the credential chain is exhausted (nothing in it can
# authenticate). Observed by reproducing a failing DefaultAzureCredential
# probe: that WARNING is the only azure-identity record that clears the
# default threshold; the per-member
# "... .get_token failed:" lines are DEBUG. azure.core is deliberately not
# included: its HTTP request/response logging sits at INFO, so it never reaches
# the last-resort stderr handler and is not part of this noise class.
_AZURE_IDENTITY_LOGGER = "azure.identity"


def _quiet_azure_identity_logging() -> None:
    """Keep azure-identity's own credential-failure WARNING off stderr so the
    operator sees only agentworks' typed :class:`AzureError`.

    agentworks configures no logging handlers, so Python's last-resort handler
    prints WARNING+ to stderr. When a credential fails to authenticate,
    azure-identity logs its WARNING there directly ahead of the typed error we
    render for the same failure, so the operator sees the failure twice: once
    as raw SDK chatter, once as the clean typed one-liner. Raising the
    ``azure.identity`` logger's threshold above WARNING drops exactly that
    chatter (and the INFO/DEBUG below it) while still letting a genuine
    ERROR-level record through, leaving the typed rendering to stand alone.

    Skipped under --debug / AGW_DEBUG=1: there the operator wants the SDK
    detail, so the logger is left at its default threshold. ``AGW_DEBUG`` is
    the process-wide debug signal (the CLI mirrors ``--debug`` into it); a
    plugin reading it avoids importing ``agentworks.cli`` and inverting the
    layering.

    Idempotent and side-effect-localized: it only ever adjusts the single
    ``azure.identity`` logger, so calling it on every azure platform
    construction is safe. This one-time set (rather than a context manager
    scoped to the credential probe) covers both the probe and the lazy,
    op-time token requests inside the SDK clients, which emit the same WARNING.

    Single-shot assumption: the ``setLevel`` is decided once, when the first
    ``AzureVMPlatform`` is built, and never reset for the process lifetime.
    If ``AGW_DEBUG`` were to flip true AFTER an instance was already built with
    it off, the logger would stay quieted (no reset path). That is fine for
    today's one-command CLI; a longer-lived client (e.g. the anticipated
    web-UI) that toggles debug mid-process would need to re-decide the level.
    """
    if os.environ.get("AGW_DEBUG") == "1":
        return
    logging.getLogger(_AZURE_IDENTITY_LOGGER).setLevel(logging.ERROR)


def _build_credential() -> object:
    """Build the Azure credential to use, deciding the interactive-browser
    fallback with a single live probe.

    Tries DefaultAzureCredential first (picks up az login, env vars,
    managed identity, etc.) and probes it once with a real token request:
    that probe is BOTH the validation and the fallback decision. If it
    succeeds, the DefaultAzureCredential is the credential to use; if it
    raises ClientAuthenticationError (nothing in the chain can
    authenticate), fall back to interactive browser login. The browser
    credential is returned unprobed (its interaction happens lazily on the
    first real token request), exactly as before.

    Returns object to avoid a hard import of azure.core at module load
    time. Callers cast to the appropriate type when constructing SDK
    clients. The credential is subscription-independent, so this is
    called at most once per platform instance (via the caching
    :meth:`AzureVMPlatform._get_credential`) even when the instance's
    SDK clients span multiple subscriptions; the probe's cost, and its
    browser-fallback decision, are paid once per instance, not per op.
    """
    from azure.core.exceptions import ClientAuthenticationError
    from azure.identity import DefaultAzureCredential, InteractiveBrowserCredential

    cred = DefaultAzureCredential()
    try:
        cred.get_token("https://management.azure.com/.default")
        return cred
    except ClientAuthenticationError:
        output.info("No Azure credentials found, opening browser for login...")
        return InteractiveBrowserCredential()


_AZURE_REQUIRED_KEYS = ("subscription_id", "resource_group", "region")
_AZURE_OPTIONAL_KEYS = ("vm_sizes",)


class _VMSize(NamedTuple):
    """One Azure VM size in the selection catalog: a SKU name plus the
    cpus and memory (GiB) it provides."""

    cpus: int
    memory_gib: int
    name: str


# Built-in Azure VM size catalog: the B-series (burstable) general-purpose
# ladder. `vm create` picks the smallest entry whose cpus AND memory both
# satisfy the vm-template's request, so the operator specifies compute and
# memory like every other platform instead of an Azure-specific SKU. The
# ratios are fixed by Azure (a template asking for an off-ratio shape, e.g.
# 4 vCPU / 8 GiB, rounds UP to the nearest fitting SKU and warns). Ordered
# small to large for readability; selection takes the minimum by
# (cpus, memory), so an operator override in platform_config.vm_sizes
# need not be pre-sorted.
_DEFAULT_VM_SIZES: tuple[_VMSize, ...] = (
    _VMSize(1, 2, "Standard_B1ms"),
    _VMSize(2, 4, "Standard_B2s"),
    _VMSize(2, 8, "Standard_B2ms"),
    _VMSize(4, 16, "Standard_B4ms"),
    _VMSize(8, 32, "Standard_B8ms"),
    _VMSize(12, 48, "Standard_B12ms"),
    _VMSize(16, 64, "Standard_B16ms"),
    _VMSize(20, 80, "Standard_B20ms"),
)


def _parse_size_catalog(config: Mapping[str, object], owner: str) -> tuple[_VMSize, ...]:
    """The site's VM-size catalog: the operator override
    (``platform_config.vm_sizes``) when present, else the built-in
    B-series ladder. Raises ``ConfigError`` on a malformed override so
    the shape is validated at registry build time (the finalize
    ``validate`` pass), not first ``vm create``.
    """
    raw = config.get("vm_sizes")
    if raw is None:
        return _DEFAULT_VM_SIZES
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ConfigError(f"{owner}.vm_sizes must be a non-empty list of {{cpus, memory, size}} tables")
    catalog: list[_VMSize] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"{owner}.vm_sizes[{i}] must be a {{cpus, memory, size}} table")
        unknown = sorted(set(entry) - {"cpus", "memory", "size"})
        if unknown:
            raise ConfigError(f"{owner}.vm_sizes[{i}]: unknown field(s): {', '.join(unknown)}")
        cpus, memory, size = entry.get("cpus"), entry.get("memory"), entry.get("size")
        # bool is an int subclass; reject it explicitly so `cpus = true`
        # does not sneak through as 1.
        if not isinstance(cpus, int) or isinstance(cpus, bool) or cpus <= 0:
            raise ConfigError(f"{owner}.vm_sizes[{i}].cpus must be a positive integer")
        if not isinstance(memory, int) or isinstance(memory, bool) or memory <= 0:
            raise ConfigError(f"{owner}.vm_sizes[{i}].memory must be a positive integer")
        if not isinstance(size, str) or not size:
            raise ConfigError(f"{owner}.vm_sizes[{i}].size must be a non-empty string")
        catalog.append(_VMSize(cpus, memory, size))
    return tuple(catalog)


def _select_vm_size(catalog: tuple[_VMSize, ...], *, cpus: int, memory_gib: int) -> _VMSize:
    """The catalog entry that both satisfies the request and is smallest
    by (cpus, memory), chosen with ``min`` so the result is independent
    of catalog order. Raises ``ConfigError`` when the request exceeds
    every entry (the template is bigger than anything on offer)."""
    fits = [s for s in catalog if s.cpus >= cpus and s.memory_gib >= memory_gib]
    if not fits:
        largest = max(catalog, key=lambda s: (s.cpus, s.memory_gib))
        raise ConfigError(
            f"no Azure VM size satisfies the requested {cpus} vCPU / "
            f"{memory_gib} GiB (largest available is {largest.name}: "
            f"{largest.cpus} vCPU / {largest.memory_gib} GiB)",
            hint="shrink the vm-template's cpus/memory, or add a larger "
            "entry to the site's vm_sizes catalog (vm-site platform config)",
        )
    return min(fits, key=lambda s: (s.cpus, s.memory_gib))


# The marketplace image every VM is provisioned from. These feed the single
# ImageReference in create(); keep them here so the OS-disk floor below stays
# visibly coupled to the exact image it describes.
IMAGE_PUBLISHER = "Debian"
IMAGE_OFFER = "debian-12"
IMAGE_SKU = "12-gen2"
IMAGE_VERSION = "latest"

# Minimum OS-disk size (GiB) baked into the image above. Azure rejects a VM
# whose OS disk is smaller than the image's own disk, so a vm-template disk
# below this is clamped up to it (see create()). This is not exposed on the
# marketplace VirtualMachineImage model, so it is pinned by hand: keep it in
# sync if IMAGE_SKU/IMAGE_VERSION change (a test asserts the pairing).
IMAGE_OS_DISK_FLOOR_GIB = 30


class AzureVMPlatform(VMPlatform):
    """Runs VMs on the Azure Virtual Machines service via the Azure
    Python SDK. Named ``azure-vm``, not ``azure``: the capability is
    one specific Azure service, and other Azure services could plausibly
    back platforms of their own someday."""

    name: ClassVar[str] = "azure-vm"
    description: ClassVar[str] = "Azure Virtual Machines (subscription + resource group)"

    # Warned by the transports factory when every reachability probe
    # fails: the ephemeral NSG allow is scoped to the DETECTED egress
    # IP, so an operator whose SSH traffic leaves through a different
    # address (VPN split tunnel, proxy, CGNAT) gets a hole that does
    # not match and every probe times out.
    probe_failure_hint: ClassVar[str | None] = (
        "The transient Azure SSH allow is scoped to your detected "
        "public IP; if your SSH traffic egresses elsewhere (VPN "
        "split tunnel, proxy, CGNAT), add your address(es) to "
        "operator.ssh_allow_cidrs in your agentworks config."
    )

    def __init__(self, owner_name: str, config: Mapping[str, object]) -> None:
        super().__init__(owner_name, config)
        # Quiet azure-identity's own credential-failure WARNING (unless
        # debugging) so a failed probe or op-time token request surfaces only
        # as our typed AzureError, not as raw SDK chatter ahead of it. Done at
        # construction (the first point an azure platform is in use) so it
        # covers both the credential probe and later op-time token requests;
        # idempotent, so building more than one instance is harmless.
        _quiet_azure_identity_logging()
        # Azure credential and SDK clients, built on FIRST need by the
        # accessors below and reused for the instance's remaining ops.
        # The credential is subscription-independent, so it caches once
        # per instance (one live get_token probe per command, given the
        # vms/nodes.py site memo shares one instance per site). The
        # clients are keyed by subscription_id: the site's config names
        # one subscription, but power ops parse each VM's stored resource
        # ID, and rows created under an older subscription must keep
        # operating regardless of what the config says today, so one
        # instance can legitimately see heterogeneous subscriptions in a
        # multi-VM batch.
        self._credential_cached: object | None = None
        self._compute_cached: dict[str, ComputeManagementClient] = {}
        self._network_cached: dict[str, NetworkManagementClient] = {}
        self._resource_cached: dict[str, ResourceManagementClient] = {}

    # No preflight override: azure has no config secrets (the base's
    # prediction pass is a no-op) and no unauthenticated readiness
    # check worth making. A credential probe is deliberately NOT one:
    # verifying credentials before the resolve/credential stage forks
    # behavior on where they happen to come from (a non-interactive
    # chain passes, the browser-login fallback can't be probed without
    # BEING the interaction). Credential and reachability failures
    # surface at the op with typed errors (``network.wrap_azure_error``),
    # which is the contract: preflight is capped at what it can check
    # without resolved credentials.

    @classmethod
    def dependencies(cls, owner: str, config: Mapping[str, object]) -> tuple[ConfigReference, ...]:
        """``azure-vm`` implies no resource reference, so its edge set is
        empty (total, non-throwing per the ``dependencies`` contract)."""
        return ()

    @classmethod
    def validate(cls, owner: str, config: Mapping[str, object]) -> None:
        for key in _AZURE_REQUIRED_KEYS:
            value = config.get(key)
            if not isinstance(value, str) or not value:
                raise ConfigError(f"{owner}.{key} is required for the azure-vm platform and must be a non-empty string")
        unknown = sorted(set(config) - set(_AZURE_REQUIRED_KEYS) - set(_AZURE_OPTIONAL_KEYS))
        if unknown:
            raise ConfigError(f"{owner}: unknown azure-vm platform field(s): {', '.join(unknown)}")
        # Validate the optional size-catalog override's shape here so a
        # malformed vm_sizes fails at config load, not first vm create.
        _parse_size_catalog(config, owner)

    @classmethod
    def legacy_platform_metadata(cls, row: Mapping[str, Any], legacy: Mapping[str, Any]) -> dict[str, str]:
        if row["azure_resource_id"]:
            return {"resource_id": str(row["azure_resource_id"])}
        return {}

    def _get_credential(self) -> object:
        """The Azure credential, built on first need (one live probe, one
        browser-fallback decision) and reused for the instance's remaining
        ops. See :func:`_build_credential` for the decision itself."""
        cred = self._credential_cached
        if cred is None:
            cred = _build_credential()
            self._credential_cached = cred
        return cred

    def _compute_client(self, az: _HasSubscriptionId) -> ComputeManagementClient:
        """The compute client for ``az``'s subscription, built on first
        need from the cached credential and reused for the instance's
        remaining ops against that subscription (see ``__init__`` for why
        the cache keys by subscription)."""
        from azure.mgmt.compute import ComputeManagementClient

        compute = self._compute_cached.get(az.subscription_id)
        if compute is None:
            # _get_credential() returns a TokenCredential-compatible object;
            # the cast avoids a hard azure.core import at module load time.
            compute = ComputeManagementClient(self._get_credential(), az.subscription_id)  # type: ignore[arg-type]
            self._compute_cached[az.subscription_id] = compute
        return compute

    def _network_client(self, az: _HasSubscriptionId) -> NetworkManagementClient:
        """The network client for ``az``'s subscription, built on first
        need from the cached credential and reused for the instance's
        remaining ops against that subscription (see ``__init__`` for why
        the cache keys by subscription)."""
        from azure.mgmt.network import NetworkManagementClient

        network = self._network_cached.get(az.subscription_id)
        if network is None:
            # Same as _compute_client: credential is TokenCredential-compatible at runtime.
            network = NetworkManagementClient(self._get_credential(), az.subscription_id)  # type: ignore[arg-type]
            self._network_cached[az.subscription_id] = network
        return network

    def _resource_client(self, az: _HasSubscriptionId) -> ResourceManagementClient:
        """The resource-management client for ``az``'s subscription, built
        on first need from the cached credential and reused for the
        instance's remaining ops against that subscription (see
        ``__init__`` for why the cache keys by subscription). Used by
        ``runup`` for the read-only resource-group existence check. In
        azure-mgmt-resource the client lives under the ``.resources``
        subpackage (the top-level ``azure.mgmt.resource`` namespace does
        not re-export it), and the import stays function-local like the
        other SDK imports so azure modules never load at CLI startup."""
        from azure.mgmt.resource.resources import ResourceManagementClient

        resource = self._resource_cached.get(az.subscription_id)
        if resource is None:
            # Same as _compute_client: credential is TokenCredential-compatible at runtime.
            resource = ResourceManagementClient(self._get_credential(), az.subscription_id)  # type: ignore[arg-type]
            self._resource_cached[az.subscription_id] = resource
        return resource

    def runup(self, ctx: RunContext) -> None:
        """Provisioning-phase runup: an authenticated, read-only check that
        the site's configured resource group exists before ``create``
        provisions into it. A missing group is a definitive rejection
        (fatal, before the DB row or any Azure resource exists), so
        ``vm create`` aborts here with a clear message instead of failing
        partway through creating a public IP / NSG / VNet / NIC in a group
        that was never there. Unconditional (there is nothing to gate it
        on): every azure-vm site targets a resource group.

        Post-resolve and authenticated: the credential is whatever
        :meth:`_get_credential` resolves (ambient ``DefaultAzureCredential``
        today; issue #199 will make it a framework secret and plug it in
        here, which is why runup declares no secret of its own). The
        existence probe (``resource_groups.check_existence``) is read-only
        and mutates nothing. A credential or reachability failure is NOT a
        "group missing" verdict: those surface through
        :func:`agentworks.plugins.azure.network.wrap_azure_error` exactly
        as the ops report them, so a bad or absent credential never
        masquerades as an absent resource group.

        Reachability failures are fatal here, which diverges from the
        proxmox runup on purpose. Proxmox warns and continues unverified on
        a transient outage because its token check is incidental (the op
        uses the token directly regardless). Azure's ``create`` makes many
        Resource Manager calls, so an unreachable RM at runup means the
        whole create cannot proceed anyway; aborting cleanly here, with
        nothing realized, beats warning past it into a cryptic mid-provision
        failure.
        """
        from types import SimpleNamespace

        az = SimpleNamespace(
            subscription_id=str(self.platform_config["subscription_id"]),
            resource_group=str(self.platform_config["resource_group"]),
            region=str(self.platform_config["region"]),
        )
        output.info(f"Performing runup test for vm-site/{self.site_name}...")
        try:
            exists = self._resource_client(az).resource_groups.check_existence(az.resource_group)
        except Exception as exc:
            raise wrap_azure_error(exc) from exc
        if not exists:
            raise NotFoundError(
                f"Azure resource group '{az.resource_group}' does not exist in "
                f"subscription '{az.subscription_id}' (vm-site '{self.site_name}')",
                entity_kind="resource-group",
                entity_name=az.resource_group,
                hint=(
                    f"create it with 'az group create -n {az.resource_group} "
                    f"-l {az.region}', or point vm-site '{self.site_name}' at an "
                    f"existing resource group"
                ),
            )

    def create(self, request: ProvisionRequest, ctx: RunContext) -> ProvisionResult:
        from types import SimpleNamespace

        # The site's platform_config, shaped like the old AzureConfig so
        # the SDK-call body below stays byte-identical.
        az = SimpleNamespace(
            subscription_id=str(self.platform_config["subscription_id"]),
            resource_group=str(self.platform_config["resource_group"]),
            region=str(self.platform_config["region"]),
        )

        # Select the smallest Azure SKU that satisfies the template's
        # compute/memory request (the standard cross-platform model);
        # the catalog is the built-in B-series ladder or the site's
        # platform_config.vm_sizes override.
        catalog = _parse_size_catalog(self.platform_config, f"vm-site/{self.site_name}")
        req_cpus = request.cpus if request.cpus is not None else 4
        req_memory = request.memory_gib if request.memory_gib is not None else 8
        selected = _select_vm_size(catalog, cpus=req_cpus, memory_gib=req_memory)
        azure_vm_size = selected.name
        # The provisioning line always names the selected SKU and its spec. A
        # round-up (an off-ratio request that no SKU matches exactly) also
        # warns, naming the requested shape as the reason.
        size_summary = f"{selected.name} ({selected.cpus} vCPU / {selected.memory_gib} GiB)"
        if selected.cpus > req_cpus or selected.memory_gib > req_memory:
            output.warn(
                f"Rounded up to {selected.name} "
                f"({selected.cpus} vCPU / {selected.memory_gib} GiB) "
                f"for requested {req_cpus} vCPU / {req_memory} GiB."
            )
        requested_disk = request.disk_gib if request.disk_gib is not None else 50
        # Clamp the OS disk up to the image's minimum, mirroring the cpu/memory
        # round-up above: Azure rejects a VM whose OS disk is smaller than the
        # disk baked into the image, so a below-floor template disk grows to it.
        disk = max(requested_disk, IMAGE_OS_DISK_FLOOR_GIB)
        if disk != requested_disk:
            output.warn(f"Rounded up to {disk} GiB OS disk (image minimum) for requested {requested_disk} GiB.")
        swap = request.swap_gib if request.swap_gib is not None else 0
        admin_username = request.admin_username
        tailscale_auth_key = request.tailscale_auth_key
        ssh_pub_key = request.ssh_public_key

        # Platform-owned naming with the slug as the
        # namespacing token; azure resource names are the primary
        # identifier, so a collision is an error.
        vm_name = f"{request.system_slug}-{request.vm_name}" if request.system_slug else request.vm_name

        # Resolve the SSH allow scope BEFORE any resource exists: the
        # bootstrap allow is poked at NSG creation, and a detection
        # failure with no operator.ssh_allow_cidrs escape hatch is a
        # typed error while there is still nothing to roll back.
        ssh_allow_prefixes = operator_ssh_prefixes(config_allow_cidrs(ctx.config))

        output.detail("Connecting to Azure...")
        compute = self._compute_client(az)
        network = self._network_client(az)

        if self._vm_exists(compute, az.resource_group, vm_name):
            raise StateError(
                f"an Azure VM named '{vm_name}' already exists in resource group '{az.resource_group}'",
                entity_kind="vm",
                entity_name=request.vm_name,
                hint="delete it first or pick a different VM name",
            )

        # The primary provisioning step (promoted to info); the concrete
        # resource-creation sub-steps below render as detail one notch
        # deeper. This runs inside vm create's "Provisioning" section, so
        # info sits at the section body level and detail one level under.
        output.info(f"Provisioning Azure VM '{vm_name}' in {az.region}: size {size_summary}...")
        if swap > 0:
            output.detail(f"Swap: {swap} GiB")

        # Generate the same bootstrap script used by Lima, wrapped in
        # cloud-init write_files + runcmd for delivery via Azure custom_data.
        if tailscale_auth_key:
            bootstrap = generate_bootstrap_script(
                admin_username=admin_username,
                ssh_public_key=ssh_pub_key,
                provisioning_packages=PROVISIONING_PACKAGES,
                tailscale_auth_key=tailscale_auth_key,
                hostname=request.hostname,
                swap=swap,
            )
            cloud_init = generate_cloud_init(bootstrap)
        else:
            # No Tailscale key: minimal cloud-init, bootstrap deferred to Phase A
            cloud_init = "#cloud-config\npackage_update: true\npackages:\n  - openssh-server\n"
        cloud_init_b64 = base64.b64encode(cloud_init.encode()).decode()

        # SDK model classes are imported function-locally like the SDK
        # clients so azure modules never load at CLI startup. They are what
        # make the ARM request bodies serialize correctly: the models map
        # flattened snake_case attributes (e.g. public_ip_allocation_method)
        # into the nested ``properties`` envelope ARM expects, which raw
        # dicts do not.
        from azure.mgmt.compute.models import (
            HardwareProfile,
            ImageReference,
            LinuxConfiguration,
            ManagedDiskParameters,
            NetworkInterfaceReference,
            NetworkProfile,
            OSDisk,
            OSProfile,
            SshConfiguration,
            SshPublicKey,
            StorageProfile,
            VirtualMachine,
        )
        from azure.mgmt.network.models import (
            AddressSpace,
            NetworkInterface,
            NetworkInterfaceIPConfiguration,
            NetworkSecurityGroup,
            PublicIPAddress,
            PublicIPAddressSku,
            Subnet,
            VirtualNetwork,
        )

        # Interrupt rollback: spans BOTH the resource creation below and the
        # inline bootstrap wait after it (the inner Exception arm keeps the
        # plain-failure rollback as it was). Without it a Ctrl-C escapes
        # create() uncleaned and the caller's row unwind orphans the whole
        # resource set (#338).
        try:
            try:
                # Create public IP
                output.detail("Creating public IP...")
                ip_poller = network.public_ip_addresses.begin_create_or_update(
                    az.resource_group,
                    f"{vm_name}-ip",
                    PublicIPAddress(
                        location=az.region,
                        sku=PublicIPAddressSku(name="Standard"),
                        public_ip_allocation_method="Static",
                        tags={"owner": "agentworks"},
                    ),
                )
                ip_result = ip_poller.result()
                public_ip = ip_result.ip_address or ""

                # Create the NSG: the permanent deny-all-inbound baseline plus
                # the ephemeral bootstrap allow scoped to the operator's egress
                # prefixes (cloud-init needs inbound SSH from the operator;
                # post_tailscale_ready deletes the allow once Tailscale is up).
                output.detail("Creating network security group...")
                nsg_poller = network.network_security_groups.begin_create_or_update(
                    az.resource_group,
                    f"{vm_name}-nsg",
                    NetworkSecurityGroup(
                        location=az.region,
                        security_rules=initial_security_rules(ssh_allow_prefixes),
                        tags={"owner": "agentworks"},
                    ),
                )
                nsg_result = nsg_poller.result()

                # Create NIC
                output.detail("Creating network interface...")

                # Need a subnet: use default VNet or create one
                vnet_name = f"{vm_name}{VNET_NAME_SUFFIX}"
                subnet_name = "default"
                vnet_poller = network.virtual_networks.begin_create_or_update(
                    az.resource_group,
                    vnet_name,
                    VirtualNetwork(
                        location=az.region,
                        address_space=AddressSpace(address_prefixes=["10.0.0.0/16"]),
                        subnets=[
                            Subnet(
                                name=subnet_name,
                                address_prefix="10.0.0.0/24",
                            )
                        ],
                        tags={"owner": "agentworks"},
                    ),
                )
                vnet_result = vnet_poller.result()
                # The vnet was just created with exactly the one subnet above, so
                # the returned resource always carries it back.
                assert vnet_result.subnets is not None
                subnet_id = vnet_result.subnets[0].id

                nic_poller = network.network_interfaces.begin_create_or_update(
                    az.resource_group,
                    f"{vm_name}-nic",
                    NetworkInterface(
                        location=az.region,
                        ip_configurations=[
                            NetworkInterfaceIPConfiguration(
                                name="default",
                                subnet=Subnet(id=subnet_id),
                                public_ip_address=PublicIPAddress(id=ip_result.id),
                            )
                        ],
                        network_security_group=NetworkSecurityGroup(id=nsg_result.id),
                        tags={"owner": "agentworks"},
                    ),
                )
                nic_result = nic_poller.result()

                # Create VM
                output.detail("Creating VM...")
                vm_poller = compute.virtual_machines.begin_create_or_update(
                    az.resource_group,
                    vm_name,
                    VirtualMachine(
                        location=az.region,
                        hardware_profile=HardwareProfile(vm_size=azure_vm_size),
                        storage_profile=StorageProfile(
                            image_reference=ImageReference(
                                publisher=IMAGE_PUBLISHER,
                                offer=IMAGE_OFFER,
                                sku=IMAGE_SKU,
                                version=IMAGE_VERSION,
                            ),
                            os_disk=OSDisk(
                                create_option="FromImage",
                                disk_size_gb=disk,
                                # Azure deletes the disk with the VM. This does
                                # not replace the tag-based sweep in
                                # cleanup_vm_resources: that covers the rollback
                                # window where create fails before a VM exists
                                # to carry the disk away (#334), and it is what
                                # deletes the disks of VMs created before this
                                # option was set (their disks default to
                                # Detach).
                                delete_option="Delete",
                                managed_disk=ManagedDiskParameters(storage_account_type="StandardSSD_LRS"),
                            ),
                        ),
                        os_profile=OSProfile(
                            computer_name=vm_name,
                            admin_username=admin_username,
                            custom_data=cloud_init_b64,
                            linux_configuration=LinuxConfiguration(
                                disable_password_authentication=True,
                                ssh=SshConfiguration(
                                    public_keys=[
                                        SshPublicKey(
                                            path=f"/home/{admin_username}/.ssh/authorized_keys",
                                            key_data=ssh_pub_key,
                                        )
                                    ]
                                ),
                            ),
                        ),
                        network_profile=NetworkProfile(
                            network_interfaces=[NetworkInterfaceReference(id=nic_result.id)],
                        ),
                        tags={"owner": "agentworks"},
                    ),
                )
                vm_result = vm_poller.result()
                resource_id = vm_result.id or ""

            except Exception as exc:
                output.detail("Cleaning up resources...")
                cleanup_vm_resources(compute, network, az.resource_group, vm_name)
                raise wrap_azure_error(exc) from exc

            output.detail(f"Azure VM '{vm_name}' provisioned (IP: {public_ip})")

            import sys

            prov_transport = SSHTransport(
                host=public_ip,
                user=admin_username,
                identity_file=request.ssh_private_key,
                force_tty=sys.platform == "win32",
            )

            # If bootstrap was embedded in cloud-init, wait for it to finish
            # and extract the Tailscale IP.
            tailscale_ip = None
            bootstrap_complete = False
            if tailscale_auth_key:
                tailscale_ip = self._wait_for_bootstrap(prov_transport, vm_name)
                if tailscale_ip:
                    bootstrap_complete = True
        except KeyboardInterrupt:
            rollback_create_on_interrupt(compute, network, az.resource_group, vm_name)
            raise

        metadata = {"resource_id": resource_id} if resource_id else {}
        return ProvisionResult(
            native_transport=prov_transport,
            platform_metadata=metadata,
            bootstrap_complete=bootstrap_complete,
            tailscale_ip=tailscale_ip,
        )

    @staticmethod
    def _vm_exists(compute: ComputeManagementClient, resource_group: str, vm_name: str) -> bool:
        """Pre-flight: does a VM with this name exist in the group?"""
        try:
            compute.virtual_machines.get(resource_group, vm_name)
        except Exception:
            return False
        return True

    def _wait_for_bootstrap(self, target: Transport, vm_name: str) -> str | None:
        """Wait for cloud-init to finish and return the Tailscale IP.

        SSH may not be immediately available after VM creation, so we retry.
        Returns None if we cannot get the IP (Phase A will handle it).
        """
        import time

        output.detail("Waiting for cloud-init bootstrap to complete (this may take several minutes)...")

        for attempt in range(30):
            try:
                target.run("echo ok", check=True, timeout=10)
                break
            except SSHError:
                if attempt == 29:
                    output.warn("SSH not available, deferring bootstrap to Phase A")
                    return None
                time.sleep(10)

        try:
            target.run("cloud-init status --wait", check=True, timeout=600)
        except SSHError as e:
            output.warn(f"cloud-init wait failed: {e}")
            output.warn("Deferring bootstrap to Phase A")
            return None

        try:
            result = target.run("sudo tailscale ip -4", check=True, timeout=15)
            tailscale_ip = result.stdout.strip()
            output.detail(f"Tailscale IP: {tailscale_ip}")
            return tailscale_ip
        except SSHError as e:
            output.warn(f"could not retrieve Tailscale IP: {e}")
            return None

    def start(self, vm: VMRow, ctx: RunContext) -> None:
        # Idempotent by construction (the ABC flags start): the Azure
        # begin_start operation no-ops on an already-running VM.
        output.info(f"Starting Azure VM '{vm.name}'...")
        rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
        try:
            compute = self._compute_client(az_cfg)
            compute.virtual_machines.begin_start(rg, name).result()
        except Exception as exc:
            raise wrap_azure_error(exc) from exc
        output.info(f"Azure VM '{vm.name}' started")

    def stop(self, vm: VMRow, ctx: RunContext) -> None:
        # Idempotent by construction (the ABC flags stop): the Azure
        # begin_deallocate operation no-ops on a deallocated VM.
        output.info(f"Deallocating Azure VM '{vm.name}'...")
        rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
        try:
            compute = self._compute_client(az_cfg)
            compute.virtual_machines.begin_deallocate(rg, name).result()
        except Exception as exc:
            raise wrap_azure_error(exc) from exc
        output.info(f"Azure VM '{vm.name}' deallocated")

    def delete(self, vm: VMRow, ctx: RunContext) -> None:
        output.info(f"Deleting Azure VM '{vm.name}'...")
        if not vm.platform_metadata.get("resource_id"):
            output.warn("no Azure resource ID, skipping Azure cleanup")
            return

        rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
        delete_vm_and_resources(self._compute_client(az_cfg), self._network_client(az_cfg), rg, name)
        output.info(f"Azure VM '{vm.name}' deleted")

    # The route-state helpers below are thin delegates into the network
    # module (which owns the mechanics and full docstrings). They stay
    # as methods because the platform object is the seam callers and
    # tests compose against: transient_route and the hooks call them on
    # self, and the shell-provisioner tests spy on them as attributes.

    def _ensure_public_ip(self, vm: VMRow) -> None:
        """Heal the VM's public IP if its NIC has none. See
        :func:`agentworks.plugins.azure.network.ensure_public_ip`."""
        rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
        ensure_public_ip(self._network_client(az_cfg), self._compute_client(az_cfg), rg, name)

    def _converge_nsg(self, vm: VMRow) -> None:
        """Converge a pre-existing VM's NSG onto the baseline-deny model
        (re-pin the deny, drop the legacy standing SSH allow). See
        :func:`agentworks.plugins.azure.network.converge_nsg`."""
        rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
        converge_nsg(self._network_client(az_cfg), rg, name)

    def _poke_ssh_allow(self, vm: VMRow, extra_cidrs: list[str] | None = None) -> tuple[str, list[str]]:
        """Create this operation's own ephemeral SSH allow scoped to the
        operator's current egress prefixes plus ``extra_cidrs``,
        returning the created rule's name (the exit path removes exactly
        that rule) and the prefixes used (named if the removal fails).
        See :func:`agentworks.plugins.azure.network.poke_ssh_allow`."""
        rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
        prefixes = operator_ssh_prefixes(extra_cidrs or ())
        rule_name = poke_ssh_allow(self._network_client(az_cfg), rg, name, prefixes)
        return rule_name, prefixes

    def _remove_ssh_allow(self, vm: VMRow, rule_name: str, prefixes: list[str] | None = None) -> None:
        """Delete one ephemeral SSH allow rule by name, restoring zero
        inbound exposure through it. See
        :func:`agentworks.plugins.azure.network.remove_ssh_allow`."""
        rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
        remove_ssh_allow(self._network_client(az_cfg), rg, name, rule_name, prefixes)

    def display_backend_name(self, vm: VMRow) -> str:
        resource_id = vm.platform_metadata.get("resource_id")
        if not resource_id:
            return vm.name
        _rg, name, _cfg = _parse_resource_id(resource_id)
        return name

    def native_transport(
        self,
        vm: VMRow,
        *,
        config: Config | None = None,
    ) -> Transport | None:
        rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
        try:
            compute = self._compute_client(az_cfg)
            vm_info = compute.virtual_machines.get(
                rg,
                name,
                expand="instanceView",
            )
        except Exception as exc:
            raise wrap_azure_error(exc) from exc

        # Walk NICs to find the public IP, read live off the NIC (never
        # persisted). The IP is permanent for the VM's lifetime and
        # transient_route heals absence on enter, so an empty string here
        # is a genuinely defensive corner; it propagates to
        # ``SSHTransport(host="")`` which the transports.native_transport
        # factory catches with a typed StateError.
        public_ip = get_vm_public_ip(self._network_client(az_cfg), vm_info)
        import sys

        # Include identity file if config is available (needed for SSH auth
        # via public IP, e.g., during Tailscale logout on delete).
        identity_file = None
        if config is not None:
            identity_file = getattr(getattr(config, "operator", None), "ssh_private_key", None)

        return SSHTransport(
            host=public_ip,
            user=vm.admin_username,
            identity_file=identity_file,
            force_tty=sys.platform == "win32",
        )

    def post_tailscale_ready(self, vm: VMRow) -> None:
        """Close provisioning access now that Tailscale is up.

        The VM comes out of :meth:`create` reachable only through the
        ephemeral bootstrap allow rule (SSH scoped to the operator's
        egress prefixes; the deny-all-inbound baseline is permanent).
        This hook fires at the async Tailscale-ready point inside
        ``bootstrap_vm`` (Phase A) and deletes that allow by its fixed
        name (no in-process state needed), leaving zero inbound
        exposure; the public IP stays for the VM's whole lifetime.
        """
        self._remove_ssh_allow(vm, BOOTSTRAP_ALLOW_RULE_NAME)

    def secure_failed_vm(self, vm: VMRow) -> None:
        """Fail closed: close provisioning access on a kept, not-completed VM.

        Mirrors :meth:`post_tailscale_ready`, which only fires on
        success: a VM whose bootstrap or Tailscale verification died
        (marked FAILED) or was interrupted mid-bootstrap (row status
        untouched by the abort) is kept for debugging, and this hook
        deletes the fixed-name bootstrap allow so it defaults to zero
        inbound exposure. Debugging and recovery stay possible: ``vm
        shell --platform`` and ``vm delete`` poke a fresh transient
        allow via :meth:`transient_route`, and the Azure serial console
        is not NSG-gated.
        """
        self._remove_ssh_allow(vm, BOOTSTRAP_ALLOW_RULE_NAME)

    @contextlib.contextmanager
    def transient_route(self, vm: VMRow, *, config: Config | None = None) -> Iterator[None]:
        """Open a scoped SSH route to the VM for the context's duration.

        Enter heals the public IP (:meth:`_ensure_public_ip`; legacy
        detach-scheme VMs converge here), converges the NSG
        (:meth:`_converge_nsg`; deny re-pin before any allow
        allocation), then pokes this operation's own nonce-named allow
        scoped to the operator's egress prefixes (widened by
        ``config.operator.ssh_allow_cidrs``). The poke sits INSIDE the
        try and the finally removes exactly the poked rule by name on
        every unwind path, so concurrent native ops never cross-remove
        each other's allows; a poke that fails without returning a name
        has already cleaned up its own attempt, and a removal failure
        warns naming the rule and prefixes (both per the network
        module's ``poke_ssh_allow`` / ``remove_ssh_allow``, which carry
        the full contracts). The
        :func:`agentworks.transports.native_transport` factory wraps
        this around the per-platform :meth:`native_transport` call so
        the lifecycle stays polymorphic.
        """
        self._ensure_public_ip(vm)
        self._converge_nsg(vm)
        rule_name: str | None = None
        prefixes: list[str] | None = None
        try:
            rule_name, prefixes = self._poke_ssh_allow(vm, config_allow_cidrs(config))
            yield
        finally:
            if rule_name is not None:
                self._remove_ssh_allow(vm, rule_name, prefixes)

    def status(self, vm: VMRow, ctx: RunContext) -> VMStatus:
        if not vm.platform_metadata.get("resource_id"):
            return VMStatus.UNKNOWN
        rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
        try:
            compute = self._compute_client(az_cfg)
            instance = compute.virtual_machines.instance_view(rg, name)
        except Exception:
            return VMStatus.UNKNOWN

        for s in instance.statuses or []:
            code = s.code or ""
            if code == "PowerState/running":
                return VMStatus.RUNNING
            if code == "PowerState/stopped":
                return VMStatus.STOPPED
            if code == "PowerState/deallocated":
                return VMStatus.DEALLOCATED
        return VMStatus.UNKNOWN


def _resource_id(vm: VMRow) -> str:
    """The VM's Azure resource ID from platform metadata, or a typed error."""
    resource_id = vm.platform_metadata.get("resource_id")
    if not resource_id:
        raise StateError(
            f"VM '{vm.name}' has no azure resource_id in its platform metadata; the DB row is incomplete",
            entity_kind="vm",
            entity_name=vm.name,
        )
    return str(resource_id)


class _MinimalAzureConfig:
    """Minimal config for SDK clients, parsed from a resource ID."""

    def __init__(self, subscription_id: str) -> None:
        self.subscription_id = subscription_id


def _parse_resource_id(resource_id: str) -> tuple[str, str, _MinimalAzureConfig]:
    """Extract resource group, VM name, and a config from an Azure resource ID."""
    parts = resource_id.split("/")
    sub_idx = next(i for i, p in enumerate(parts) if p.lower() == "subscriptions")
    rg_idx = next(i for i, p in enumerate(parts) if p.lower() == "resourcegroups")
    name_idx = next(i for i, p in enumerate(parts) if p.lower() == "virtualmachines")
    cfg = _MinimalAzureConfig(parts[sub_idx + 1])
    return parts[rg_idx + 1], parts[name_idx + 1], cfg
