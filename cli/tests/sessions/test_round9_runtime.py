"""Native ownership and dispatch checkpoints bound session recovery authority."""

from __future__ import annotations

import socket
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from agentworks.db import PID_STOPPED, SessionMode, SessionStatus
from agentworks.errors import AgentworksError, ExternalError, StateError
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager, tmux
from agentworks.sessions.manager import _lifecycle

from .test_create_start_restart_orchestrated import _create_stubs, _restart_fixture
from .test_create_start_restart_orchestrated import _stub_build_registry as _stub_build_registry
from .test_native_startup import _NATIVE_CAPTURE, launch
from .test_native_startup import native as native
from .test_private_startup_boundaries import record_native_runtime
from .test_round8_startup import bind_native_runtime

CONFIG = SimpleNamespace(session=SimpleNamespace(history_limit=1))

if TYPE_CHECKING:
    from agentworks.db import Database

    from .test_native_startup import NativeTarget


def stopped_row(db: Database, native: NativeTarget) -> None:
    db.update_session_runtime(
        "s1", socket_path=native.socket, pid=PID_STOPPED, boot_id=None, tmux_server_start_ticks=None
    )


def invoke(db: Database, operation: str, **kwargs: Any) -> None:
    if operation in {"restart_session", "delete_session"}:
        kwargs["yes"] = True
    getattr(manager, operation)(db, CONFIG, name="s1", interaction=TtyInteractionPolicy.REFUSE, **kwargs)


@pytest.mark.parametrize("operation", ["start_session", "stop_session", "restart_session", "delete_session"])
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("row_kind", ["stopped", "uncertain"])
@pytest.mark.parametrize("native_name", ["s1", "unrecorded"])
def test_unrecorded_native_server_is_never_adopted_or_destroyed(
    tmp_path, monkeypatch, native, operation, force, row_kind, native_name
):
    db, events = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    bind_native_runtime(db, monkeypatch, native)
    stopped_row(db, native)
    if row_kind == "uncertain":
        db.set_session_run_id("s1", str(uuid4()))
        db.update_session_runtime(
            "s1", socket_path=native.socket, pid=None, boot_id=tmux.read_boot_id(native), tmux_server_start_ticks=None
        )
    launch(native, "sleep 30", is_admin=False)
    if native_name != "s1":
        native.native(f"rename-session -t '=s1' {native_name}")
    before = db.get_session("s1")
    assert before is not None
    assert manager.check_session_status(before, target=native) is SessionStatus.UNKNOWN
    assert manager.batch_check_status([replace(before, mode="admin")], target=native) == {"s1": SessionStatus.UNKNOWN}
    fingerprint = _NATIVE_CAPTURE(target=native, socket_path=native.socket).fingerprint
    events.clear()
    first = len(native.calls)
    try:
        with pytest.raises(AgentworksError):
            invoke(db, operation, force=force)
        if operation == "stop_session" and row_kind == "stopped":
            with pytest.raises(StateError):
                manager._teardown_session(
                    before, target=native, target_owns_session=True, db=db, force=force, legacy_running_authorized=False
                )
        assert db.get_session("s1") == before
        assert _NATIVE_CAPTURE(target=native, socket_path=native.socket).fingerprint == fingerprint
        if operation in {"start_session", "restart_session"}:
            assert "resolve" not in events and "resolve_env" not in events
        assert not any(
            mutation in command
            for command, _ in native.calls[first:]
            for mutation in ("kill-server", "new-session", "chmod ", "server-access", "remain-on-exit off")
        )
    finally:
        db.close()


