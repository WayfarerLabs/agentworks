"""Mocked WSL body admission, not native credential or WSL evidence."""

from __future__ import annotations

import builtins
import errno
import io
import os
import shutil
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

from agentworks.execution import _guest_bootstrap as bootstrap
from agentworks.execution import _wsl2_early_bootstrap as early
from agentworks.execution._helper_launcher import IdentityMode
from agentworks.execution._named_guest_bootstrap import build_named_guest_bootstrap_argv
from agentworks.execution._runtime_prerequisite import _TRAMPOLINE
from agentworks.execution._wsl2_guest_query import FIXED_GUEST_QUERY_SOURCE, LEGACY_GUEST_QUERY_SOURCE
from agentworks.execution._wsl2_lifecycle import _HELPER_SOURCE
from agentworks.execution.carriers.wsl2 import WSL2Connection
from tests.execution.test_named_guest_bootstrap import _prepare

NONCE = "a" * 32
BOOT = "12345678-1234-1234-1234-123456789abc"


def _stat(pid: int, ticks: int) -> bytes:
    return f"{pid} (a (process)) ".encode() + b"S " + b"0 " * 18 + str(ticks).encode() + b"\n"


@pytest.mark.parametrize("body", [_HELPER_SOURCE, "_agw_query_pid = 137\n" + FIXED_GUEST_QUERY_SOURCE])
def test_root_launcher_binds_body_account_and_direct_root_plan(body: str, monkeypatch: pytest.MonkeyPatch) -> None:
    original = build_named_guest_bootstrap_argv
    admitted: list[tuple[object, str]] = []

    def build(plan: Any, account: str, **kwargs: Any) -> Any:
        assert plan.mode is IdentityMode.DIRECT and plan.expected.euid == 0
        admitted.append((plan, account))
        return original(plan, account, **kwargs)

    monkeypatch.setattr(early, "build_named_guest_bootstrap_argv", build)
    argv = early.build_early_argv(WSL2Connection("Ubuntu", "agent account", "wsl.exe"), body, NONCE)
    assert argv[:6] == ("wsl.exe", "--distribution", "Ubuntu", "--user", "root", "--exec")
    assert admitted[0][1] == "agent account"
    assert NONCE in argv


@pytest.mark.skipif(sys.platform != "linux", reason="mocked Linux credential admission")
@pytest.mark.parametrize("body_kind", ["hold", "found", "missing"])
@pytest.mark.parametrize("target_uid", [1001, 0])
@pytest.mark.parametrize("changed_init", [False, True])
def test_admitted_bodies_use_fresh_held_init_and_target_user_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    body_kind: str,
    target_uid: int,
    changed_init: bool,
) -> None:
    events: list[object] = []
    credentials, opened = _prepare(tmp_path, monkeypatch, events, target_uid=target_uid)
    stat_path = tmp_path / "stat"
    stat_path.write_bytes(_stat(1, 4096))
    pid = os.getpid() if body_kind == "hold" else 137
    original_open = builtins.open
    original_read = bootstrap._read_init
    reads: list[int] = []
    process_queries: list[str] = []

    def body_open(path: str, *args: Any, **kwargs: Any) -> Any:
        if path.startswith("/proc/"):
            assert credentials.uid == (target_uid,) * 3
            process_queries.append(path)
            if path == "/proc/1/stat":
                pytest.fail("post-admission init-stat reopen")
            if path == "/proc/sys/kernel/random/boot_id":
                return io.BytesIO((BOOT + "\n").encode())
            assert path == f"/proc/{pid}/stat"
            if body_kind == "missing":
                raise FileNotFoundError
            return io.BytesIO(_stat(pid, 8192))
        return original_open(path, *args, **kwargs)

    def read_init(descriptor: int) -> bytes:
        assert credentials.uid == (target_uid,) * 3
        reads.append(descriptor)
        if changed_init and len(reads) == 2:
            stat_path.write_bytes(_stat(1, 4097))
        return original_read(descriptor)

    def kill(query_pid: int, signal: int) -> None:
        assert credentials.uid == (target_uid,) * 3
        assert query_pid == pid and signal == 0
        process_queries.append("kill0")
        raise ProcessLookupError(errno.ESRCH, "mocked absence")

    class Stdin(io.BytesIO):
        def read(self, *args: Any) -> bytes:
            assert credentials.uid == (target_uid,) * 3
            assert len(reads) == 2
            events.append("stdin")
            return super().read(*args)

    monkeypatch.setattr(builtins, "open", body_open)
    monkeypatch.setattr(bootstrap, "_read_init", read_init)
    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(Stdin()))
    source = _HELPER_SOURCE if body_kind == "hold" else "_agw_query_pid = 137\n" + FIXED_GUEST_QUERY_SOURCE
    result = bootstrap.main_named("configured-agent", source)
    output = capfd.readouterr().out
    assert len(opened) == 1 and reads == [opened[0], opened[0]]
    assert f"/proc/{pid}/stat" in process_queries
    assert ("kill0" in process_queries) is (body_kind == "missing")
    if body_kind == "hold":
        assert result == (2 if changed_init else 0)
        assert output == ("" if changed_init else f"READY {NONCE} {BOOT} {pid} 8192 4096\nEXITING {NONCE}\n")
        assert ("stdin" in events) is not changed_init
    else:
        assert result == 0
        expected = (
            "- - unknown -" if changed_init else f"{BOOT} 4096 {body_kind} {'8192' if body_kind == 'found' else '-'}"
        )
        assert output == f"AGW_GQ2 {NONCE} {pid} {expected}\n"
        assert "stdin" not in events
    with pytest.raises(OSError):
        os.fstat(opened[0])


