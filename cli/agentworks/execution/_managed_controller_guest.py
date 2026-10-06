"""One bounded local systemd read for an already validated managed launch."""

from __future__ import annotations

import os
import selectors
import subprocess
import sys
import time

from ._managed_observation_protocol import ControllerState

_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "SYSTEMD_PAGER": "cat"}
_QUERY_SECONDS = 5.0
_REAP_SECONDS = 1.0
_MAX_BYTES = 4096
_FIELDS = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "MainPID",
    "ControlPID",
    "ExecMainPID",
    "ExecMainCode",
    "ExecMainStatus",
    "ExecMainStartTimestampMonotonic",
    "ExecMainExitTimestampMonotonic",
)


def _query_argv(run_id: str) -> tuple[str, ...]:
    return (
        "/usr/bin/systemctl",
        "--system",
        "--no-pager",
        "--no-ask-password",
        "show",
        "--property=" + ",".join(_FIELDS),
        "--",
        f"agw-managed-{run_id}.service",
    )


def _query(argv: tuple[str, ...]) -> bytes | None:
    """Allow five seconds for the client and one for reap, independently of carrier time.

    The caller's deadline bounds local observation, not guest cancellation. Request
    I/O, fact reads and response framing are outside this finite child budget.
    """
    data = bytearray()
    deadline = time.monotonic() + _QUERY_SECONDS
    try:
        child = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            env=_ENV,
        )
    except OSError:
        return None
    try:
        assert child.stdout is not None and child.stderr is not None
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            selector.register(child.stderr, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, min(1024, _MAX_BYTES + 1 - len(data)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.fileobj is child.stderr:
                        return None
                    else:
                        data.extend(chunk)
                        if len(data) > _MAX_BYTES:
                            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0 or child.wait(timeout=remaining) != 0:
            return None
        return bytes(data)
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        # Cleanup must not replace an interruption with a query-success response.
        interrupted = sys.exc_info()[0] is not None
        cleanup_error: BaseException | None = None
        try:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=_REAP_SECONDS)
        except BaseException as error:
            cleanup_error = error
        for stream in (child.stdout, child.stderr):
            if stream is not None:
                try:
                    stream.close()
                except BaseException as error:
                    if cleanup_error is None:
                        cleanup_error = error
        data.clear()
        if cleanup_error is not None and not interrupted:
            raise cleanup_error


def _native_state(data: bytes, unit: str) -> ControllerState:
    """Reduce external properties conservatively, including explicit collection."""
    try:
        if len(data) > _MAX_BYTES or not data.endswith(b"\n"):
            raise ValueError
        fields: dict[str, str] = {}
        for line in data.decode("ascii").splitlines():
            name, value = line.split("=", 1)
            if name not in _FIELDS or name in fields or not value:
                raise ValueError
            fields[name] = value
        if set(fields) != set(_FIELDS) or fields["Id"] != unit:
            raise ValueError
        numbers: dict[str, int] = {}
        for name in _FIELDS[4:]:
            value = fields[name]
            if not value.isdecimal() or len(value) > 20:
                raise ValueError
            number = int(value)
            if str(number) != value or number > 2**64 - 1:
                raise ValueError
            numbers[name] = number
        main, control, executed = (numbers[name] for name in ("MainPID", "ControlPID", "ExecMainPID"))
        if any(pid > 2**32 - 1 for pid in (main, control, executed)):
            raise ValueError
        load, active, sub = (fields[name] for name in ("LoadState", "ActiveState", "SubState"))
        code, status = (numbers[name] for name in ("ExecMainCode", "ExecMainStatus"))
        start, exit_time = (
            numbers[name]
            for name in (
                "ExecMainStartTimestampMonotonic",
                "ExecMainExitTimestampMonotonic",
            )
        )
        if code > 6 or status > 255:
            raise ValueError
        if (
            load == "not-found"
            and active == "inactive"
            and sub == "dead"
            and main == control == executed == code == status == start == exit_time == 0
        ):
            return ControllerState.ABSENT
        if load != "loaded":
            return ControllerState.UNKNOWN
        if (
            active == "active"
            and sub == "running"
            and main > 0
            and main == executed
            and start > 0
            and exit_time == code == status == 0
        ):
            return ControllerState.RUNNING
        if (
            (active, sub) in (("inactive", "dead"), ("failed", "failed"))
            and main == control == 0
            and executed > 0
            and 0 < start <= exit_time
            and (code == 1 or (code in (2, 3) and 0 < status <= 64))
        ):
            return ControllerState.EXITED
    except (ValueError, UnicodeError):
        pass
    return ControllerState.UNKNOWN


def observe_controller(run_id: str) -> ControllerState:
    """The typed caller supplies the run ID from its exact protected launch."""
    data = _query(_query_argv(run_id))
    return ControllerState.UNKNOWN if data is None else _native_state(data, f"agw-managed-{run_id}.service")
