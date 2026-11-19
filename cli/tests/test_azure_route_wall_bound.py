"""Each Azure call on a route ends at its bound on the wall clock, however slowly Azure answers.

azure-core's per-try timeouts bound only silence, so a request whose answer
keeps arriving, or never arrives over a connection that stays up, could hold
a call, and a held interrupt, far past ``_OPERATION_TIMEOUT_SECONDS``. Each
case drives the generated client over an adapter that holds its request
until the test ends, with the bound shrunk and counted from the hold, and
checks that the caller returns at the bound. How the bound itself is kept
is :mod:`~agentworks.capabilities.vm_platform.wall_bound`'s, tested with it.
"""

from __future__ import annotations

import signal
import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.network import NetworkManagementClient

from agentworks import deadline as deadline_module
from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import wall_bound
from agentworks.deadline import Deadline
from agentworks.plugins.azure import network as azure_network
from agentworks.plugins.azure import route_calls
from agentworks.plugins.azure.errors import AzureError
from agentworks.plugins.azure.network import TRANSIENT_ALLOW_RULE_PREFIX
from agentworks.plugins.azure.route_calls import _RouteCallTimeout
from tests._azure_platform_support import (
    _fake_vm,
    _FakeClock,
    _FakeSecurityRules,
    _install_fakes,
    _platform,
    _Poller,
)
from tests._azure_platform_support import _stub_egress_detection as _stub_egress_detection
from tests._azure_scripted_transport import _HeldAdapter, _HeldClock, _scripted_client

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agentworks.db import VMRow
    from tests.conftest import CapturedOutput

_PREFIX = "198.18.0.7/32"
_RULE = f"{TRANSIENT_ALLOW_RULE_PREFIX}held"

# The shrunk bound, and how long a held request waits for release before it is answered anyway, so code that
# waits for the request itself fails the test rather than hanging it. A caller that returned well before the
# cap returned at the bound.
_BOUND = 0.2
_CAP = 10.0


@pytest.fixture
def held(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., _HeldAdapter]]:
    """Makes a held adapter whose hold starts the bound, and at teardown releases it and joins its senders.

    So no call outlives the test, and a loaded host slow to send the request cannot spend the bound first.
    """
    adapters: list[_HeldAdapter] = []

    def _make(on_hold: Callable[[], None] | None = None) -> _HeldAdapter:
        adapters.append(_HeldAdapter(cap=_CAP, on_hold=on_hold))
        monkeypatch.setattr(wall_bound, "time", _HeldClock(adapters[-1]))
        return adapters[-1]

    yield _make
    for adapter in adapters:
        adapter.finish()


@pytest.fixture
def bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(route_calls, "_OPERATION_TIMEOUT_SECONDS", _BOUND)


def _ctrl_c() -> None:
    signal.raise_signal(signal.SIGINT)


@pytest.mark.usefixtures("bound")
def test_a_removal_whose_delete_outlives_the_bound_warns_before_the_held_interrupt_lands(
    held: Callable[..., _HeldAdapter], captured_output: CapturedOutput
) -> None:
    """Ctrl-C while the delete is held waits for the bound, not the request, and the warning goes out first."""
    network = _scripted_client(NetworkManagementClient, held(on_hold=_ctrl_c))

    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        azure_network.remove_ssh_allow(network, "rg1", "vm1", _RULE, [_PREFIX])
    elapsed = time.monotonic() - started

    assert _BOUND <= elapsed < _CAP / 2
    (warning,) = captured_output.warnings
    # The same warning as a removal Azure accepted but did not finish within the bound: either way nobody saw it end.
    unfinished = SimpleNamespace(
        security_rules=SimpleNamespace(begin_delete=lambda *_a, **_k: _Poller(None, finishes=False))
    )
    azure_network.remove_ssh_allow(unfinished, "rg1", "vm1", _RULE, [_PREFIX])  # type: ignore[arg-type]
    assert captured_output.warnings == [warning, warning]


@pytest.mark.usefixtures("bound")
def test_a_poke_whose_create_outlives_the_bound_removes_and_names_its_rule_without_another_attempt(
    monkeypatch: pytest.MonkeyPatch, held: Callable[..., _HeldAdapter], captured_output: CapturedOutput
) -> None:
    """With no poller there is nothing to settle; the create may still land, so the rule goes and is named."""
    adapter = held()
    fakes = _install_fakes(monkeypatch)
    # The held request ends the bound on the wall clock.
    monkeypatch.setattr(azure_network, "time", time)
    monkeypatch.setattr(deadline_module, "time", time)
    rules = fakes.network.security_rules
    monkeypatch.setattr(
        rules,
        "begin_create_or_update",
        _scripted_client(NetworkManagementClient, adapter).security_rules.begin_create_or_update,
    )

    started = time.monotonic()
    with pytest.raises(_RouteCallTimeout):
        azure_network.poke_ssh_allow(fakes.network, "rg1", "vm1", [_PREFIX])  # type: ignore[arg-type]
    elapsed = time.monotonic() - started

    assert elapsed < _CAP / 2
    assert adapter.methods == ["PUT"]
    ((_rg, _nsg, attempted),) = rules.deletes()
    assert attempted.startswith(TRANSIENT_ALLOW_RULE_PREFIX)
    (warning,) = captured_output.warnings
    assert attempted in warning and _PREFIX in warning


