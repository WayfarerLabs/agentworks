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
from tests.conftest import _FakeTarget
from tests.console_helpers import create_console_record

if TYPE_CHECKING:
    from agentworks.db import Database


_FAILURE_CASES = [
    (ExternalError, StateError),
    (ExternalError, UserAbort),
    (ExternalError, KeyboardInterrupt),
    (UserAbort, StateError),
    (KeyboardInterrupt, StateError),
]


@pytest.mark.parametrize("logged", [False, True])
@pytest.mark.parametrize("primary_type,cleanup_type", _FAILURE_CASES)
def test_build_cleanup_preserves_failure_priority_and_never_publishes(
    db: Database,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
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

    class Target(_FakeTarget):
        creation_failed = False
        cleanup_attempted = False

        def run(self, command, **kwargs):
            if "tmux new-session" in command:
                self.commands.append(command)
                self.creation_failed = True
                raise primary
            if self.creation_failed:
                self.commands.append(command)
                self.cleanup_attempted = True
                raise cleanup
            return super().run(command, **kwargs)

    target = Target(model=model)
    build = mc._build_console_tmux if logged else _build_console_tmux_unlogged
    expected = cleanup if isinstance(cleanup, (UserAbort, KeyboardInterrupt)) else primary
    with pytest.raises(type(expected)) as caught:
        build(target, db, registry, db.get_console("con"), db.get_vm("vm1"), values={}, layout="tiled")

    assert caught.value is expected
    assert target.cleanup_attempted
    assert not model.has_session("aw-console-con")
    assert not any("rename-session" in command for command in target.commands)
    console = db.get_console("con")
    assert console is not None
    assert console.last_started_at is None
    assert target.logger is None


@pytest.mark.parametrize("primary_type,close_type", _FAILURE_CASES)
def test_log_close_preserves_failure_priority_and_restores_transport_logger(
    db: Database,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
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

    class Target(_FakeTarget):
        def run(self, command, **kwargs):
            if "tmux new-session" in command:
                self.commands.append(command)
                raise primary
            return super().run(command, **kwargs)

    target = Target(model=model)
    expected = closing if isinstance(closing, (UserAbort, KeyboardInterrupt)) else primary
    with pytest.raises(type(expected)) as caught:
        mc._build_console_tmux(
            target,
            db,
            registry,
            db.get_console("con"),
            db.get_vm("vm1"),
            values={},
            layout="tiled",
        )

    assert caught.value is expected
    assert closes == [True]
    assert target.logger is None
    assert not model.has_session("aw-console-con")
    assert not any("rename-session" in command for command in target.commands)
    console = db.get_console("con")
    assert console is not None
    assert console.last_started_at is None
