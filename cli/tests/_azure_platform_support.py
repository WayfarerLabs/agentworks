"""Shared Azure SDK fakes for the azure platform test files.

``test_azure_nsg_exposure.py`` grew the recording fakes for the SDK
surfaces the platform imports function-locally; the #338 interrupt
rollback tests (``test_azure_create_interrupt.py``) assert on the same
shapes plus the delete side, so the fakes live here and both files
import them. Installation patches the SDK symbols on the real modules
(``monkeypatch.setattr``, matching test_azure_credential_caching.py),
so no test touches Azure.

Error injection is typed ``BaseException`` (not ``Exception``) because
the interrupt tests inject ``KeyboardInterrupt`` mid-call.
"""

from __future__ import annotations

import builtins
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from agentworks.plugins.azure.network import TRANSIENT_ALLOW_RULE_PREFIX

if TYPE_CHECKING:
    import pytest

# The resource ID the fake VM create returns; shared with the test
# files' VMRow stand-ins so route ops parse back to the same rg/name.
_RESOURCE_ID = "/subscriptions/sub-A/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1"


def _authorization_denied() -> Exception:
    """An ARM RBAC rejection as the SDK raises it: ``HttpResponseError``
    carrying the documented ``AuthorizationFailed`` error code. Shared
    by the #329 delete-verify suite and the manager-seam integration pin
    in ``tests/vms/test_delete_vm_gating.py``."""
    from azure.core.exceptions import HttpResponseError

    exc = HttpResponseError(message="denied")
    exc.error = SimpleNamespace(  # type: ignore[assignment]
        code="AuthorizationFailed",
        message="The client does not have authorization to perform action 'Microsoft.Compute/virtualMachines/delete'.",
        details=None,
    )
    return exc


class _Poller:
    """A begin_* long-running-operation stub: ``.result()`` yields a value."""

    def __init__(self, value: object) -> None:
        self._value = value

    def result(self) -> object:
        return self._value


