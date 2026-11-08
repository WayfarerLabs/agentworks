"""Acknowledged startup failures keep their managed recovery boundaries."""

from __future__ import annotations

import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from agentworks.db import Database, SessionStatus
from agentworks.errors import ExternalError, StateError
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager, tmux

from .test_create_start_restart_orchestrated import (
    BOOT_ID,
    _create_stubs,
    _restart_fixture,
)
from .test_create_start_restart_orchestrated import (
    _stub_build_registry as _stub_build_registry,
)


@pytest.mark.parametrize("operation", ["start_session", "restart_session"])
def test_launch_failure_redacts_the_resolved_secret_without_replacing_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    db, _events = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    secret = uuid4().hex
    failure = ExternalError(secret, hint=secret)
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: {"token": secret})

    def fail(*args: object, **kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(tmux, "create_session", fail)
    with pytest.raises(ExternalError) as caught:
        getattr(manager, operation)(
            db,
            SimpleNamespace(session=SimpleNamespace(history_limit=1)),
            name="s1",
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert caught.value is failure
    assert secret not in "".join(traceback.format_exception(failure))
    assert failure.hint is not None and secret not in failure.hint
    db.close()


def _fake_owner_realizers(monkeypatch: pytest.MonkeyPatch) -> None:
    def realize_workspace(db_: Database, config: Any, registry: Any, **kwargs: Any) -> None:
        db_._conn.execute(
            "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) VALUES (?, ?, '/tmp/ws', ?)",
            (kwargs["name"], kwargs["vm"].name, f"ws-{kwargs['name']}"),
        )
        db_._conn.commit()
        from agentworks.instance_specs import persist_creation_overlay

        persist_creation_overlay(db_, "workspace", kwargs["name"], kwargs["overlay"])

    def realize_agent(db_: Database, config: Any, registry: Any, **kwargs: Any) -> None:
        db_.insert_agent(kwargs["name"], kwargs["vm"].name, f"agt-{kwargs['name']}")
        from agentworks.instance_specs import persist_creation_overlay

        persist_creation_overlay(db_, "agent", kwargs["name"], kwargs["overlay"])

    monkeypatch.setattr("agentworks.workspaces.realize.realize_workspace", realize_workspace)
    monkeypatch.setattr("agentworks.agents.realize.realize_agent", realize_agent)
    monkeypatch.setattr(
        "agentworks.agents.manager.delete_agent", lambda db_, config, *, name, **k: db_.delete_agent(name)
    )
    monkeypatch.setattr(
        "agentworks.workspaces.manager.delete_workspace", lambda db_, config, *, name, **k: db_.delete_workspace(name)
    )


@pytest.mark.parametrize("stage", ["fingerprint", "observation", "grant", "interrupt"])
def test_post_spawn_failure_retains_new_owners_and_original_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    db = _create_stubs(tmp_path, monkeypatch, [])

    _fake_owner_realizers(monkeypatch)
    failure: BaseException
    if stage == "interrupt":
        failure = KeyboardInterrupt()
    elif stage == "observation":
        failure = StateError(uuid4().hex)
    else:
        failure = ExternalError(uuid4().hex)

    def finalize(*args: object, **kwargs: object) -> None:
        row = db.get_session("s1")
        assert row is not None and row.pid == 4243 and row.boot_id == BOOT_ID
        assert row.tmux_server_start_ticks == 1
        raise failure

    monkeypatch.setattr(tmux, "finalize_session_start", finalize, raising=False)
    if stage == "fingerprint":
        monkeypatch.setattr(
            tmux, "capture_tmux_server_fingerprint", lambda **kwargs: tmux.FingerprintProbe(tmux.ProbeStatus.UNKNOWN)
        )

    with pytest.raises(type(failure)) as caught:
        manager.create_session(
            db,
            SimpleNamespace(session=SimpleNamespace(history_limit=1), paths=SimpleNamespace(vm_workspaces="/srv")),
            name="s1",
            new_workspace=True,
            workspace_spec='{"tmuxinator":false}',
            new_agent=True,
            agent_spec='{"shell":"zsh"}',
            vm_name="vm1",
            interaction=TtyInteractionPolicy.REFUSE,
        )  # type: ignore[arg-type]

    if stage != "fingerprint":
        assert caught.value is failure
    row = db.get_session("s1")
    assert row is not None and row.socket_path is not None
    assert db.get_agent("s1") is not None and db.get_workspace("s1") is not None
    if stage == "fingerprint":
        assert row.pid is None and row.boot_id == BOOT_ID
    else:
        assert row.pid == 4243 and row.boot_id == BOOT_ID
    db.close()


@pytest.mark.parametrize("operation", ["start_session", "restart_session"])
@pytest.mark.parametrize("value_shape", ["literal", "quoted", "ansi"])
def test_launch_secret_stays_out_of_terminal_and_cli_error_log(tmp_path, monkeypatch, capsys, operation, value_shape):
    from agentworks import cli
    from agentworks.sessions.tmux import _tmux_env_flags

    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    marker = uuid4().hex
    secret = {
        "literal": marker,
        "quoted": f"prefix'{marker}'suffix",
        "ansi": f"prefix'{marker}\x1b[31m'suffix",
    }[value_shape]
    command = "tmux new-session" + _tmux_env_flags({"TOKEN": secret})
    failure = ExternalError(command, hint=command)
    monkeypatch.setattr("agentworks.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("sys.argv", ["agw", "session", operation, "s1"])
    monkeypatch.delenv("AGW_DEBUG", raising=False)
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: {"token": secret})

    def fail(*args, **kwargs):
        raise failure

    def run_operation():
        getattr(manager, operation)(
            db,
            SimpleNamespace(session=SimpleNamespace(history_limit=1)),
            name="s1",
            interaction=TtyInteractionPolicy.REFUSE,
        )

    monkeypatch.setattr(tmux, "create_session", fail)
    monkeypatch.setattr(cli, "app", run_operation)
    try:
        with pytest.raises(SystemExit) as caught:
            cli.main()
        assert caught.value.code == 1
        printed = capsys.readouterr()
        assert marker not in printed.out + printed.err
        error_log = tmp_path / "logs" / "error.log"
        assert error_log.exists() and marker not in error_log.read_text()
        assert marker not in str(failure) + str(failure.hint)
    finally:
        db.close()
