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
from agentworks.resources.registry import Registry
from agentworks.vms.manager.verification import verify_vm_connection


def test_verify_connection_uses_one_canonical_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    vm = SimpleNamespace(name="worker", site="local")
    calls: list[tuple[object, ...]] = []

    class Target:
        def run(self, command: str, **kwargs: object) -> None:
            calls.append((command, kwargs))

        def describe(self) -> str:
            return "ssh:100.64.0.2"

    monkeypatch.setattr("agentworks.vms.sites.resolve_site", lambda site, registry: calls.append(("site", site)))
    monkeypatch.setattr(
        "agentworks.vms.manager.boundary.require_vm_ssh_boundary",
        lambda db, config, candidate: calls.append(("identity", candidate.name)),
    )
    monkeypatch.setattr("agentworks.transports.transport", lambda candidate, config: Target())

    result = verify_vm_connection(
        SimpleNamespace(get_vm=lambda name: vm),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        "worker",
    )

    assert calls == [
        ("site", "local"),
        ("identity", "worker"),
        ("true", {"sudo": False, "tty": False, "env": None, "timeout": 10}),
    ]
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
    failure: Exception,
) -> None:
    vm = SimpleNamespace(name="worker", site="local")
    calls: list[tuple[object, ...]] = []

    class DatabaseSpy:
        def get_vm(self, name: str) -> object:
            calls.append(("get_vm", name))
            return vm

        def __getattr__(self, name: str) -> object:
            if name.startswith(("add_", "update_", "delete_", "set_", "record_")):
                raise AssertionError(f"database mutation attempted: {name}")
            raise AttributeError(name)

    class FailingTarget:
        def run(self, command: str, **kwargs: object) -> None:
            calls.append(("run", command, kwargs))
            raise failure

        def describe(self) -> str:
            raise AssertionError("failed transport must not be described")

    forbidden = {
        "gated_vm_boundary",
        "start_vm",
        "_query_live_resources",
        "_ensure_tailscale",
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
    monkeypatch.setattr("agentworks.transports.transport", lambda candidate, config: FailingTarget())

    with pytest.raises(type(failure), match=str(failure)):
        verify_vm_connection(
            cast("Database", DatabaseSpy()),
            cast("Config", SimpleNamespace()),
            cast("Registry", SimpleNamespace()),
            "worker",
        )

    assert calls == [
        ("get_vm", "worker"),
        ("site", "local"),
        ("identity", "worker"),
        ("run", "true", {"sudo": False, "tty": False, "env": None, "timeout": 10}),
    ]


def test_verify_connection_missing_vm() -> None:
    with pytest.raises(NotFoundError, match="VM 'missing' not found"):
        verify_vm_connection(
            cast("Database", SimpleNamespace(get_vm=lambda name: None)),
            cast("Config", SimpleNamespace()),
            cast("Registry", SimpleNamespace()),
            "missing",
        )


def test_verify_connection_refuses_identity_state_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm = SimpleNamespace(name="worker", site="local")
    monkeypatch.setattr("agentworks.vms.sites.resolve_site", lambda site, registry: None)

    def refuse(*args: object, **kwargs: object) -> None:
        raise StateError("SSH identity drift")

    monkeypatch.setattr("agentworks.vms.manager.boundary.require_vm_ssh_boundary", refuse)
    monkeypatch.setattr(
        "agentworks.transports.transport",
        lambda *args, **kwargs: pytest.fail("transport constructed before identity refusal"),
    )

    with pytest.raises(StateError):
        verify_vm_connection(
            cast("Database", SimpleNamespace(get_vm=lambda name: vm)),
            cast("Config", SimpleNamespace()),
            cast("Registry", SimpleNamespace()),
            "worker",
        )


def test_live_resource_probe_degrades_identity_refusal_without_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.vms.manager._helpers import _query_live_resources

    def refuse(*args: object, **kwargs: object) -> None:
        raise StateError("SSH identity drift")

    monkeypatch.setattr("agentworks.vms.manager.boundary.require_vm_ssh_boundary", refuse)
    monkeypatch.setattr(
        "agentworks.transports.transport",
        lambda *args, **kwargs: pytest.fail("transport constructed after identity refusal"),
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