class _FakeSecurityRules:
    """Recording stub for ``network.security_rules``; per-test error
    injection drives the 404-tolerance and removal-failure paths. The
    shared ``events`` list records creations and deletions in call
    order so ordering contracts (deny re-pin before allow poke) can be
    asserted."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, str, Any]] = []
        self.create_error: BaseException | None = None
        self.delete_error: BaseException | None = None
        # Live rules by name: what list() serves the poke's slot
        # allocation. Creates upsert, deletes pop; tests may pre-seed
        # (e.g. a legacy deny at 100, or a full band).
        self.rules: dict[str, Any] = {}

    def begin_create_or_update(self, rg: str, nsg: str, rule_name: str, rule: object) -> _Poller:
        if self.create_error is not None:
            raise self.create_error
        self.events.append(("create", rg, nsg, rule_name, rule))
        self.rules[rule_name] = rule
        return _Poller(rule)

    def begin_delete(self, rg: str, nsg: str, rule_name: str) -> _Poller:
        if self.delete_error is not None:
            raise self.delete_error
        self.events.append(("delete", rg, nsg, rule_name, None))
        self.rules.pop(rule_name, None)
        return _Poller(None)

    def creates(self) -> list[tuple[str, str, str, Any]]:
        return [(e[1], e[2], e[3], e[4]) for e in self.events if e[0] == "create"]

    def deletes(self) -> list[tuple[str, str, str]]:
        return [(e[1], e[2], e[3]) for e in self.events if e[0] == "delete"]

    def transient_names(self) -> list[str]:
        """Names of every transient allow created so far, in call order."""
        return [e[3] for e in self.events if e[0] == "create" and e[3].startswith(TRANSIENT_ALLOW_RULE_PREFIX)]

    # The SDK method is genuinely named ``list``; annotate via builtins
    # so the method does not shadow the builtin in this class's other
    # annotations (mypy resolves them in class scope).
    def list(self, rg: str, nsg: str) -> builtins.list[Any]:
        return [rule for rule in self.rules.values()]


class _FakePublicIps:
    def __init__(self, events: list[tuple[str, str, str, str]] | None = None) -> None:
        self.created: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self._events = events if events is not None else []

    def begin_create_or_update(self, rg: str, name: str, params: object) -> _Poller:
        self.created.append((rg, name))
        return _Poller(SimpleNamespace(ip_address="203.0.113.5", id="/pip/id"))

    def begin_delete(self, rg: str, name: str) -> _Poller:
        self.deleted.append((rg, name))
        self._events.append(("delete", "ip", rg, name))
        return _Poller(None)


class _FakeNics:
    """One NIC whose single ip-configuration either carries a public IP
    reference (the permanent-IP steady state) or lacks one (a VM created
    under the old detach scheme, awaiting the heal)."""

    def __init__(self, *, public_ip_attached: bool, events: list[tuple[str, str, str, str]] | None = None) -> None:
        self.updated: list[tuple[str, str, Any]] = []
        self.deleted: list[tuple[str, str]] = []
        self.create_error: BaseException | None = None
        self.delete_error: BaseException | None = None
        self._events = events if events is not None else []
        pip = SimpleNamespace(id="/pip/id") if public_ip_attached else None
        self.nic = SimpleNamespace(ip_configurations=[SimpleNamespace(public_ip_address=pip)])

    def get(self, rg: str, name: str) -> Any:
        return self.nic

    def begin_create_or_update(self, rg: str, name: str, nic: object) -> _Poller:
        if self.create_error is not None:
            raise self.create_error
        self.updated.append((rg, name, nic))
        return _Poller(SimpleNamespace(id="/nic/id"))

    def begin_delete(self, rg: str, name: str) -> _Poller:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append((rg, name))
        self._events.append(("delete", "nic", rg, name))
        return _Poller(None)


class _FakeNsgs:
    def __init__(self, events: list[tuple[str, str, str, str]] | None = None) -> None:
        self.created: list[tuple[str, str, Any]] = []
        self.deleted: list[tuple[str, str]] = []
        self._events = events if events is not None else []

    def begin_create_or_update(self, rg: str, name: str, nsg: object) -> _Poller:
        self.created.append((rg, name, nsg))
        return _Poller(SimpleNamespace(id="/nsg/id"))

    def begin_delete(self, rg: str, name: str) -> _Poller:
        self.deleted.append((rg, name))
        self._events.append(("delete", "nsg", rg, name))
        return _Poller(None)


class _FakeVnets:
    def __init__(self, events: list[tuple[str, str, str, str]] | None = None) -> None:
        self.deleted: list[tuple[str, str]] = []
        self._events = events if events is not None else []

    def begin_create_or_update(self, rg: str, name: str, vnet: object) -> _Poller:
        return _Poller(SimpleNamespace(subnets=[SimpleNamespace(id="/subnet/id")]))

    def begin_delete(self, rg: str, name: str) -> _Poller:
        self.deleted.append((rg, name))
        self._events.append(("delete", "vnet", rg, name))
        return _Poller(None)


class _FakeVMs:
    """Compute stub for the create path: no VM pre-exists (the collision
    check sees the get raise) and creates succeed. ``delete_error``
    drives the second-interrupt-during-cleanup path. The delete-verify
    suite (#329) reuses ``delete_error`` as the failed-backend-delete
    seam, with ``get`` answering the did-the-VM-survive probe."""

    def __init__(self, events: list[tuple[str, str, str, str]] | None = None) -> None:
        self.deleted: list[tuple[str, str]] = []
        self.delete_error: BaseException | None = None
        self._events = events if events is not None else []

    def get(self, rg: str, name: str, **_kw: object) -> Any:
        from azure.core.exceptions import ResourceNotFoundError

        raise ResourceNotFoundError("no such VM")

    def begin_create_or_update(self, rg: str, name: str, vm: object) -> _Poller:
        return _Poller(SimpleNamespace(id=_RESOURCE_ID))

    def begin_delete(self, rg: str, name: str) -> _Poller:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append((rg, name))
        self._events.append(("delete", "vm", rg, name))
        return _Poller(None)


class _FakeVMsWithLocation(_FakeVMs):
    """Compute stub for the route path: location reads serve the heal's
    public-IP region lookup."""

    def get(self, rg: str, name: str, **_kw: object) -> Any:
        return SimpleNamespace(location="eastus")


class _FakeDisks:
    """Compute ``disks`` stub for the cleanup sweep: serves whatever the
    test pre-seeds into ``disks`` (default none) and records deletes;
    ``delete_error`` drives the sweep's 404-tolerance and
    straggler-warning paths."""

    def __init__(self, events: list[tuple[str, str, str, str]] | None = None) -> None:
        self.disks: list[Any] = []
        self.deleted: list[tuple[str, str]] = []
        self.delete_error: BaseException | None = None
        self._events = events if events is not None else []

    def list_by_resource_group(self, rg: str) -> list[Any]:
        return list(self.disks)

    def begin_delete(self, rg: str, name: str) -> _Poller:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append((rg, name))
        self._events.append(("delete", "disk", rg, name))
        return _Poller(None)


class _FakeNetwork(SimpleNamespace):
    pass


class _FakeCompute(SimpleNamespace):
    pass


class _FakeAzure(SimpleNamespace):
    """What ``_install_fakes`` returns: the network and compute holders
    the tests configure and assert on, plus ``events``, the shared
    cross-client delete-sequence log."""


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    public_ip_attached: bool = True,
    vm_exists_lookup: bool = True,
) -> _FakeAzure:
    """Patch the Azure SDK symbols the plugin imports, returning the
    fake client holders the tests configure and assert on.
    ``vm_exists_lookup`` picks the compute stub: location reads for the
    heal (True) vs. the create path's not-found collision check (False).
    The delete-verify suite (#329) reads the same switch as the probe's
    answer to "did the VM survive the teardown?": True serves the VM
    back (it survived), False raises the SDK's not-found (it is gone).
    """

    # One cross-client sequence log shared by every delete-capable fake
    # (the _FakeSecurityRules.events pattern), so ordering contracts
    # that span clients (the VM-first-then-sweep rollback) are pinnable.
    events: list[tuple[str, str, str, str]] = []
    network = _FakeNetwork(
        public_ip_addresses=_FakePublicIps(events),
        network_interfaces=_FakeNics(public_ip_attached=public_ip_attached, events=events),
        network_security_groups=_FakeNsgs(events),
        virtual_networks=_FakeVnets(events),
        security_rules=_FakeSecurityRules(),
    )
    compute = _FakeCompute(
        virtual_machines=_FakeVMsWithLocation(events) if vm_exists_lookup else _FakeVMs(events),
        disks=_FakeDisks(events),
    )

    class _FakeDefaultCred:
        def get_token(self, *_scopes: str, **_kw: object) -> object:
            return SimpleNamespace(token="tok", expires_on=0)

    class _FakeComputeClient:
        def __init__(self, credential: object, subscription_id: str) -> None:
            self.virtual_machines = compute.virtual_machines
            self.disks = compute.disks

    class _FakeNetworkClient:
        def __init__(self, credential: object, subscription_id: str) -> None:
            self.public_ip_addresses = network.public_ip_addresses
            self.network_interfaces = network.network_interfaces
            self.network_security_groups = network.network_security_groups
            self.virtual_networks = network.virtual_networks
            self.security_rules = network.security_rules

    monkeypatch.setattr("azure.identity.DefaultAzureCredential", _FakeDefaultCred)
    monkeypatch.setattr("azure.mgmt.compute.ComputeManagementClient", _FakeComputeClient)
    monkeypatch.setattr("azure.mgmt.network.NetworkManagementClient", _FakeNetworkClient)
    return _FakeAzure(network=network, compute=compute, events=events)
