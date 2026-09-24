"""Fixed Linux root helper that stages one request and attempts one service activation."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from ._file_wire import FileRecordKind, FileRecordWriter
from ._helper_identity import matches_current_identity
from ._managed_job_store import FactName, ManagedJobStore, RequestAsset, StoreError
from ._managed_service_source import FIXED_SOURCE
from ._managed_start_protocol import (
    MAX_REQUEST_BYTES,
    ManagedStartError,
    ManagedStartRequest,
    ManagedStartResult,
    decode_request,
    encode_result,
)

_SYSTEMD_RUN = "/usr/bin/systemd-run"
_START_TIMEOUT_SECONDS = 45.0
_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True, slots=True, repr=False)
class _PreparedStart:
    result: ManagedStartResult
    launch: bytes | None = field(default=None, repr=False)


def _read_request() -> ManagedStartRequest:
    data = bytearray()
    while len(data) <= MAX_REQUEST_BYTES:
        chunk = os.read(0, MAX_REQUEST_BYTES + 1 - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return decode_request(bytes(data))


def _service_argv(run_id: str, python: str) -> tuple[str, ...]:
    """The unit and run ID are the sole request-derived command arguments."""
    if (
        type(run_id) is not str
        or len(run_id) != 32
        or any(character not in "0123456789abcdef" for character in run_id)
        or type(python) is not str
        or not python.startswith("/")
        or "\0" in python
    ):
        raise ManagedStartError("invalid managed service identity")
    return (
        _SYSTEMD_RUN,
        "--system",
        "--quiet",
        "--no-ask-password",
        "--collect",
        f"--unit=agw-managed-{run_id}.service",
        "--service-type=notify",
        "--property=NotifyAccess=main",
        "--property=Delegate=yes",
        "--property=ExitType=main",
        "--property=KillMode=control-group",
        "--property=Restart=no",
        "--property=TimeoutStartSec=30s",
        "--property=TimeoutStopSec=5s",
        "--",
        python,
        "-I",
        "-S",
        "-B",
        "-c",
        FIXED_SOURCE,
        run_id,
    )


def _run_systemd(argv: tuple[str, ...]) -> int | None:
    """Discard client streams; timeout still leaves remote activation uncertain."""
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=_ENV,
            timeout=_START_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode


def _prepare_start(
    request: ManagedStartRequest,
    store: ManagedJobStore,
    *,
    python: str,
    runner: Callable[[tuple[str, ...]], int | None] = _run_systemd,
) -> _PreparedStart:
    """One attempt only; existing assets refuse instead of authorizing replay."""
    from . import _managed_job_request as request_wire

    launch = request_wire.decode_request_launch(request.job.launch)
    run_id = launch["run_id"]
    if store.run_id != run_id or launch["unit"] != f"agw-managed-{run_id}.service":
        raise ManagedStartError("managed start identity mismatch")
    if store.read_fact(FactName.LAUNCH) is not None or any(
        store.read_request_asset(name) is not None for name in RequestAsset
    ):
        raise ManagedStartError("managed start already staged")
    argv = _service_argv(run_id, python)
    store.publish_request(request.job)
    status = runner(argv)
    if status is not None and (type(status) is not int or not -255 <= status <= 255):
        raise ManagedStartError("invalid systemd client outcome")
    stored = store.read_fact(FactName.LAUNCH)
    if stored is not None and stored != request.job.launch:
        raise ManagedStartError("managed launch mismatch")
    result = ManagedStartResult(
        status if status is not None and status >= 0 else None,
        -status if status is not None and status < 0 else None,
        (FactName.LAUNCH,) if stored is not None else (),
    )
    return _PreparedStart(result, stored)


def _write_result(writer: FileRecordWriter, prepared: _PreparedStart) -> None:
    writer.write(FileRecordKind.RESULT, encode_result(prepared.result))
    if prepared.launch is not None:
        writer.write(FileRecordKind.DATA, prepared.launch)
    writer.write(FileRecordKind.FINISHED, b"")


def main(nonce: str) -> int:
    """Attempt one start; never infer remote absence or reread live target identity."""
    writer = FileRecordWriter(nonce)
    try:
        request = _read_request()
        if request.nonce != nonce or sys.platform != "linux" or request.identity.euid != 0:
            raise ManagedStartError("managed start prerequisite")
        if sys.version_info < (3, 11) or not os.path.isabs(sys.executable):
            raise ManagedStartError("managed start runtime")
        if not matches_current_identity(request.identity):
            raise ManagedStartError("managed start identity")
        from . import _managed_job_request as request_wire

        launch = request_wire.decode_request_launch(request.job.launch)
        with ManagedJobStore(cast("str", launch["run_id"])) as store:
            prepared = _prepare_start(request, store, python=sys.executable)
    except (ManagedStartError, StoreError, OSError, ValueError, TypeError):
        writer.write(FileRecordKind.FAILED, b"")
        writer.write(FileRecordKind.FINISHED, b"")
        return 0
    _write_result(writer, prepared)
    return 0
