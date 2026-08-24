"""Placement coverage for console session reordering."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.cli.commands import console as console_commands
from agentworks.db import Database
from agentworks.errors import ValidationError
from agentworks.sessions import multi_console
from agentworks.sessions.multi_console import create_console, reorder_sessions
from tests._consoles_support import _seed_sessions, _seed_vm, _StubConfig
from tests.conftest import _FakeResult, _FakeTarget


@pytest.fixture
def reorder_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(console_commands, "get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())
    monkeypatch.setattr(multi_console, "reorder_sessions", lambda *args, **kwargs: calls.append(kwargs))
    return calls


@pytest.mark.parametrize(
    ("arguments", "expected_start_index"),
    [
        (["con", "a", "b"], 0),
        (["con", "--to-index", "2", "a", "b"], 2),
        (["con", "--to-back", "a", "b"], None),
    ],
)
def test_cli_forwards_placement(
    arguments: list[str],
    expected_start_index: int | None,
    reorder_calls: list[dict[str, Any]],
) -> None:
    result = CliRunner().invoke(app, ["console", "reorder-sessions", *arguments])

    assert result.exit_code == 0, result.output
    assert reorder_calls == [
        {
            "console_name": "con",
            "session_names": ["a", "b"],
            "start_index": expected_start_index,
        }
    ]


def test_cli_rejects_both_placement_options(reorder_calls: list[dict[str, Any]]) -> None:
    result = CliRunner().invoke(
        app,
        ["console", "reorder-sessions", "con", "--to-index", "1", "--to-back", "a"],
    )

    assert result.exit_code == 2
    assert reorder_calls == []


@pytest.mark.parametrize(
    ("start_index", "expected"),
    [
        (0, ["d", "b", "a", "c", "e"]),
        (1, ["a", "d", "b", "c", "e"]),
        (3, ["a", "c", "e", "d", "b"]),
        (None, ["a", "c", "e", "d", "b"]),
    ],
)
def test_manager_places_listed_at_requested_index(
    db: Database,
    start_index: int | None,
    expected: list[str],
) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b", "c", "d", "e"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b", "c", "d", "e"])

    reorder_sessions(
        db,
        _StubConfig(),
        console_name="con",
        session_names=["d", "b"],
        start_index=start_index,
    )

    assert [member.session_name for member in db.list_console_sessions("con")] == expected


@pytest.mark.parametrize("start_index", [-1, 3])
def test_manager_rejects_invalid_start_index(db: Database, start_index: int) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b", "c"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b", "c"])

    with pytest.raises(ValidationError, match="outside the valid range 0..2"):
        reorder_sessions(
            db,
            _StubConfig(),
            console_name="con",
            session_names=["c"],
            start_index=start_index,
        )

    assert [member.session_name for member in db.list_console_sessions("con")] == ["a", "b", "c"]


def test_live_sync_respects_requested_index(db: Database, fake_target: _FakeTarget) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b", "c", "d"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b", "c", "d"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(
        returncode=0,
        stdout="0|a\n1|b\n2|c\n3|d\n",
    )

    reorder_sessions(
        db,
        _StubConfig(),
        console_name="con",
        session_names=["d", "b"],
        start_index=1,
    )

    swaps = [command for command in fake_target.commands if "swap-window" in command]
    assert swaps == [
        "tmux swap-window -s aw-console-con:3 -t aw-console-con:1",
        "tmux swap-window -s aw-console-con:3 -t aw-console-con:2",
    ]
    assert [member.session_name for member in db.list_console_sessions("con")] == ["a", "d", "b", "c"]
