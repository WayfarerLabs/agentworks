"""The command path a holder record names, set by the CLI layer and never holding an argument value."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import typer
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.cli._app import CommandPathGroup
from agentworks.native_locks import LockRequest, native_locks, operation_label
from agentworks.sessions import manager as session_manager

from ._lock_support import records, session

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.windows

ARGUMENT_VALUE = "distinctive-7f3a91c2"


def test_a_cli_command_records_its_path_and_none_of_its_arguments(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[bytes] = []

    def start_session(*args: object, **kwargs: object) -> None:
        with native_locks(database, LockRequest(exclusive=(session(),))):
            (path,) = records(database)
            recorded.append(path.read_bytes())

    monkeypatch.setattr("agentworks.cli.commands.session.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())
    monkeypatch.setattr(session_manager, "start_session", start_session)

    result = CliRunner().invoke(app, ["session", "start", ARGUMENT_VALUE])

    assert result.exit_code == 0, result.output
    (content,) = recorded
    assert json.loads(content)["command"] == ["agw", "session", "start"]
    assert ARGUMENT_VALUE.encode() not in content
    assert operation_label.get() == ("agw",)


def test_outside_the_cli_the_label_is_agw(database: Path) -> None:
    with native_locks(database, LockRequest(exclusive=(session(),))):
        (path,) = records(database)
        assert json.loads(path.read_bytes())["command"] == ["agw"]


def test_every_command_group_records_its_name() -> None:
    def groups(command: object) -> list[object]:
        children = getattr(command, "commands", None)
        if children is None:
            return []
        return [command, *(group for child in children.values() for group in groups(child))]

    found = groups(typer.main.get_command(app))
    assert len(found) > 1
    assert all(isinstance(group, CommandPathGroup) for group in found)
