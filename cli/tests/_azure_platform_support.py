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

The route tests' shared setup lives here too: the egress-detection stub a
test module applies by importing ``_stub_egress_detection``, the
platform and VM stand-ins, and the NSG rule-shape checks.
"""

from __future__ import annotations

import builtins
import threading
from types import SimpleNamespace
from typing import Any, NamedTuple

import pytest

from agentworks.capabilities.vm_platform import ssh_exposure
from agentworks.plugins.azure import network as azure_network
from agentworks.plugins.azure import route_calls
from agentworks.plugins.azure.network import (
    ALLOW_PRIORITY_BAND_END,
    ALLOW_PRIORITY_BAND_START,
    ALLOW_RULE_DESCRIPTION_MARKER,
    DENY_ALL_INBOUND_RULE_NAME,
    DENY_ALL_INBOUND_RULE_PRIORITY,
    TRANSIENT_ALLOW_RULE_PREFIX,
)
from agentworks.plugins.azure.platform import AzureVMPlatform

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


class _Call(NamedTuple):
    """One recorded SDK call: its method, its azure-core per-call options, and the thread it ran on."""

    method: str
    options: dict[str, Any]
    thread: str


def _called(method: str, options: dict[str, Any]) -> _Call:
    """``method``'s call with ``options``, on the thread that is making it."""
    return _Call(method, options, threading.current_thread().name)


class _Poller:
    """A begin_* long-running-operation stub: ``.result()`` yields a value, or with
    ``finishes=False`` stands for an operation still running when its wait ends.
    Its polling method is itself, whose ``finished()`` says whether it holds a
    final state and whose ``status()`` is that state, as azure-core's polling
    method does: ``Succeeded`` for an operation that finishes."""

    def __init__(self, value: object, *, finishes: bool = True) -> None:
        self._value = value
        self._finishes = finishes

    def result(self, timeout: float | None = None) -> object:
        return self._value if self._finishes else None

    def done(self) -> bool:
        return self._finishes

    def polling_method(self) -> _Poller:
        return self

    def finished(self) -> bool:
        return self._finishes

    def status(self) -> str:
        return "Succeeded" if self._finishes else "InProgress"


class _FakeClock:
    """Stands in for the ``time`` module the network helpers wait with: a sleep
    advances ``now`` at once, so a bounded wait spends its whole bound instantly."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _FakeSecurityRules:
    """Recording stub for ``network.security_rules``; per-test error
    injection drives the 404-tolerance and removal-failure paths. The
    shared ``events`` list records creations and deletions in call
    order so ordering contracts (deny re-pin before allow poke) can be
    asserted. ``calls`` records each call's method, azure-core per-call
    options and thread, in call order."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, str, Any]] = []
        self.calls: list[_Call] = []
        self.create_error: BaseException | None = None
        self.delete_error: BaseException | None = None
        # Live rules by name: what list() serves the poke's slot
        # allocation. Creates upsert, deletes pop; tests may pre-seed
        # (e.g. a legacy deny at 100, or a full band).
        self.rules: dict[str, Any] = {}

    def begin_create_or_update(self, rg: str, nsg: str, rule_name: str, rule: object, **options: Any) -> _Poller:
        self.calls.append(_called("begin_create_or_update", options))
        if self.create_error is not None:
            raise self.create_error
        self.events.append(("create", rg, nsg, rule_name, rule))
        self.rules[rule_name] = rule
        return _Poller(rule)

    def begin_delete(self, rg: str, nsg: str, rule_name: str, **options: Any) -> _Poller:
        self.calls.append(_called("begin_delete", options))
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
    def list(self, rg: str, nsg: str, **options: Any) -> builtins.list[Any]:
        self.calls.append(_called("list", options))
        return [rule for rule in self.rules.values()]


