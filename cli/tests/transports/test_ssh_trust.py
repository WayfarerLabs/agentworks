"""``SSHTransport``'s trust binding: what each binding puts on the command line,
and that a VM with no host-key record spawns nothing and never retries."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from agentworks.execution import trust
from agentworks.execution.errors import TrustMissing
from agentworks.execution.trust import HOST_TRUST, TrustBinding, TrustMode
from agentworks.ssh import SSHError
from agentworks.transports import (
    SSHTransport,
    agent_transport,
    transport,
    transport_for_user,
)
from tests.conftest import seed_vm_trust
from tests.transports.conftest import ok_completed

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

VM_BINDING = TrustBinding(TrustMode.VM, "web")


def _vm_transport() -> SSHTransport:
    return SSHTransport(trust=VM_BINDING, host="100.64.0.9", user="agentworks")


def _store_options() -> list[str]:
    return list(trust.ssh_options(VM_BINDING))


def _vm_row(name: str = "web") -> Any:
    return SimpleNamespace(name=name, tailscale_host="100.64.0.9", admin_username="admin")


def _config() -> Any:
    return SimpleNamespace(operator=SimpleNamespace(ssh_private_key=None))


# -- The factories bind VM trust ---------------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        lambda vm, config: transport(vm, config),
        lambda vm, config: agent_transport(vm, config, SimpleNamespace(linux_user="agt-a1")),
        lambda vm, config: transport_for_user(vm, config, user="someone"),
    ],
    ids=["admin", "agent", "for-user"],
)
def test_the_canonical_factories_bind_vm_trust(build: Callable[[Any, Any], Any]) -> None:
    built = build(_vm_row("web"), _config())

    assert isinstance(built, SSHTransport)
    assert built.trust == TrustBinding(TrustMode.VM, "web")


def test_building_a_vm_transport_needs_no_record_but_using_it_does() -> None:
    built = transport(_vm_row("web"), _config())

    with patch("agentworks.transports.ssh.subprocess.run") as run, pytest.raises(TrustMissing):
        built.run("echo hi")
    run.assert_not_called()

    seed_vm_trust("web")
    with patch("agentworks.transports.ssh.subprocess.run", return_value=ok_completed()) as run:
        built.run("echo hi")
    assert run.call_count == 1


# -- A VM with no record spawns nothing -----------------------------------------------------


def test_every_spawn_path_refuses_without_a_record_and_spawns_nothing(tmp_path: Path) -> None:
    t = _vm_transport()
    attempts: list[Callable[[], object]] = [
        lambda: t.run("echo hi"),
        lambda: t.run("cat", input_text="payload"),
        lambda: t.run("cat", input_data="payload"),
        lambda: t.run("echo hi", tty=True),
        lambda: t.copy_to(tmp_path / "local", "/remote"),
        lambda: t.copy_from("/remote", tmp_path / "local"),
        lambda: t.call_streaming("echo hi"),
        lambda: t._interactive("echo hi"),
    ]

    with (
        patch("agentworks.transports.ssh.subprocess") as sub,
        patch("agentworks.ssh.subprocess") as sub_ssh,
    ):
        for attempt in attempts:
            with pytest.raises(TrustMissing) as caught:
                attempt()
            assert caught.value.vm_name == "web"

    for module in (sub, sub_ssh):
        assert not module.run.called
        assert not module.call.called
        assert not module.Popen.called


def test_a_missing_record_is_not_an_ssh_error_so_best_effort_handlers_cannot_absorb_it() -> None:
    t = _vm_transport()

    with pytest.raises(TrustMissing) as caught:
        t.run("echo hi")

    assert not isinstance(caught.value, SSHError)


# -- The store options reach every ssh and scp ----------------------------------------------


def _assert_store_options_before_target(argv: list[str]) -> None:
    for option in _store_options():
        assert option in argv
    assert "StrictHostKeyChecking=accept-new" not in argv
    assert argv.index("StrictHostKeyChecking=yes") < len(argv) - 1


def test_run_and_the_input_paths_pass_the_store_options() -> None:
    seed_vm_trust("web")
    t = _vm_transport()

    with patch("agentworks.transports.ssh.subprocess.run", return_value=ok_completed()) as run:
        t.run("echo hi")
        t.run("cat", input_data="payload")
    for call in run.call_args_list:
        _assert_store_options_before_target(call.args[0])

    with patch("agentworks.ssh.subprocess.run", return_value=ok_completed()) as run:
        t.run("cat", input_text="payload")
    _assert_store_options_before_target(run.call_args.args[0])


def test_copy_and_streaming_paths_pass_the_store_options(tmp_path: Path) -> None:
    seed_vm_trust("web")
    t = _vm_transport()

    with patch("agentworks.transports.ssh.subprocess.run", return_value=ok_completed()) as run:
        t.copy_to(tmp_path / "local", "/remote")
        t.copy_from("/remote", tmp_path / "local")
    for call in run.call_args_list:
        assert call.args[0][0] == "scp"
        _assert_store_options_before_target(call.args[0])

    with patch("agentworks.transports.ssh.subprocess.call", return_value=0) as call:
        t.call_streaming("echo hi")
        t._interactive("echo hi")
    for invocation in call.call_args_list:
        _assert_store_options_before_target(invocation.args[0])


def test_host_trust_keeps_first_use_against_the_operators_configuration() -> None:
    t = SSHTransport(trust=HOST_TRUST, host="vm-host", user=None)

    with patch("agentworks.transports.ssh.subprocess.run", return_value=ok_completed()) as run:
        t.run("echo hi")

    argv = run.call_args.args[0]
    assert "StrictHostKeyChecking=accept-new" in argv
    assert not any(arg.startswith(("UserKnownHostsFile", "HostKeyAlias")) for arg in argv)


# -- The loops that retry never retry a missing record ---------------------------------------


def test_the_agent_probe_does_not_rewrap_a_missing_record_as_unreachable() -> None:
    from agentworks.agents.manager._common import _assert_agent_ssh_works

    agent = SimpleNamespace(name="a1", vm_name="web", linux_user="agt-a1")

    with pytest.raises(TrustMissing):
        _assert_agent_ssh_works(_vm_transport(), agent)  # type: ignore[arg-type]
