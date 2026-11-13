"""Runtime-target commands keep local writes and reconciliation boundaries distinct."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.completions.spec import build_spec
from agentworks.db import DesiredRuntimeState, SessionMode
from agentworks.secrets.policy import TtyInteractionPolicy

if TYPE_CHECKING:
    from agentworks.db import Database


@pytest.mark.parametrize(
    "kind,state",
    [
        ("vm", "running"),
        ("vm", "stopped"),
        ("vm", "on-demand"),
        ("session", "running"),
        ("session", "stopped"),
        ("console", "running"),
        ("console", "stopped"),
    ],
)
def test_target_command_writes_only_selected_intent_without_runtime_inputs(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    state: str,
) -> None:
    db.insert_vm("vm1", "lima", "vm1")
    db.insert_workspace("ws1", workspace_path="/srv/ws1", vm_name="vm1", linux_group="ws-ws1")
    db.insert_session("work", "ws1", "default", SessionMode.ADMIN)
    db.insert_console("view", "vm1", admin_shell=True)
    selected_name = {"vm": "vm1", "session": "work", "console": "view"}[kind]
    command = importlib.import_module(f"agentworks.cli.commands.{kind}")
    monkeypatch.setattr(command, "get_db", lambda: db)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("local target write accessed runtime inputs")

    monkeypatch.setattr(command, "ordinary_tty_interaction_policy", forbidden)
    monkeypatch.setattr("agentworks.config.load_config", forbidden)
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", forbidden)
    monkeypatch.setattr("agentworks.secrets.resolve.active_sources", forbidden)
    before = {
        "vm": db.get_vm("vm1"),
        "session": db.get_session("work"),
        "console": db.get_console("view"),
    }

    result = CliRunner().invoke(app, [kind, "set-desired-state", selected_name, state])

    assert result.exit_code == 0, result.output
    after = {
        "vm": db.get_vm("vm1"),
        "session": db.get_session("work"),
        "console": db.get_console("view"),
    }
    selected = after.pop(kind)
    assert selected is not None
    assert selected.desired_runtime_state is DesiredRuntimeState(state)
    assert all(row == before[other_kind] for other_kind, row in after.items())


@pytest.mark.parametrize("kind,state", [("vm", "invalid"), ("session", "on-demand"), ("console", "on-demand")])
def test_unsupported_target_fails_parse_before_database_access(monkeypatch: pytest.MonkeyPatch, kind: str, state: str):
    command = importlib.import_module(f"agentworks.cli.commands.{kind}")
    monkeypatch.setattr(command, "get_db", lambda: pytest.fail("opened database for rejected state"))
    result = CliRunner().invoke(app, [kind, "set-desired-state", "name", state])
    assert result.exit_code == 2
    assert result.stdout_bytes == b""


@pytest.mark.parametrize(
    "kind,arguments,expected",
    [
        ("vm", ["box"], {"name": "box", "all_vms": False, "dry_run": False}),
        ("vm", ["--all", "--dry-run"], {"name": None, "all_vms": True, "dry_run": True}),
        ("vm", [], {"name": None, "all_vms": False, "dry_run": False}),
        ("vm", ["box", "--all"], {"name": "box", "all_vms": True, "dry_run": False}),
        ("session", ["work", "--dry-run"], {"name": "work", "dry_run": True}),
        ("console", ["view"], {"name": "view", "dry_run": False}),
    ],
)
@pytest.mark.parametrize("non_interactive", [False, True])
def test_reconcile_forwards_parsed_selection_and_policy_once(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    arguments: list[str],
    expected: dict[str, object],
    non_interactive: bool,
) -> None:
    command = importlib.import_module(f"agentworks.cli.commands.{kind}")
    database, config = object(), object()
    calls: list[tuple[object, object, dict[str, object]]] = []
    monkeypatch.setattr(command, "get_db", lambda: database)
    monkeypatch.setattr("agentworks.config.load_config", lambda: config)
    service = ModuleType("agentworks.runtime_state")

    def reconcile(actual_db: object, actual_config: object, **kwargs: object) -> None:
        calls.append((actual_db, actual_config, kwargs))

    setattr(service, f"reconcile_{kind}", reconcile)
    monkeypatch.setitem(sys.modules, "agentworks.runtime_state", service)
    argv = ["--non-interactive"] if non_interactive else []
    result = CliRunner().invoke(app, [*argv, kind, "reconcile", *arguments])

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            database,
            config,
            {
                **expected,
                "interaction": TtyInteractionPolicy.REFUSE if non_interactive else TtyInteractionPolicy.ALLOW,
            },
        ),
    ]


@pytest.mark.parametrize("kind", ["session", "console"])
def test_child_reconcile_requires_name_at_parse_boundary(kind: str):
    result = CliRunner().invoke(app, [kind, "reconcile", "--dry-run"])
    assert result.exit_code == 2


@pytest.mark.parametrize("kind,completer", [("vm", "vms"), ("session", "sessions"), ("console", "consoles")])
def test_completion_metadata_carries_names_and_kind_specific_state_choices(kind: str, completer: str):
    commands = build_spec(app).subcommands[kind].subcommands
    setter = {parameter.name: parameter for parameter in commands["set-desired-state"].params}
    assert setter["name"].dynamic_completer == completer
    assert setter["name"].required
    assert setter["state"].choices == (["running", "stopped", "on-demand"] if kind == "vm" else ["running", "stopped"])
    assert setter["state"].required
    reconcile = {parameter.name: parameter for parameter in commands["reconcile"].params}
    assert reconcile["name"].dynamic_completer == completer
    assert reconcile["name"].required is (kind != "vm")
    assert reconcile["dry_run"].is_flag
    assert reconcile["dry_run"].opts == ["--dry-run"]
    if kind == "vm":
        assert reconcile["all_vms"].is_flag
        assert reconcile["all_vms"].opts == ["--all"]
