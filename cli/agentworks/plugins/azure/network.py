"""Azure SDK plumbing shared by the platform's ops: operator egress-IP
discovery and the per-VM network-resource helpers (public IP, NSG exposure
rules, resource cleanup). The typed error and its wrapper live in
``errors.py``, and the bound on each Azure call a route makes in
``route_calls.py``.

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
  ``_transient_route`` at the lowest free slot in the band and removed
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
import time
from collections.abc import Sequence
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from agentworks import output

# The provider-neutral egress detection and ssh_allow_cidrs fold were hoisted
# to the shared vm_platform home (aws reuses them); re-exported here so this
# module's own callers (and tests that monkeypatch them) keep their spellings.
from agentworks.capabilities.vm_platform.ssh_exposure import (
    _EGRESS_IP_URL,
    config_allow_cidrs,
    detect_egress_ip,
    normalize_allow_cidrs,
    operator_ssh_prefixes,
)
from agentworks.errors import AuthorizationError
from agentworks.interrupts import interrupts_deferred
from agentworks.plugins.azure.errors import AzureError, wrap_azure_error
from agentworks.plugins.azure.route_calls import (
    _bounded_route_call,
    _finish_route_operation,
    _route_call_options,
    _route_operation_options,
    _RouteCallTimeout,
    _run_route_operation,
    _start_route_operation,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from azure.core.rest import HttpResponse
    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.network import NetworkManagementClient
    from azure.mgmt.network.models import SecurityRule

__all__ = [
    "_EGRESS_IP_URL",
    "config_allow_cidrs",
    "detect_egress_ip",
    "normalize_allow_cidrs",
    "operator_ssh_prefixes",
]


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
# the name without in-process state); every root script and native shell
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

# How long the poke waits before its next allocation when Azure answered a
# create with a status azure-core would itself have retried, since the create
# goes unretried: the Retry-After Azure sent, at most
# _POKE_RETRY_AFTER_MAX_SECONDS, or else an exponential backoff on the factor of
# azure-core's own RetryPolicy (its default 0.8s gives 0.8s, 1.6s, 3.2s, 6.4s).
# The cap, and a backoff that stays far below it, keep the waits between a
# poke's five attempts to two minutes in all.
_POKE_RETRY_AFTER_MAX_SECONDS = 30.0

# How much longer a poke whose create did not finish waits for that create
# before it removes the rule, with interrupts deferred: a delete while the
# create is in flight can fail (AnotherOperationInProgress) or find nothing,
# and the rule then lands after. With the removal's own
# _OPERATION_TIMEOUT_SECONDS, a Ctrl-C can be held 360s in all.
_UNFINISHED_CREATE_SETTLE_SECONDS = 60.0

# How often that wait reads the state the create's polling method holds. It
# reads rather than joining the poller: the poller's polling thread also ends on
# a transport error while the operation still runs, and on CPython 3.12 a join
# that Ctrl-C cut short marks the still-running thread stopped, so every later
# join returns at once.
_UNFINISHED_CREATE_POLL_SECONDS = 1.0

# The old scheme's standing world-open SSH allow, deleted on
# convergence (see converge_nsg).
LEGACY_SSH_RULE_NAME = "SSH"


def _deny_all_inbound_rule() -> SecurityRule:
    """The permanent baseline rule model (see the constants above)."""
    from azure.mgmt.network.models import SecurityRule, SecurityRulePropertiesFormat

    return SecurityRule(
        name=DENY_ALL_INBOUND_RULE_NAME,
        properties=SecurityRulePropertiesFormat(
            protocol="*",
            source_port_range="*",
            destination_port_range="*",
            source_address_prefix="*",
            destination_address_prefix="*",
            access="Deny",
            priority=DENY_ALL_INBOUND_RULE_PRIORITY,
            direction="Inbound",
        ),
    )


def _allow_ssh_rule(rule_name: str, prefixes: list[str], priority: int) -> SecurityRule:
    """An ephemeral scoped allow rule model: TCP/22 from ``prefixes`` at
    ``priority``, with the marker+timestamp description the future
    doctor stale-rule sweep keys off."""
    from datetime import UTC, datetime

    from azure.mgmt.network.models import SecurityRule, SecurityRulePropertiesFormat

    created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return SecurityRule(
        name=rule_name,
        properties=SecurityRulePropertiesFormat(
            description=f"{ALLOW_RULE_DESCRIPTION_MARKER} (created {created_at})",
            protocol="Tcp",
            source_port_range="*",
            destination_port_range="22",
            source_address_prefixes=list(prefixes),
            destination_address_prefix="*",
            access="Allow",
            priority=priority,
            direction="Inbound",
        ),
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
        _run_route_operation(
            partial(
                network.security_rules.begin_create_or_update,
                rg,
                f"{name}-nsg",
                DENY_ALL_INBOUND_RULE_NAME,
                _deny_all_inbound_rule(),
                **_route_operation_options(),
            ),
            f"ensuring the '{DENY_ALL_INBOUND_RULE_NAME}' rule on NSG '{name}-nsg'",
        )
    except Exception as exc:
        raise wrap_azure_error(exc) from exc

    try:
        _run_route_operation(
            partial(
                network.security_rules.begin_delete,
                rg,
                f"{name}-nsg",
                LEGACY_SSH_RULE_NAME,
                **_route_operation_options(),
            ),
            f"deleting the legacy '{LEGACY_SSH_RULE_NAME}' rule on NSG '{name}-nsg'",
        )
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
        # list() returns a lazy ItemPaged: the HTTP requests happen during
        # iteration, so the bounded call and the wrap must cover the
        # comprehension, not just the call.
        used = _bounded_route_call(
            lambda: {
                rule.priority
                for rule in network.security_rules.list(rg, f"{name}-nsg", **_route_call_options())
                if rule.direction == "Inbound"
            },
            f"listing the rules on NSG '{name}-nsg'",
        )
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
    last error raises wrapped. A create is never retried (``retry_total=0``),
    so no try whose delivery is unknown is replayed: azure-core resends it
    only after an explicit answer (a redirect, or a 401 challenge).
    An answer azure-core would have retried (a throttle, a timeout, a server
    error) gets a wait on azure-core's backoff factor before the next
    attempt (see :func:`_back_off_after`), so a throttled or briefly
    unavailable network provider does not burn every attempt at once.

    Every create that did not return normally, whatever stopped it, goes
    through :func:`_remove_unfinished_allow` before anything else happens:
    only this function knows the nonce name, so that cleanup cannot be left
    to the caller's exit path. A timeout or interrupt then propagates; the
    route's own exit never runs for a poke that did not return.
    """
    from uuid import uuid4

    output.info(f"Opening SSH route (allow scoped to {', '.join(prefixes)})...")
    last_exc: Exception | None = None
    for attempt in range(_POKE_ALLOCATION_ATTEMPTS):
        if last_exc is not None:
            _back_off_after(last_exc, attempt)
        priority = _lowest_free_allow_priority(network, rg, name)
        rule_name = f"{TRANSIENT_ALLOW_RULE_PREFIX}{uuid4().hex[:8]}"
        what = f"creating the '{rule_name}' rule on NSG '{name}-nsg'"
        poller: Any = None
        try:
            poller, deadline = _start_route_operation(
                partial(
                    network.security_rules.begin_create_or_update,
                    rg,
                    f"{name}-nsg",
                    rule_name,
                    _allow_ssh_rule(rule_name, prefixes, priority),
                    **_route_operation_options(retries=0),
                ),
                what,
            )
            _finish_route_operation(poller, what, deadline)
        except BaseException as exc:
            # The create did not return, so the rule may exist or still land, and only this name finds it.
            _remove_unfinished_allow(network, rg, name, rule_name, prefixes, poller, exc)
            if not isinstance(exc, Exception) or isinstance(exc, _RouteCallTimeout):
                raise  # an interrupt, or a timeout that another attempt would wait out as long again
            last_exc = exc
            continue
        return rule_name
    assert last_exc is not None  # the loop only falls through via the except arm
    raise wrap_azure_error(last_exc) from last_exc


