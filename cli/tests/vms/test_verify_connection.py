"""VM connection verification is exactly one non-activating no-op."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.config import Config
from agentworks.db import Database
from agentworks.errors import ConnectivityError, NotFoundError, StateError
from agentworks.execution.access import Admin
from agentworks.execution.errors import ClientUnsupported
from agentworks.execution.requests import Command
from agentworks.resources.registry import Registry
from agentworks.vms.manager.verification import verify_vm_connection
from tests.vm_access_support import AccessRecorder


def test_verify_connection_uses_one_canonical_no_op(
    monkeypatch: pytest.MonkeyPatch, vm_access_recorder: AccessRecorder
) -> None:
    vm = SimpleNamespace(name="worker", site="local", debian_release=None)
    calls: list[tuple[object, ...]] = []

    monkeypatch.setattr("agentworks.vms.sites.resolve_site", lambda site, registry: calls.append(("site", site)))
    monkeypatch.setattr(
        "agentworks.vms.manager.boundary.require_vm_ssh_boundary",
        lambda db, config, candidate: calls.append(("identity", candidate.name)),
    )

    result = verify_vm_connection(
        SimpleNamespace(get_vm=lambda name: vm),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        "worker",
    )

    assert calls == [("site", "local"), ("identity", "worker")]
    (call,) = vm_access_recorder.calls
    assert (call.identity, call.operation, call.request) == (Admin(), "run", Command(("true",), deadline=10))
    assert call.options["check"] is True
    assert not hasattr(result, "connected")
    assert result.transport == "ssh"


@pytest.mark.parametrize(
    "failure",
    [
        StateError("VM is stopped"),
        ConnectivityError("VM is unreachable"),
        StateError("unsupported transport"),
    ],
    ids=["stopped", "unreachable", "bad-transport"],
)
def test_verify_connection_surfaces_failure_without_activation_or_mutation(
    monkeypatch: pytest.MonkeyPatch,
    vm_access_recorder: AccessRecorder,
    failure: Exception,
) -> None:
    vm = SimpleNamespace(name="worker", site="local", debian_release=None, provisioning_status="complete")
    calls: list[tuple[object, ...]] = []

    class DatabaseSpy:
        def get_vm(self, name: str) -> object:
            calls.append(("get_vm", name))
            return vm

        def __getattr__(self, name: str) -> object:
            if name.startswith(("add_", "update_", "delete_", "set_", "record_")):
                raise AssertionError(f"database mutation attempted: {name}")
            raise AttributeError(name)

    forbidden = {
        "gated_vm_boundary",
        "start_vm",
        "_query_live_resources",
        "_await_tailnet",
        "recover_vm",
        "rekey_vm",
        "reinit_vm",
        "_resolve_vm_admin_env_scopes",
    }

    def forbidden_call(*args: object, **kwargs: object) -> None:
        raise AssertionError("verification crossed an activation, repair, secret, or mutation boundary")

    for name in forbidden:
        monkeypatch.setattr(f"agentworks.vms.manager.{name}", forbidden_call)
    monkeypatch.setattr("agentworks.output.prompt", forbidden_call)
    monkeypatch.setattr("agentworks.secrets.resolve.resolve_batch", forbidden_call)
    monkeypatch.setattr("agentworks.vms.sites.resolve_site", lambda site, registry: calls.append(("site", site)))
    monkeypatch.setattr(
        "agentworks.vms.manager.boundary.require_vm_ssh_boundary",
        lambda db, config, candidate: calls.append(("identity", candidate.name)),
    )
    vm_access_recorder.access(Admin()).run_outcomes = [failure]

    with pytest.raises(type(failure), match=str(failure)):
        verify_vm_connection(
            cast("Database", DatabaseSpy()),
            cast("Config", SimpleNamespace()),
            cast("Registry", SimpleNamespace()),
            "worker",
        )

    assert calls == [("get_vm", "worker"), ("site", "local"), ("identity", "worker")]
    assert vm_access_recorder.operations == [("admin", "run")]


def test_verify_connection_missing_vm(vm_access_recorder: AccessRecorder) -> None:
    with pytest.raises(NotFoundError, match="VM 'missing' not found"):
        verify_vm_connection(
            cast("Database", SimpleNamespace(get_vm=lambda name: None)),
            cast("Config", SimpleNamespace()),
            cast("Registry", SimpleNamespace()),
            "missing",
        )


def test_verify_connection_refuses_identity_state_before_transport(
    monkeypatch: pytest.MonkeyPatch,
    vm_access_recorder: AccessRecorder,
) -> None:
    vm = SimpleNamespace(name="worker", site="local", debian_release=None)
    monkeypatch.setattr("agentworks.vms.sites.resolve_site", lambda site, registry: None)

    def refuse(*args: object, **kwargs: object) -> None:
        raise StateError("SSH identity drift")

    monkeypatch.setattr("agentworks.vms.manager.boundary.require_vm_ssh_boundary", refuse)

    with pytest.raises(StateError):
        verify_vm_connection(
            cast("Database", SimpleNamespace(get_vm=lambda name: vm)),
            cast("Config", SimpleNamespace()),
            cast("Registry", SimpleNamespace()),
            "worker",
        )

    assert vm_access_recorder.built == []  # no access object was built after the refusal


@pytest.mark.parametrize("refusal", ["no-host-key", "no-identity"])
@pytest.mark.parametrize("provisioning", ["pending", "failed"])
def test_an_incomplete_vm_s_refusal_names_delete_not_a_command_that_refuses_it(
    monkeypatch: pytest.MonkeyPatch,
    vm_access_recorder: AccessRecorder,
    refusal: str,
    provisioning: str,
) -> None:
    """The boundary's own hints name reinit, recover or host-key import, and each refuses an incomplete VM."""
    from agentworks.execution.errors import TrustMissing
    from agentworks.vms.applied_state import (
        VMSSHIdentityComparison,
        VMSSHIdentityState,
        require_vm_ssh_identity,
    )

    vm = SimpleNamespace(name="worker", site="local", debian_release=None, provisioning_status=provisioning)
    monkeypatch.setattr("agentworks.vms.sites.resolve_site", lambda site, registry: None)

    def refuse(*args: object, **kwargs: object) -> None:
        if refusal == "no-host-key":
            raise TrustMissing("worker")
        require_vm_ssh_identity(VMSSHIdentityComparison(VMSSHIdentityState.NOT_RECORDED, "worker"))

    monkeypatch.setattr("agentworks.vms.manager.boundary.require_vm_ssh_boundary", refuse)

    with pytest.raises(StateError) as caught:
        verify_vm_connection(
            cast("Database", SimpleNamespace(get_vm=lambda name: vm)),
            cast("Config", SimpleNamespace()),
            cast("Registry", SimpleNamespace()),
            "worker",
        )

    hint = caught.value.hint or ""
    assert "agw vm delete worker" in hint
    assert not any(command in hint for command in ("reinit", "recover", "import-host-keys"))
    assert vm_access_recorder.built == []


