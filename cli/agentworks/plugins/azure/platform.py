"""The Azure VM platform: creates and manages VMs via the Azure SDK."""

from __future__ import annotations

import base64
import contextlib
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple, Protocol

from agentworks import output
from agentworks.capabilities.vm_platform.base import ProvisionRequest, ProvisionResult, VMPlatform
from agentworks.capabilities.vm_platform.bootstrap_script import generate_bootstrap_script
from agentworks.capabilities.vm_platform.cloud_init import PROVISIONING_PACKAGES, generate_cloud_init
from agentworks.db import VMStatus
from agentworks.errors import ConfigError, NotFoundError, ProvisioningError, StateError
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


# Suffix appended to the VM hostname to name its virtual-network subresource:
# {slug}-{vm}-vnet. This is the tightest sink bounding MAX_VM_NAME_LENGTH (the
# vnet name limit is 64), so the length derivation in config/validation.py
# mirrors this literal and a pinned test asserts the worst-case name is exactly
# 64 at the cap. Keep the two in sync (the test fails if this suffix grows).
VNET_NAME_SUFFIX = "-vnet"


class _HasSubscriptionId(Protocol):
    """Structural protocol for anything with subscription_id (AzureConfig or _MinimalAzureConfig)."""

    @property
    def subscription_id(self) -> str: ...


