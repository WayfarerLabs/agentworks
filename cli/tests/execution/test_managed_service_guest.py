"""Hermetic process tests for the fixed managed-service controller."""

from __future__ import annotations

import hashlib
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.execution import _managed_job_request as request_wire
from agentworks.execution import _managed_job_wire as wire
from agentworks.execution import _managed_service_bundle as bundle
from agentworks.execution import _managed_service_guest as guest
from agentworks.execution._managed_job_store import FactName, ManagedJobStore, RequestAsset, Stream

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux controller")
RUN = "a" * 32


class Boundary:
    def __init__(self) -> None:
        self.placed = False
        self.killed = False

    def place(self, pid: int) -> None:
        assert pid > 0
        self.placed = True

    def kill(self) -> None:
        self.killed = True

    def empty(self) -> bool:
        return self.killed


class HeldBoundary(Boundary):
    def empty(self) -> bool:
        return False


class FailedKillBoundary(Boundary):
    def kill(self) -> None:
        self.killed = True
        raise OSError("injected cgroup kill failure")

    def empty(self) -> bool:
        return False


class FailedEmptyBoundary(Boundary):
    def __init__(self) -> None:
        super().__init__()
        self.empty_calls = 0

    def empty(self) -> bool:
        self.empty_calls += 1
        if self.empty_calls == 1:
            raise OSError("injected cgroup observation failure")
        return False


def _launch(*, shell: str | None = None, resolved_shell: str | None = None, login: bool = False) -> bytes:
    groups = sorted(set(os.getgroups()) | {os.getegid()})
    return wire.encode_fact(
        {
            "version": 1,
            "kind": "launch",
            "run_id": RUN,
            "unit": f"agw-managed-{RUN}.service",
            "target": {
                "kind": "vm",
                "name": "vm-one",
                "incarnation": "v1:" + "b" * 64,
                "boot_id": "00000000-0000-4000-8000-000000000001",
            },
            "workload": {"euid": os.geteuid(), "egid": os.getegid(), "groups": groups},
            "shell": {
                "requested": shell,
                "resolved_executable": resolved_shell or {"sh": "/bin/sh", "bash": "/bin/bash"}.get(shell),
                "login": login,
                "interactive": False,
            },
            "owner": {"kind": "resource", "owner_id": "session-7"},
            "lifetime": "independent",
            "profile_revision": 1,
            "receipt_namespace": "agentworks-managed-runs-v1",
            "receipt_protocol_version": 1,
        }
    )


def _store(tmp_path: Path) -> ManagedJobStore:
    os.chmod(tmp_path, 0o700)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return ManagedJobStore(RUN, _anchor_fd=fd, _namespace="managed", _owner_uid=os.getuid())
    finally:
        os.close(fd)


def _execute(
    tmp_path: Path,
    *,
    kind: str = "command",
    argv: tuple[str, ...] = ("/bin/sh", "-c", "cat"),
    source: bytes = b"",
    stdin: bytes = b"",
    shell: str | None = None,
    resolved_shell: str | None = None,
    login: bool = False,
    output_mode: str = "capture",
    limit: int | None = 1024,
    environment: tuple[tuple[str, str], ...] = (),
    boundary: Boundary | None = None,
) -> tuple[ManagedJobStore, Boundary]:
    store = _store(tmp_path)
    launch = _launch(shell=shell, resolved_shell=resolved_shell, login=login)
    store.publish_request(
        request_wire.ManagedJobRequest(launch, kind, argv, None, output_mode, limit, environment, source, stdin)
    )
    boundary = boundary if boundary is not None else Boundary()

    def ready() -> None:
        assert boundary.placed
        assert store.read_fact(FactName.LAUNCH) == launch
        assert store.read_fact(FactName.WAIT) is None

    assert (
        guest.run(RUN, _store=store, _boundary=boundary, _notify=ready, _identity_check=False, _apply_identity=False)
        == 0
    )
    assert boundary.killed
    assert (store.read_fact(FactName.BOUNDARY_EMPTY) is not None) == boundary.empty()
    return store, boundary


