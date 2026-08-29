"""Placement coverage for adding sessions to named consoles."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.cli.commands import console as console_commands
from agentworks.db import Database
from agentworks.errors import AlreadyExistsError, ValidationError
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import multi_console
from agentworks.sessions.multi_console import add_sessions, create_console
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests._tmux_model import TmuxModel

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.conftest import _FakeTarget

CON = "aw-console-con"


@pytest.fixture
def add_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(console_commands, "get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())
    monkeypatch.setattr(multi_console, "add_sessions", lambda *args, **kwargs: calls.append(kwargs))
    return calls


@pytest.mark.parametrize(
    ("arguments", "expected_start_index"),
    [
        (["con", "a", "b"], None),
        (["con", "--to-index", "2", "a", "b"], 2),
    ],
)
def test_cli_forwards_placement(
    arguments: list[str],
    expected_start_index: int | None,
    add_calls: list[dict[str, Any]],
) -> None:
    result = CliRunner().invoke(app, ["console", "add-sessions", *arguments])

    assert result.exit_code == 0, result.output
    assert add_calls == [
        {
            "console_name": "con",
            "session_specs": ["a", "b"],
            "interaction": TtyInteractionPolicy.ALLOW,
            "start_index": expected_start_index,
        }
    ]


@pytest.mark.parametrize(
    ("start_index", "expected"),
    [
        (0, ["d", "e", "a", "b", "c"]),
        (1, ["a", "d", "e", "b", "c"]),
        (3, ["a", "b", "c", "d", "e"]),
    ],
)
def test_manager_places_new_block_at_requested_index(
    db: Database,
    start_index: int | None,
    expected: list[str],
) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b", "c", "d", "e"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b", "c"])

    add_sessions(
        db,
        _StubConfig(),
        console_name="con",
        session_specs=["d", "e"],
        interaction=TtyInteractionPolicy.REFUSE,
        start_index=start_index,
    )

    assert [member.session_name for member in db.list_console_sessions("con")] == expected


@pytest.mark.parametrize("start_index", [-1, 4])
def test_manager_rejects_invalid_index_without_mutation(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    start_index: int,
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b", "c", "d"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b", "c"])
    fake_target.commands.clear()
    monkeypatch.setattr(
        "agentworks.secrets.resolve_for_command",
        lambda *args, **kwargs: pytest.fail("invalid placement must not resolve secrets"),
    )

    with pytest.raises(ValidationError) as exc_info:
        add_sessions(
            db,
            _StubConfig(),
            console_name="con",
            session_specs=["d+1"],
            interaction=TtyInteractionPolicy.REFUSE,
            start_index=start_index,
        )

    assert exc_info.value.entity_kind == "console"
    assert exc_info.value.entity_name == "con"
    assert [member.session_name for member in db.list_console_sessions("con")] == ["a", "b", "c"]
    assert fake_target.commands == []


def test_manager_rolls_back_additions_when_reorder_fails(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b", "c"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    def fail_reorder(*args: object, **kwargs: object) -> None:
        raise RuntimeError("reorder failed")

    monkeypatch.setattr(db, "reorder_console_sessions", fail_reorder)

    with pytest.raises(RuntimeError, match="reorder failed"):
        add_sessions(
            db,
            _StubConfig(),
            console_name="con",
            session_specs=["b", "c"],
            interaction=TtyInteractionPolicy.REFUSE,
            start_index=0,
        )

    assert [member.session_name for member in db.list_console_sessions("con")] == ["a"]


def test_manager_recomputes_explicit_end_after_membership_changes(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import bootstrap

    _seed_vm(db)
    _seed_sessions(db, ["a", "b", "c", "d"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])
    load_registry = bootstrap.load_request_registry

    def load_registry_after_concurrent_add(*args: object, **kwargs: object) -> object:
        db.add_console_session("con", "c", [])
        return load_registry(*args, **kwargs)

    monkeypatch.setattr(bootstrap, "load_request_registry", load_registry_after_concurrent_add)

    add_sessions(
        db,
        _StubConfig(),
        console_name="con",
        session_specs=["d"],
        interaction=TtyInteractionPolicy.REFUSE,
        start_index=2,
    )

    assert [member.session_name for member in db.list_console_sessions("con")] == ["a", "b", "d", "c"]


def test_manager_reports_typed_conflict_when_requested_membership_changes(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import bootstrap

    _seed_vm(db)
    _seed_sessions(db, ["a", "b", "c", "d"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])
    load_registry = bootstrap.load_request_registry

    def load_registry_after_concurrent_add(*args: object, **kwargs: object) -> object:
        db.add_console_session("con", "c", [])
        return load_registry(*args, **kwargs)

    monkeypatch.setattr(bootstrap, "load_request_registry", load_registry_after_concurrent_add)

    with pytest.raises(AlreadyExistsError) as exc_info:
        add_sessions(
            db,
            _StubConfig(),
            console_name="con",
            session_specs=["c", "d"],
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert exc_info.value.entity_kind == "console-member"
    assert exc_info.value.entity_name == "c"
    assert [member.session_name for member in db.list_console_sessions("con")] == ["a", "b", "c"]


def test_live_sync_matches_indexed_database_order(
    db: Database,
    console_target_factory: Callable[..., _FakeTarget],
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b", "c", "d"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])

    model = TmuxModel()
    model.new_session(CON, "a")
    model.new_window(CON, "b")
    console_target_factory(model)

    add_sessions(
        db,
        _StubConfig(),
        console_name="con",
        session_specs=["c", "d"],
        interaction=TtyInteractionPolicy.REFUSE,
        start_index=1,
    )

    live_order = model.window_names(CON)
    db_order = [member.session_name for member in db.list_console_sessions("con")]
    assert live_order == db_order == ["a", "c", "d", "b"]
