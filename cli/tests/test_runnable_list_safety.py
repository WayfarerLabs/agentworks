"""Cross-resource safety boundaries for local and live-enriched lists."""

from __future__ import annotations

import base64
import contextlib
import sqlite3
from typing import TYPE_CHECKING

import pytest

from agentworks.db import SessionMode, SessionStatus, VMStatus
from agentworks.execution.requests import Bytes, Script, Shell
from agentworks.execution.results import Completed, Result
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.secrets.resolver import Resolver
from agentworks.sessions import manager as session_manager
from agentworks.sessions.manager._status import _BATCH_ROW_SECONDS
from agentworks.sessions.multi_console import console_listing
from agentworks.sessions.tmux import ADMIN_SOCKET_ROOT
from agentworks.vms.manager import inspect
from tests.vm_access_support import Admin, Call, install_access_recorder

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.config import Config
    from agentworks.db import Database


_DATABASE_WRITE_ACTIONS = {
    sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_UPDATE,
}


@contextlib.contextmanager
def _record_database_writes(db: Database) -> Iterator[list[tuple[int, str | None, str | None]]]:
    """Observe SQLite's write boundary without replacing Database methods."""
    writes: list[tuple[int, str | None, str | None]] = []

    def authorize(
        action: int,
        argument_one: str | None,
        argument_two: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        if action in _DATABASE_WRITE_ACTIONS:
            writes.append((action, argument_one, argument_two))
        return sqlite3.SQLITE_OK

    changes_before = db._conn.total_changes  # noqa: SLF001
    db._conn.set_authorizer(authorize)  # noqa: SLF001
    try:
        yield writes
    finally:
        db._conn.set_authorizer(None)  # noqa: SLF001
    assert db._conn.total_changes == changes_before  # noqa: SLF001


def _forbidden(seam: str):  # noqa: ANN202
    def fail(*_args: object, **_kwargs: object) -> None:
        pytest.fail(f"runnable list reached forbidden {seam} seam")

    return fail


def _guard_mutating_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """Arm the shared activation, post-start wait, and provider-mutation seams."""
    monkeypatch.setattr(
        "agentworks.orchestration.activation.activation_gate",
        _forbidden("activation"),
    )
    monkeypatch.setattr(ProxmoxPlatform, "start", _forbidden("provider start"))
    monkeypatch.setattr(ProxmoxPlatform, "stop", _forbidden("provider stop"))
    monkeypatch.setattr("agentworks.vms.manager._await_tailnet", _forbidden("post-start wait"))
    monkeypatch.setattr(session_manager, "ensure_pids_batch", _forbidden("PID repair"))
    monkeypatch.setattr(session_manager, "_repair_session_pid", _forbidden("single PID repair"))


WORK_SOCKET = f"{ADMIN_SOCKET_ROOT}/agentworks/work.sock"
"""The seeded session's socket, in the seeded VM admin's (``insert_vm``'s default ``agentworks``) directory."""


def _seed_vm(db: Database) -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")


def _seed_session(db: Database) -> None:
    _seed_vm(db)
    db.insert_workspace("ws", "/srv/ws", "box", "ws-ws")
    db.insert_session(
        "work",
        "ws",
        "default",
        SessionMode.ADMIN,
        socket_path=WORK_SOCKET,
    )
    db.update_session_runtime(
        "work",
        socket_path=WORK_SOCKET,
        pid=100,
        boot_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        tmux_server_start_ticks=1,
    )


def test_vm_plain_and_status_lists_keep_their_distinct_safety_boundaries(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only VM status enrichment may resolve and read its provider."""
    _seed_vm(db)
    config: Config = make_config()
    events: list[str] = []
    real_resolve = Resolver.resolve

    def resolve(resolver: Resolver) -> None:
        events.append("secret")
        real_resolve(resolver)

    def provider_status(
        _platform: ProxmoxPlatform,
        _row: object,
        _context: object,
    ) -> VMStatus:
        events.append("provider")
        return VMStatus.RUNNING

    monkeypatch.setattr(Resolver, "resolve", resolve)
    monkeypatch.setattr(ProxmoxPlatform, "status", provider_status)
    monkeypatch.setattr("agentworks.execution.vm_access", _forbidden("guest access"))
    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", _forbidden("SSH identity"))
    _guard_mutating_seams(monkeypatch)

    with _record_database_writes(db) as plain_writes:
        plain = inspect.vm_listing(db)

    assert plain_writes == []
    assert plain.vms[0].observed_status is None
    assert events == []

    with _record_database_writes(db) as status_writes:
        observed = inspect.vm_listing(
            db,
            config,
            include_status=True,
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert status_writes == []
    assert observed.vms[0].observed_status == "running"
    assert events == ["secret", "provider"]


def test_session_plain_and_status_lists_keep_their_distinct_safety_boundaries(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only session status enrichment may read the canonical guest transport."""
    _seed_session(db)
    config: Config = make_config()
    events: list[str] = []
    vm = db.get_vm("box")
    assert vm is not None

    def read_status(call: Call) -> Result:
        request = call.request
        assert isinstance(request, Script)
        # The guest timeout plus one row's bound, whatever the row count.
        assert (call.identity, request.elevated, request.deadline) == (Admin(), True, 10 + _BATCH_ROW_SECONDS)
        assert isinstance(request.stdin, Bytes)
        lines = request.stdin.data.decode().splitlines()
        assert len(lines) == 1
        encoded_name, encoded_socket, encoded_owner, pid = lines[0].split()
        assert base64.b64decode(encoded_name, validate=True).decode() == "work"
        assert base64.b64decode(encoded_socket, validate=True).decode() == WORK_SOCKET
        assert base64.b64decode(encoded_owner, validate=True).decode() == "agentworks"
        assert pid == "100"
        events.append("status-read")
        return Result(Completed(0), b"\nAGW_RUNTIME_BATCH=S:1:0::0::0::0:\n")

    def require_identity(*_args: object, **_kwargs: object) -> None:
        events.append("ssh-identity")

    monkeypatch.setattr(ProxmoxPlatform, "status", _forbidden("provider status"))
    monkeypatch.setattr(Resolver, "resolve", _forbidden("secret resolution"))
    recorder = install_access_recorder(monkeypatch)
    recorder.answer = read_status
    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", require_identity)
    _guard_mutating_seams(monkeypatch)

    with _record_database_writes(db) as plain_writes:
        plain = session_manager.session_listing(db, config)

    assert plain_writes == []
    assert plain.sessions[0].status == "unavailable"
    assert events == []

    with _record_database_writes(db) as status_writes:
        observed = session_manager.session_listing(db, config, include_status=True)

    assert status_writes == []
    assert observed.sessions[0].status == SessionStatus.RUNNING.value
    assert events == ["ssh-identity", "status-read"]
    assert recorder.built == [("box", Admin())]


def test_console_plain_and_status_lists_keep_their_distinct_safety_boundaries(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only console status enrichment may enumerate tmux through its guest."""
    _seed_vm(db)
    db.insert_console("desk", "box")
    config: Config = make_config()
    events: list[str] = []

    def read_status(call: Call) -> Result:
        # One bounded enumeration of the admin's default tmux server, unelevated.
        assert (call.identity, call.request) == (
            Admin(),
            Script("tmux list-sessions -F '#{session_name}'", Shell.SH, deadline=10),
        )
        events.append("status-read")
        return Result(Completed(0), b"aw-console-desk\n")

    def require_identity(*_args: object, **_kwargs: object) -> None:
        events.append("ssh-identity")

    monkeypatch.setattr(ProxmoxPlatform, "status", _forbidden("provider status"))
    monkeypatch.setattr(Resolver, "resolve", _forbidden("secret resolution"))
    recorder = install_access_recorder(monkeypatch)
    recorder.answer = read_status
    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", require_identity)
    _guard_mutating_seams(monkeypatch)

    with _record_database_writes(db) as plain_writes:
        plain = console_listing(db)

    assert plain_writes == []
    assert plain.consoles[0].status == "unavailable"
    assert events == []

    with _record_database_writes(db) as status_writes:
        observed = console_listing(db, config, include_status=True)

    assert status_writes == []
    assert observed.consoles[0].status == "running"
    assert events == ["ssh-identity", "status-read"]
    assert recorder.built == [("box", Admin())]
