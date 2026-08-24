"""Placement coverage for console session reordering."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.cli.commands import console as console_commands
from agentworks.db import Database
from agentworks.errors import ValidationError
from agentworks.sessions import multi_console
from agentworks.sessions.multi_console import create_console, reorder_sessions
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests._tmux_model import TmuxModel

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.conftest import _FakeTarget

CON = "aw-console-con"


@pytest.fixture
def reorder_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(console_commands, "get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())
    monkeypatch.setattr(multi_console, "reorder_sessions", lambda *args, **kwargs: calls.append(kwargs))
    return calls


@pytest.mark.parametrize(
    ("arguments", "expected_start_index", "expected_to_back"),
    [
        (["con", "a", "b"], None, False),
        (["con", "--to-index", "2", "a", "b"], 2, False),
        (["con", "--to-back", "a", "b"], None, True),
    ],
)
def test_cli_forwards_placement(
    arguments: list[str],
    expected_start_index: int | None,
    expected_to_back: bool,
    reorder_calls: list[dict[str, Any]],
) -> None:
    result = CliRunner().invoke(app, ["console", "reorder-sessions", *arguments])

    assert result.exit_code == 0, result.output
    assert reorder_calls == [
        {
            "console_name": "con",
            "session_names": ["a", "b"],
            "start_index": expected_start_index,
            "to_back": expected_to_back,
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
    ("start_index", "to_back", "expected"),
    [
        (0, False, ["d", "b", "a", "c", "e"]),
        (1, False, ["a", "d", "b", "c", "e"]),
        (3, False, ["a", "c", "e", "d", "b"]),
        (None, True, ["a", "c", "e", "d", "b"]),
    ],
)
def test_manager_places_listed_at_requested_index(
    db: Database,
    start_index: int | None,
    to_back: bool,
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
        to_back=to_back,
    )

    assert [member.session_name for member in db.list_console_sessions("con")] == expected


def test_manager_omitted_placement_defaults_to_front(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b", "c"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b", "c"])

    reorder_sessions(db, _StubConfig(), console_name="con", session_names=["c"])

    assert [member.session_name for member in db.list_console_sessions("con")] == ["c", "a", "b"]


def test_manager_rejects_both_placement_options(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b", "c"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b", "c"])

    with pytest.raises(ValidationError) as exc_info:
        reorder_sessions(
            db,
            _StubConfig(),
            console_name="con",
            session_names=["c"],
            start_index=0,
            to_back=True,
        )

    assert exc_info.value.entity_kind == "console"
    assert exc_info.value.entity_name == "con"
    assert [member.session_name for member in db.list_console_sessions("con")] == ["a", "b", "c"]


@pytest.mark.parametrize("start_index", [-1, 3])
def test_manager_rejects_invalid_start_index(db: Database, start_index: int) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b", "c"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b", "c"])

    with pytest.raises(ValidationError) as exc_info:
        reorder_sessions(
            db,
            _StubConfig(),
            console_name="con",
            session_names=["c"],
            start_index=start_index,
        )

    assert exc_info.value.entity_kind == "console"
    assert exc_info.value.entity_name == "con"
    assert [member.session_name for member in db.list_console_sessions("con")] == ["a", "b", "c"]


def test_live_sync_respects_requested_index(
    db: Database,
    console_target_factory: Callable[..., _FakeTarget],
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b", "c", "d"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b", "c", "d"])

    model = TmuxModel()
    model.new_session(CON, "a")
    model.new_window(CON, "b")
    model.new_window(CON, "c")
    model.new_window(CON, "d")
    console_target_factory(model)

    reorder_sessions(
        db,
        _StubConfig(),
        console_name="con",
        session_names=["d", "b"],
        start_index=1,
    )

    live_order = model.window_names(CON)
    db_order = [member.session_name for member in db.list_console_sessions("con")]
    assert live_order == db_order == ["a", "d", "b", "c"]
