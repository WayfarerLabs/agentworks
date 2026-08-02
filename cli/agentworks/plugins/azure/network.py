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
carries a permanent ``deny-all-inbound`` rule at priority 200, and no
standing allow-from-anywhere rule ever exists. SSH access happens
through ephemeral allow rules scoped to the operator's egress IP (plus
the ``operator.ssh_allow_cidrs`` config extras), all living in the
priority band [100, 199] under the deny:

- ``allow-ssh-bootstrap`` (fixed name, slot 100): created with the NSG
  at provisioning (the fresh NSG makes slot 100 free by construction)
  so cloud-init can be reached, deleted the moment Tailscale is
  confirmed or the create fails/aborts. The fixed name is what lets the
  close hooks delete it without any in-process state.
- ``allow-ssh-transient-<nonce>`` (per operation): poked by
  ``transient_route`` at the lowest free slot in the band and removed
  by name on exit. Per-operation rules are what make concurrent native
  ops on one VM safe: the old single well-known rule meant one op's
  exit could remove the allow another op still relied on; now each op
  owns its rule, and duplicate source prefixes across concurrent rules
  are fine and expected (the rules are independent). The band gives 100
  concurrent operations of headroom; each rule's description carries a
  created-at timestamp for a future doctor check to flag stale rules
  leaked by killed processes (never auto-pruned by age here: a
  legitimate shell session can live for days).
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import AuthorizationError, ConfigError, ConnectivityError, ProvisioningError

if TYPE_CHECKING:
    from collections.abc import Callable

    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.network import NetworkManagementClient
    from azure.mgmt.network.models import SecurityRule

    from agentworks.config import Config


# Suffix appended to the VM hostname to name its virtual-network subresource:
# {slug}-{vm}-vnet. This is the tightest sink bounding MAX_VM_NAME_LENGTH (the
# vnet name limit is 64), so the length derivation in config/validation.py
# mirrors this literal and a pinned test asserts the worst-case name is exactly
# 64 at the cap. Keep the two in sync (the test fails if this suffix grows).
VNET_NAME_SUFFIX = "-vnet"

# The permanent NSG baseline: deny all inbound traffic. The VM's public
# IP stays attached for its whole lifetime (Azure is retiring default
# outbound access, so a VM whose public IP is removed simply goes
# offline); exposure is controlled purely by NSG rules. The final
# priority layout: the ephemeral allows live in the band [100, 199]
# (100 is Azure's minimum custom priority), the deny sits at 200, so
# every allow outranks the deny, no custom rule can be inserted above
# the band, and 200 still far outranks Azure's 65000-range defaults.
# Tailscale is unaffected by the deny (the overlay rides outbound flows,
# though direct inbound hole-punched UDP is blocked, so peer-to-peer
# paths degrade to DERP relay).
DENY_ALL_INBOUND_RULE_NAME = "deny-all-inbound"
DENY_ALL_INBOUND_RULE_PRIORITY = 200

# The ephemeral SSH allows: TCP/22 from the operator's egress prefixes
# only, created when a route is needed and deleted after. Bootstrap uses
# the FIXED name (one bootstrap per VM, and the close hooks must know
# the name without in-process state); every native-transport session
# pokes its own PREFIX+nonce rule so concurrent ops cannot cross-remove.
# Both allocate priorities from the band below (bootstrap takes 100 by
# construction: the NSG is created fresh with it).
BOOTSTRAP_ALLOW_RULE_NAME = "allow-ssh-bootstrap"
TRANSIENT_ALLOW_RULE_PREFIX = "allow-ssh-transient-"
ALLOW_PRIORITY_BAND_START = 100
ALLOW_PRIORITY_BAND_END = 199

# Stamped into every ephemeral allow's description, with a created-at
# timestamp: the future doctor stale-rule sweep keys off it to flag
# rules leaked by killed processes.
ALLOW_RULE_DESCRIPTION_MARKER = "agentworks transient SSH allow"

# Bounded retry for the poke's slot-allocation race: Azure enforces
# priority uniqueness per direction, so two concurrent allocations of
# the same slot surface as a create error; the loser re-lists and takes
# the next free slot.
_POKE_ALLOCATION_ATTEMPTS = 5

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

    The optional entity / hint keywords are the base ``AgentworksError``
    ones, forwarded so a failure that KNOWS which resource it is about
    can say so (the service-principal credential build names its site and
    its secret). :func:`wrap_azure_error`, which converts an arbitrary SDK
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


