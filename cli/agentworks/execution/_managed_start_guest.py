"""Fixed Linux root helper that stages one request and attempts one service activation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from ._file_wire import FileRecordKind, FileRecordWriter
from ._helper_identity import matches_current_identity
from ._managed_job_store import FactName, ManagedJobStore, RequestAsset, StoreError
from ._managed_lease_store import publish_initial_lease
from ._managed_lease_wire import boottime_ns, checked_lease
from ._managed_service_bundle import FIXED_SOURCE
from ._managed_start_protocol import (
    MAX_REQUEST_BYTES,
    ManagedStartError,
    ManagedStartRequest,
    ManagedStartResult,
    decode_request,
    encode_result,
)
from ._vm_guest_identity_guest import _GuestRefusal, _identity
from ._vm_guest_identity_protocol import vm_guest_boot_id

_SYSTEMD_RUN = "/usr/bin/systemd-run"
_START_TIMEOUT_SECONDS = 45.0
_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}

if TYPE_CHECKING:
    from collections.abc import Callable

    from ._helper_identity import IdentityExpectation
    from ._vm_guest_identity_protocol import VMGuestIdentity


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


def _service_argv(run_id: str, python: str, identity: IdentityExpectation, guest: VMGuestIdentity) -> tuple[str, ...]:
    """Carry validated controller credentials and guest facts as bounded data."""
    if (
        type(run_id) is not str
        or len(run_id) != 32
        or any(character not in "0123456789abcdef" for character in run_id)
        or type(python) is not str
        or not python.startswith("/")
        or "\0" in python
    ):
        raise ManagedStartError("invalid managed service identity")
    admission = json.dumps(
        {
            "identity": {"euid": identity.euid, "egid": identity.egid, "groups": list(identity.groups)},
            "guest": [guest.instance_marker, guest.boot_id, guest.init_start_ticks],
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(admission) > 65_536:
        raise ManagedStartError("managed service admission exceeds bound")
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
        admission,
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
    argv = _service_argv(run_id, python, request.identity, request.guest)
    if request.job.operation_lease is not None:
        checked_lease(request.job.operation_lease, request.job.launch, boottime_ns())
    store.publish_request(request.job)
    if request.job.operation_lease is not None:
        publish_initial_lease(store, request.job.launch, request.job.operation_lease)
        checked_lease(request.job.operation_lease, request.job.launch, boottime_ns())
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
    """Fence one guest identity before opening the managed job store."""
    writer = FileRecordWriter(nonce)
    try:
        request = _read_request()
        if request.nonce != nonce or sys.platform != "linux" or request.identity.euid != 0:
            raise ManagedStartError("managed start prerequisite")
        if sys.version_info < (3, 11) or not os.path.isabs(sys.executable):
            raise ManagedStartError("managed start runtime")
        if not matches_current_identity(request.identity):
            raise ManagedStartError("managed start identity")
        if _identity() != request.guest:
            raise ManagedStartError("managed start guest identity mismatch")
        from . import _managed_job_request as request_wire

        launch = request_wire.decode_request_launch(request.job.launch)
        target = cast("dict[str, object]", launch["target"])
        if target["kind"] != "vm" or target["boot_id"] != vm_guest_boot_id(request.guest):
            raise ManagedStartError("managed start target boot mismatch")
        with ManagedJobStore(cast("str", launch["run_id"])) as store:
            prepared = _prepare_start(request, store, python=sys.executable)
    except (ManagedStartError, StoreError, _GuestRefusal, OSError, ValueError, TypeError):
        writer.write(FileRecordKind.FAILED, b"")
        writer.write(FileRecordKind.FINISHED, b"")
        return 0
    _write_result(writer, prepared)
    return 0