@pytest.mark.parametrize("operation", ["create_session", "start_session", "restart_session"])
@pytest.mark.parametrize("failure", ["directory", "regular-file", "stale-remove", "checkpoint"])
def test_known_pre_spawn_failure_can_retry_in_same_boot(tmp_path, monkeypatch, native, operation, failure):
    if operation == "create_session":
        db = _create_stubs(tmp_path, monkeypatch, [])
        db.insert_agent("a1", "vm1", "agt-a1")
        options = {"workspace": "ws1", "agent": "a1"}
    else:
        db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
        options = {}
    bind_native_runtime(db, monkeypatch, native)
    monkeypatch.setattr(
        tmux, "create_session", lambda *a, **k: launch(native, "sleep 30", is_admin=False, launch_options=k)
    )
    if operation != "create_session":
        stopped_row(db, native)
    original_update = db.update_session_runtime
    original_run = native.run

    def reject_directory(*args):
        raise ExternalError("socket directory unavailable")

    def reject_checkpoint(*args, **kwargs):
        if kwargs.get("pid") is None:
            raise StateError("checkpoint unavailable")
        return original_update(*args, **kwargs)

    def reject_remove(command, **kwargs):
        if "rm -f --" in command:
            raise ExternalError("stale socket removal unavailable")
        return original_run(command, **kwargs)

    if failure == "directory":
        monkeypatch.setattr(tmux, "ensure_agent_socket_dir", reject_directory)
    elif failure == "regular-file":
        Path(native.socket).write_text("preserve this file")
    elif failure == "stale-remove":
        with socket.socket(socket.AF_UNIX) as stale:
            stale.bind(native.socket)
        monkeypatch.setattr(native, "run", reject_remove)
    else:
        monkeypatch.setattr(db, "update_session_runtime", reject_checkpoint)
    try:
        with pytest.raises(AgentworksError):
            invoke(db, operation, **options)
        assert not any("new-session" in command for command, _ in native.calls)
        row = db.get_session("s1")
        if operation == "create_session":
            assert row is None
        else:
            assert row is not None and row.pid == PID_STOPPED and row.boot_id is None
        if failure == "regular-file":
            assert Path(native.socket).read_text() == "preserve this file"
            Path(native.socket).unlink()
        monkeypatch.setattr(tmux, "ensure_agent_socket_dir", lambda *a: None)
        monkeypatch.setattr(db, "update_session_runtime", original_update)
        monkeypatch.setattr(native, "run", original_run)
        invoke(db, operation, **options)
        current = db.get_session("s1")
        observed = _NATIVE_CAPTURE(target=native, socket_path=native.socket).fingerprint
        assert current is not None and observed is not None and current.pid == observed.pid
        assert sum("new-session" in command for command, _ in native.calls) == 1
    finally:
        db.close()


@pytest.mark.parametrize("mutation", ["uuid", "run", "name", "replacement"])
def test_unacknowledged_recovery_requires_exact_attempt_and_stable_server(tmp_path, monkeypatch, native, mutation):
    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    bind_native_runtime(db, monkeypatch, native)
    stopped_row(db, native)

    def lost_ack(*args, **kwargs):
        launch(native, "sleep 30", is_admin=False, launch_options=kwargs)
        raise ExternalError("lost acknowledgement")

    monkeypatch.setattr(tmux, "create_session", lost_ack)
    try:
        with pytest.raises(ExternalError):
            invoke(db, "start_session")
        before = db.get_session("s1")
        assert before is not None and before.pid is None
        if mutation == "name":
            native.native("rename-session -t '=s1' different")
        elif mutation == "replacement":
            original_run = native.run
            changed: list[bool] = []

            def replace_after_metadata(command, **kwargs):
                result = original_run(command, **kwargs)
                if "AGW_RUNTIME_LAUNCH=" in command and not changed:
                    changed.append(True)
                    native.native("kill-server")
                    Path(native.socket).unlink(missing_ok=True)
                    launch(native, "sleep 30", is_admin=False)
                return result

            monkeypatch.setattr(native, "run", replace_after_metadata)
        else:
            key = "_AGW_SESSION_UUID" if mutation == "uuid" else "_AGW_RUN_ID"
            native.native(f"set-environment -t '=s1' {key} {uuid4()}")
        first = len(native.calls)
        with pytest.raises(StateError):
            invoke(db, "start_session")
        assert db.get_session("s1") == before
        assert native.native("list-sessions").ok
        if mutation != "replacement":
            assert not any("kill-server" in command for command, _ in native.calls[first:])
    finally:
        db.close()