def test_literal_binary_io_and_exact_status(tmp_path: Path) -> None:
    payload = b"\x00\xff" * 50000
    store, _ = _execute(
        tmp_path,
        argv=("/bin/sh", "-c", "cat; printf '\\377' >&2; exit 255"),
        stdin=payload,
        limit=31,
    )
    assert store.read_capture(Stream.STDOUT, _launch()) == payload[:31]
    stdout = wire.decode_fact(store.read_fact(FactName.STDOUT_END))  # type: ignore[arg-type]
    assert stdout["disposition"] == "truncated-capture"
    assert stdout["retained_sha256"] == hashlib.sha256(payload[:31]).hexdigest()
    assert store.read_capture(Stream.STDERR, _launch()) == b"\xff"
    wait = wire.decode_fact(store.read_fact(FactName.WAIT))  # type: ignore[arg-type]
    assert wait["exit_code"] == 255
    store.close()


def test_ready_precedes_caller_code_and_failed_notify_keeps_gate_closed(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    store = _store(tmp_path)
    launch = _launch()
    store.publish_request(
        request_wire.ManagedJobRequest(
            launch,
            "command",
            ("/bin/sh", "-c", f"printf started > {marker}"),
            None,
            "discard",
            None,
            (),
            b"",
            b"",
        )
    )
    boundary = Boundary()

    def failing_notify() -> None:
        assert boundary.placed
        assert store.read_fact(FactName.LAUNCH) == launch
        assert not marker.exists()
        raise OSError("private-notify-canary")

    assert (
        guest.run(
            RUN, _store=store, _boundary=boundary, _notify=failing_notify, _identity_check=False, _apply_identity=False
        )
        == 125
    )
    assert not marker.exists()
    assert store.read_fact(FactName.WAIT) is None
    store.close()


def test_prelaunch_reap_uses_bounded_nonblocking_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    options: list[int] = []

    def unreaped(_pid: int, option: int) -> tuple[int, int]:
        options.append(option)
        return 0, 0

    monkeypatch.setattr(guest.os, "waitpid", unreaped)
    monkeypatch.setattr(guest, "_PRELAUNCH_REAP_SECONDS", 0.0)
    guest._reap_prelaunch(12345)
    assert options == [os.WNOHANG]


def test_preexec_signal_leaves_wait_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    store.publish_request(
        request_wire.ManagedJobRequest(_launch(), "command", ("/bin/true",), None, "discard", None, (), b"", b"")
    )
    boundary = Boundary()

    def interrupted_child(*_args: object) -> None:
        os.kill(os.getpid(), signal.SIGKILL)

    monkeypatch.setattr(guest, "_child", interrupted_child)
    assert (
        guest.run(
            RUN, _store=store, _boundary=boundary, _notify=lambda: None, _identity_check=False, _apply_identity=False
        )
        == 125
    )
    assert boundary.killed
    assert store.read_fact(FactName.WAIT) is None
    store.close()


def test_application_signal_leaves_wait_unknown(tmp_path: Path) -> None:
    store, _ = _execute(tmp_path, argv=("/bin/sh", "-c", "kill -TERM $$"))
    assert store.read_fact(FactName.WAIT) is None
    store.close()


def test_application_exit_126_is_not_setup_failure(tmp_path: Path) -> None:
    store, _ = _execute(tmp_path, argv=("/bin/sh", "-c", "exit 126"))
    wait = wire.decode_fact(store.read_fact(FactName.WAIT))  # type: ignore[arg-type]
    assert wait["exit_code"] == 126
    store.close()


def test_cleanup_failure_does_not_discard_proved_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guest, "_CLEANUP_SECONDS", 0.05)
    store, _ = _execute(tmp_path, argv=("/bin/true",), boundary=FailedKillBoundary())
    wait = wire.decode_fact(store.read_fact(FactName.WAIT))  # type: ignore[arg-type]
    assert wait["exit_code"] == 0
    store.close()