def _back_off_after(error: Exception, attempts: int) -> None:
    """Wait before the poke's next allocation if azure-core would have retried the create ``error`` answered.

    azure-core's own ``RetryPolicy`` decides, so the answers are the ones it
    retries: 408, 429, 500, 502, 503, 504 and every status from 506, and any
    error status carrying Retry-After. The wait honors Retry-After, capped at
    ``_POKE_RETRY_AFTER_MAX_SECONDS``, and otherwise doubles from the
    policy's ``backoff_factor`` with each of the ``attempts`` made. It
    holds no interrupts, so Ctrl-C ends it; the failed attempt's cleanup is
    already done.
    """
    from azure.core.exceptions import HttpResponseError
    from azure.core.pipeline import PipelineContext, PipelineResponse
    from azure.core.pipeline.policies import RetryPolicy

    if not isinstance(error, HttpResponseError) or error.response is None:
        return
    policy = RetryPolicy()
    response = cast("HttpResponse", error.response)  # what the generated clients raise with
    answer = PipelineResponse(response.request, response, PipelineContext(None))
    if not policy.is_retry(policy.configure_retries({}), answer):
        return
    try:
        retry_after = policy.get_retry_after(answer)
    except ValueError:  # a Retry-After azure-core cannot parse takes the backoff
        retry_after = None
    if retry_after is not None:
        wait = min(retry_after, _POKE_RETRY_AFTER_MAX_SECONDS)
    else:
        wait = policy.backoff_factor * 2 ** (attempts - 1)
    output.info(f"Azure answered the create with {error.status_code}; trying again in {wait:.1f}s...")
    time.sleep(wait)


