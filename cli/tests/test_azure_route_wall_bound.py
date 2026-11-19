"""Each Azure call on a route ends at its bound on the wall clock, however slowly Azure answers.

azure-core's per-try timeouts bound only silence, so a request whose answer
keeps arriving, or never arrives over a connection that stays up, could hold
a call, and a held interrupt, far past ``_OPERATION_TIMEOUT_SECONDS``. Each
case drives the generated client over an adapter that holds its request
until the test ends, with the bound shrunk, and checks that the caller
returns at the bound.
"""

from __future__ import annotations

import signal
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.network import NetworkManagementClient

from agentworks.plugins.azure import network as azure_network
from agentworks.plugins.azure import route_calls
from agentworks.plugins.azure.errors import AzureError
from agentworks.plugins.azure.network import TRANSIENT_ALLOW_RULE_PREFIX
from agentworks.plugins.azure.route_calls import _RouteOperationTimeout
from tests._azure_platform_support import _install_fakes, _Poller
from tests._azure_scripted_transport import _HeldAdapter, _scripted_client

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from tests.conftest import CapturedOutput

_PREFIX = "198.18.0.7/32"
_RULE = f"{TRANSIENT_ALLOW_RULE_PREFIX}held"

# The shrunk bound, and how long a held request waits for release before it is answered anyway, so code that
# waits for the request itself fails the test rather than hanging it. A caller that returned well before the
# cap returned at the bound.
_BOUND = 0.2
_CAP = 10.0


@pytest.fixture
def held() -> Iterator[Callable[..., _HeldAdapter]]:
    """Makes held adapters, and releases each at teardown and joins its senders so no call outlives the test."""
    adapters: list[_HeldAdapter] = []

    def _make(on_hold: Callable[[], None] | None = None) -> _HeldAdapter:
        adapters.append(_HeldAdapter(cap=_CAP, on_hold=on_hold))
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
    monkeypatch.setattr(route_calls, "time", time)
    rules = fakes.network.security_rules
    monkeypatch.setattr(
        rules,
        "begin_create_or_update",
        _scripted_client(NetworkManagementClient, adapter).security_rules.begin_create_or_update,
    )

    started = time.monotonic()
    with pytest.raises(_RouteOperationTimeout):
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


@pytest.mark.usefixtures("bound")
@pytest.mark.parametrize("read", [_read_nic, _read_location, _list_rules], ids=["nic", "vm-location", "rule-list"])
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
    assert _RouteOperationTimeout in {type(caught.value), type(caught.value.__cause__)}


def test_ctrl_c_ends_a_route_call_at_once(held: Callable[..., _HeldAdapter]) -> None:
    """Interrupts are not held for a route call, even one whose signal reaches the call's own thread.

    The signal comes once the caller is surely waiting, since one that lands before is handled at once anyway.
    """

    def _ctrl_c_while_waiting() -> None:
        time.sleep(1.0)
        _ctrl_c()

    adapter = held(on_hold=_ctrl_c_while_waiting)

    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        _list_rules(adapter)
    elapsed = time.monotonic() - started

    assert elapsed < _CAP / 2