def _read_nic(adapter: _HeldAdapter) -> None:
    network = _scripted_client(NetworkManagementClient, adapter)
    azure_network.ensure_public_ip(network, None, "rg1", "vm1")  # type: ignore[arg-type]


def _read_location(adapter: _HeldAdapter) -> None:
    azure_network._get_vm_location(_scripted_client(ComputeManagementClient, adapter), "rg1", "vm1")


def _list_rules(adapter: _HeldAdapter) -> None:
    azure_network._lowest_free_allow_priority(_scripted_client(NetworkManagementClient, adapter), "rg1", "vm1")


def _read_address(adapter: _HeldAdapter) -> None:
    azure_network.get_vm_public_ip(_scripted_client(ComputeManagementClient, adapter), None, "rg1", "vm1")  # type: ignore[arg-type]


@pytest.mark.usefixtures("bound")
@pytest.mark.parametrize(
    "read",
    [_read_nic, _read_location, _list_rules, _read_address],
    ids=["nic", "vm-location", "rule-list", "public-address"],
)
def test_a_read_whose_request_outlives_the_bound_fails_at_it(
    held: Callable[..., _HeldAdapter], read: Callable[[_HeldAdapter], Any]
) -> None:
    """The rule list's requests go out as it is iterated, so the bound covers the iteration too."""
    adapter = held()

    started = time.monotonic()
    with pytest.raises(AzureError) as caught:
        read(adapter)
    elapsed = time.monotonic() - started

    assert elapsed < _CAP / 2
    assert adapter.methods == ["GET"]
    assert _RouteCallTimeout in {type(caught.value), type(caught.value.__cause__)}


def test_ctrl_c_ends_an_operations_wait_promptly() -> None:
    """The wait for a long-running operation is a bounded call too, so a Ctrl-C another thread received ends it."""
    release = threading.Event()

    class _Running(_Poller):
        def result(self, timeout: float | None = None) -> object:
            release.wait(_CAP if timeout is None else min(timeout, _CAP))
            return None

    def _ctrl_c_once_waiting() -> None:
        time.sleep(1.0)
        _ctrl_c()

    threading.Thread(target=_ctrl_c_once_waiting, daemon=True).start()
    started = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            route_calls._run_route_operation(lambda: _Running(None, finishes=False), "waiting for an operation")
    finally:
        release.set()
    elapsed = time.monotonic() - started

    assert elapsed < _CAP / 2