def config_allow_cidrs(config: Config | None) -> list[str]:
    """The ``operator.ssh_allow_cidrs`` extras from an operator
    config, or none when no config was threaded in. Same defensive
    getattr chain as ``AzureVMPlatform.native_transport``'s
    identity-file read (callers may thread partial config stand-ins)."""
    operator = getattr(config, "operator", None)
    return list(getattr(operator, "ssh_allow_cidrs", None) or [])


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


def _allow_ssh_rule(rule_name: str, prefixes: list[str], priority: int) -> SecurityRule:
    """An ephemeral scoped allow rule model: TCP/22 from ``prefixes`` at
    ``priority``, with the marker+timestamp description the future
    doctor stale-rule sweep keys off."""
    from datetime import UTC, datetime

    from azure.mgmt.network.models import SecurityRule

    created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return SecurityRule(
        name=rule_name,
        description=f"{ALLOW_RULE_DESCRIPTION_MARKER} (created {created_at})",
        protocol="Tcp",
        source_port_range="*",
        destination_port_range="22",
        source_address_prefixes=list(prefixes),
        destination_address_prefix="*",
        access="Allow",
        priority=priority,
        direction="Inbound",
    )


def initial_security_rules(prefixes: list[str]) -> list[SecurityRule]:
    """The NSG rule set a fresh VM provisions with: the permanent
    deny-all baseline plus the fixed-name scoped bootstrap allow
    (cloud-init needs inbound SSH from the operator; the close hooks
    delete it by its fixed name once Tailscale is confirmed or the
    create fails). The NSG is created fresh with exactly these rules,
    so the bootstrap allow's slot at the band start (100) is free by
    construction."""
    return [
        _deny_all_inbound_rule(),
        _allow_ssh_rule(BOOTSTRAP_ALLOW_RULE_NAME, prefixes, ALLOW_PRIORITY_BAND_START),
    ]


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


def _lowest_free_allow_priority(network: NetworkManagementClient, rg: str, name: str) -> int:
    """The lowest free inbound priority in the allow band [100, 199].

    Lists the NSG's current rules and takes the first unoccupied slot
    (Azure enforces priority uniqueness per direction, so occupancy is
    exactly the listed inbound priorities). A full band is a typed
    error: either 100 genuinely concurrent operations, or stale
    transient rules leaked by killed processes.
    """
    from agentworks.errors import StateError

    try:
        rules = network.security_rules.list(rg, f"{name}-nsg")
        # list() returns a lazy ItemPaged: the HTTP requests happen
        # during iteration, so the wrap must cover the comprehension,
        # not just the call.
        used = {rule.priority for rule in rules if rule.direction == "Inbound"}
    except Exception as exc:
        raise wrap_azure_error(exc) from exc
    for priority in range(ALLOW_PRIORITY_BAND_START, ALLOW_PRIORITY_BAND_END + 1):
        if priority not in used:
            return priority
    raise StateError(
        f"no free NSG priority in the transient SSH allow band "
        f"[{ALLOW_PRIORITY_BAND_START}, {ALLOW_PRIORITY_BAND_END}] on NSG '{name}-nsg'",
        entity_kind="vm",
        entity_name=name,
        hint=(
            f"delete stale '{TRANSIENT_ALLOW_RULE_PREFIX}*' rules on the NSG "
            f"(leaked by killed processes; their descriptions carry a created-at timestamp)"
        ),
    )


def poke_ssh_allow(network: NetworkManagementClient, rg: str, name: str, prefixes: list[str]) -> str:
    """Create this operation's own ephemeral SSH allow scoped to
    ``prefixes``, returning the rule's name for the caller's exit
    removal.

    Each poke creates a fresh ``allow-ssh-transient-<nonce>`` rule at
    the lowest free priority in the band, so concurrent native ops on
    the same VM each own an independent rule (duplicate prefixes across
    them are fine and expected) and one op's exit can never remove an
    allow another op still relies on.

    Allocation is optimistic with a bounded retry: Azure enforces
    priority uniqueness per direction, so losing a slot race to a
    concurrent allocation surfaces as a create error; the loser re-lists
    and takes the next free slot. After the attempts are exhausted the
    last error raises wrapped. A failed attempt cleans up its own rule
    name (via :func:`remove_ssh_allow`: 404-tolerant, and a cleanup
    failure warns naming the rule, NSG, and prefixes rather than being
    swallowed) before moving on: the create may have succeeded
    server-side even though the client-side wait failed, and only this
    function knows the nonce name, so the cleanup cannot be left to the
    caller's exit path.
    """
    from uuid import uuid4

    output.info(f"Opening SSH route (allow scoped to {', '.join(prefixes)})...")
    last_exc: Exception | None = None
    for _attempt in range(_POKE_ALLOCATION_ATTEMPTS):
        priority = _lowest_free_allow_priority(network, rg, name)
        rule_name = f"{TRANSIENT_ALLOW_RULE_PREFIX}{uuid4().hex[:8]}"
        try:
            network.security_rules.begin_create_or_update(
                rg,
                f"{name}-nsg",
                rule_name,
                _allow_ssh_rule(rule_name, prefixes, priority),
            ).result()
        except Exception as exc:
            remove_ssh_allow(network, rg, name, rule_name, prefixes)
            last_exc = exc
            continue
        return rule_name
    assert last_exc is not None  # the loop only falls through via the except arm
    raise wrap_azure_error(last_exc) from last_exc