class _FakePublicIps:
    def __init__(self, events: list[tuple[str, str, str, str]] | None = None) -> None:
        self.created: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self.calls: list[_Call] = []
        self._events = events if events is not None else []

    def begin_create_or_update(self, rg: str, name: str, params: object, **options: Any) -> _Poller:
        self.calls.append(_called("begin_create_or_update", options))
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
        self.calls: list[_Call] = []
        self._events = events if events is not None else []
        pip = SimpleNamespace(id="/pip/id") if public_ip_attached else None
        self.nic = SimpleNamespace(ip_configurations=[SimpleNamespace(public_ip_address=pip)])

    def get(self, rg: str, name: str, **options: Any) -> Any:
        self.calls.append(_called("get", options))
        return self.nic

    def begin_create_or_update(self, rg: str, name: str, nic: object, **options: Any) -> _Poller:
        self.calls.append(_called("begin_create_or_update", options))
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
        self.created: list[tuple[str, str, Any]] = []
        self.deleted: list[tuple[str, str]] = []
        self.delete_error: BaseException | None = None
        self._events = events if events is not None else []

    def get(self, rg: str, name: str, **_kw: object) -> Any:
        from azure.core.exceptions import ResourceNotFoundError

        raise ResourceNotFoundError("no such VM")

    def begin_create_or_update(self, rg: str, name: str, vm: object) -> _Poller:
        self.created.append((rg, name, vm))
        return _Poller(SimpleNamespace(id=_RESOURCE_ID))

    def begin_delete(self, rg: str, name: str) -> _Poller:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append((rg, name))
        self._events.append(("delete", "vm", rg, name))
        return _Poller(None)


class _FakeVMsWithLocation(_FakeVMs):
    """Compute stub for the route path: location reads serve the heal's
    public-IP region lookup, recording their per-call options."""

    def __init__(self, events: list[tuple[str, str, str, str]] | None = None) -> None:
        super().__init__(events)
        self.calls: list[_Call] = []

    def get(self, rg: str, name: str, **options: Any) -> Any:
        self.calls.append(_called("get", options))
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
    the tests configure and assert on, ``events``, the shared
    cross-client delete-sequence log, and ``clock``, the network
    helpers' time."""


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
    clock = _FakeClock()
    monkeypatch.setattr(azure_network, "time", clock)
    monkeypatch.setattr(route_calls, "time", clock)
    return _FakeAzure(network=network, compute=compute, events=events, clock=clock)


_CONFIG = {"subscription_id": "sub-A", "resource_group": "rg1", "region": "eastus", "auth": {"mode": "ambient"}}
_DETECTED = "198.18.0.7"
_DETECTED_PREFIX = f"{_DETECTED}/32"


@pytest.fixture(autouse=True)
def _stub_egress_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs with detection stubbed (never a live probe) and a
    clean per-process cache; individual tests override the stub to drive
    the failure branches."""
    monkeypatch.setattr(ssh_exposure, "_egress_ip_cache", None)
    monkeypatch.setattr(ssh_exposure, "detect_egress_ip", lambda: _DETECTED)
    monkeypatch.setattr(
        "agentworks.plugins.azure.platform.complete_cloud_bootstrap",
        lambda run_as_root, auth_key, **kwargs: "100.64.0.5",
    )


def _fail_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> str:
        raise OSError("no route to checkip")

    monkeypatch.setattr(ssh_exposure, "detect_egress_ip", _boom)


def _fake_vm() -> Any:
    """A stand-in for a VMRow carrying just what the network ops read."""
    return SimpleNamespace(
        name="vm1",
        admin_username="agentworks",
        platform_metadata={"resource_id": _RESOURCE_ID},
    )


def _operator_config(allow_cidrs: list[str] | None = None) -> Any:
    """A config stand-in carrying just ``operator.ssh_allow_cidrs``, the
    only operator field the route path reads."""
    return SimpleNamespace(operator=SimpleNamespace(ssh_allow_cidrs=allow_cidrs or []))


def _platform() -> AzureVMPlatform:
    return AzureVMPlatform("az-site", dict(_CONFIG))


def _assert_deny_shape(rule: Any) -> None:
    assert rule.name == DENY_ALL_INBOUND_RULE_NAME
    assert rule.priority == DENY_ALL_INBOUND_RULE_PRIORITY == 200
    assert rule.access == "Deny"
    assert rule.direction == "Inbound"
    assert rule.protocol == "*"
    assert rule.source_address_prefix == "*"
    assert rule.destination_address_prefix == "*"


def _assert_allow_shape(rule: Any, prefixes: list[str], *, name: str, priority: int) -> None:
    assert rule.name == name
    assert rule.priority == priority
    assert ALLOW_PRIORITY_BAND_START <= rule.priority <= ALLOW_PRIORITY_BAND_END
    assert rule.access == "Allow"
    assert rule.direction == "Inbound"
    assert rule.protocol == "Tcp"
    assert rule.destination_port_range == "22"
    assert rule.source_address_prefixes == prefixes
    assert rule.source_address_prefix is None
    assert rule.description.startswith(ALLOW_RULE_DESCRIPTION_MARKER)