@pytest.mark.parametrize("old_null_address", [False, True])
@pytest.mark.parametrize("operation", ["start_session", "stop_session", "restart_session", "delete_session"])
def test_failed_admin_launch_recovers_after_boot_absence(tmp_path, monkeypatch, native, old_null_address, operation):
    db = _create_stubs(tmp_path, monkeypatch, [])
    bind_native_runtime(db, monkeypatch, native)
    monkeypatch.setattr(_lifecycle, "ADMIN_SOCKET_ROOT", str(native.root))
    # The namespaced owner path must agree with the fixture VM admin user.
    vm = db.get_vm("vm1")
    assert vm is not None
    native.socket = str(native.root / vm.admin_username / "s1.sock")
    Path(native.socket).parent.mkdir(exist_ok=True)
    original_run = native.run

    def lose_dispatch(command, **kwargs):
        if "new-session" in command:
            raise ExternalError("launch dispatch unavailable")
        return original_run(command, **kwargs)

    monkeypatch.setattr(native, "run", lose_dispatch)
    monkeypatch.setattr(
        tmux, "create_session", lambda *a, **k: launch(native, "sleep 30", is_admin=True, launch_options=k)
    )
    try:
        with pytest.raises(ExternalError):
            invoke(db, "create_session", workspace="ws1", admin=True)
        row = db.get_session("s1")
        assert row is not None and row.pid is None and row.socket_path == native.socket
        if old_null_address:
            db.update_session_runtime(
                "s1", socket_path=None, pid=None, boot_id=row.boot_id, tmux_server_start_ticks=None
            )
        before = db.get_session("s1")
        with pytest.raises(StateError):
            invoke(db, operation)
        assert db.get_session("s1") == before
        monkeypatch.setattr(native, "run", original_run)
        # Simulate only the authoritative boot observation, with real native
        # default/dedicated server absence. No VM or host boot is changed.
        monkeypatch.setattr(manager, "_get_boot_id", lambda *a, **k: str(uuid4()))
        invoke(db, operation)
        current = db.get_session("s1")
        if operation == "delete_session":
            assert current is None
        else:
            assert current is not None and current.socket_path == native.socket
            assert (current.pid == PID_STOPPED) is (operation == "stop_session")
        assert db.get_vm("vm1") is not None and db.get_workspace("ws1") is not None
    finally:
        db.close()


def test_unknown_startup_observation_refuses_before_secrets_and_runtime_mutation(tmp_path, monkeypatch, native):
    db, events = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    bind_native_runtime(db, monkeypatch, native)
    fingerprint = record_native_runtime(db, native)
    before = db.get_session("s1")
    original_run = native.run

    def unknown_startup(command, **kwargs):
        if "AGW_RUNTIME_STARTUP=" in command:
            return SimpleNamespace(returncode=0, ok=True, stdout="unframed", stderr="")
        return original_run(command, **kwargs)

    monkeypatch.setattr(native, "run", unknown_startup)
    events.clear()
    first = len(native.calls)
    try:
        with pytest.raises(StateError):
            invoke(db, "start_session")
        assert events == []
        assert db.get_session("s1") == before
        assert _NATIVE_CAPTURE(target=native, socket_path=native.socket).fingerprint == fingerprint
        assert native.native("show-option -wAqv -t %0 remain-on-exit").stdout.strip() == "on"
        assert not any(
            mutation in command
            for command, _ in native.calls[first:]
            for mutation in ("kill-server", "new-session", "chmod ", "server-access", "remain-on-exit off")
        )
    finally:
        db.close()


