"""The root-script wrapper over fabricated channel output, and ``run_process`` against a local Bash."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.errors import ConnectivityError, StateError, ValidationError
from agentworks.execution import root_script, trust
from agentworks.execution.errors import ClientUnsupported
from agentworks.execution.oplog import format_record
from agentworks.execution.pump import CAPTURE_LIMIT_BYTES
from agentworks.execution.root_script import RawRun

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.execution.requests import RunRequest
    from agentworks.execution.results import Result

SECRET = "tskey-auth-k1Secret-0123456789"
ED25519 = "AAAAC3NzaC1lZDI1NTE5AAAAIOU3HUqrGubKZio8MVx9WOFPYHGIHl5tIyF3skwoAxv0"
needs_bash = pytest.mark.skipif(not os.path.exists("/bin/bash"), reason="needs /bin/bash")


def nonce_of(script: bytes) -> str:
    match = re.search(rb"\\036([0-9a-f]{32}) start", script)
    assert match is not None, "the wrapper names its nonce in a start marker"
    return match[1].decode()


def channel(
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    status: int | None = 0,
    end: bool = True,
    start: bool = True,
    trailer: bytes | None = None,
    before: bytes = b"",
    after: bytes = b"",
) -> tuple[Callable[[bytes, float], RawRun], list[bytes]]:
    """A fake ``run_as_root`` that answers with the markers the real script would print, and its calls."""
    calls: list[bytes] = []

    def run_as_root(script: bytes, deadline: float) -> RawRun:
        calls.append(script)
        prefix = b"\x1e" + nonce_of(script).encode() + b" "
        out, err = before, before
        if start:
            out += prefix + b"start\n" + stdout
            err += prefix + b"start\n" + stderr
            if end:
                out += prefix + b"end\n"
            if trailer is not None:
                err += prefix + trailer + b"\n"
            elif status is not None:
                err += prefix + b"exit %d\n" % status
        return RawRun(out + after, err + after)

    return run_as_root, calls


# -- The wrapper's status rule -----------------------------------------------------------


@pytest.mark.parametrize("status", [0, 1, 255])
def test_both_closing_markers_give_the_payloads_status(status: int) -> None:
    run_as_root, _ = channel(status=status)

    assert root_script.run(run_as_root, "true", deadline=5).status == status


@pytest.mark.parametrize(
    "shape",
    [
        {"end": False},
        {"status": None},
        {"trailer": b"exit lots"},
        {"trailer": b"refused cwd"},
        {"start": False},
    ],
    ids=["no-end", "no-trailer", "malformed-trailer", "refusal-word", "no-start"],
)
def test_anything_short_of_both_closing_markers_leaves_no_status(shape: dict[str, object]) -> None:
    run_as_root, _ = channel(**shape)  # type: ignore[arg-type]

    assert root_script.run(run_as_root, "true", deadline=5).status is None


def test_bytes_outside_the_markers_never_reach_the_payload_streams() -> None:
    run_as_root, _ = channel(stdout=b"payload-out", stderr=b"payload-err", before=b"motd\n", after=b"late\n")

    result = root_script.run(run_as_root, "true", deadline=5)

    assert (result.stdout, result.stderr) == (b"payload-out", b"payload-err")
    assert "motd" in result.channel_tail
    assert "late" not in result.channel_tail


def test_nul_interleaved_channel_output_before_any_marker_reads_as_text() -> None:
    def wsl_error(script: bytes, deadline: float) -> RawRun:
        return RawRun("There is no distribution with the supplied name.\r\n".encode("utf-16-le"), b"")

    result = root_script.run(wsl_error, "true", deadline=5)

    assert result.status is None
    assert "There is no distribution with the supplied name." in result.channel_tail


def test_the_channel_tail_is_bounded_and_sanitized() -> None:
    def noisy(script: bytes, deadline: float) -> RawRun:
        return RawRun(b"", b"".join(b"line %d\n" % n for n in range(100)) + b"\x1b[31mred\n")

    tail = root_script.run(noisy, "true", deadline=5).channel_tail

    assert len(tail.splitlines()) == root_script.CHANNEL_TAIL_LINES
    assert "\x1b" not in tail


# -- The secret --------------------------------------------------------------------------


def test_the_secret_travels_in_the_script_text_only() -> None:
    run_as_root, calls = channel()

    root_script.run(run_as_root, "true", deadline=5, secret=SECRET)

    (script,) = calls
    assert SECRET.encode() in script


def test_the_secret_is_in_no_field_repr_or_logged_record() -> None:
    leak = SECRET.encode()
    run_as_root, _ = channel(stdout=b"out " + leak, stderr=b"err '" + leak + b"'", before=b"pre " + leak + b"\n")
    records: list[tuple[RunRequest, Result]] = []

    result = root_script.run(
        run_as_root, "true", deadline=5, secret=SECRET, log=lambda request, logged: records.append((request, logged))
    )

    assert leak not in result.stdout
    assert leak not in result.stderr
    assert SECRET not in result.channel_tail
    assert SECRET not in repr(result)
    ((request, logged),) = records
    assert SECRET not in repr(request)
    assert SECRET not in format_record(request, logged, label="step")


def test_a_secret_that_is_not_line_safe_is_refused_without_naming_it() -> None:
    run_as_root, calls = channel()

    with pytest.raises(ValidationError) as caught:
        root_script.run(run_as_root, "true", deadline=5, secret="tskey\nsecond-line")

    assert "second-line" not in str(caught.value)
    assert "second-line" not in repr(caught.value)
    assert calls == []


# -- One dispatch, one record ------------------------------------------------------------


@pytest.mark.parametrize("shape", [{}, {"start": False}], ids=["completed", "unknown"])
def test_the_channel_is_called_once_and_the_log_gets_one_record(shape: dict[str, object]) -> None:
    run_as_root, calls = channel(**shape)  # type: ignore[arg-type]
    records: list[object] = []

    root_script.run(run_as_root, "true", deadline=5, log=lambda request, result: records.append(result))

    assert len(calls) == 1
    assert len(records) == 1


def test_a_channel_that_raises_is_not_dispatched_again() -> None:
    calls: list[bytes] = []

    def failing(script: bytes, deadline: float) -> RawRun:
        calls.append(script)
        raise ConnectivityError("the channel failed")

    with pytest.raises(ConnectivityError):
        root_script.run(failing, "true", deadline=5, secret=SECRET)

    assert len(calls) == 1


# -- Host keys ---------------------------------------------------------------------------


def test_read_host_keys_keeps_only_valid_keys_once_each() -> None:
    listing = (
        f"ssh-ed25519 {ED25519} root@vm\n"
        f"ssh-ed25519 {ED25519} duplicate\n"
        "ssh-rsa not-base64!! root@vm\n"
        "garbage\n"
        f"ssh-rsa {ED25519} wrong-type-for-blob\n"
    ).encode()
    run_as_root, calls = channel(stdout=listing)

    keys = root_script.read_host_keys(run_as_root)

    assert keys == (trust.HostKey("ssh-ed25519", ED25519),)
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("shape", "error"),
    [
        ({"stdout": b"garbage\n"}, StateError),
        ({"status": 1}, StateError),
        ({"start": False}, ConnectivityError),
    ],
    ids=["no-valid-key", "failed", "unknown"],
)
def test_read_host_keys_refuses_anything_but_a_successful_read_with_a_key(
    shape: dict[str, object], error: type[Exception]
) -> None:
    run_as_root, _ = channel(**shape)  # type: ignore[arg-type]

    with pytest.raises(error):
        root_script.read_host_keys(run_as_root)


# -- run_process -------------------------------------------------------------------------


@needs_bash
def test_run_process_returns_at_the_deadline_with_the_process_reaped() -> None:
    started = time.monotonic()

    raw = root_script.run_process(["/bin/bash", "-s"], b"echo $$\nexec sleep 30\n", 0.5)

    assert time.monotonic() - started < 5
    pid = int(raw.stdout)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@needs_bash
def test_run_process_stops_a_writer_at_the_output_limit() -> None:
    started = time.monotonic()

    raw = root_script.run_process(["/bin/bash", "-s"], b"exec cat /dev/zero\n", 30)

    assert time.monotonic() - started < 10
    assert len(raw.stdout) == CAPTURE_LIMIT_BYTES


@needs_bash
def test_a_background_child_holding_stdout_does_not_stall_run_process() -> None:
    started = time.monotonic()

    raw = root_script.run_process(["/bin/bash", "-s"], b"sleep 30 &\necho done\n", 30)

    assert time.monotonic() - started < 10
    assert raw.stdout == b"done\n"


def test_a_missing_program_is_a_connectivity_error_naming_it() -> None:
    with pytest.raises(ConnectivityError) as caught:
        root_script.run_process(["/nonexistent/bin/limactl", "shell", "vm"], b"true\n", 5)

    assert not isinstance(caught.value, ClientUnsupported)
    assert "limactl" in str(caught.value)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX program path")
def test_a_missing_openssh_client_is_still_client_unsupported() -> None:
    with pytest.raises(ClientUnsupported):
        root_script.run_process(["/nonexistent/bin/ssh", "-T", "host"], b"true\n", 5)


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_the_fingerprint_is_the_one_ssh_keygen_prints(tmp_path: Path) -> None:
    key = tmp_path / "host"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    key_type, blob = Path(f"{key}.pub").read_text().split()[:2]
    printed = subprocess.run(
        ["ssh-keygen", "-l", "-E", "sha256", "-f", f"{key}.pub"], capture_output=True, text=True, check=True
    ).stdout.split()[1]

    assert trust.fingerprint(trust.HostKey(key_type, blob)) == printed
