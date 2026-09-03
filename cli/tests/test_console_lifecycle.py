"""Behavioral coverage for explicit named-console runtime lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.db import Database
from agentworks.errors import ExternalError, SecretUnavailableError, StateError, ValidationError
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions.multi_console import (
    attach_console,
    create_console,
    restart_console,
    start_console,
    stop_console,
)
from agentworks.sessions.tmux import ProbeStatus
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests._tmux_model import TmuxModel
from tests.conftest import _FakeResult
from tests.console_helpers import create_console_record

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.conftest import _FakeTarget


def _refuse() -> TtyInteractionPolicy:
    return TtyInteractionPolicy.REFUSE


def test_create_persists_and_publishes_only_canonical_runtime(
    db: Database,
    console_target_factory: Callable[[TmuxModel], _FakeTarget],
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    model = TmuxModel()
    target = console_target_factory(model)

    create_console(
        db,
        _StubConfig(),
        name="con",
        vm_name="vm1",
        session_specs=["alpha"],
        interaction=_refuse(),
    )

    assert db.get_console("con") is not None
    assert model.has_session("aw-console-con")
    assert not model.has_session("aw-console-build+con")
    assert any("rename-session -t '=aw-console-build+con' aw-console-con" in command for command in target.commands)


def test_create_failure_retains_stopped_definition_and_cleans_staging(
    db: Database,
    console_target_factory: Callable[[TmuxModel, dict[str, _FakeResult]], _FakeTarget],
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    model = TmuxModel()
    console_target_factory(
        model,
        {"new-window -t '=aw-console-build+con'": _FakeResult(returncode=1, stderr="boom")},
    )

    with pytest.raises(ExternalError):
        create_console(
            db,
            _StubConfig(),
            name="con",
            vm_name="vm1",
            session_specs=["alpha"],
            interaction=_refuse(),
        )

    assert db.get_console("con") is not None
    assert not model.has_session("aw-console-con")
    assert not model.has_session("aw-console-build+con")


def test_create_failure_retains_definition_when_runtime_absence_is_indeterminate(
    db: Database,
    console_target_factory: Callable[[TmuxModel, dict[str, _FakeResult]], _FakeTarget],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.sessions.multi_console as multi_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    model = TmuxModel()
    console_target_factory(
        model,
        {"new-window -t '=aw-console-build+con'": _FakeResult(returncode=1, stderr="boom")},
    )
    monkeypatch.setattr(
        multi_console,
        "_console_runtime_presence",
        lambda *args, **kwargs: (ProbeStatus.UNKNOWN, ProbeStatus.ABSENT),
    )

    with pytest.raises(StateError):
        create_console(
            db,
            _StubConfig(),
            name="con",
            vm_name="vm1",
            session_specs=["alpha"],
            interaction=_refuse(),
        )

    assert db.get_console("con") is not None
    assert [member.session_name for member in db.list_console_sessions("con")] == ["alpha"]
    assert not model.has_session("aw-console-con")
    assert not model.has_session("aw-console-build+con")


def test_start_is_idempotent_and_stop_removes_both_managed_names(
    db: Database,
    console_target_factory: Callable[[TmuxModel], _FakeTarget],
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["alpha"])
    model = TmuxModel()
    model.seed_session("aw-console-con", "alpha")
    model.seed_session("aw-console-build+con", "partial")
    console_target_factory(model)

    start_console(db, _StubConfig(), name="con", interaction=_refuse())
    assert model.has_session("aw-console-con")
    assert not model.has_session("aw-console-build+con")

    stop_console(db, _StubConfig(), name="con", interaction=_refuse())
    assert not model.has_session("aw-console-con")
    assert not model.has_session("aw-console-build+con")
    assert db.get_console("con") is not None


def test_attach_never_realizes_an_absent_runtime(
    db: Database,
    console_target_factory: Callable[[TmuxModel], _FakeTarget],
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["alpha"])
    model = TmuxModel()
    target = console_target_factory(model)

    with pytest.raises(StateError):
        attach_console(db, _StubConfig(), name="con", allow_nesting=True, interaction=_refuse())

    assert not model.has_session("aw-console-con")
    assert not any("new-session" in command for command in target.commands)


def test_restart_resolves_inputs_before_destroying_running_runtime(
    db: Database,
    console_target_factory: Callable[[TmuxModel], _FakeTarget],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["alpha"])
    model = TmuxModel()
    model.seed_session("aw-console-con", "alpha")
    console_target_factory(model)

    def _unavailable(*args: object, **kwargs: object) -> dict[str, str]:
        raise SecretUnavailableError("missing")

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _unavailable)
    with pytest.raises(SecretUnavailableError):
        restart_console(db, _StubConfig(), name="con", interaction=_refuse())
    assert model.has_session("aw-console-con")


def test_exact_targets_do_not_touch_a_prefix_neighbor(
    db: Database,
    console_target_factory: Callable[[TmuxModel], _FakeTarget],
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["alpha"])
    model = TmuxModel()
    model.seed_session("aw-console-conlong", "operator")
    console_target_factory(model)

    start_console(db, _StubConfig(), name="con", interaction=_refuse())

    assert model.has_session("aw-console-con")
    assert model.has_session("aw-console-conlong")


def test_console_transport_failure_never_triggers_teardown_or_build(
    db: Database,
    console_target_factory: Callable[[TmuxModel, dict[str, _FakeResult]], _FakeTarget],
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    model = TmuxModel()
    target = console_target_factory(
        model,
        {"has-session -t '=aw-console-con'": _FakeResult(returncode=255)},
    )

    with pytest.raises(StateError):
        create_console(
            db,
            _StubConfig(),
            name="con",
            vm_name="vm1",
            session_specs=["alpha"],
            interaction=_refuse(),
        )

    assert db.get_console("con") is None
    assert not any("kill-session" in command or "new-session" in command for command in target.commands)


def test_empty_console_is_rejected_before_registry_or_remote_work(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_vm(db, with_tailscale=True)
    registry_loads: list[bool] = []

    def load_registry(*args: object, **kwargs: object) -> None:
        registry_loads.append(True)
        raise AssertionError("registry loading must follow definition validation")

    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", load_registry)

    with pytest.raises(ValidationError):
        create_console(
            db,
            _StubConfig(),
            name="con",
            vm_name="vm1",
            session_specs=[],
            interaction=_refuse(),
        )

    assert registry_loads == []
    assert db.get_console("con") is None


def test_deprecated_recreate_checks_nesting_before_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    import agentworks.sessions.multi_console as multi_console

    calls: list[str] = []

    def refuse(*, allow_nesting: bool) -> None:
        calls.append("nesting")
        raise StateError("nested tmux")

    monkeypatch.setattr(multi_console, "refuse_console_nesting", refuse)
    monkeypatch.setattr(
        multi_console,
        "restart_console",
        lambda *args, **kwargs: calls.append("restart"),
    )

    result = CliRunner().invoke(app, ["console", "attach", "con", "--recreate"])

    assert result.exit_code != 0
    assert calls == ["nesting"]