def test_cleanup_observation_failure_does_not_discard_independent_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(guest, "_CLEANUP_SECONDS", 0.05)
    store, _ = _execute(tmp_path, argv=("/bin/true",), boundary=FailedEmptyBoundary())
    wait = wire.decode_fact(store.read_fact(FactName.WAIT))  # type: ignore[arg-type]
    assert wait["exit_code"] == 0
    assert store.read_fact(FactName.STDOUT_END) is not None
    assert store.read_fact(FactName.STDERR_END) is not None
    assert store.read_fact(FactName.BOUNDARY_EMPTY) is None
    store.close()


def test_wait_status_before_exec_pipe_eof_still_publishes_normal_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    boundary = Boundary()
    input_r, input_w = os.pipe()
    exec_r, exec_w = os.pipe()
    os.close(input_r)
    os.close(exec_w)
    monkeypatch.setattr(guest.os, "waitpid", lambda pid, option: (pid, 7 << 8))
    request = request_wire.ManagedJobRequest(_launch(), "command", ("/bin/true",), None, "discard", None, (), b"", b"")
    try:
        guest._observe(RUN, "a" * 64, request, store, boundary, 12345, input_w, exec_r, [])
    finally:
        os.close(exec_r)
    assert boundary.killed
    wait = wire.decode_fact(store.read_fact(FactName.WAIT))  # type: ignore[arg-type]
    assert wait["exit_code"] == 7
    store.close()


def test_missing_executable_leaves_wait_unknown(tmp_path: Path) -> None:
    store, _ = _execute(tmp_path, argv=("/definitely/missing-agw-executable",))
    assert store.read_fact(FactName.WAIT) is None
    store.close()


def test_child_restores_sigpipe_before_exec(tmp_path: Path) -> None:
    previous = signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    try:
        store, _ = _execute(tmp_path, argv=("/bin/sh", "-c", "kill -PIPE $$; printf survived"))
    finally:
        signal.signal(signal.SIGPIPE, previous)
    assert store.read_fact(FactName.WAIT) is None
    assert store.read_capture(Stream.STDOUT, _launch()) == b""
    store.close()


def test_child_clears_inherited_signal_mask_before_exec(tmp_path: Path) -> None:
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    try:
        store, _ = _execute(tmp_path, argv=("/bin/sh", "-c", "kill -TERM $$; printf survived"))
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
    assert store.read_fact(FactName.WAIT) is None
    assert store.read_capture(Stream.STDOUT, _launch()) == b""
    store.close()


def test_literal_command_uses_explicit_request_path(tmp_path: Path) -> None:
    store, _ = _execute(
        tmp_path,
        argv=("printf", "path-search"),
        environment=(("PATH", "/usr/bin:/bin"),),
    )
    assert store.read_capture(Stream.STDOUT, _launch()) == b"path-search"
    store.close()


def test_incomplete_request_refuses_before_fork(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.publish_request_asset(RequestAsset.LAUNCH, _launch())
    boundary = Boundary()
    assert guest.run(RUN, _store=store, _boundary=boundary, _notify=lambda: None, _identity_check=False) == 125
    assert not boundary.placed
    assert store.read_fact(FactName.LAUNCH) is None
    store.close()


def test_held_stream_leaves_unknown_after_cleanup_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guest, "_CLEANUP_SECONDS", 0.1)
    store, _ = _execute(tmp_path, argv=("/bin/sh", "-c", "sleep 0.5 & exit 0"), boundary=HeldBoundary())
    assert store.read_fact(FactName.WAIT) is not None
    assert store.read_fact(FactName.STDOUT_END) is None
    assert store.read_fact(FactName.BOUNDARY_EMPTY) is None
    store.close()


@pytest.mark.parametrize(
    ("shell", "resolved_shell", "login"),
    [
        ("sh", "/bin/sh", False),
        ("sh", "/bin/sh", True),
        ("bash", "/bin/bash", False),
        ("bash", "/bin/bash", True),
        ("user_default", "/usr/bin/sh", False),
        ("user_default", "/usr/bin/bash", False),
    ],
)
def test_script_source_is_separate_from_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell: str, resolved_shell: str, login: bool
) -> None:
    if shell == "user_default":
        monkeypatch.setattr(guest, "_account_shell", lambda _uid: resolved_shell)
    store, _ = _execute(
        tmp_path,
        kind="script",
        argv=(),
        source=b"printf 'script:'; cat",
        stdin=b"\x00binary\xff",
        shell=shell,
        resolved_shell=resolved_shell,
        login=login,
    )
    assert (
        store.read_capture(
            Stream.STDOUT,
            _launch(shell=shell, resolved_shell=resolved_shell, login=login),
        )
        == b"script:\x00binary\xff"
    )
    store.close()