def remove_ssh_allow(
    network: NetworkManagementClient,
    rg: str,
    name: str,
    rule_name: str,
    prefixes: list[str] | None = None,
) -> None:
    """Delete one ephemeral SSH allow rule by name, restoring zero
    inbound exposure through it (the deny-all baseline stays, and other
    operations' transient rules are untouched).

    ``rule_name`` is the fixed ``allow-ssh-bootstrap`` on the hook paths
    and the poke's nonce name on the ``transient_route`` exit and the
    poke's own attempt cleanup. A 404 is fine (the rule was already
    gone: a hook re-run, a VM that never had it, or a create that never
    landed server-side). Any other failure warns instead of raising:
    every call site must keep unwinding, and the warning names the rule
    and states the actual residual exposure: the rule's scoped source
    prefixes, not the world (pass ``prefixes`` when known so the warning
    can name them exactly). The hook paths deliberately pass no prefixes
    and settle for the generic scope phrasing: they can fire in flows
    that never computed the allow's scope in-process (the rule was
    created by an earlier create), and stashing per-VM prefix state on
    the platform instance just to sharpen a warning string is not worth
    the coupling; the rule NAME, by contrast, is always known (fixed for
    bootstrap, returned by the poke), so it is always named.
    """
    from azure.core.exceptions import ResourceNotFoundError

    output.info(f"Closing SSH route (removing allow rule '{rule_name}')...")
    try:
        network.security_rules.begin_delete(rg, f"{name}-nsg", rule_name).result()
    except ResourceNotFoundError:
        pass
    except Exception as exc:
        err = wrap_azure_error(exc)
        scope = f"source(s) {', '.join(prefixes)}" if prefixes else "the rule's scoped source addresses"
        output.warn(
            f"could not remove the '{rule_name}' rule on NSG "
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
    """Best-effort sweep of the VM's named auxiliary resources (NIC,
    public IP, NSG, vnet, tagged OS disk).

    Per-resource failures WARN (naming the resource so the operator can
    finish removal in the portal) and the sweep continues; a straggler
    here is recoverable cost, never an orphaned VM, so it must not fail
    the surrounding delete or rollback (#329 draws that line: the VM
    itself is the gate, via :func:`verify_vm_deleted`). A 404 stays
    quiet: it is the normal answer on a retry or a rollback of a
    partially created set. A ``KeyboardInterrupt`` escapes to the
    caller's interrupt protocol, as before.
    """
    from azure.core.exceptions import ResourceNotFoundError

    steps: list[tuple[str, Callable[[], object]]] = [
        (f"{name}-nic", lambda: network.network_interfaces.begin_delete(rg, f"{name}-nic").result()),
        (f"{name}-ip", lambda: network.public_ip_addresses.begin_delete(rg, f"{name}-ip").result()),
        (f"{name}-nsg", lambda: network.network_security_groups.begin_delete(rg, f"{name}-nsg").result()),
        (
            f"{name}{VNET_NAME_SUFFIX}",
            lambda: network.virtual_networks.begin_delete(rg, f"{name}{VNET_NAME_SUFFIX}").result(),
        ),
    ]
    for label, step in steps:
        try:
            step()
        except ResourceNotFoundError:
            pass
        except Exception as exc:
            output.warn(
                f"could not delete Azure resource '{label}' in resource group "
                f"'{rg}': {wrap_azure_error(exc).summary}; delete it there manually."
            )

    # OS disk name is generated by Azure, find by tag
    try:
        for disk in compute.disks.list_by_resource_group(rg):
            disk_name = disk.name or ""
            if disk.tags and disk.tags.get("owner") == "agentworks" and name in disk_name and disk_name:
                # An already-gone disk is the normal answer on a retry.
                with contextlib.suppress(ResourceNotFoundError):
                    compute.disks.begin_delete(rg, disk_name).result()
    except ResourceNotFoundError:
        pass  # the resource group itself is gone: nothing left to sweep
    except Exception as exc:
        output.warn(
            f"could not sweep the OS disk(s) for '{name}' in resource group "
            f"'{rg}': {wrap_azure_error(exc).summary}; delete any remaining "
            f"agentworks-tagged disk there manually."
        )


def delete_vm_and_resources(
    compute: ComputeManagementClient,
    network: NetworkManagementClient,
    rg: str,
    name: str,
) -> Exception | None:
    """Delete the VM first (it holds the NIC and managed disk, which
    Azure refuses to delete while attached), then run the name-based
    resource sweep. The one place the VM-first-then-sweep ordering
    lives: shared by the delete op and
    :func:`rollback_create_on_interrupt`.

    The VM delete's failure is captured and RETURNED rather than raised
    or swallowed: the sweep must still run either way (a half-deleted
    VM's stragglers are still worth collecting), and only the caller
    knows what a failed VM delete means: the delete op feeds it to
    :func:`verify_vm_deleted`, whose raise keeps the DB row (#329); the
    interrupt rollback warns and lets the original interrupt propagate.
    A ``KeyboardInterrupt`` still escapes (it is not an ``Exception``),
    keeping the two-Ctrl-C interrupt protocol.
    """
    vm_delete_exc: Exception | None = None
    try:
        compute.virtual_machines.begin_delete(rg, name).result()
    except Exception as exc:
        vm_delete_exc = exc
    cleanup_vm_resources(compute, network, rg, name)
    return vm_delete_exc


# The documented ARM RBAC rejection codes: ``AuthorizationFailed`` (the
# credential lacks rights on the resource itself) and
# ``LinkedAuthorizationFailed`` (it lacks rights on a linked resource,
# e.g. the NIC's subnet during a VM delete).
_AUTHORIZATION_FAILURE_CODES = frozenset({"AuthorizationFailed", "LinkedAuthorizationFailed"})


def _is_authorization_failure(exc: Exception) -> bool:
    """Whether the SDK exception is an ARM authorization rejection (the
    credential is authenticated but RBAC denies it the operation).
    Matched on the documented RBAC error codes
    (:data:`_AUTHORIZATION_FAILURE_CODES`), read with the same
    nested-details walk as :func:`wrap_azure_error` (ARM sometimes
    reports a generic top-level code and buries the specific one in
    ``error.details[0]``), with an HTTP 403 fallback. Every attribute is
    read defensively because the exception may be anything the SDK
    raised; ``details`` in particular is only indexed when it is a real
    sequence, so a malformed shape can never raise out of
    classification and replace the failure being classified.
    (``wrap_azure_error``'s walk needs no such guard: it runs only
    under the ``HttpResponseError`` isinstance gate, whose
    ``error.details`` is the SDK's typed list.)"""
    error = getattr(exc, "error", None)
    code = getattr(error, "code", None)
    details = getattr(error, "details", None)
    if isinstance(details, Sequence) and details:
        code = getattr(details[0], "code", None) or code
    if code in _AUTHORIZATION_FAILURE_CODES:
        return True
    return getattr(exc, "status_code", None) == 403


def verify_vm_deleted(
    compute: ComputeManagementClient,
    rg: str,
    name: str,
    delete_exc: Exception | None = None,
) -> None:
    """The delete op's post-teardown gate (#329): positively confirm the
    backend VM is gone, raising a typed error when it is not or when its
    absence cannot be confirmed.

    The teardown above this is best-effort, so a failed VM delete (the
    reported case: ambient credentials without delete rights, swallowed
    into a clean "deleted" message) would otherwise look like success,
    and the caller would drop the DB row, orphaning a VM that nothing
    can target anymore. The probe's not-found answer IS the success
    (idempotent delete: already-gone finishes the job); anything else
    raises so the manager keeps the row and the operator can fix the
    cause and re-run ``agw vm delete``:

    - VM still present, ``delete_exc`` identifiable as an RBAC denial:
      ``AuthorizationError`` (a clean one-liner; the cause is known).
    - VM still present otherwise: ``AzureError`` naming the captured
      delete failure when there is one.
    - Probe itself failed (cannot confirm): ``AzureError``; claiming
      success without positive confirmation is how #329 happened. But
      when the probe failure OR the captured delete failure is itself
      an RBAC denial, ``AuthorizationError`` instead: a credential
      without delete rights often lacks read rights too, and a generic
      could-not-confirm would bury the actionable denial and its
      grant hint.

    ``delete_exc`` is the VM delete's captured failure from
    :func:`delete_vm_and_resources`, threaded in so the raise names the
    actual cause instead of just "still exists".
    """
    from azure.core.exceptions import ResourceNotFoundError

    retry_hint = "the VM record is kept so the delete can be retried"
    grant_hint = (
        f"{retry_hint}; grant the active Azure credential delete "
        f"rights on the resource group (e.g. the Contributor role) "
        f"and re-run `agw vm delete`"
    )

    def _rbac_refusal(cause: Exception) -> AuthorizationError:
        """The typed RBAC rejection for a delete Azure refused, shared
        by the still-present branch and the denied-probe branch."""
        return AuthorizationError(
            f"Azure refused to delete VM '{name}' in resource group '{rg}': {wrap_azure_error(cause).summary}",
            entity_kind="vm",
            entity_name=name,
            hint=grant_hint,
        )

    try:
        compute.virtual_machines.get(rg, name)
    except ResourceNotFoundError:
        return
    except Exception as probe_exc:
        if delete_exc is not None and _is_authorization_failure(delete_exc):
            raise _rbac_refusal(delete_exc) from delete_exc
        if _is_authorization_failure(probe_exc):
            raise AuthorizationError(
                f"Azure denied the credential read access while confirming "
                f"VM '{name}' was deleted from resource group '{rg}': "
                f"{wrap_azure_error(probe_exc).summary}",
                entity_kind="vm",
                entity_name=name,
                hint=grant_hint,
            ) from probe_exc
        cause = delete_exc or probe_exc
        raise AzureError(
            f"could not confirm Azure VM '{name}' was deleted from resource "
            f"group '{rg}': {wrap_azure_error(probe_exc).summary}",
            detail=str(cause),
            entity_kind="vm",
            entity_name=name,
            hint=f"{retry_hint}; re-run `agw vm delete` once Azure is reachable",
        ) from cause

    if delete_exc is not None and _is_authorization_failure(delete_exc):
        raise _rbac_refusal(delete_exc) from delete_exc
    summary = (
        wrap_azure_error(delete_exc).summary
        if delete_exc is not None
        else "the delete reported success but the VM is still present"
    )
    raise AzureError(
        f"Azure VM '{name}' still exists in resource group '{rg}' after the delete attempt: {summary}",
        detail=str(delete_exc) if delete_exc is not None else summary,
        entity_kind="vm",
        entity_name=name,
        hint=f"{retry_hint}; fix the failure and re-run `agw vm delete`",
    ) from delete_exc


def rollback_create_on_interrupt(
    compute: ComputeManagementClient,
    network: NetworkManagementClient,
    rg: str,
    name: str,
) -> None:
    """Roll back a partially created resource set after an operator
    interrupt inside ``AzureVMPlatform.create``.

    The VM may fully exist here (the likeliest interrupt point is the
    minutes-long inline bootstrap wait, after every resource is up), so
    this mirrors the delete op's ordering: the VM first (it holds the
    NIC and disk), then the shared name-based sweep. Best-effort per
    resource, like the delete op. A SECOND interrupt during the cleanup
    abandons it cleanly instead of wedging: the surviving resources
    keep the bootstrap-window NSG state (the deny-all baseline plus the
    scoped bootstrap allow), and the warning names the resource group
    and name prefix so the operator can finish the removal manually.
    The second interrupt is absorbed so the caller re-raises the
    ORIGINAL one; either way a KeyboardInterrupt propagates to
    ``create_vm``, whose unwind then deletes the DB row it no longer
    needs. A VM delete failure inside the teardown warns here (the
    proxmox rollbacks' ``_warn_if_vm_remains`` precedent, using the
    captured failure instead of a probe): this path is already
    unwinding on the operator's interrupt, so raising like the delete
    op does would replace it, but a silent orphan is never acceptable.
    """
    output.warn(
        f"Interrupted: cleaning up partial Azure resources for '{name}', please wait (Ctrl-C again to abandon them)..."
    )
    try:
        vm_delete_exc = delete_vm_and_resources(compute, network, rg, name)
    except KeyboardInterrupt:
        output.warn(
            f"Cleanup abandoned: Azure resources named '{name}*' may remain "
            f"in resource group '{rg}'; delete them there manually."
        )
    else:
        if vm_delete_exc is not None:
            output.warn(
                f"Azure VM '{name}' may remain in resource group '{rg}' "
                f"({wrap_azure_error(vm_delete_exc).summary}); delete it there manually."
            )


def _get_vm_location(compute: ComputeManagementClient, rg: str, name: str) -> str:
    """Get the Azure region for a VM by querying the compute API, using
    the caller's (cached) compute client."""
    vm_info = compute.virtual_machines.get(rg, name)
    return vm_info.location or "eastus"