def _remove_unfinished_allow(
    network: NetworkManagementClient,
    rg: str,
    name: str,
    rule_name: str,
    prefixes: list[str],
    create: Any | None,
    error: BaseException,
) -> None:
    """Remove the poke's rule whose create did not return, holding interrupts.

    That is a create that raised ``error``, ran out of time or was
    interrupted, whether or not it had returned a poller (``create`` is
    ``None`` when it had not, as when its first request ran out of time).
    Interrupts are held for the settle below, when there is a
    poller, and the removal's own ``_OPERATION_TIMEOUT_SECONDS``: at most
    360s in all. The removal is silent only when the outcome
    is known; otherwise the warning names the rule for removal by hand, and
    goes out before the held interrupt is delivered, which would otherwise
    skip it.

    Before a poller, the outcome is known only when ``error`` is Azure's 4xx
    answer to the create: the create is never retried, and azure-core
    resends it only after an explicit answer (a redirect, or a 401
    challenge), so every try was turned away and the rule never exists. A request with no answer may have reached Azure,
    and a 5xx (a gateway timeout among them) does not say the network
    provider never took it, so both warn.

    A create with a poller gets up to ``_UNFINISHED_CREATE_SETTLE_SECONDS``
    more first, so the delete does not race it, reading the state its
    polling method holds every ``_UNFINISHED_CREATE_POLL_SECONDS`` until
    that state is final; when polling itself has stopped that state never
    changes and the wait takes its whole bound. Its outcome is known only
    when that state is the ``Succeeded`` Azure reported, since then the rule
    exists and the delete removes it: azure-core records its own failure to
    observe the operation (a status read Azure refused, or one without a
    status) as ``Failed``, so no other state proves the rule will not land.
    """
    with interrupts_deferred():
        settled = create is not None and _create_settled(create)
        if create is not None and not settled:
            output.info(
                f"Waiting up to {_UNFINISHED_CREATE_SETTLE_SECONDS:.0f}s for Azure to settle "
                f"the '{rule_name}' rule before removing it..."
            )
            deadline = time.monotonic() + _UNFINISHED_CREATE_SETTLE_SECONDS
            while not settled and (remaining := deadline - time.monotonic()) > 0:
                time.sleep(min(_UNFINISHED_CREATE_POLL_SECONDS, remaining))
                settled = _create_settled(create)
        known = _create_succeeded(create) if create is not None else _refused_by_azure(error)
        remove_ssh_allow(network, rg, name, rule_name, prefixes)
        if not known:
            output.warn(
                f"Could not confirm that Azure created the '{rule_name}' rule on NSG '{name}-nsg' before removing "
                f"it, so it may still appear, open to source(s) {', '.join(prefixes)}; check the NSG and delete that "
                "rule by name."
            )


def _refused_by_azure(error: BaseException) -> bool:
    """Whether ``error`` is Azure's 4xx answer to a request, which it therefore did not carry out."""
    from azure.core.exceptions import HttpResponseError

    return isinstance(error, HttpResponseError) and error.response is not None and 400 <= (error.status_code or 0) < 500


def _create_settled(create: Any) -> bool:
    """Whether ``create``'s polling method holds a final state; a failed read counts as not."""
    try:
        return bool(create.polling_method().finished())
    except Exception:
        return False