class TestRouteOperationsAreBounded:
    """A route opens before the script's deadline starts, so each Azure operation it waits on has a bound."""

    def test_every_call_on_a_route_is_bounded_and_only_the_pokes_create_goes_unretried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each call runs as a bounded call, with per-call bounds on its tries; a poll keeps the client's."""
        from azure.mgmt.core.polling.arm_polling import ARMPolling

        fakes = _install_fakes(monkeypatch, public_ip_attached=False)
        network = fakes.network
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        platform = _platform()
        with platform._transient_route(vm, RunContext()):
            platform._public_address(vm, RunContext())

        calls = [
            *network.network_interfaces.calls,
            *fakes.compute.virtual_machines.calls,
            *network.public_ip_addresses.calls,
            *network.security_rules.calls,
        ]
        assert {call.method for call in calls} == {"get", "list", "begin_create_or_update", "begin_delete"}
        assert {call.thread for call in calls} == {wall_bound.THREAD_NAME}
        _deny_create, poke_create = (
            call.options for call in network.security_rules.calls if call.method == "begin_create_or_update"
        )
        assert poke_create["retry_total"] == 0
        for method, options, _thread in calls:
            assert options["connection_timeout"] == route_calls._ROUTE_CONNECTION_TIMEOUT_SECONDS
            assert options["read_timeout"] == route_calls._ROUTE_READ_TIMEOUT_SECONDS
            if options is not poke_create:
                assert options["retry_total"] == route_calls._ROUTE_REQUEST_RETRIES
            # A begin_* call hands its options on to the polling it builds unless it is given one.
            assert isinstance(options.get("polling"), ARMPolling) == method.startswith("begin_")

    def test_a_removal_waits_only_for_what_its_first_request_left_of_the_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fakes = _install_fakes(monkeypatch)
        waits: list[float | None] = []

        class _WaitRecorded(_Poller):
            def result(self, timeout: float | None = None) -> object:
                waits.append(timeout)
                return super().result(timeout)

        def _delete(rg: str, nsg: str, rule_name: str, **options: Any) -> _Poller:
            fakes.clock.now += 250.0  # the delete request's tries
            return _WaitRecorded(None, finishes=False)  # still running, so the wait is made

        monkeypatch.setattr(fakes.network.security_rules, "begin_delete", _delete)
        azure_network.remove_ssh_allow(fakes.network, "rg1", "vm1", f"{TRANSIENT_ALLOW_RULE_PREFIX}x")  # type: ignore[arg-type]

        assert waits == [route_calls._OPERATION_TIMEOUT_SECONDS - 250.0]

    @staticmethod
    def _poke_with(
        monkeypatch: pytest.MonkeyPatch, poller_type: type[_Poller], order: list[str] | None = None
    ) -> tuple[_FakeSecurityRules, _FakeClock]:
        fakes = _install_fakes(monkeypatch)
        rules: _FakeSecurityRules = fakes.network.security_rules
        real_create, real_delete = rules.begin_create_or_update, rules.begin_delete

        def _create(rg: str, nsg: str, rule_name: str, rule: object, **options: Any) -> _Poller:
            poller = real_create(rg, nsg, rule_name, rule)
            return poller_type(rule) if rule_name.startswith(TRANSIENT_ALLOW_RULE_PREFIX) else poller

        def _delete(rg: str, nsg: str, rule_name: str, **options: Any) -> _Poller:
            if order is not None and rule_name.startswith(TRANSIENT_ALLOW_RULE_PREFIX):
                order.append("deleted")
            return real_delete(rg, nsg, rule_name)

        monkeypatch.setattr(rules, "begin_create_or_update", _create)
        monkeypatch.setattr(rules, "begin_delete", _delete)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]
        with pytest.raises(_RouteCallTimeout), _platform()._transient_route(vm, RunContext()):
            pytest.fail("the body must not run when the poke does not finish")
        return rules, fakes.clock

    def test_a_poke_whose_create_finishes_late_is_removed_only_after_it_finishes(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """A delete racing the create can fail or find nothing and let the rule land after it."""
        order: list[str] = []
        waits: list[float | None] = []

        class _LatePoller(_Poller):
            def __init__(self, value: object) -> None:
                super().__init__(value, finishes=False)
                self.reads = 0

            def result(self, timeout: float | None = None) -> object:
                waits.append(timeout)
                return super().result(timeout)

            def finished(self) -> bool:
                self.reads += 1
                if self.reads == 3:  # the wait ran out; Azure reports the create succeeded a little later
                    order.append("created")
                return self.reads >= 3

            def status(self) -> str:
                return "Succeeded" if self.reads >= 3 else "InProgress"

        rules, _clock = self._poke_with(monkeypatch, _LatePoller, order)

        assert order == ["created", "deleted"]
        assert rules.transient_names()[0] in {d[2] for d in rules.deletes()}
        assert waits and all(wait is not None for wait in waits)
        assert captured_output.warnings == []

    @pytest.mark.parametrize("hung", ["deny", "public-ip"])
    def test_a_heal_or_convergence_that_does_not_finish_fails_the_route_before_any_poke(
        self, monkeypatch: pytest.MonkeyPatch, hung: str
    ) -> None:
        network = _install_fakes(monkeypatch, public_ip_attached=hung != "public-ip").network
        rules: _FakeSecurityRules = network.security_rules
        hung_poller = _Poller(SimpleNamespace(ip_address="203.0.113.5", id="/pip/id"), finishes=False)
        if hung == "deny":
            monkeypatch.setattr(rules, "begin_create_or_update", lambda *_args, **_options: hung_poller)
        else:
            monkeypatch.setattr(
                network.public_ip_addresses, "begin_create_or_update", lambda *_args, **_options: hung_poller
            )
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with pytest.raises(AzureError) as caught, _platform()._transient_route(vm, RunContext()):
            pytest.fail("the body must not run when the route cannot open")

        # Both wrap their failures, the timeout among them.
        assert isinstance(caught.value.__cause__, _RouteCallTimeout)
        assert not rules.transient_names()

    def test_a_poller_already_done_returns_its_result_with_none_of_the_bound_left(self) -> None:
        """The first request can spend the whole bound on an operation Azure finished at once."""

        class _Done(_Poller):
            def result(self, timeout: float | None = None) -> object:
                time.sleep(0.05)  # longer than a wait with nothing left of the bound
                return super().result(timeout)

        spent = Deadline.after(0.0)
        assert route_calls._finish_route_operation(_Done("rule"), "creating a rule", spent) == "rule"