@pytest.mark.parametrize("provisioning", ["pending", "failed"])
def test_an_incomplete_vm_s_failed_connection_names_delete_not_a_command_that_refuses_it(
    monkeypatch: pytest.MonkeyPatch,
    vm_access_recorder: AccessRecorder,
    provisioning: str,
) -> None:
    """The connection's own hint names recover, which refuses a VM whose provisioning did not complete."""
    vm = SimpleNamespace(name="worker", site="local", debian_release=None, provisioning_status=provisioning)
    monkeypatch.setattr("agentworks.vms.sites.resolve_site", lambda site, registry: None)
    monkeypatch.setattr("agentworks.vms.manager.boundary.require_vm_ssh_boundary", lambda *args: None)
    recover_hint = "Run 'agw vm recover worker'."
    failure = ConnectivityError("VM is unreachable", hint=recover_hint)
    vm_access_recorder.access(Admin()).run_outcomes = [failure]

    with pytest.raises(ConnectivityError) as caught:
        verify_vm_connection(
            cast("Database", SimpleNamespace(get_vm=lambda name: vm)),
            cast("Config", SimpleNamespace()),
            cast("Registry", SimpleNamespace()),
            "worker",
        )

    assert caught.value is failure
    assert caught.value.hint
    assert caught.value.hint != recover_hint


def test_verify_connection_refuses_an_unsupported_client_before_reading_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unsupported() -> None:
        raise ClientUnsupported("no client")

    monkeypatch.setattr("agentworks.execution.resolve_ssh", unsupported)

    class UntouchableDatabase:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"database read before the client was resolved: {name}")

    with pytest.raises(ClientUnsupported):
        verify_vm_connection(
            cast("Database", UntouchableDatabase()),
            cast("Config", SimpleNamespace()),
            cast("Registry", SimpleNamespace()),
            "worker",
        )


def test_live_resource_probe_degrades_identity_refusal_without_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.vms.manager._helpers import _query_live_resources

    def refuse(*args: object, **kwargs: object) -> None:
        raise StateError("SSH identity drift")

    monkeypatch.setattr("agentworks.vms.manager.boundary.require_vm_ssh_boundary", refuse)
    monkeypatch.setattr(
        "agentworks.execution.vm_access",
        lambda *args, **kwargs: pytest.fail("access built after identity refusal"),
    )

    config = SimpleNamespace()
    vm = SimpleNamespace(name="worker")
    assert (
        _query_live_resources(
            cast("Database", SimpleNamespace()),
            vm,  # type: ignore[arg-type]
            cast("Config", config),
        )
        is None
    )


def test_verify_connection_cli_reports_service_result(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []
    database = object()
    config = object()
    registry = object()
    monkeypatch.setattr("agentworks.cli.commands.vm.get_db", lambda: database)
    monkeypatch.setattr("agentworks.config.load_config", lambda: config)
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda candidate, **_kwargs: registry)
    monkeypatch.setattr(
        "agentworks.vms.manager.verify_vm_connection",
        lambda db, cfg, reg, name: calls.append((db, cfg, reg, name)) or SimpleNamespace(name=name, transport="ssh"),
    )

    result = CliRunner().invoke(app, ["vm", "verify-connection", "worker"])

    assert result.exit_code == 0
    assert result.stdout == "VM 'worker' connection verified via ssh.\n"
    assert calls == [(database, config, registry, "worker")]