class AzureError(ProvisioningError):
    """An Azure API operation failed.

    Attributes:
        summary: A concise, user-facing error message.
        detail: The full error details (for logs).

    The optional entity / hint keywords are the base ``AgentworksError``
    ones, forwarded so a failure that KNOWS which resource it is about
    can say so (the service-principal credential names its site and its
    secret). ``_wrap_azure_error``, which converts an arbitrary SDK
    exception, has no such knowledge and passes none.
    """

    def __init__(
        self,
        summary: str,
        detail: str,
        *,
        entity_kind: str | None = None,
        entity_name: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(summary, entity_kind=entity_kind, entity_name=entity_name, hint=hint)
        self.summary = summary
        self.detail = detail


def _wrap_azure_error(exc: Exception) -> AzureError:
    """Convert an Azure SDK exception into an AzureError."""
    from azure.core.exceptions import HttpResponseError

    if isinstance(exc, HttpResponseError):
        # Walk inner errors to find the most specific message
        code = exc.error.code if exc.error else None
        message = exc.error.message if exc.error else str(exc)

        if exc.error and exc.error.details:
            inner = exc.error.details[0]
            code = inner.code or code
            message = inner.message or message

        summary = f"{code}: {_trim_message(str(message))}" if code else _trim_message(str(message))
        return AzureError(summary, detail=str(exc))

    return AzureError(str(exc), detail=str(exc))


def _trim_message(message: str) -> str:
    """Trim an Azure error message to the first meaningful sentence."""
    # Cut at first URL or "Learn more" / "Submit a request" noise
    for marker in [". Setup Alerts", ". Learn more", ". Submit a request", " https://"]:
        idx = message.find(marker)
        if idx != -1:
            return message[: idx + 1] if marker.startswith(".") else message[:idx]
    return message


# The token scope every credential below is probed against: Azure
# Resource Manager, which is the only API surface this platform talks to.
_ARM_SCOPE = "https://management.azure.com/.default"


def _build_ambient_credential() -> object:
    """Build the AMBIENT Azure credential (the path taken when the site
    declares no ``service_principal``), deciding the interactive-browser
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
        cred.get_token(_ARM_SCOPE)
        return cred
    except ClientAuthenticationError:
        output.info("No Azure credentials found, opening browser for login...")
        return InteractiveBrowserCredential()


def _build_service_principal_credential(sp: _ServicePrincipal, client_secret: str, site_name: str) -> object:
    """Build the EXPLICIT service-principal credential from the site's
    configured tenant / client and the resolved client secret, probed
    once against ARM.

    The probe mirrors the ambient path's, but for a different reason:
    there is no fallback to decide here (an operator who configured a
    service principal means that credential and no other, so falling
    through to ambient or to a browser prompt would silently authenticate
    as the wrong identity). It exists purely to turn a bad credential
    into a clear, site-and-secret-named error at the point of
    construction, rather than a raw SDK exception surfacing from
    whichever network call happened to be first.

    That failure is FATAL and is deliberately not classified any finer.
    azure-identity's ``wrap_exceptions`` converts everything a token
    request can hit, an Entra rejection and an unreachable STS alike,
    into ``ClientAuthenticationError``, so the "definitive rejection vs.
    transient outage" split that proxmox's runup makes (its API answers
    401/403 distinguishably) is not available to us. Given the choice
    between calling a network blip a rejection and letting a genuinely
    bad credential through as a warning, this refuses; the hint names
    both possibilities so the operator can tell them apart.
    """
    from azure.core.exceptions import ClientAuthenticationError
    from azure.identity import ClientSecretCredential

    cred = ClientSecretCredential(sp.tenant_id, sp.client_id, client_secret)
    try:
        cred.get_token(_ARM_SCOPE)
    except ClientAuthenticationError as exc:
        raise AzureError(
            f"could not authenticate the Azure service principal for "
            f"vm-site '{site_name}' (client {sp.client_id} in tenant "
            f"{sp.tenant_id}, secret '{sp.secret_name}')",
            detail=str(exc),
            entity_kind="vm-site",
            entity_name=site_name,
            hint=(
                f"Check service_principal.tenant_id / client_id and the "
                f"value of the '{sp.secret_name}' secret (an expired client "
                f"secret is the usual cause; `az ad app credential list` "
                f"shows expiry). If Entra ID is simply unreachable this "
                f"fails the same way, because azure-identity reports both "
                f"as an authentication failure."
            ),
        ) from exc
    return cred


# The well-known secret name the service_principal.secret field defaults
# to, mirroring proxmox's DEFAULT_TOKEN_SECRET. The default env-var
# backend convention reads AW_SECRET_AZURE_CLIENT_SECRET. The config
# field is `secret`, NOT `client_secret`: it names a secret in the
# framework's secret system, and a field called client_secret would
# invite operators to paste the literal value into plaintext config.
DEFAULT_CLIENT_SECRET = "azure-client-secret"

_AZURE_REQUIRED_KEYS = ("subscription_id", "resource_group", "region")
_AZURE_OPTIONAL_KEYS = ("vm_sizes", "service_principal")

_SP_REQUIRED_KEYS = ("tenant_id", "client_id")
_SP_OPTIONAL_KEYS = ("secret",)


class _ServicePrincipal(NamedTuple):
    """A site's explicit Azure service-principal credential: the two
    plain-config identifiers plus the NAME of the framework secret
    holding the client secret. Never the secret's value: the platform
    instance holds no value source, and the value arrives per call
    through ``ctx.secret``."""

    tenant_id: str
    client_id: str
    secret_name: str


def _parse_service_principal(config: Mapping[str, object], owner: str) -> _ServicePrincipal | None:
    """The site's explicit service principal, or ``None`` when the
    optional ``service_principal`` table is absent (the ambient
    credential path, which is what every azure-vm site did before issue
    #199 and what every site that omits the table still does).

    Raises ``ConfigError`` on a malformed table so the shape is validated
    at registry build time (the finalize ``validate`` pass), not at first
    ``vm create``. Mirrors :func:`_parse_size_catalog`: one parser, called
    from ``validate`` for the check and from the credential build for the
    value.

    The nested-table shape is deliberate. A future certificate-based
    variant (``service_principal.certificate``) slots in beside
    ``secret`` without touching the top-level ``platform_config``
    namespace or breaking a declared site.
    """
    raw = config.get("service_principal")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"{owner}.service_principal must be a table of {{tenant_id, client_id, secret}}")
    parsed: dict[str, str] = {}
    for key in _SP_REQUIRED_KEYS:
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            raise ConfigError(
                f"{owner}.service_principal.{key} is required and must be a non-empty string; "
                f"omit the whole service_principal table to use ambient Azure credentials"
            )
        parsed[key] = value
    secret_name = raw.get("secret", DEFAULT_CLIENT_SECRET)
    if not isinstance(secret_name, str) or not secret_name:
        raise ConfigError(
            f"{owner}.service_principal.secret must be a bare secret name (string), "
            f"not the client secret's value; omit the key to use the default '{DEFAULT_CLIENT_SECRET}'"
        )
    unknown = sorted(set(raw) - set(_SP_REQUIRED_KEYS) - set(_SP_OPTIONAL_KEYS))
    if unknown:
        raise ConfigError(f"{owner}.service_principal: unknown field(s): {', '.join(unknown)}")
    return _ServicePrincipal(parsed["tenant_id"], parsed["client_id"], secret_name)


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
            "entry to the site's platform_config.vm_sizes catalog",
        )
    return min(fits, key=lambda s: (s.cpus, s.memory_gib))


class AzureVMPlatform(VMPlatform):
    """Runs VMs on the Azure Virtual Machines service via the Azure
    Python SDK. Named ``azure-vm``, not ``azure``: the capability is
    one specific Azure service, and other Azure services could plausibly
    back platforms of their own someday."""

    name: ClassVar[str] = "azure-vm"
    description: ClassVar[str] = "Azure Virtual Machines (subscription + resource group)"

    def __init__(self, owner_name: str, config: Mapping[str, object]) -> None:
        super().__init__(owner_name, config)
        # Azure credential and SDK clients, built on FIRST need by the
        # accessors below and reused for the instance's remaining ops.
        # The credential is subscription-independent AND its identity is
        # fixed by the bound config (the site's service_principal, or
        # the ambient chain when it declares none), so it caches once
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

    # No preflight override, on either credential path. A site with a
    # ``service_principal`` DOES declare a config secret, so the base's
    # central prediction pass over the site's declared references is
    # real work now (an unresolvable client secret fails the sweep with
    # the usual owner/usage framing, without this class touching the
    # secret machinery); a site without one declares nothing and that
    # pass stays vacuous. What is missing either way is an
    # unauthenticated readiness check worth making. A credential probe
    # is deliberately NOT one: verifying credentials before the
    # resolve/credential stage forks behavior on where they happen to
    # come from (a non-interactive chain passes, the browser-login
    # fallback can't be probed without BEING the interaction, and a
    # service principal's secret is not resolved yet). Credential and
    # reachability failures surface at runup and at the op with typed
    # errors, which is the contract: preflight is capped at what it can
    # check without resolved credentials.

    @classmethod
    def dependencies(cls, owner: str, config: Mapping[str, object]) -> tuple[ConfigReference, ...]:
        """The client-secret reference a declared ``service_principal``
        implies: its ``secret`` field names it (default
        ``azure-client-secret``). A site with no ``service_principal``
        authenticates ambiently and implies no reference, so its edge
        set is empty, as every azure-vm site's was before issue #199.

        Total and non-throwing: the edge's identity is the secret name
        alone, so it emits even when the table's OTHER fields are absent
        or malformed (their absence does not change what the edge points
        at), and is omitted only when the table itself, or the ``secret``
        field that names the edge, is malformed. ``validate`` is where
        those shape errors surface.
        """
        raw = config.get("service_principal")
        if not isinstance(raw, dict):
            return ()
        secret_name = raw.get("secret", DEFAULT_CLIENT_SECRET)
        if not isinstance(secret_name, str) or not secret_name:
            return ()
        from agentworks.resources.reference import ConfigReference

        return (
            ConfigReference(
                kind="secret",
                name=secret_name,
                usage="the Azure service-principal client secret",
            ),
        )

    @classmethod
    def validate(cls, owner: str, config: Mapping[str, object]) -> None:
        for key in _AZURE_REQUIRED_KEYS:
            value = config.get(key)
            if not isinstance(value, str) or not value:
                raise ConfigError(f"{owner}.{key} is required for the azure-vm platform and must be a non-empty string")
        unknown = sorted(set(config) - set(_AZURE_REQUIRED_KEYS) - set(_AZURE_OPTIONAL_KEYS))
        if unknown:
            raise ConfigError(f"{owner}: unknown azure-vm platform field(s): {', '.join(unknown)}")
        # Validate the optional blocks' shapes here so a malformed
        # vm_sizes or service_principal fails at config load, not first
        # vm create.
        _parse_size_catalog(config, owner)
        _parse_service_principal(config, owner)

    @classmethod
    def legacy_platform_metadata(cls, row: Mapping[str, Any], legacy: Mapping[str, Any]) -> dict[str, str]:
        if row["azure_resource_id"]:
            return {"resource_id": str(row["azure_resource_id"])}
        return {}

    def _get_credential(self, ctx: RunContext) -> object:
        """The Azure credential, built on first need (one live probe) and
        reused for the instance's remaining ops.

        Which credential is a config decision, not a runtime one: a site
        that declares a ``service_principal`` gets exactly that
        credential, built from the client secret ``ctx.secret`` delivers
        (the declare/receive contract: the instance never holds a
        resolver or a raw reader, only the credential derived from the
        delivered value), and NEVER falls back to the ambient chain or a
        browser prompt if it fails; a site that declares none gets the
        ambient chain with its browser fallback, byte-for-byte as before.
        Falling back would authenticate as a different identity than the
        operator configured, which is worse than failing.

        Caching stays correct under the fork because the fork's inputs
        are fixed per instance: the site's tenant, client, and secret
        NAME come from the bound ``platform_config``, so a given instance
        resolves the same credential every time. See
        :func:`_build_ambient_credential` and
        :func:`_build_service_principal_credential`.
        """
        cred = self._credential_cached
        if cred is None:
            sp = _parse_service_principal(self.platform_config, self._owner_display)
            if sp is None:
                cred = _build_ambient_credential()
            else:
                cred = _build_service_principal_credential(sp, ctx.secret(sp.secret_name), self.site_name)
            self._credential_cached = cred
        return cred

    def _compute_client(self, az: _HasSubscriptionId, ctx: RunContext) -> ComputeManagementClient:
        """The compute client for ``az``'s subscription, built on first
        need from the cached credential and reused for the instance's
        remaining ops against that subscription (see ``__init__`` for why
        the cache keys by subscription)."""
        from azure.mgmt.compute import ComputeManagementClient

        compute = self._compute_cached.get(az.subscription_id)
        if compute is None:
            # _get_credential() returns a TokenCredential-compatible object;
            # the cast avoids a hard azure.core import at module load time.
            compute = ComputeManagementClient(self._get_credential(ctx), az.subscription_id)  # type: ignore[arg-type]
            self._compute_cached[az.subscription_id] = compute
        return compute

    def _network_client(self, az: _HasSubscriptionId, ctx: RunContext) -> NetworkManagementClient:
        """The network client for ``az``'s subscription, built on first
        need from the cached credential and reused for the instance's
        remaining ops against that subscription (see ``__init__`` for why
        the cache keys by subscription)."""
        from azure.mgmt.network import NetworkManagementClient

        network = self._network_cached.get(az.subscription_id)
        if network is None:
            # Same as _compute_client: credential is TokenCredential-compatible at runtime.
            network = NetworkManagementClient(self._get_credential(ctx), az.subscription_id)  # type: ignore[arg-type]
            self._network_cached[az.subscription_id] = network
        return network

    def _resource_client(self, az: _HasSubscriptionId, ctx: RunContext) -> ResourceManagementClient:
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
            resource = ResourceManagementClient(self._get_credential(ctx), az.subscription_id)  # type: ignore[arg-type]
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
        :meth:`_get_credential` resolves off the site's config, and this
        is where BOTH paths first pay for it. On a site with a declared
        ``service_principal`` that means the client secret is read from
        the context here and the credential is probed here, so a bad or
        expired one aborts ``vm create`` with a typed, secret-naming
        error before the DB row or any Azure resource exists, which is
        the whole point of running this ahead of ``create``. The
        existence probe (``resource_groups.check_existence``) is
        read-only and mutates nothing. A credential or reachability
        failure is NOT a "group missing" verdict: those surface through
        :func:`_wrap_azure_error` exactly as the ops report them, so a
        bad or absent credential never masquerades as an absent resource
        group.

        Reachability failures are fatal here, which diverges from the
        proxmox runup on purpose, for two independent reasons. First,
        Azure's ``create`` makes many Resource Manager calls, so an
        unreachable RM at runup means the whole create cannot proceed
        anyway; aborting cleanly here, with nothing realized, beats
        warning past it into a cryptic mid-provision failure. (Proxmox
        warns and continues unverified because its token check is
        incidental: the op uses the token directly regardless.) Second,
        on the service-principal path the warn-vs-fail classification is
        not available even in principle: azure-identity reports an Entra
        rejection and an unreachable Entra identically, as
        ``ClientAuthenticationError`` (see
        :func:`_build_service_principal_credential`).
        """
        from types import SimpleNamespace

        az = SimpleNamespace(
            subscription_id=str(self.platform_config["subscription_id"]),
            resource_group=str(self.platform_config["resource_group"]),
            region=str(self.platform_config["region"]),
        )
        output.info(f"Performing runup test for vm-site/{self.site_name}...")
        # The client build sits OUTSIDE the try, here and at every op
        # below: it is where the credential is resolved, and its typed
        # failures (a context with no resolved secrets, a rejected
        # service principal) are already the answer. Wrapping them as
        # generic SDK errors would strip the hint that names the secret.
        # The try covers the SDK CALL, which is what _wrap_azure_error is
        # for.
        resource = self._resource_client(az, ctx)
        try:
            exists = resource.resource_groups.check_existence(az.resource_group)
        except Exception as exc:
            raise _wrap_azure_error(exc) from exc
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
        disk = request.disk_gib if request.disk_gib is not None else 50
        swap = request.swap_gib if request.swap_gib is not None else 0
        admin_username = request.admin_username
        tailscale_auth_key = request.tailscale_auth_key
        ssh_pub_key = request.ssh_public_key

        # Platform-owned naming with the slug as the
        # namespacing token; azure resource names are the primary
        # identifier, so a collision is an error.
        vm_name = f"{request.system_slug}-{request.vm_name}" if request.system_slug else request.vm_name

        output.detail("Connecting to Azure...")
        compute = self._compute_client(az, ctx)
        network = self._network_client(az, ctx)

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

        try:
            # Create public IP
            output.detail("Creating public IP...")
            ip_poller = network.public_ip_addresses.begin_create_or_update(  # type: ignore[call-overload]
                az.resource_group,
                f"{vm_name}-ip",
                {
                    "location": az.region,
                    "sku": {"name": "Standard"},
                    "public_ip_allocation_method": "Static",
                    "tags": {"owner": "agentworks"},
                },
            )
            ip_result = ip_poller.result()
            public_ip = ip_result.ip_address or ""

            # Create NSG with SSH rule
            output.detail("Creating network security group...")
            nsg_poller = network.network_security_groups.begin_create_or_update(  # type: ignore[call-overload]
                az.resource_group,
                f"{vm_name}-nsg",
                {
                    "location": az.region,
                    "security_rules": [
                        {
                            "name": "SSH",
                            "protocol": "Tcp",
                            "source_port_range": "*",
                            "destination_port_range": "22",
                            "source_address_prefix": "*",
                            "destination_address_prefix": "*",
                            "access": "Allow",
                            "priority": 1000,
                            "direction": "Inbound",
                        }
                    ],
                    "tags": {"owner": "agentworks"},
                },
            )
            nsg_result = nsg_poller.result()

            # Create NIC
            output.detail("Creating network interface...")

            # Need a subnet: use default VNet or create one
            vnet_name = f"{vm_name}{VNET_NAME_SUFFIX}"
            subnet_name = "default"
            vnet_poller = network.virtual_networks.begin_create_or_update(  # type: ignore[call-overload]
                az.resource_group,
                vnet_name,
                {
                    "location": az.region,
                    "address_space": {"address_prefixes": ["10.0.0.0/16"]},
                    "subnets": [
                        {
                            "name": subnet_name,
                            "address_prefix": "10.0.0.0/24",
                        }
                    ],
                    "tags": {"owner": "agentworks"},
                },
            )
            vnet_result = vnet_poller.result()
            subnet_id = vnet_result.subnets[0].id

            nic_poller = network.network_interfaces.begin_create_or_update(  # type: ignore[call-overload]
                az.resource_group,
                f"{vm_name}-nic",
                {
                    "location": az.region,
                    "ip_configurations": [
                        {
                            "name": "default",
                            "subnet": {"id": subnet_id},
                            "public_ip_address": {"id": ip_result.id},
                        }
                    ],
                    "network_security_group": {"id": nsg_result.id},
                    "tags": {"owner": "agentworks"},
                },
            )
            nic_result = nic_poller.result()

            # Create VM
            output.detail("Creating VM...")
            vm_poller = compute.virtual_machines.begin_create_or_update(  # type: ignore[call-overload]
                az.resource_group,
                vm_name,
                {
                    "location": az.region,
                    "hardware_profile": {"vm_size": azure_vm_size},
                    "storage_profile": {
                        "image_reference": {
                            "publisher": "Debian",
                            "offer": "debian-12",
                            "sku": "12-gen2",
                            "version": "latest",
                        },
                        "os_disk": {
                            "create_option": "FromImage",
                            "disk_size_gb": disk,
                            "managed_disk": {"storage_account_type": "StandardSSD_LRS"},
                        },
                    },
                    "os_profile": {
                        "computer_name": vm_name,
                        "admin_username": admin_username,
                        "custom_data": cloud_init_b64,
                        "linux_configuration": {
                            "disable_password_authentication": True,
                            "ssh": {
                                "public_keys": [
                                    {
                                        "path": f"/home/{admin_username}/.ssh/authorized_keys",
                                        "key_data": ssh_pub_key,
                                    }
                                ]
                            },
                        },
                    },
                    "network_profile": {
                        "network_interfaces": [{"id": nic_result.id}],
                    },
                    "tags": {"owner": "agentworks"},
                },
            )
            vm_result = vm_poller.result()
            resource_id = vm_result.id or ""

        except Exception as exc:
            output.detail("Cleaning up resources...")
            _cleanup_vm_resources(compute, network, az.resource_group, vm_name)
            raise _wrap_azure_error(exc) from exc

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
        compute = self._compute_client(az_cfg, ctx)
        try:
            compute.virtual_machines.begin_start(rg, name).result()
        except Exception as exc:
            raise _wrap_azure_error(exc) from exc
        output.info(f"Azure VM '{vm.name}' started")

    def stop(self, vm: VMRow, ctx: RunContext) -> None:
        # Idempotent by construction (the ABC flags stop): the Azure
        # begin_deallocate operation no-ops on a deallocated VM.
        output.info(f"Deallocating Azure VM '{vm.name}'...")
        rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
        compute = self._compute_client(az_cfg, ctx)
        try:
            compute.virtual_machines.begin_deallocate(rg, name).result()
        except Exception as exc:
            raise _wrap_azure_error(exc) from exc
        output.info(f"Azure VM '{vm.name}' deallocated")

    def delete(self, vm: VMRow, ctx: RunContext) -> None:
        output.info(f"Deleting Azure VM '{vm.name}'...")
        if not vm.platform_metadata.get("resource_id"):
            output.warn("no Azure resource ID, skipping Azure cleanup")
            return

        rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
        compute = self._compute_client(az_cfg, ctx)
        network = self._network_client(az_cfg, ctx)

        # Delete VM first (must complete before dependent resources)
        with contextlib.suppress(Exception):
            compute.virtual_machines.begin_delete(rg, name).result()

        _cleanup_vm_resources(compute, network, rg, name)

        output.info(f"Azure VM '{vm.name}' deleted")

    def attach_public_ip(self, vm: VMRow, ctx: RunContext) -> str:
        """Attach a temporary public IP to the VM's NIC. Returns the IP address."""
        rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
        network = self._network_client(az_cfg, ctx)
        compute = self._compute_client(az_cfg, ctx)

        try:
            # Create (or re-create) the public IP
            output.info("Attaching temporary public IP...")
            ip_poller = network.public_ip_addresses.begin_create_or_update(  # type: ignore[call-overload]
                rg,
                f"{name}-ip",
                {
                    "location": _get_vm_location(compute, vm),
                    "sku": {"name": "Standard"},
                    "public_ip_allocation_method": "Static",
                    "tags": {"owner": "agentworks"},
                },
            )
            ip_result = ip_poller.result()

            # Attach to NIC
            nic = network.network_interfaces.get(rg, f"{name}-nic")
            if nic.ip_configurations:
                # Azure SDK accepts dict for PublicIPAddress at runtime despite type stubs
                nic.ip_configurations[0].public_ip_address = {"id": ip_result.id}  # type: ignore[assignment]
            network.network_interfaces.begin_create_or_update(
                rg,
                f"{name}-nic",
                nic,
            ).result()

        except Exception as exc:
            raise _wrap_azure_error(exc) from exc

        return ip_result.ip_address or ""

    def detach_public_ip(self, vm: VMRow, ctx: RunContext) -> None:
        """Detach and delete the public IP from the VM's NIC."""
        rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
        network = self._network_client(az_cfg, ctx)

        output.info("Removing public IP...")
        # Detach from NIC
        with contextlib.suppress(Exception):
            nic = network.network_interfaces.get(rg, f"{name}-nic")
            if nic.ip_configurations:
                nic.ip_configurations[0].public_ip_address = None
            network.network_interfaces.begin_create_or_update(
                rg,
                f"{name}-nic",
                nic,
            ).result()

        # Delete the public IP resource
        with contextlib.suppress(Exception):
            network.public_ip_addresses.begin_delete(rg, f"{name}-ip").result()

    def display_backend_name(self, vm: VMRow) -> str:
        resource_id = vm.platform_metadata.get("resource_id")
        if not resource_id:
            return vm.name
        _rg, name, _cfg = _parse_resource_id(resource_id)
        return name

    def native_transport(
        self,
        vm: VMRow,
        ctx: RunContext,
        *,
        config: Config | None = None,
    ) -> Transport | None:
        rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
        compute = self._compute_client(az_cfg, ctx)
        network = self._network_client(az_cfg, ctx)
        try:
            vm_info = compute.virtual_machines.get(
                rg,
                name,
                expand="instanceView",
            )
        except Exception as exc:
            raise _wrap_azure_error(exc) from exc

        # Walk NICs to find the public IP (may not exist if detached). An
        # empty string here propagates to ``SSHTransport(host="")`` which
        # the transports.native_transport factory catches with a typed
        # StateError; on the canonical path this method is reached only
        # inside the transient_route context manager which guarantees a
        # public IP is attached, so the empty case is a defensive guard.
        public_ip = _get_vm_public_ip(network, vm_info)
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

    def post_tailscale_ready(self, vm: VMRow, ctx: RunContext) -> None:
        """Detach the cloud-init public IP now that Tailscale is up.

        The attach happens inside :meth:`create` (Azure needs the IP to
        drive cloud-init bootstrap); this hook fires at the async
        Tailscale-ready point inside ``bootstrap_vm`` (Phase A) to close the
        public-exposure window.
        """
        self.detach_public_ip(vm, ctx)

    @contextlib.contextmanager
    def transient_route(self, vm: VMRow, ctx: RunContext) -> Iterator[None]:
        """Attach a transient public IP for the duration of the context.

        The native transport for Azure reaches the VM via a temporary
        public IP. Attach on enter, detach on exit (regardless of how
        the caller unwinds). The
        :func:`agentworks.transports.native_transport` factory wraps
        this around the per-platform :meth:`native_transport` call so
        the lifecycle stays polymorphic.
        """
        self.attach_public_ip(vm, ctx)
        try:
            yield
        finally:
            self.detach_public_ip(vm, ctx)

    def status(self, vm: VMRow, ctx: RunContext) -> VMStatus:
        if not vm.platform_metadata.get("resource_id"):
            return VMStatus.UNKNOWN
        rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
        # Outside the degrade-to-UNKNOWN catch on purpose: a status probe
        # tolerating an unreachable backend is one thing, but silently
        # reporting UNKNOWN because the site's credential is rejected
        # would hide a misconfiguration behind a plausible-looking answer.
        compute = self._compute_client(az_cfg, ctx)
        try:
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


def _get_vm_public_ip(network: NetworkManagementClient, vm_info: object) -> str:
    """Resolve the public IP address for a VM from its NIC, using the
    caller's (cached) network client."""
    nic_refs = (
        getattr(
            getattr(vm_info, "network_profile", None),
            "network_interfaces",
            [],
        )
        or []
    )
    for nic_ref in nic_refs:
        nic_id = nic_ref.id
        if not nic_id:
            continue
        # Parse NIC resource group and name from ID
        parts = nic_id.split("/")
        rg_idx = next(i for i, p in enumerate(parts) if p.lower() == "resourcegroups")
        nic_rg = parts[rg_idx + 1]
        nic_name = parts[-1]

        nic = network.network_interfaces.get(nic_rg, nic_name)
        for ip_config in nic.ip_configurations or []:
            pip_ref = ip_config.public_ip_address
            if pip_ref and pip_ref.id:
                pip_parts = pip_ref.id.split("/")
                pip_rg_idx = next(i for i, p in enumerate(pip_parts) if p.lower() == "resourcegroups")
                pip_rg = pip_parts[pip_rg_idx + 1]
                pip_name = pip_parts[-1]
                pip = network.public_ip_addresses.get(pip_rg, pip_name)
                if pip.ip_address:
                    return pip.ip_address
    return ""


def _cleanup_vm_resources(
    compute: ComputeManagementClient,
    network: NetworkManagementClient,
    rg: str,
    name: str,
) -> None:
    """Best-effort cleanup of all resources associated with a VM."""
    for cleanup in [
        lambda: network.network_interfaces.begin_delete(rg, f"{name}-nic").result(),
        lambda: network.public_ip_addresses.begin_delete(rg, f"{name}-ip").result(),
        lambda: network.network_security_groups.begin_delete(rg, f"{name}-nsg").result(),
        lambda: network.virtual_networks.begin_delete(rg, f"{name}{VNET_NAME_SUFFIX}").result(),
    ]:
        with contextlib.suppress(Exception):
            cleanup()  # type: ignore[no-untyped-call]

    # OS disk name is generated by Azure, find by tag
    with contextlib.suppress(Exception):
        for disk in compute.disks.list_by_resource_group(rg):
            disk_name = disk.name or ""
            if disk.tags and disk.tags.get("owner") == "agentworks" and name in disk_name and disk_name:
                compute.disks.begin_delete(rg, disk_name).result()


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


def _get_vm_location(compute: ComputeManagementClient, vm: VMRow) -> str:
    """Get the Azure region for a VM by querying the compute API, using
    the caller's (cached) compute client."""
    rg, name, _az_cfg = _parse_resource_id(_resource_id(vm))
    vm_info = compute.virtual_machines.get(rg, name)
    return vm_info.location or "eastus"


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