def test_user_default_refuses_stale_or_unsupported_account_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for configured in ("/bin/bash", "/bin/zsh"):
        monkeypatch.setattr(guest, "_account_shell", lambda _uid, path=configured: path)
        root = tmp_path / ("stale" if configured == "/bin/bash" else "unsupported")
        root.mkdir()
        store = _store(root)
        store.publish_request(
            request_wire.ManagedJobRequest(
                _launch(shell="user_default", resolved_shell="/bin/sh"),
                "script",
                (),
                None,
                "discard",
                None,
                (),
                b"exit 0",
                b"",
            )
        )
        boundary = Boundary()
        assert (
            guest.run(
                RUN,
                _store=store,
                _boundary=boundary,
                _notify=lambda: None,
                _identity_check=False,
                _apply_identity=False,
            )
            == 125
        )
        assert boundary.killed
        assert store.read_fact(FactName.LAUNCH) is None
        assert store.read_fact(FactName.WAIT) is None
        store.close()


@pytest.mark.parametrize("mode", ["discard", "sensitivity-suppressed"])
def test_no_spool_for_non_capture(tmp_path: Path, mode: str) -> None:
    store, _ = _execute(tmp_path, argv=("/bin/sh", "-c", "printf secret"), output_mode=mode, limit=None)
    assert store.read_capture(Stream.STDOUT, _launch()) is None
    assert not (tmp_path / "managed" / RUN / "stdout").exists()
    fact = wire.decode_fact(store.read_fact(FactName.STDOUT_END))  # type: ignore[arg-type]
    assert fact["disposition"] == ("discarded" if mode == "discard" else "sensitivity-suppressed")
    store.close()


def test_notify_path_and_abstract(tmp_path: Path) -> None:
    path = str(tmp_path / "notify")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as listener:
        listener.bind(path)
        guest.sd_notify(path)
        assert listener.recv(64) == b"READY=1"
    abstract = "\0agw-test-" + str(os.getpid())
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as listener:
        listener.bind(abstract)
        guest.sd_notify("@" + abstract[1:])
        assert listener.recv(64) == b"READY=1"


@pytest.mark.parametrize(
    "value",
    [
        f"0::/system.slice/agw-managed-{RUN}.service\n".encode(),
        f"0::/system.slice/agw-managed-{RUN}.service".encode(),
    ],
)
def test_cgroup_parser(value: bytes) -> None:
    assert guest.service_cgroup(value, RUN) == f"/system.slice/agw-managed-{RUN}.service"


@pytest.mark.parametrize(
    ("value", "run_id"),
    [
        (b"1::/x\n", RUN),
        (b"0::/\n", RUN),
        (b"0::/x/../y\n", RUN),
        (b"0::/x\n0::/y\n", RUN),
        (b"0::/system.slice/unrelated.service\n", RUN),
        (f"0::/system.slice/agw-managed-{RUN}.service\n".encode(), "invalid"),
    ],
)
def test_cgroup_parser_refuses(value: bytes, run_id: str) -> None:
    with pytest.raises(guest.ControllerError):
        guest.service_cgroup(value, run_id)


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3.11"])
def test_exact_bundle_runs_without_installed_package(interpreter: str, tmp_path: Path) -> None:
    if not Path(interpreter).exists():
        pytest.skip("interpreter unavailable")
    assert "request-canary" not in bundle.FIXED_SOURCE
    result = subprocess.run(
        [interpreter, "-I", "-S", "-B", "-c", bundle.FIXED_SOURCE, "invalid"],
        env={"PYTHONPATH": str(tmp_path)},
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 125
    assert result.stdout == result.stderr == b""