def _create_succeeded(create: Any) -> bool:
    """Whether ``create``'s polling method holds the ``Succeeded`` Azure reported; a failed read counts as not."""
    try:
        return str(create.polling_method().status()).lower() == "succeeded"
    except Exception:
        return False


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
    and the poke's nonce name on the ``_transient_route`` exit and the
    poke's own attempt cleanup. A 404 is fine and silent (the rule was
    already gone: a hook re-run, a VM that never had it, or a create that
    never landed server-side). Any other failure warns instead of raising:
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

    Ctrl-C and the terminations wait for the removal to finish
    (:func:`~agentworks.interrupts.interrupts_deferred`), since a removal cut
    short leaves the rule open. So it waits at most
    ``_OPERATION_TIMEOUT_SECONDS`` for Azure on the wall clock, however
    slowly the delete request is answered (see :func:`_bounded_route_call`),
    and that is as long as the operator waits; an operation still running
    then is an outcome nobody saw, so it warns that the rule may remain.
    """
    with interrupts_deferred():
        _delete_ssh_allow(network, rg, name, rule_name, prefixes)


def _delete_ssh_allow(
    network: NetworkManagementClient, rg: str, name: str, rule_name: str, prefixes: list[str] | None
) -> None:
    from azure.core.exceptions import ResourceNotFoundError

    scope = f"source(s) {', '.join(prefixes)}" if prefixes else "the rule's scoped source addresses"
    try:
        _run_route_operation(
            partial(network.security_rules.begin_delete, rg, f"{name}-nsg", rule_name, **_route_operation_options()),
            f"removing the '{rule_name}' rule on NSG '{name}-nsg'",
        )
    except ResourceNotFoundError:
        return
    except _RouteCallTimeout as timeout:
        output.warn(
            f"Azure did not confirm the removal of the '{rule_name}' rule on NSG '{name}-nsg' within "
            f"{timeout.seconds:.0f}s, so the VM's SSH port may remain open to {scope}; check "
            "the rule and delete it manually if it is still there."
        )
        return
    except Exception as exc:
        err = wrap_azure_error(exc)
        output.warn(
            f"could not remove the '{rule_name}' rule on NSG "
            f"'{name}-nsg': {err.summary}. The VM's SSH port stays open to "
            f"{scope}; delete the rule manually to restore zero inbound exposure."
        )
        return
    output.info(f"Closed SSH route (removed allow rule '{rule_name}')")


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
    from azure.mgmt.network.models import (
        PublicIPAddress,
        PublicIPAddressPropertiesFormat,
        PublicIPAddressSku,
    )

    try:
        nic = _bounded_route_call(
            partial(network.network_interfaces.get, rg, f"{name}-nic", **_route_call_options()),
            f"reading the NIC '{name}-nic'",
        )
        if nic.ip_configurations and nic.ip_configurations[0].public_ip_address is not None:
            return

        output.info("Restoring missing public IP...")
        location = _get_vm_location(compute, rg, name)
        ip_result = _run_route_operation(
            partial(
                network.public_ip_addresses.begin_create_or_update,
                rg,
                f"{name}-ip",
                PublicIPAddress(
                    location=location,
                    sku=PublicIPAddressSku(name="Standard"),
                    properties=PublicIPAddressPropertiesFormat(public_ip_allocation_method="Static"),
                    tags={"owner": "agentworks"},
                ),
                **_route_operation_options(),
            ),
            f"creating the public IP '{name}-ip'",
        )

        if nic.ip_configurations:
            nic.ip_configurations[0].public_ip_address = PublicIPAddress(id=ip_result.id)
        _run_route_operation(
            partial(
                network.network_interfaces.begin_create_or_update, rg, f"{name}-nic", nic, **_route_operation_options()
            ),
            f"attaching the public IP to '{name}-nic'",
        )

    except Exception as exc:
        raise wrap_azure_error(exc) from exc


def get_vm_public_ip(compute: ComputeManagementClient, network: NetworkManagementClient, rg: str, name: str) -> str:
    """The VM's public IP address, read live through its NIC, or ``""`` when it has none.

    It is the route's address read, so each of its reads (the VM, its NIC,
    the public IP) is bounded as a route call.
    """
    vm_info = _bounded_route_call(
        partial(compute.virtual_machines.get, rg, name, expand="instanceView", **_route_call_options()),
        f"reading the VM '{name}'",
    )
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

        nic = _bounded_route_call(
            partial(network.network_interfaces.get, nic_rg, nic_name, **_route_call_options()),
            f"reading the NIC '{nic_name}'",
        )
        for ip_config in nic.ip_configurations or []:
            pip_ref = ip_config.public_ip_address
            if pip_ref and pip_ref.id:
                pip_parts = pip_ref.id.split("/")
                pip_rg_idx = next(i for i, p in enumerate(pip_parts) if p.lower() == "resourcegroups")
                pip_rg = pip_parts[pip_rg_idx + 1]
                pip_name = pip_parts[-1]
                pip = _bounded_route_call(
                    partial(network.public_ip_addresses.get, pip_rg, pip_name, **_route_call_options()),
                    f"reading the public IP '{pip_name}'",
                )
                if pip.properties and pip.properties.ip_address:
                    return pip.properties.ip_address
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
    needs. A VM delete failure inside the teardown warns here, using the
    captured failure: this path is already unwinding on the operator's
    interrupt, so raising like the delete op does would replace it, but a
    silent orphan is never acceptable.
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
    the caller's (cached) compute client, bounded as a route call."""
    vm_info = _bounded_route_call(
        partial(compute.virtual_machines.get, rg, name, **_route_call_options()), f"reading the VM '{name}'"
    )
    return vm_info.location or "eastus"
