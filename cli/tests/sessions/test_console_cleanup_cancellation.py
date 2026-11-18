"""Cancellation during console cleanup must still stop the invocation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import agentworks.bootstrap as bootstrap
from agentworks.errors import ExternalError, StateError, UserAbort
from agentworks.sessions import multi_console as mc
from agentworks.sessions.multi_console.tmux_build import _build_console_tmux_unlogged
from tests._consoles_support import _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests._tmux_model import TmuxModel
from tests.console_helpers import create_console_record

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import Database
    from tests.conftest import _FakeGuest, _FakeResult


_FAILURE_CASES = [
    (ExternalError, StateError),
    (ExternalError, UserAbort),
    (ExternalError, KeyboardInterrupt),
    (UserAbort, StateError),
    (KeyboardInterrupt, StateError),
]


def _guest(
    factory: Callable[..., _FakeGuest], model: TmuxModel, *, primary: BaseException, cleanup: BaseException | None
) -> _FakeGuest:
    """A modeled guest whose ``new-session`` raises ``primary`` and, after it, every request raises ``cleanup``."""
    guest = factory(model)
    modeled = guest.dispatch
    failed: list[str] = []

    def dispatch(command: str) -> _FakeResult:
        if "tmux new-session" in command:
            guest.commands.append(command)
            failed.append(command)
            raise primary
        if failed and cleanup is not None:
            guest.commands.append(command)
            raise cleanup
        return modeled(command)

    guest.dispatch = dispatch  # type: ignore[method-assign]
    return guest


@pytest.mark.parametrize("logged", [False, True])
@pytest.mark.parametrize("primary_type,cleanup_type", _FAILURE_CASES)
def test_build_cleanup_preserves_failure_priority_and_never_publishes(
    db: Database,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    console_guest_factory: Callable[..., _FakeGuest],
    logged: bool,
    primary_type: type[BaseException],
    cleanup_type: type[BaseException],
) -> None:
    _seed_vm(db)
    create_console_record(db, name="con", vm_name="vm1", session_specs=[], add_admin_shell=True)
    registry = bootstrap.load_request_registry(_StubConfig(), live_database=db)
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    primary = primary_type("build failed")
    cleanup = cleanup_type("cleanup interrupted")
    model = TmuxModel()
    guest = _guest(console_guest_factory, model, primary=primary, cleanup=cleanup)
    vm = db.get_vm("vm1")
    assert vm is not None

    build = mc._build_console_tmux if logged else _build_console_tmux_unlogged
    expected = cleanup if isinstance(cleanup, (UserAbort, KeyboardInterrupt)) else primary
    with pytest.raises(type(expected)) as caught:
        build(
            guest.console_access(vm),
            db,
            registry,
            db.get_console("con"),
            values={},
            layout="tiled",
        )

    assert caught.value is expected
    # The cleanup was attempted after the failed creation.
    new_session = next(i for i, command in enumerate(guest.commands) if "tmux new-session" in command)
    assert guest.commands[new_session + 1 :]
    assert not model.has_session("aw-console-con")
    assert not any("rename-session" in command for command in guest.commands)
    console = db.get_console("con")
    assert console is not None
    assert console.last_started_at is None


@pytest.mark.parametrize("primary_type,close_type", _FAILURE_CASES)
def test_log_close_preserves_failure_priority(
    db: Database,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    console_guest_factory: Callable[..., _FakeGuest],
    primary_type: type[BaseException],
    close_type: type[BaseException],
) -> None:
    _seed_vm(db)
    create_console_record(db, name="con", vm_name="vm1", session_specs=[], add_admin_shell=True)
    registry = bootstrap.load_request_registry(_StubConfig(), live_database=db)
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    primary = primary_type("build failed")
    closing = close_type("log close interrupted")
    model = TmuxModel()
    closes = []

    def close(_logger):
        closes.append(True)
        raise closing

    monkeypatch.setattr("agentworks.ssh.SSHLogger.close", close)
    guest = _guest(console_guest_factory, model, primary=primary, cleanup=None)
    vm = db.get_vm("vm1")
    assert vm is not None

    expected = closing if isinstance(closing, (UserAbort, KeyboardInterrupt)) else primary
    with pytest.raises(type(expected)) as caught:
        mc._build_console_tmux(
            guest.console_access(vm),
            db,
            registry,
            db.get_console("con"),
            values={},
            layout="tiled",
        )

    assert caught.value is expected
    assert closes == [True]
    assert not model.has_session("aw-console-con")
    assert not any("rename-session" in command for command in guest.commands)
    console = db.get_console("con")
    assert console is not None
    assert console.last_started_at is None
