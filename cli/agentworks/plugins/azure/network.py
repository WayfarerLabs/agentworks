"""Azure SDK plumbing shared by the platform's ops: the typed error
wrapper, operator egress-IP discovery, and the per-VM network-resource
helpers (public IP, NSG exposure rules, resource cleanup).

Split out of ``platform.py`` (issue #331): ``platform.py`` keeps the
``VMPlatform`` capability surface (lifecycle ops, SDK clients, sizing);
this module owns the network-resource mechanics those ops share. The
SDK-facing functions take already-built clients plus the parsed resource
group and VM name, so this module never touches ``VMRow`` or client
caching. Azure SDK imports stay function-local, matching
``platform.py``, so azure modules never load at CLI startup.

Exposure model (baseline deny, ephemeral scoped allows): every VM's NSG
carries a permanent ``deny-all-inbound`` rule, and no standing
allow-from-anywhere rule ever exists. SSH access happens through one
ephemeral ``allow-ssh-transient`` rule scoped to the operator's egress
IP (plus the ``operator.ssh_allow_cidrs`` config extras): created for
cloud-init bootstrap at provisioning, deleted the moment Tailscale is
confirmed (or the create fails), and poked/removed around each
native-transport session by ``transient_route``.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import ConfigError, ConnectivityError, ProvisioningError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.network import NetworkManagementClient
    from azure.mgmt.network.models import SecurityRule


# Suffix appended to the VM hostname to name its virtual-network subresource:
# {slug}-{vm}-vnet. This is the tightest sink bounding MAX_VM_NAME_LENGTH (the
# vnet name limit is 64), so the length derivation in config/validation.py
# mirrors this literal and a pinned test asserts the worst-case name is exactly
# 64 at the cap. Keep the two in sync (the test fails if this suffix grows).
VNET_NAME_SUFFIX = "-vnet"

# The permanent NSG baseline: deny all inbound traffic. The VM's public
# IP stays attached for its whole lifetime (Azure is retiring default
# outbound access, so a VM whose public IP is removed simply goes
# offline); exposure is controlled purely by NSG rules. The deny sits at
# priority 200 so the ephemeral allow below (at 100, Azure's minimum
# custom priority) always outranks it and no custom rule can be inserted
# above the allow; 200 still far outranks Azure's 65000-range defaults.
# Tailscale is unaffected by the deny (the overlay rides outbound flows,
# though direct inbound hole-punched UDP is blocked, so peer-to-peer
# paths degrade to DERP relay).
DENY_ALL_INBOUND_RULE_NAME = "deny-all-inbound"
DENY_ALL_INBOUND_RULE_PRIORITY = 200

# The single ephemeral SSH allow: TCP/22 from the operator's egress
# prefixes only, created when a route is needed (cloud-init bootstrap,
# a native-transport session) and deleted after. One well-known name so
# create/update/delete all converge on the same rule.
ALLOW_SSH_RULE_NAME = "allow-ssh-transient"
ALLOW_SSH_RULE_PRIORITY = 100

# The old scheme's standing world-open SSH allow, deleted on
# convergence (see converge_nsg).
LEGACY_SSH_RULE_NAME = "SSH"

# The what's-my-ip service the egress detection queries.
_EGRESS_IP_URL = "https://checkip.amazonaws.com"

# Per-process cache for the detected egress IP: one probe per command,
# not one per poke.
_egress_ip_cache: str | None = None


class AzureError(ProvisioningError):
    """An Azure API operation failed.

    Attributes:
        summary: A concise, user-facing error message.
        detail: The full error details (for logs).
    """

    def __init__(self, summary: str, detail: str) -> None:
        super().__init__(summary)
        self.summary = summary
        self.detail = detail


def wrap_azure_error(exc: Exception) -> AzureError:
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


def detect_egress_ip() -> str:
    """The operator's public IPv4 address, detected via a what's-my-ip
    probe and cached per process (one probe per command, not per poke).

    Raises whatever the probe raises (URLError on unreachability,
    ValueError on a non-IPv4 response body); callers decide the policy
    (see :func:`operator_ssh_prefixes`).
    """
    global _egress_ip_cache
    if _egress_ip_cache is not None:
        return _egress_ip_cache

    import ipaddress
    import urllib.request

    with urllib.request.urlopen(_EGRESS_IP_URL, timeout=5) as response:  # noqa: S310  # fixed https URL
        body = response.read().decode("ascii", errors="strict").strip()
    # Strict parse: anything that is not a bare IPv4 address is a
    # detection failure, never a prefix we would poke into an NSG.
    _egress_ip_cache = str(ipaddress.IPv4Address(body))
    return _egress_ip_cache


def normalize_allow_cidrs(entries: Sequence[str]) -> list[str]:
    """Normalize ``operator.ssh_allow_cidrs`` entries to canonical IPv4
    prefixes (a bare IP becomes its /32). The config loader validates
    and normalizes at load, so this mostly re-normalizes already-clean
    values; a bad entry that reached here anyway (a hand-built config
    object) raises the same shape of typed ConfigError."""
    import ipaddress

    prefixes: list[str] = []
    for entry in entries:
        text = str(entry).strip()
        try:
            prefixes.append(str(ipaddress.IPv4Network(text, strict=False)))
        except ValueError as exc:
            raise ConfigError(
                f"operator.ssh_allow_cidrs: invalid entry {text!r}: must be an IPv4 address or CIDR"
            ) from exc
    return prefixes


def operator_ssh_prefixes(extra_cidrs: Sequence[str] = ()) -> list[str]:
    """The source prefixes for the ephemeral SSH allow rule: the detected
    operator egress IPv4 as a /32, plus the ``operator.ssh_allow_cidrs``
    config extras handed in by the caller. Recomputed at every poke
    (detection caches per process) so the scope stays current.

    Detection-failure policy: with extras configured, proceed on the
    extras alone with a warning; with none, raise a typed
    ConnectivityError whose hint names the config setting as the escape
    hatch (an unscoped allow is never poked as a fallback).
    """
    extras = normalize_allow_cidrs(extra_cidrs)
    try:
        detected = f"{detect_egress_ip()}/32"
    except Exception as exc:
        if extras:
            output.warn(
                f"could not detect the operator's public IP ({exc}); "
                f"scoping SSH access to the operator.ssh_allow_cidrs entries only"
            )
            return extras
        raise ConnectivityError(
            f"could not detect the operator's public IP for the scoped SSH allow rule: {exc}",
            hint=(
                "set operator.ssh_allow_cidrs in your agentworks config to a list "
                "of IPv4 addresses and/or CIDRs (e.g. your VPN or NAT egress "
                "addresses) to grant SSH access explicitly"
            ),
        ) from exc
    return [detected, *(p for p in extras if p != detected)]


def _deny_all_inbound_rule() -> SecurityRule:
    """The permanent baseline rule model (see the constants above)."""
    from azure.mgmt.network.models import SecurityRule

    return SecurityRule(
        name=DENY_ALL_INBOUND_RULE_NAME,
        protocol="*",
        source_port_range="*",
        destination_port_range="*",
        source_address_prefix="*",
        destination_address_prefix="*",
        access="Deny",
        priority=DENY_ALL_INBOUND_RULE_PRIORITY,
        direction="Inbound",
    )


def _allow_ssh_rule(prefixes: list[str]) -> SecurityRule:
    """The ephemeral scoped allow rule model: TCP/22 from ``prefixes``."""
    from azure.mgmt.network.models import SecurityRule

    return SecurityRule(
        name=ALLOW_SSH_RULE_NAME,
        protocol="Tcp",
        source_port_range="*",
        destination_port_range="22",
        source_address_prefixes=list(prefixes),
        destination_address_prefix="*",
        access="Allow",
        priority=ALLOW_SSH_RULE_PRIORITY,
        direction="Inbound",
    )


def initial_security_rules(prefixes: list[str]) -> list[SecurityRule]:
    """The NSG rule set a fresh VM provisions with: the permanent
    deny-all baseline plus the scoped bootstrap allow (cloud-init needs
    inbound SSH from the operator; ``post_tailscale_ready`` deletes the
    allow once Tailscale is confirmed)."""
    return [_deny_all_inbound_rule(), _allow_ssh_rule(prefixes)]


def converge_nsg(network: NetworkManagementClient, rg: str, name: str) -> None:
    """Converge a pre-existing VM's NSG onto the baseline-deny model.

    Two steps, in a load-bearing order:

    1. Ensure ``deny-all-inbound`` exists at priority 200 via
       ``begin_create_or_update``. This also re-pins a legacy deny
       created at the old priority 100, and it MUST happen before the
       allow poke: Azure requires unique priorities per direction, so a
       legacy deny still sitting at 100 would collide with the allow
       rule's slot.
    2. Delete the old scheme's standing world-open ``SSH`` allow if
       present (404 means already converged or never existed).

    A deny-ensure failure raises (the security baseline could not be
    established); a legacy-allow delete failure warns loudly, because
    the VM's SSH port stays open to the internet until that rule goes.
    """
    from azure.core.exceptions import ResourceNotFoundError

    try:
        network.security_rules.begin_create_or_update(
            rg,
            f"{name}-nsg",
            DENY_ALL_INBOUND_RULE_NAME,
            _deny_all_inbound_rule(),
        ).result()
    except Exception as exc:
        raise wrap_azure_error(exc) from exc

    try:
        network.security_rules.begin_delete(rg, f"{name}-nsg", LEGACY_SSH_RULE_NAME).result()
    except ResourceNotFoundError:
        pass
    except Exception as exc:
        err = wrap_azure_error(exc)
        output.warn(
            f"could not delete the legacy '{LEGACY_SSH_RULE_NAME}' allow rule on "
            f"NSG '{name}-nsg': {err.summary}. The VM's SSH port remains open to "
            f"the internet until that rule is removed manually."
        )


def poke_ssh_allow(network: NetworkManagementClient, rg: str, name: str, prefixes: list[str]) -> None:
    """Create (or update) the ephemeral SSH allow scoped to ``prefixes``."""
    output.info(f"Opening SSH route (allow scoped to {', '.join(prefixes)})...")
    try:
        network.security_rules.begin_create_or_update(
            rg,
            f"{name}-nsg",
            ALLOW_SSH_RULE_NAME,
            _allow_ssh_rule(prefixes),
        ).result()
    except Exception as exc:
        raise wrap_azure_error(exc) from exc


def remove_ssh_allow(
    network: NetworkManagementClient,
    rg: str,
    name: str,
    prefixes: list[str] | None = None,
) -> None:
    """Delete the ephemeral SSH allow rule, restoring zero inbound
    exposure (the deny-all baseline stays).

    A 404 is fine (the rule was already gone: a converged hook re-run,
    or a VM that never had it). Any other failure warns instead of
    raising: every call site (the post-Tailscale hook, the kept-FAILED
    hook, the ``transient_route`` exit) must keep unwinding, and the
    warning states the actual residual exposure: the rule's scoped
    source prefixes, not the world (pass ``prefixes`` when known so the
    warning can name them exactly). The hook paths deliberately pass no
    prefixes and settle for the generic phrasing: they can fire in
    flows that never computed the allow's scope in-process (the rule
    was created by an earlier create), and stashing per-VM prefix state
    on the platform instance just to sharpen a warning string is not
    worth the coupling.
    """
    from azure.core.exceptions import ResourceNotFoundError

    output.info("Closing SSH route (removing the transient allow rule)...")
    try:
        network.security_rules.begin_delete(rg, f"{name}-nsg", ALLOW_SSH_RULE_NAME).result()
    except ResourceNotFoundError:
        pass
    except Exception as exc:
        err = wrap_azure_error(exc)
        scope = f"source(s) {', '.join(prefixes)}" if prefixes else "the rule's scoped source addresses"
        output.warn(
            f"could not remove the '{ALLOW_SSH_RULE_NAME}' rule on NSG "
            f"'{name}-nsg': {err.summary}. The VM's SSH port stays open to "
            f"{scope}; delete the rule manually to restore zero inbound exposure."
        )


def ensure_public_ip(
    network: NetworkManagementClient,
    compute: ComputeManagementClient,
    rg: str,
    name: str,
) -> None:
    """Attach the VM's public IP if its NIC has none.

    The IP (``{name}-ip``) is created at provisioning time and kept for
    the VM's whole lifetime, so the steady state is a single NIC read
    that finds it already attached. VMs created under the old scheme
    (public IP detached once Tailscale came up) converge here: any time
    the NIC has no public IP, one is created (idempotent
    ``begin_create_or_update``, same shape as create) and attached.
    """
    from azure.mgmt.network.models import PublicIPAddress, PublicIPAddressSku

    try:
        nic = network.network_interfaces.get(rg, f"{name}-nic")
        if nic.ip_configurations and nic.ip_configurations[0].public_ip_address is not None:
            return

        output.info("Restoring missing public IP...")
        ip_poller = network.public_ip_addresses.begin_create_or_update(
            rg,
            f"{name}-ip",
            PublicIPAddress(
                location=_get_vm_location(compute, rg, name),
                sku=PublicIPAddressSku(name="Standard"),
                public_ip_allocation_method="Static",
                tags={"owner": "agentworks"},
            ),
        )
        ip_result = ip_poller.result()

        if nic.ip_configurations:
            nic.ip_configurations[0].public_ip_address = PublicIPAddress(id=ip_result.id)
        network.network_interfaces.begin_create_or_update(
            rg,
            f"{name}-nic",
            nic,
        ).result()

    except Exception as exc:
        raise wrap_azure_error(exc) from exc


def get_vm_public_ip(network: NetworkManagementClient, vm_info: object) -> str:
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


def cleanup_vm_resources(
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


def _get_vm_location(compute: ComputeManagementClient, rg: str, name: str) -> str:
    """Get the Azure region for a VM by querying the compute API, using
    the caller's (cached) compute client."""
    vm_info = compute.virtual_machines.get(rg, name)
    return vm_info.location or "eastus"