def test_maintenance_socket_guard_covers_the_entire_framed_probe(monkeypatch, native):
    import shutil

    launch(native, "sleep 30")
    scan = native.root / "scan"
    scan.mkdir()
    stale = scan / "stale.sock"
    with socket.socket(socket.AF_UNIX) as handle:
        handle.bind(str(stale))
    observed = native.root / "tmux-was-invoked"
    binary = native.root / "tmux"
    binary.write_text(f'#!/bin/sh\nprintf called >> {observed}\nexec {shutil.which("tmux")} "$@"\n')
    binary.chmod(0o700)
    native.environment["PATH"] = f"{native.root}:/usr/bin:/bin"
    original_run = native.run

    def replace_after_enumeration(command, **kwargs):
        result = original_run(command, **kwargs)
        if "AGW_STALE_SOCKETS_BEGIN" in command:
            stale.unlink()
            stale.symlink_to(native.socket)
        return result

    monkeypatch.setattr(native, "run", replace_after_enumeration)
    assert tmux._cleanup_stale_sockets_under(native, str(scan)) == 0
    assert stale.is_symlink()
    assert not observed.exists()
    assert native.native("list-sessions").ok


@pytest.mark.parametrize("obstruction", ["default", "dedicated", "unknown-default"])
def test_old_admin_null_address_requires_native_absence_before_repair(tmp_path, monkeypatch, native, obstruction):
    db = _create_stubs(tmp_path, monkeypatch, [])
    bind_native_runtime(db, monkeypatch, native)
    db.insert_session("s1", "ws1", "claude", SessionMode.ADMIN)
    db.update_session_runtime("s1", socket_path=None, pid=None, boot_id=str(uuid4()), tmux_server_start_ticks=None)
    before = db.get_session("s1")
    original_run = native.run
    if obstruction == "dedicated":
        launch(native, "sleep 30")
    elif obstruction == "default":
        native.run("tmux new-session -d -s s1 'sleep 30'")
    else:

        def unknown_default(command, **kwargs):
            if "AGW_RUNTIME_SESSION=" in command and "-S " not in command:
                return SimpleNamespace(returncode=255, ok=False, stdout="", stderr="unavailable")
            return original_run(command, **kwargs)

        monkeypatch.setattr(native, "run", unknown_default)
    try:
        with pytest.raises(AgentworksError):
            manager._ensure_pid(before, target=native, db=db)
        assert db.get_session("s1") == before
        if obstruction == "default":
            assert native.run("tmux has-session -t '=s1'", check=False).ok
        elif obstruction == "dedicated":
            assert native.native("list-sessions").ok
    finally:
        if obstruction == "default":
            native.run("tmux kill-server", check=False)
        db.close()


@pytest.mark.parametrize("operation", ["start_session", "stop_session", "restart_session", "delete_session"])
def test_old_admin_stopped_null_address_becomes_addressable_after_native_absence(
    tmp_path, monkeypatch, native, operation
):
    db = _create_stubs(tmp_path, monkeypatch, [])
    bind_native_runtime(db, monkeypatch, native)
    monkeypatch.setattr(_lifecycle, "ADMIN_SOCKET_ROOT", str(native.root))
    native.socket = str(native.root / "admin" / "s1.sock")
    Path(native.socket).parent.mkdir()
    monkeypatch.setattr(tmux, "create_session", lambda *a, **k: launch(native, "sleep 30", launch_options=k))
    db.insert_session("s1", "ws1", "claude", SessionMode.ADMIN)
    db.update_session_runtime("s1", socket_path=None, pid=PID_STOPPED, boot_id=None, tmux_server_start_ticks=None)
    native.run("tmux new-session -d -s unrelated 'sleep 30'")
    first = len(native.calls)
    try:
        invoke(db, operation)
        row = db.get_session("s1")
        if operation == "delete_session":
            assert row is None
        else:
            assert row is not None and row.socket_path == native.socket
            assert (row.pid == PID_STOPPED) is (operation == "stop_session")
        assert db.get_workspace("ws1") is not None and db.get_vm("vm1") is not None
        assert native.run("tmux has-session -t '=unrelated'", check=False).ok
        assert not any("kill-server" in command for command, _ in native.calls[first:])
        assert sum("new-session" in command for command, _ in native.calls[first:]) == int(
            operation in {"start_session", "restart_session"}
        )
    finally:
        native.run("tmux kill-server", check=False)
        db.close()


