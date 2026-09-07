"""Behavioral coverage for the shared list ordering contract."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from agentworks.agents.manager import agent_listing
from agentworks.cli import app
from agentworks.completions.spec import build_spec
from agentworks.db import Database, SessionMode
from agentworks.errors import ValidationError
from agentworks.list_sorting import sort_rows
from agentworks.sessions.manager import list_sessions, session_listing
from agentworks.sessions.multi_console import console_listing
from agentworks.vms.manager import vm_listing
from agentworks.workspaces.manager import workspace_listing

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.config import Config
    from agentworks.secrets.policy import TtyInteractionPolicy
    from tests.conftest import CapturedOutput


def _seed_inventory(path: Path) -> Database:
    db = Database(path)
    db.insert_vm("a-vm", site="lima", hostname="lima--a-vm")
    db.insert_vm("z-vm", site="lima", hostname="lima--z-vm")
    db.insert_workspace("a-ws", workspace_path="/a", vm_name="z-vm", linux_group="ws-a")
    db.insert_workspace("z-ws", workspace_path="/z", vm_name="a-vm", linux_group="ws-z")
    db.insert_agent("a-agent", "z-vm", "agt-a")
    db.insert_agent("z-agent", "a-vm", "agt-z")
    db.insert_session("a-session", "z-ws", template="default", mode=SessionMode.ADMIN)
    db.insert_session(
        "z-session",
        "a-ws",
        template="default",
        mode=SessionMode.AGENT,
        agent_name="a-agent",
        socket_path="/tmp/z-session.sock",
    )
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES ('a-console', 'z-vm')")
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES ('z-console', 'a-vm')")
    for table, older, newer in (
        ("vms", "z-vm", "a-vm"),
        ("workspaces", "z-ws", "a-ws"),
        ("agents", "z-agent", "a-agent"),
        ("sessions", "z-session", "a-session"),
        ("consoles", "z-console", "a-console"),
    ):
        db._conn.execute(f"UPDATE {table} SET created_at = '2025-01-01T00:00:00Z' WHERE name = ?", (older,))
        db._conn.execute(f"UPDATE {table} SET created_at = '2026-01-01T00:00:00Z' WHERE name = ?", (newer,))
    db._conn.commit()
    return db


def test_alpha_is_always_the_final_tiebreaker() -> None:
    rows = [("b", "vm-a"), ("a", "vm-b"), ("c", "vm-a")]
    assert sort_rows(
        ["b", "a"],
        sort_keys=None,
        key_functions={"alpha": lambda row: (row,)},
        entity_kind="row",
    ) == ("a", "b")
    assert sort_rows(
        rows,
        sort_keys=("alpha", "vm"),
        key_functions={"alpha": lambda row: (row[0],), "vm": lambda row: (row[1],)},
        entity_kind="row",
    ) == (("b", "vm-a"), ("c", "vm-a"), ("a", "vm-b"))


def test_missing_values_sort_before_present_values() -> None:
    rows = [("z-row", None), ("a-row", "vm-a"), ("b-row", None)]
    assert sort_rows(
        rows,
        sort_keys=("vm",),
        key_functions={
            "alpha": lambda row: (row[0],),
            "vm": lambda row: ("0", "") if row[1] is None else ("1", row[1]),
        },
        entity_kind="row",
    ) == (("b-row", None), ("z-row", None), ("a-row", "vm-a"))


@pytest.mark.parametrize("sort_keys", [(), ("",), ("alpha", "alpha"), ("workspace",)])
def test_sort_validation_rejects_empty_duplicate_and_inapplicable_keys(sort_keys: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        sort_rows(
            ["row"],
            sort_keys=sort_keys,
            key_functions={"alpha": lambda row: (row,)},
            entity_kind="vm",
        )


def test_database_list_services_apply_creation_and_relationship_order(tmp_path: Path) -> None:
    db = _seed_inventory(tmp_path / "state.db")
    try:
        assert [row.name for row in vm_listing(db).vms] == ["a-vm", "z-vm"]
        assert [row.name for row in vm_listing(db, sort_keys=("alpha",)).vms] == ["a-vm", "z-vm"]
        assert [row.name for row in vm_listing(db, sort_keys=("creation",)).vms] == ["z-vm", "a-vm"]

        assert [row.name for row in agent_listing(db).agents] == ["a-agent", "z-agent"]
        assert [row.name for row in agent_listing(db, sort_keys=("alpha",)).agents] == ["a-agent", "z-agent"]
        assert [row.name for row in agent_listing(db, sort_keys=("creation",)).agents] == [
            "z-agent",
            "a-agent",
        ]
        assert [row.name for row in agent_listing(db, sort_keys=("vm",)).agents] == ["z-agent", "a-agent"]

        assert [row.name for row in workspace_listing(db).workspaces] == ["a-ws", "z-ws"]
        assert [row.name for row in workspace_listing(db, sort_keys=("alpha",)).workspaces] == ["a-ws", "z-ws"]
        assert [row.name for row in workspace_listing(db, sort_keys=("creation",)).workspaces] == [
            "z-ws",
            "a-ws",
        ]
        assert [row.name for row in workspace_listing(db, sort_keys=("vm",)).workspaces] == ["z-ws", "a-ws"]

        assert [row.name for row in console_listing(db).consoles] == ["a-console", "z-console"]
        assert [row.name for row in console_listing(db, sort_keys=("alpha",)).consoles] == [
            "a-console",
            "z-console",
        ]
        assert [row.name for row in console_listing(db, sort_keys=("creation",)).consoles] == [
            "z-console",
            "a-console",
        ]
        assert [row.name for row in console_listing(db, sort_keys=("vm",)).consoles] == [
            "z-console",
            "a-console",
        ]
    finally:
        db.close()


def test_vm_sort_order_is_shared_by_human_json_and_names_only(tmp_path: Path) -> None:
    db = _seed_inventory(tmp_path / "state.db")
    runner = CliRunner()
    try:
        with patch("agentworks.cli.commands.vm.get_db", return_value=db):
            human = runner.invoke(app, ["vm", "list", "--sort", "creation"])
            machine = runner.invoke(app, ["vm", "list", "--sort", "creation", "--output", "json"])
            names = runner.invoke(app, ["vm", "list", "--sort", "creation", "--names-only"])

        assert human.exit_code == machine.exit_code == names.exit_code == 0
        assert human.stdout.index("z-vm") < human.stdout.index("a-vm")
        assert [row["name"] for row in json.loads(machine.stdout)["data"]["vms"]] == ["z-vm", "a-vm"]
        assert names.stdout.splitlines() == ["z-vm", "a-vm"]
    finally:
        db.close()


@pytest.mark.parametrize("value", ["", "alpha,alpha", "workspace"])
def test_cli_rejects_malformed_duplicate_and_inapplicable_sort_keys(tmp_path: Path, value: str) -> None:
    db = _seed_inventory(tmp_path / "state.db")
    try:
        with patch("agentworks.cli.commands.vm.get_db", return_value=db):
            result = CliRunner().invoke(app, ["vm", "list", "--sort", value, "--names-only"])
        assert result.exit_code != 0
        assert isinstance(result.exception, ValidationError)
    finally:
        db.close()


def test_invalid_sort_precedes_live_status_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_observation(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("invalid sort must fail before live observation")

    monkeypatch.setattr("agentworks.vms.manager.inspect.observe_vm_statuses", unexpected_observation)
    monkeypatch.setattr("agentworks.sessions.manager.observe_session_statuses", unexpected_observation)
    monkeypatch.setattr("agentworks.sessions.multi_console.observe_console_statuses", unexpected_observation)
    db = _seed_inventory(tmp_path / "state.db")
    try:
        with pytest.raises(ValidationError):
            vm_listing(
                db,
                cast("Config", object()),
                include_status=True,
                interaction=cast("TtyInteractionPolicy", object()),
                sort_keys=("workspace",),
            )
        with pytest.raises(ValidationError):
            session_listing(
                db,
                cast("Config", object()),
                include_status=True,
                sort_keys=("bogus",),
            )
        with pytest.raises(ValidationError):
            console_listing(
                db,
                cast("Config", object()),
                include_status=True,
                sort_keys=("workspace",),
            )
    finally:
        db.close()


@pytest.mark.parametrize(
    ("sort_keys", "expected"),
    [
        (None, ["a-session", "z-session"]),
        (("alpha",), ["a-session", "z-session"]),
        (("creation",), ["z-session", "a-session"]),
        (("vm",), ["a-session", "z-session"]),
        (("agent",), ["a-session", "z-session"]),
        (("workspace",), ["z-session", "a-session"]),
    ],
)
def test_session_names_only_uses_the_same_service_sort(
    tmp_path: Path,
    captured_output: CapturedOutput,
    sort_keys: tuple[str, ...] | None,
    expected: list[str],
) -> None:
    db = _seed_inventory(tmp_path / "state.db")
    try:
        list_sessions(
            db,
            object(),
            names_only=True,
            sort_keys=sort_keys,
        )
        assert captured_output.info == expected
    finally:
        db.close()


def test_resource_kinds_sort_flag_has_json_and_names_only_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Registry:
        @staticmethod
        def iter_kind(_name: str) -> tuple[object, ...]:
            return ()

    from agentworks import bootstrap, config

    monkeypatch.setattr(config, "load_config", lambda **_kwargs: object())
    monkeypatch.setattr(bootstrap, "load_request_registry", lambda _config, **_kwargs: _Registry())
    runner = CliRunner()
    names = runner.invoke(app, ["resource", "kinds", "--sort", "alpha", "--names-only"])
    machine = runner.invoke(app, ["resource", "kinds", "--sort", "alpha", "--output", "json"])
    invalid = runner.invoke(app, ["resource", "kinds", "--sort", "creation", "--names-only"])

    assert names.exit_code == machine.exit_code == 0
    expected = names.stdout.splitlines()
    assert expected == sorted(expected)
    assert [row["kind"] for row in json.loads(machine.stdout)["data"]["kinds"]] == expected
    assert isinstance(invalid.exception, ValidationError)


def test_sort_key_completions_cover_each_command_matrix() -> None:
    spec = build_spec(app)
    expected = {
        ("vm", "list"): ["alpha", "creation"],
        ("agent", "list"): ["alpha", "creation", "vm"],
        ("workspace", "list"): ["alpha", "creation", "vm"],
        ("session", "list"): ["alpha", "creation", "vm", "agent", "workspace"],
        ("console", "list"): ["alpha", "creation", "vm"],
        ("secret", "list"): ["alpha", "source", "backend"],
        ("resource", "list"): ["alpha"],
        ("resource", "kinds"): ["alpha"],
    }
    for path, suggestions in expected.items():
        command = spec.subcommands[path[0]].subcommands[path[1]]
        sort_param = next(param for param in command.params if param.name == "sort")
        assert sort_param.suggestions == suggestions
