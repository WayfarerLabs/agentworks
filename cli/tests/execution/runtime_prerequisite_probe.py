#!/usr/bin/env python3
"""Bounded POSIX runtime-prerequisite experiment, not a production API.

One fixed, non-login shell invocation selects a trusted absolute candidate and
either emits a closed prerequisite observation or replaces itself with that
candidate. The runtime source is compatible with older Python 3 releases; its
3.11 continuation runs in the same interpreter process.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_SHELL = "/bin/sh"
_DEFAULT_CANDIDATES = (Path("/opt/homebrew/bin/python3"), Path("/usr/local/bin/python3"))
_SYSTEM_SHIM = Path("/usr/bin/python3")
_MAX_CAPTURE = 1_024
_READ_SIZE = 4_096
_CLEANUP_SECONDS = 0.2

_CONTINUATION = "sys.stdout.write('AGW_RUNTIME_1:%s:ready:%s\\n' % (nonce, candidate_index))"
_TRAMPOLINE = f"""import sys
nonce = sys.argv[1]
candidate_index = sys.argv[2]
if sys.version_info < (3, 11):
    sys.stdout.write('AGW_RUNTIME_1:%s:unsupported:%s\\n' % (nonce, candidate_index))
else:
    exec(compile({_CONTINUATION!r}, '<agentworks-runtime-continuation>', 'exec'))
sys.stdout.flush()
"""

_SHELL_SOURCE = r"""
nonce=$1
shim=$2
trampoline=$3
shift 3

selected=
selected_index=
index=0
for candidate do
    if [ -e "$candidate" ] || [ -L "$candidate" ]; then
        selected=$candidate
        selected_index=$index
        break
    fi
    index=$((index + 1))
done

if [ -z "$selected" ]; then
    if [ -e "$shim" ] || [ -L "$shim" ]; then
        printf 'AGW_RUNTIME_1:%s:shim:s\n' "$nonce"
    else
        printf 'AGW_RUNTIME_1:%s:missing:-\n' "$nonce"
    fi
    exit 0
fi

if [ ! -f "$selected" ] || [ ! -x "$selected" ]; then
    printf 'AGW_RUNTIME_1:%s:unusable:%s\n' "$nonce" "$selected_index"
    exit 0
fi

if { [ -e "$shim" ] || [ -L "$shim" ]; } && [ "$selected" -ef "$shim" ]; then
    printf 'AGW_RUNTIME_1:%s:shim:%s\n' "$nonce" "$selected_index"
    exit 0
fi

exec "$selected" -I -S -B -c "$trampoline" "$nonce" "$selected_index"
"""


class PrerequisiteState(Enum):
    """Closed observations produced by the bounded experiment."""

    MISSING = "missing"
    SHIM = "shim"
    UNSUPPORTED = "unsupported"
    UNUSABLE = "unusable"
    READY = "ready"
    OBSERVATION_UNKNOWN = "observation_unknown"


@dataclass(frozen=True)
class PrerequisiteObservation:
    state: PrerequisiteState
    selected_path: Path | None


class _ObservationTimeout(Exception):
    pass


def _absolute(path: Path) -> Path:
    value = os.fspath(path)
    if not path.is_absolute() or "\0" in value:
        raise ValueError("runtime prerequisite paths must be absolute and contain no NUL")
    return path


def _read_bounded(process: subprocess.Popen[bytes], deadline: float) -> tuple[bytes, bool]:
    """Drain both child streams while retaining only bounded stdout."""
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_fd = process.stdout.fileno()
    stderr_fd = process.stderr.fileno()
    retained = bytearray()
    overflow = False
    selector = selectors.DefaultSelector()
    selector.register(stdout_fd, selectors.EVENT_READ, True)
    selector.register(stderr_fd, selectors.EVENT_READ, False)
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ObservationTimeout
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, _READ_SIZE)
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                if key.data:
                    available = max(0, _MAX_CAPTURE + 1 - len(retained))
                    retained.extend(chunk[:available])
                    overflow = overflow or len(retained) > _MAX_CAPTURE or len(chunk) > available

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _ObservationTimeout
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise _ObservationTimeout from error
        return bytes(retained[:_MAX_CAPTURE]), overflow
    finally:
        selector.close()


def _signal_group(process: subprocess.Popen[bytes], signum: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signum)


def _cleanup(process: subprocess.Popen[bytes]) -> None:
    """Terminate and reap the experiment-owned process group."""
    if process.poll() is None:
        _signal_group(process, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_CLEANUP_SECONDS)
    _signal_group(process, signal.SIGKILL)
    if process.poll() is None:
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=_CLEANUP_SECONDS)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()


def _unknown() -> PrerequisiteObservation:
    return PrerequisiteObservation(PrerequisiteState.OBSERVATION_UNKNOWN, None)


def _interpret(
    raw: bytes,
    *,
    returncode: int | None,
    overflow: bool,
    nonce: str,
    candidates: tuple[Path, ...],
    shim: Path,
) -> PrerequisiteObservation:
    if returncode != 0 or overflow:
        return _unknown()
    match = re.fullmatch(
        rb"AGW_RUNTIME_1:([0-9a-f]{32}):(missing|shim|unsupported|unusable|ready):(-|s|[0-9]+)\n",
        raw,
    )
    if match is None or match.group(1).decode("ascii") != nonce:
        return _unknown()

    state = PrerequisiteState(match.group(2).decode("ascii"))
    token = match.group(3)
    if state is PrerequisiteState.MISSING:
        if token == b"-":
            return PrerequisiteObservation(state, None)
        return _unknown()
    if state is PrerequisiteState.SHIM and token == b"s":
        return PrerequisiteObservation(state, shim)
    if not token.isdigit():
        return _unknown()
    index = int(token)
    if index >= len(candidates):
        return _unknown()
    return PrerequisiteObservation(state, candidates[index])


def observe_runtime_prerequisite(
    explicit_path: Path | None = None,
    *,
    timeout: float = 5.0,
    fixture_candidates: tuple[Path, ...] | None = None,
    fixture_system_shim: Path | None = None,
) -> PrerequisiteObservation:
    """Run one fixture-injectable, carrier-shaped prerequisite observation."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    candidates: tuple[Path, ...]
    if explicit_path is not None:
        candidates = (_absolute(explicit_path),)
    else:
        candidate_source = _DEFAULT_CANDIDATES if fixture_candidates is None else fixture_candidates
        candidates = tuple(_absolute(path) for path in candidate_source)
    if not 0 < len(candidates) <= 2:
        raise ValueError("the experiment accepts one explicit or two fixed candidates")
    shim = _absolute(fixture_system_shim or _SYSTEM_SHIM)
    nonce = secrets.token_hex(16)
    argv = [
        _SHELL,
        "-c",
        _SHELL_SOURCE,
        "agentworks-runtime-prerequisite",
        nonce,
        os.fspath(shim),
        _TRAMPOLINE,
        *[os.fspath(path) for path in candidates],
    ]

    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError:
        return _unknown()

    try:
        try:
            raw, overflow = _read_bounded(process, time.monotonic() + timeout)
        except _ObservationTimeout:
            return _unknown()
        return _interpret(
            raw,
            returncode=process.returncode,
            overflow=overflow,
            nonce=nonce,
            candidates=candidates,
            shim=shim,
        )
    finally:
        _cleanup(process)
