"""Cross-resource safety boundaries for local and live-enriched lists."""

from __future__ import annotations

import base64
import contextlib
import re
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from agentworks.db import SessionMode, SessionStatus, VMStatus
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.secrets.resolver import Resolver
from agentworks.sessions import manager as session_manager
from agentworks.sessions.multi_console import console_listing
from agentworks.vms import manager as vm_manager
from agentworks.vms.manager import inspect

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.config import Config
    from agentworks.db import Database


_DATABASE_WRITE_ACTIONS = {
    sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_UPDATE,
}

# This complete program is pinned independently because it crosses a security
# boundary: a semantic tmux whitelist alone cannot detect an unrelated shell
# mutator appended elsewhere in the transport command.
_SESSION_READ_ONLY_PROBE_COMMAND = (
    "hex() { od -An -tx1 | tr -d ' \\n'; }; "
    'decode() { printf %s "$1" | base64 -d; }; '
    "BOOT=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null); B=$?; "
    'BOOT_HEX=$(printf %s "$BOOT" | hex); '
    "while read -r N64 S64 M PID; do "
    'N=$(decode "$N64") || exit 90; SOCK=$(decode "$S64") || exit 90; '
    'run_tmux() { if [ "$M" = a ]; then sudo -n tmux -S "$SOCK" "$@"; '
    'else tmux -S "$SOCK" "$@"; fi; }; '
    'HERR=$(run_tmux has-session -t "=$N" 2>&1 >/dev/null); H=$?; '
    'HHEX=$(printf %s "$HERR" | hex); '
    "SERR=$(run_tmux list-sessions 2>&1 >/dev/null); S=$?; "
    'SHEX=$(printf %s "$SERR" | hex); '
    'if [ "$PID" = - ]; then PFACT=; P=1; '
    'elif [ "$M" = a ]; then '
    'PFACT=$(sudo -n sh -c "if test -d /proc/$PID; then printf present; '
    'else printf absent; fi" 2>/dev/null); P=$?; '
    'else PFACT=$(sh -c "if test -d /proc/$PID; then printf present; '
    'else printf absent; fi" 2>/dev/null); P=$?; fi; '
    'PHEX=$(printf %s "$PFACT" | hex); '
    'printf "S:%s:%s:%s:%s:%s:%s:%s:%s\\n" '
    '"$N64" "$H" "$HHEX" "$S" "$SHEX" "$B" "$BOOT_HEX" "$P" "$PHEX"; '
    "done"
)


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
    """Arm the shared activation, repair, and provider-mutation seams."""
    monkeypatch.setattr(
        "agentworks.orchestration.activation.activation_gate",
        _forbidden("activation"),
    )
    monkeypatch.setattr(ProxmoxPlatform, "start", _forbidden("provider start"))
    monkeypatch.setattr(ProxmoxPlatform, "stop", _forbidden("provider stop"))
    monkeypatch.setattr(vm_manager, "_ensure_tailscale", _forbidden("Tailscale repair"))
    monkeypatch.setattr(session_manager, "ensure_pids_batch", _forbidden("PID repair"))
    monkeypatch.setattr(session_manager, "_repair_session_pid", _forbidden("single PID repair"))


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
        socket_path="/tmp/work.sock",
    )


@dataclass
class _Result:
    returncode: int
    stdout: str
    stderr: str = ""


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
    monkeypatch.setattr("agentworks.transports.transport", _forbidden("guest transport"))
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
    name_key = base64.b64encode(b"work").decode("ascii")

    class Target:
        def run(self, command: str, **kwargs: object) -> _Result:
            assert command == _SESSION_READ_ONLY_PROBE_COMMAND
            input_data = kwargs.pop("input_data")
            assert kwargs == {
                "check": False,
                "timeout": 10,
                "retries": 1,
                "tty": False,
            }
            raw_tmux = r"(?<![A-Za-z0-9_])tmux(?![A-Za-z0-9_])"
            wrapper = re.search(r"run_tmux\(\) \{(?P<body>.*?)\};", command)
            assert wrapper is not None
            wrapper_body = " ".join(wrapper.group("body").split())
            assert wrapper_body == (
                'if [ "$M" = a ]; then sudo -n tmux -S "$SOCK" "$@"; else tmux -S "$SOCK" "$@"; fi;'
            )
            assert re.findall(raw_tmux, wrapper_body) == ["tmux", "tmux"]
            outside_wrapper = command[: wrapper.start()] + command[wrapper.end() :]
            assert re.search(raw_tmux, outside_wrapper) is None
            run_tmux = r"(?<![A-Za-z0-9_])run_tmux(?![A-Za-z0-9_])"
            assert len(re.findall(run_tmux, outside_wrapper)) == 2
            assert re.findall(r"\$\(run_tmux ([^)]*)\)", outside_wrapper) == [
                'has-session -t "=$N" 2>&1 >/dev/null',
                "list-sessions 2>&1 >/dev/null",
            ]
            assert re.search(r"\b(eval|exec|source)\b", command) is None

            assert isinstance(input_data, str)
            assert input_data.endswith("\n")
            lines = input_data.splitlines()
            assert len(lines) == 1
            encoded_name, encoded_socket, mode, pid = lines[0].split()
            assert base64.b64decode(encoded_name, validate=True).decode() == "work"
            assert base64.b64decode(encoded_socket, validate=True).decode() == "/tmp/work.sock"
            assert (mode, pid) == ("u", "-")
            events.append("transport-read")
            return _Result(0, f"S:{name_key}:0::0::0::0:\n")

    def open_transport(*args: object, **kwargs: object) -> Target:
        assert len(args) == 2
        assert args[0] == vm
        assert args[1] is config
        assert kwargs == {"default_timeout": 10}
        events.append("transport-open")
        return Target()

    def require_identity(*_args: object, **_kwargs: object) -> None:
        events.append("ssh-identity")

    monkeypatch.setattr(ProxmoxPlatform, "status", _forbidden("provider status"))
    monkeypatch.setattr(Resolver, "resolve", _forbidden("secret resolution"))
    monkeypatch.setattr(session_manager, "transport", open_transport)
    monkeypatch.setattr("agentworks.transports.transport", open_transport)
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
    assert events == ["ssh-identity", "transport-open", "transport-read"]


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
    vm = db.get_vm("box")
    assert vm is not None

    class Target:
        def run(self, command: str, **kwargs: object) -> _Result:
            assert command == "tmux list-sessions -F '#{session_name}'"
            assert kwargs == {
                "check": False,
                "tty": False,
                "timeout": 10,
                "retries": 1,
            }
            events.append("transport-read")
            return _Result(0, "aw-console-desk\n")

    def open_transport(*args: object, **kwargs: object) -> Target:
        assert len(args) == 2
        assert args[0] == vm
        assert args[1] is config
        assert kwargs == {"default_timeout": 10}
        events.append("transport-open")
        return Target()

    def require_identity(*_args: object, **_kwargs: object) -> None:
        events.append("ssh-identity")

    monkeypatch.setattr(ProxmoxPlatform, "status", _forbidden("provider status"))
    monkeypatch.setattr(Resolver, "resolve", _forbidden("secret resolution"))
    monkeypatch.setattr("agentworks.transports.transport", open_transport)
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
    assert events == ["ssh-identity", "transport-open", "transport-read"]
