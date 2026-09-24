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
) -> tuple[ManagedJobStore, Boundary]:
    store = _store(tmp_path)
    launch = _launch(shell=shell, resolved_shell=resolved_shell, login=login)
    store.publish_request(
        request_wire.ManagedJobRequest(launch, kind, argv, None, output_mode, limit, environment, source, stdin)
    )
    boundary = Boundary()

    def ready() -> None:
        assert boundary.placed
        assert store.read_fact(FactName.LAUNCH) == launch
        assert store.read_fact(FactName.WAIT) is None

    assert (
        guest.run(RUN, _store=store, _boundary=boundary, _notify=ready, _identity_check=False, _apply_identity=False)
        == 0
    )
    assert boundary.killed
    assert store.read_fact(FactName.BOUNDARY_EMPTY) is not None
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


def test_signal_wait_fact(tmp_path: Path) -> None:
    store, _ = _execute(tmp_path, argv=("/bin/sh", "-c", "kill -TERM $$"))
    wait = wire.decode_fact(store.read_fact(FactName.WAIT))  # type: ignore[arg-type]
    assert wait["exit_code"] is None
    assert wait["signal"] == signal.SIGTERM
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
    store, _ = _execute(tmp_path, argv=("/bin/sh", "-c", "sleep 0.5 & exit 0"))
    assert store.read_fact(FactName.WAIT) is not None
    assert store.read_fact(FactName.STDOUT_END) is None
    assert store.read_fact(FactName.BOUNDARY_EMPTY) is not None
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
def test_script_source_is_separate_from_stdin(tmp_path: Path, shell: str, resolved_shell: str, login: bool) -> None:
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


@pytest.mark.parametrize("value", [b"0::/system.slice/test.service\n", b"0::/system.slice/test.service"])
def test_cgroup_parser(value: bytes) -> None:
    assert guest.service_cgroup(value) == "/system.slice/test.service"


@pytest.mark.parametrize("value", [b"1::/x\n", b"0::/\n", b"0::/x/../y\n", b"0::/x\n0::/y\n"])
def test_cgroup_parser_refuses(value: bytes) -> None:
    with pytest.raises(guest.ControllerError):
        guest.service_cgroup(value)


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
