"""Private native Windows owner for the WSL2 guest-anchor client.

The owner deliberately uses CreateProcessW directly. A Job Object and the
three child standard handles are both creation-time attributes, so there is no
post-spawn interval in which a WSL client can exist outside its Job.
"""

from __future__ import annotations

import _thread
import os
import threading
import time
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from agentworks.execution._wsl2_lifecycle import (
    HandleSettlement,
    HostClientStatus,
    JobAssignment,
    LocalResourceSnapshot,
)
from agentworks.execution._wsl2_win32 import WindowsApi
from agentworks.execution.carrier import Deadline

if TYPE_CHECKING:
    from collections.abc import Callable

_POLL_SECONDS = 0.01
_CLEANUP_SECONDS = 0.5
_READ_CHUNK_BYTES = 4096
_MAX_STDOUT_BYTES = 1024


class _Admission(StrEnum):
    WAITING = "waiting"
    ADMITTED = "admitted"
    CANCELLED = "cancelled"


class _NoDispatchDeadline(Exception):
    """The owner declined native creation before CreateProcessW."""


class WindowsWSL2HostClient:
    """Own one literal Windows WSL client and its creation-time Job Object.

    Construction only allocates Python synchronization state. Native modules,
    handles, and threads which acquire handles are reached after ``spawn_owned``
    admits an argv to the default-deny owner thread.
    """

    def __init__(self, *, api_factory: Callable[[], WindowsApi] | None = None) -> None:
        self._condition = threading.Condition()
        self._admission = _Admission.WAITING
        self._argv: tuple[str, ...] | None = None
        self._operation_deadline: Deadline | None = None
        self._spawn_called = False
        self._creation_finished = False
        self._creation_failure = False
        self._creation_error: OSError | None = None
        self._acquiring = False
        self._api: WindowsApi | None = None
        self._api_factory = WindowsApi if api_factory is None else api_factory
        self._process_info: Any | None = None
        self._process_info_observed = False
        self._process_info_incomplete = False
        self._host_status = HostClientStatus.NOT_CREATED
        self._exit_status: int | None = None
        self._job_assignment = JobAssignment.NOT_CREATED
        self._job_handle: int | None = None
        self._job_ever_created = False
        self._job_uncertain = False
        self._job_failed_generation: int | None = None
        self._handles: dict[str, int] = {}
        self._handle_uncertain: set[str] = set()
        self._handle_failed_generation: dict[str, int] = {}
        self._stdin_close_requested = False
        self._cleanup_generation = 0
        self._cleanup_finished_generation = 0
        self._cleanup_deadline: Deadline | None = None
        self._stdout = bytearray()
        self._stdout_eof = False
        self._stdout_error = False
        self._drain_admission = _Admission.WAITING
        self._drain_terminal = False

    def spawn_owned(self, argv: tuple[str, ...], deadline: Deadline) -> None:
        """Admit one literal argv and wait only for its creation result."""
        if not _literal_argv(argv):
            raise ValueError("Windows WSL host client requires literal non-NUL argv")
        if deadline.expired:
            raise TimeoutError("Windows WSL host-client creation deadline expired")
        with self._condition:
            if self._spawn_called:
                raise RuntimeError("Windows WSL host client was already started")
            self._spawn_called = True
        try:
            _thread.start_new_thread(self._owner_entry, ())
        except BaseException:
            with self._condition:
                self._admission = _Admission.CANCELLED
                self._condition.notify_all()
            raise
        try:
            with self._condition:
                self._argv = argv
                self._operation_deadline = deadline
                self._admission = _Admission.ADMITTED
                self._host_status = HostClientStatus.UNKNOWN
                self._job_assignment = JobAssignment.UNKNOWN
                self._acquiring = True
                self._condition.notify_all()
        except BaseException:
            with self._condition:
                if self._admission == _Admission.WAITING:
                    self._admission = _Admission.CANCELLED
                    self._condition.notify_all()
            raise
        while True:
            with self._condition:
                if self._creation_finished:
                    if self._creation_failure:
                        if self._creation_error is not None:
                            raise self._creation_error
                        raise OSError("Windows WSL host-client creation failed")
                    if deadline.expired:
                        self._request_cleanup_locked(Deadline.after(_CLEANUP_SECONDS))
                        raise TimeoutError("Windows WSL host-client creation deadline expired")
                    return
                remaining = deadline.remaining()
                if remaining is not None and remaining <= 0:
                    self._request_cleanup_locked(Deadline.after(_CLEANUP_SECONDS))
                    raise TimeoutError("Windows WSL host-client creation deadline expired")
                self._condition.wait(_POLL_SECONDS if remaining is None else min(_POLL_SECONDS, remaining))

    def read_stdout_line(self, limit: int, deadline: Deadline) -> bytes:
        """Return one complete buffered stdout line without consuming a timeout."""
        if type(limit) is not int or limit < 1:
            raise ValueError("stdout line limit must be a positive integer")
        while True:
            with self._condition:
                newline = self._stdout.find(b"\n")
                if newline >= 0:
                    line = bytes(self._stdout[: newline + 1])
                    del self._stdout[: newline + 1]
                    return line if len(line) <= limit else line[: limit + 1]
                if len(self._stdout) > limit:
                    return bytes(self._stdout[: limit + 1])
                if self._stdout_error:
                    raise OSError("Windows WSL host-client stdout observation failed")
                if self._stdout_eof:
                    line = bytes(self._stdout)
                    self._stdout.clear()
                    return line
                remaining = deadline.remaining()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("Windows WSL host-client stdout deadline expired")
                self._condition.wait(_POLL_SECONDS if remaining is None else min(_POLL_SECONDS, remaining))

    def close_stdin(self) -> None:
        """Request EOF from the owner thread without borrowing its handle."""
        with self._condition:
            self._stdin_close_requested = True
            self._condition.notify_all()

    def wait(self, deadline: Deadline) -> int | None:
        """Wait for the owner thread's exact process observation."""
        while True:
            with self._condition:
                if self._host_status == HostClientStatus.EXITED:
                    return self._exit_status
                if self._creation_failure or self._admission == _Admission.CANCELLED:
                    return None
                remaining = deadline.remaining()
                if remaining is not None and remaining <= 0:
                    return None
                self._condition.wait(_POLL_SECONDS if remaining is None else min(_POLL_SECONDS, remaining))

    def snapshot(self) -> LocalResourceSnapshot:
        """Return separate host, Job, and handle-settlement facts without I/O."""
        with self._condition:
            return LocalResourceSnapshot(
                self._host_status,
                self._exit_status,
                self._job_assignment,
                self._job_settlement_locked(),
                self._host_settlement_locked(),
            )

    def settle(self, deadline: Deadline) -> LocalResourceSnapshot:
        """Ask the owner to close every owned capability once within this budget."""
        with self._condition:
            if self._resources_closed_locked():
                return self.snapshot()
            generation = self._request_cleanup_locked(deadline)
        while True:
            with self._condition:
                if self._cleanup_finished_generation >= generation:
                    return self.snapshot()
                remaining = deadline.remaining()
                if remaining is not None and remaining <= 0:
                    return self.snapshot()
                self._condition.wait(_POLL_SECONDS if remaining is None else min(_POLL_SECONDS, remaining))

    def _request_cleanup_locked(self, deadline: Deadline) -> int:
        self._cleanup_generation += 1
        self._cleanup_deadline = deadline
        self._stdin_close_requested = True
        self._condition.notify_all()
        return self._cleanup_generation

    def _owner_entry(self) -> None:
        """Keep raw-thread failures private while retaining publishable state."""
        try:
            self._run_owner()
        except BaseException:
            with self._condition:
                self._creation_failure = True
                self._creation_finished = True
                self._acquiring = False
                if self._host_status == HostClientStatus.UNKNOWN and not self._process_info_observed:
                    self._host_status = HostClientStatus.NOT_CREATED
                if self._job_assignment == JobAssignment.UNKNOWN and not self._process_info_observed:
                    self._job_assignment = JobAssignment.FAILED
                self._condition.notify_all()
            try:
                if self._api is not None:
                    self._cleanup_until(Deadline.after(_CLEANUP_SECONDS))
            except BaseException:
                pass
            self._owner_loop()

    def _run_owner(self) -> None:
        argv = self._take_admission()
        if argv is None:
            return
        if self._operation_expired():
            with self._condition:
                self._creation_failure = True
                self._creation_finished = True
                self._acquiring = False
                self._host_status = HostClientStatus.NOT_CREATED
                self._job_assignment = JobAssignment.NOT_CREATED
                self._cleanup_finished_generation = self._cleanup_generation
                self._condition.notify_all()
            return
        api = self._api_factory()
        with self._condition:
            self._api = api
        try:
            self._create_process(api, self._resolve_application(api, argv))
        except _NoDispatchDeadline:
            with self._condition:
                self._creation_failure = True
                self._creation_finished = True
                self._acquiring = False
                self._host_status = HostClientStatus.NOT_CREATED
                self._job_assignment = JobAssignment.FAILED
                self._condition.notify_all()
            self._cleanup_until(Deadline.after(_CLEANUP_SECONDS))
            self._owner_loop()
            return
        except OSError as error:
            with self._condition:
                self._creation_failure = True
                self._creation_error = error
                self._creation_finished = True
                self._acquiring = False
                if self._job_assignment == JobAssignment.UNKNOWN and not self._process_info_observed:
                    self._job_assignment = JobAssignment.FAILED
                if self._host_status == HostClientStatus.UNKNOWN and not self._process_info_observed:
                    self._host_status = HostClientStatus.NOT_CREATED
                self._condition.notify_all()
            self._cleanup_until(Deadline.after(_CLEANUP_SECONDS))
            self._owner_loop()
            return
        with self._condition:
            self._host_status = HostClientStatus.ACTIVE
            self._job_assignment = JobAssignment.ASSIGNED_AT_CREATION
            self._creation_finished = True
            self._acquiring = False
            self._condition.notify_all()
            cleanup = self._cleanup_deadline
        if cleanup is not None:
            self._cleanup_until(cleanup)
        self._owner_loop()

    def _take_admission(self) -> tuple[str, ...] | None:
        with self._condition:
            while self._admission == _Admission.WAITING:
                self._condition.wait(_POLL_SECONDS)
            if self._admission != _Admission.ADMITTED:
                return None
            argv = self._argv
            self._argv = None
            assert argv is not None
            return argv

    def _operation_expired(self) -> bool:
        with self._condition:
            deadline = self._operation_deadline
            return deadline is not None and deadline.expired

    def _resolve_application(self, api: WindowsApi, argv: tuple[str, ...]) -> tuple[str, ...]:
        application = argv[0]
        if os.path.isabs(application):
            return argv
        if application.casefold() not in {"wsl", "wsl.exe"}:
            raise OSError("Windows WSL host client requires an absolute executable or wsl.exe")
        return (api.wsl_executable(), *argv[1:])

    def _create_process(self, api: WindowsApi, argv: tuple[str, ...]) -> None:
        job = api.create_job()
        with self._condition:
            self._job_handle = job
            self._job_ever_created = True
        api.configure_kill_on_close(job)
        stdin_read, stdin_write = api.pipe()
        with self._condition:
            self._handles["stdin_child"] = stdin_read
            self._handles["stdin_parent"] = stdin_write
        stdout_read, stdout_write = api.pipe()
        with self._condition:
            self._handles["stdout_parent"] = stdout_read
            self._handles["stdout_child"] = stdout_write
        stderr_nul = api.nul_for_child()
        with self._condition:
            self._handles["stderr_child"] = stderr_nul
        api.non_inheritable(stdin_write)
        api.non_inheritable(stdout_read)
        if not self._dispatch_permitted():
            raise _NoDispatchDeadline
        # This owner-held storage exists before the native call. A control
        # interruption after CreateProcessW returns therefore cannot discard
        # the process and thread handles it filled.
        self._process_info = api.ProcessInformation()
        try:
            api.create_process(argv, job, stdin_read, stdout_write, stderr_nul, self._process_info)
        finally:
            process, thread = _process_handles(self._process_info)
            with self._condition:
                if process:
                    self._handles["process"] = process
                    self._process_info_observed = True
                if thread:
                    self._handles["thread"] = thread
                    self._process_info_observed = True
                if process and thread:
                    self._host_status = HostClientStatus.ACTIVE
                    self._job_assignment = JobAssignment.ASSIGNED_AT_CREATION
                elif process or thread:
                    self._process_info_incomplete = True
        # The child copies are no longer needed. Keep their array storage alive
        # through CreateProcessW, then release every non-parent duplicate.
        for name in ("stdin_child", "stdout_child", "stderr_child"):
            self._close_handle(name)
        if not self._start_drain_thread():
            raise OSError("Windows WSL host-client stdout drain could not start")

    def _start_drain_thread(self) -> bool:
        try:
            _thread.start_new_thread(self._drain_entry, ())
        except BaseException:
            with self._condition:
                if self._drain_admission == _Admission.WAITING:
                    self._drain_admission = _Admission.CANCELLED
                    self._condition.notify_all()
            return False
        try:
            with self._condition:
                self._drain_admission = _Admission.ADMITTED
                self._condition.notify_all()
                return True
        except BaseException:
            with self._condition:
                if self._drain_admission == _Admission.WAITING:
                    self._drain_admission = _Admission.CANCELLED
                    self._condition.notify_all()
            raise

    def _drain_entry(self) -> None:
        try:
            with self._condition:
                while self._drain_admission == _Admission.WAITING:
                    self._condition.wait(_POLL_SECONDS)
                if self._drain_admission != _Admission.ADMITTED:
                    return
            self._drain_stdout()
        except BaseException:
            with self._condition:
                self._stdout_error = True
                self._condition.notify_all()
        finally:
            with self._condition:
                self._drain_terminal = True
                self._condition.notify_all()

    def _drain_stdout(self) -> None:
        api = self._require_api()
        while True:
            with self._condition:
                handle = self._handles.get("stdout_parent")
                cleaning = self._cleanup_generation > 0
            if handle is None:
                with self._condition:
                    self._stdout_eof = True
                    self._condition.notify_all()
                return
            chunk, error = api.read_pipe(handle, _READ_CHUNK_BYTES)
            if chunk is not None:
                with self._condition:
                    available = _MAX_STDOUT_BYTES - len(self._stdout)
                    self._stdout.extend(chunk[:available])
                    if len(chunk) > available:
                        self._stdout_error = True
                    self._condition.notify_all()
                continue
            with self._condition:
                if error == api.ERROR_BROKEN_PIPE or (error == api.ERROR_INVALID_HANDLE and cleaning):
                    self._stdout_eof = True
                else:
                    self._stdout_error = True
                self._condition.notify_all()
            return

    def _owner_loop(self) -> None:
        while True:
            self._poll_process()
            with self._condition:
                if self._resources_closed_locked():
                    self._condition.notify_all()
                    return
                if self._stdin_close_requested:
                    close_stdin = True
                    self._stdin_close_requested = False
                else:
                    close_stdin = False
                generation = self._cleanup_generation
                deadline = self._cleanup_deadline
            if close_stdin:
                self._close_handle("stdin_parent")
            if generation > self._cleanup_finished_generation:
                assert deadline is not None
                self._cleanup_until(deadline)
                with self._condition:
                    self._cleanup_finished_generation = generation
                    self._condition.notify_all()
                continue
            with self._condition:
                self._condition.wait(_POLL_SECONDS)

    def _cleanup_until(self, deadline: Deadline) -> None:
        self._close_handle("stdin_parent")
        self._close_handle("thread")
        initial_active = self._job_active_processes()
        if initial_active is not None and initial_active > 0:
            self._terminate_job()
        while True:
            self._poll_process()
            with self._condition:
                exited = self._host_status == HostClientStatus.EXITED
                not_created = self._host_status == HostClientStatus.NOT_CREATED
                unknown = self._host_status == HostClientStatus.UNKNOWN
                drain_terminal = self._drain_terminal or self._drain_admission != _Admission.ADMITTED
            active = self._job_active_processes()
            if active is not None and active > 0:
                self._terminate_job()
            if not_created:
                for name in tuple(self._handles):
                    self._close_handle(name)
                if active == 0:
                    self._close_job()
                return
            if exited and active == 0 and drain_terminal:
                for name in tuple(self._handles):
                    self._close_handle(name)
                self._close_job()
                return
            if unknown and active == 0 and drain_terminal:
                for name in tuple(self._handles):
                    self._close_handle(name)
                self._close_job()
                return
            remaining = deadline.remaining()
            if remaining is not None and remaining <= 0:
                return
            time.sleep(_POLL_SECONDS if remaining is None else min(_POLL_SECONDS, remaining))

    def _poll_process(self) -> None:
        with self._condition:
            handle = self._handles.get("process")
            already_exited = self._host_status == HostClientStatus.EXITED
        if handle is None or already_exited:
            return
        api = self._require_api()
        result = api.wait_process(handle, 0)
        if result == api.WAIT_OBJECT_0:
            if self._process_info_incomplete:
                self._close_handle("process")
                return
            status = api.exit_code(handle)
            with self._condition:
                self._host_status = HostClientStatus.EXITED
                self._exit_status = status
                self._condition.notify_all()
        elif result == api.WAIT_FAILED:
            with self._condition:
                self._host_status = HostClientStatus.UNKNOWN
                self._condition.notify_all()

    def _close_job(self) -> None:
        with self._condition:
            handle = self._job_handle
            if handle is None or self._job_failed_generation == self._cleanup_generation:
                return
        try:
            self._require_api().close_handle(handle)
        except OSError:
            with self._condition:
                self._job_uncertain = True
                self._job_failed_generation = self._cleanup_generation
                self._condition.notify_all()
        else:
            with self._condition:
                self._job_handle = None
                self._job_uncertain = False
                self._job_failed_generation = None
                self._condition.notify_all()

    def _terminate_job(self) -> None:
        with self._condition:
            handle = self._job_handle
        if handle is None:
            return
        try:
            self._require_api().terminate_job(handle)
        except OSError:
            with self._condition:
                self._job_uncertain = True
                self._condition.notify_all()

    def _job_active_processes(self) -> int | None:
        with self._condition:
            handle = self._job_handle
        if handle is None:
            return 0
        try:
            active = self._require_api().active_processes(handle)
        except OSError:
            with self._condition:
                self._job_uncertain = True
                self._condition.notify_all()
            return None
        with self._condition:
            self._job_uncertain = False
        return active

    def _close_handle(self, name: str) -> None:
        with self._condition:
            handle = self._handles.get(name)
            if handle is None or self._handle_failed_generation.get(name) == self._cleanup_generation:
                return
        try:
            self._require_api().close_handle(handle)
        except OSError:
            with self._condition:
                self._handle_uncertain.add(name)
                self._handle_failed_generation[name] = self._cleanup_generation
                self._condition.notify_all()
        else:
            with self._condition:
                self._handles.pop(name, None)
                self._handle_uncertain.discard(name)
                self._handle_failed_generation.pop(name, None)
                self._condition.notify_all()

    def _job_settlement_locked(self) -> HandleSettlement:
        if self._job_uncertain or self._acquiring or self._cleanup_generation > self._cleanup_finished_generation:
            return HandleSettlement.UNKNOWN
        if self._job_handle is not None:
            return HandleSettlement.OPEN
        if not self._job_ever_created:
            return HandleSettlement.NOT_CREATED
        return HandleSettlement.CLOSED

    def _host_settlement_locked(self) -> HandleSettlement:
        if self._handle_uncertain or self._acquiring or self._cleanup_generation > self._cleanup_finished_generation:
            return HandleSettlement.UNKNOWN
        if self._handles:
            return HandleSettlement.OPEN
        if self._host_status == HostClientStatus.NOT_CREATED:
            return HandleSettlement.NOT_CREATED
        return HandleSettlement.CLOSED

    def _resources_closed_locked(self) -> bool:
        return (
            not self._acquiring
            and self._cleanup_generation <= self._cleanup_finished_generation
            and self._job_settlement_locked() in {HandleSettlement.NOT_CREATED, HandleSettlement.CLOSED}
            and self._host_settlement_locked() in {HandleSettlement.NOT_CREATED, HandleSettlement.CLOSED}
        )

    def _require_api(self) -> WindowsApi:
        with self._condition:
            assert self._api is not None
            return self._api

    def _dispatch_permitted(self) -> bool:
        with self._condition:
            return not self._operation_expired() and self._cleanup_generation == 0


def _literal_argv(argv: object) -> bool:
    return (
        isinstance(argv, tuple)
        and bool(argv)
        and all(isinstance(value, str) and "\0" not in value for value in argv)
        and bool(argv[0])
    )


def _process_handles(process_info: Any) -> tuple[int, int]:
    process = getattr(getattr(process_info, "hProcess", None), "value", getattr(process_info, "hProcess", 0))
    thread = getattr(getattr(process_info, "hThread", None), "value", getattr(process_info, "hThread", 0))
    return (0 if process is None else int(process), 0 if thread is None else int(thread))