@pytest.mark.skipif(sys.platform != "linux", reason="mocked Linux credential admission")
@pytest.mark.parametrize("source", [_HELPER_SOURCE, "_agw_query_pid = 137\n" + FIXED_GUEST_QUERY_SOURCE])
def test_failed_drop_never_reads_init_enters_body_or_uses_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], source: str
) -> None:
    events: list[object] = []
    _, opened = _prepare(tmp_path, monkeypatch, events)
    monkeypatch.setattr(bootstrap, "_capabilities_zero", lambda **_: False)
    monkeypatch.setattr(bootstrap, "_read_init", lambda _: pytest.fail("init read before admission"))
    assert bootstrap.main_named("agent", source) == 125
    assert "compile" not in events
    assert capfd.readouterr() == ("", "")
    with pytest.raises(OSError):
        os.fstat(opened[0])


@pytest.mark.parametrize(
    "source", [_HELPER_SOURCE, "_agw_query_pid = 137\n" + FIXED_GUEST_QUERY_SOURCE, LEGACY_GUEST_QUERY_SOURCE]
)
def test_fixed_bootstrap_and_bodies_compile_in_actual_python311(source: str) -> None:
    python = shutil.which("python3.11")
    if python is None:
        pytest.skip("Python 3.11 is unavailable")
    bootstrap_source = files("agentworks.execution").joinpath("_guest_bootstrap.py").read_text(encoding="utf-8")
    result = subprocess.run(
        [python, "-I", "-S", "-B", "-c", "import sys; compile(sys.stdin.read(), '<fixed>', 'exec')"],
        input=bootstrap_source + "\n" + source,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="local non-root admission refusal")
@pytest.mark.parametrize("source", [_HELPER_SOURCE, "_agw_query_pid = 137\n" + FIXED_GUEST_QUERY_SOURCE])
def test_actual_python311_runtime_ready_is_separate_from_nonroot_admission_refusal(source: str) -> None:
    python = shutil.which("python3.11")
    if python is None or os.geteuid() == 0:
        pytest.skip("requires non-root caller and Python 3.11")
    argv = early.build_early_argv(WSL2Connection("Ubuntu", "agent", "wsl.exe"), source, NONCE)
    with subprocess.Popen(
        [python, "-I", "-S", "-B", "-c", _TRAMPOLINE, NONCE, "0", argv[-2]],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        assert child.wait(timeout=5) == 125
        assert child.stdin is not None and not child.stdin.closed
        assert child.stdout is not None and child.stderr is not None
        assert child.stdout.read() == f"AGW_RUNTIME_1:{NONCE}:ready:0\n".encode()
        assert child.stderr.read() == b""