def test_real_create_lost_ack_repr_secret_is_redacted_in_rollback_and_canonical_log(
    tmp_path, monkeypatch, native, capsys
):
    from agentworks import cli
    from agentworks.secrets.resolver import Resolver

    db = _create_stubs(tmp_path, monkeypatch, [])
    db.insert_agent("a1", "vm1", "agt-a1")
    bind_native_runtime(db, monkeypatch, native)
    monkeypatch.setattr(
        tmux, "create_session", lambda *a, **k: launch(native, "sleep 30", is_admin=False, launch_options=k)
    )
    marker = uuid4().hex
    secret = f"prefix'{marker}\"suffix"
    monkeypatch.setattr(Resolver, "values", property(lambda self: {"token": secret}))
    monkeypatch.setattr(manager, "_resolve_session_env", lambda *a, **k: {"TOKEN": secret})
    monkeypatch.setattr("agentworks.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("sys.argv", ["agw", "session", "create"])
    original_run = native.run
    failures = []

    def lose_ack(command, **kwargs):
        result = original_run(command, **kwargs)
        if "new-session" in command:
            failure = ExternalError(f"native launch failed: {command!r}", hint=repr(command))
            failures.append(failure)
            raise failure
        return result

    monkeypatch.setattr(native, "run", lose_ack)
    monkeypatch.setattr(cli, "app", lambda **_: invoke(db, "create_session", workspace="ws1", agent="a1"))
    try:
        with pytest.raises(SystemExit) as caught:
            cli.main()
        assert caught.value.code == 1
        row = db.get_session("s1")
        assert row is not None and row.pid is None and row.socket_path == native.socket
        assert native.native("list-sessions").ok
        assert failures
        printed = capsys.readouterr()
        log = tmp_path / "logs" / "error.log"
        assert log.is_file()
        assert marker not in printed.out + printed.err + log.read_text()
        assert marker not in str(failures[0]) + str(failures[0].hint)
    finally:
        db.close()


@pytest.mark.parametrize("operation", ["start_session", "stop_session", "restart_session", "delete_session"])
def test_rejected_create_recovery_row_cannot_adopt_preexisting_server(tmp_path, monkeypatch, native, operation):
    db = _create_stubs(tmp_path, monkeypatch, [])
    db.insert_agent("a1", "vm1", "agt-a1")
    bind_native_runtime(db, monkeypatch, native)
    monkeypatch.setattr(
        tmux, "create_session", lambda *a, **k: launch(native, "sleep 30", is_admin=False, launch_options=k)
    )
    launch(native, "sleep 30", is_admin=False)
    native.native("rename-session -t '=s1' preexisting")
    observed = _NATIVE_CAPTURE(target=native, socket_path=native.socket).fingerprint

    def retain_artifacts(*args, **kwargs):
        raise StateError("artifact cleanup observation unavailable")

    monkeypatch.setattr("agentworks.artifacts.session.cleanup_session_artifacts", retain_artifacts)
    try:
        with pytest.raises(RuntimeError):
            invoke(db, "create_session", workspace="ws1", agent="a1")
        before = db.get_session("s1")
        assert before is not None and before.pid is None and before.boot_id is None
        first = len(native.calls)
        with pytest.raises(StateError):
            invoke(db, operation, force=True)
        assert db.get_session("s1") == before
        assert _NATIVE_CAPTURE(target=native, socket_path=native.socket).fingerprint == observed
        assert native.native("has-session -t '=preexisting'").ok
        assert not any("kill-server" in command for command, _ in native.calls[first:])
    finally:
        db.close()


@pytest.mark.parametrize("native_name", ["s1", "residual"])
def test_recorded_native_runtime_keeps_running_and_residual_inventory(tmp_path, monkeypatch, native, native_name):
    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    bind_native_runtime(db, monkeypatch, native)
    record_native_runtime(db, native)
    if native_name != "s1":
        native.native("rename-session -t '=s1' residual")
    row = db.get_session("s1")
    assert row is not None
    expected = SessionStatus.RUNNING if native_name == "s1" else SessionStatus.RESIDUAL
    try:
        assert manager.check_session_status(row, target=native) is expected
        assert manager.batch_check_status([replace(row, mode="admin")], target=native) == {"s1": expected}
    finally:
        db.close()
