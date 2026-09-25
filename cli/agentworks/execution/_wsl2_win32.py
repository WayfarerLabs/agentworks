"""Lazy public-ctypes boundary for the private Windows WSL2 owner."""

from __future__ import annotations

import os
import subprocess
from typing import Any


class WindowsApi:
    """The small lazy ctypes boundary used by ``WindowsWSL2HostClient``."""

    ERROR_INSUFFICIENT_BUFFER = 122
    ERROR_BROKEN_PIPE = 109
    ERROR_INVALID_HANDLE = 6
    ERROR_NO_MORE_FILES = 18
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 258
    WAIT_FAILED = 0xFFFFFFFF

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows WSL host client is unavailable on this host")
        import ctypes
        from ctypes import wintypes

        self.ctypes: Any = ctypes
        self.wintypes: Any = wintypes
        self.kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        self.invalid_handle = _handle_value(wintypes.HANDLE(-1))

        class SecurityAttributes(ctypes.Structure):
            _fields_ = [
                ("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", wintypes.LPVOID),
                ("bInheritHandle", wintypes.BOOL),
            ]

        class StartupInfo(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD),
                ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD),
                ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD),
                ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE),
            ]

        class StartupInfoEx(ctypes.Structure):
            _fields_ = [("StartupInfo", StartupInfo), ("lpAttributeList", wintypes.LPVOID)]

        class ProcessInformation(ctypes.Structure):
            _fields_ = [
                ("hProcess", wintypes.HANDLE),
                ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD),
            ]

        class ProcessEntry32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class BasicAccountingInformation(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_int64),
                ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64),
                ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        self.SecurityAttributes: Any = SecurityAttributes
        self.StartupInfoEx: Any = StartupInfoEx
        self.ProcessInformation: Any = ProcessInformation
        self.ProcessEntry32: Any = ProcessEntry32
        self.ExtendedLimitInformation: Any = ExtendedLimitInformation
        self.BasicAccountingInformation: Any = BasicAccountingInformation
        self._bind()

    def _bind(self) -> None:
        c = self.ctypes
        w = self.wintypes
        k = self.kernel
        k.CreateJobObjectW.argtypes = [w.LPVOID, w.LPCWSTR]
        k.CreateJobObjectW.restype = w.HANDLE
        k.GetSystemDirectoryW.argtypes = [w.LPWSTR, w.UINT]
        k.GetSystemDirectoryW.restype = w.UINT
        k.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, w.LPVOID, w.DWORD]
        k.SetInformationJobObject.restype = w.BOOL
        k.CreatePipe.argtypes = [c.POINTER(w.HANDLE), c.POINTER(w.HANDLE), w.LPVOID, w.DWORD]
        k.CreatePipe.restype = w.BOOL
        k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
        k.CreateFileW.restype = w.HANDLE
        k.SetHandleInformation.argtypes = [w.HANDLE, w.DWORD, w.DWORD]
        k.SetHandleInformation.restype = w.BOOL
        k.InitializeProcThreadAttributeList.argtypes = [w.LPVOID, w.DWORD, w.DWORD, c.POINTER(c.c_size_t)]
        k.InitializeProcThreadAttributeList.restype = w.BOOL
        k.UpdateProcThreadAttribute.argtypes = [w.LPVOID, w.DWORD, c.c_size_t, w.LPVOID, c.c_size_t, w.LPVOID, w.LPVOID]
        k.UpdateProcThreadAttribute.restype = w.BOOL
        k.DeleteProcThreadAttributeList.argtypes = [w.LPVOID]
        k.DeleteProcThreadAttributeList.restype = None
        k.CreateProcessW.argtypes = [
            w.LPCWSTR,
            w.LPWSTR,
            w.LPVOID,
            w.LPVOID,
            w.BOOL,
            w.DWORD,
            w.LPVOID,
            w.LPCWSTR,
            w.LPVOID,
            w.LPVOID,
        ]
        k.CreateProcessW.restype = w.BOOL
        k.CloseHandle.argtypes = [w.HANDLE]
        k.CloseHandle.restype = w.BOOL
        k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        k.WaitForSingleObject.restype = w.DWORD
        k.GetExitCodeProcess.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]
        k.GetExitCodeProcess.restype = w.BOOL
        k.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        k.TerminateJobObject.restype = w.BOOL
        k.QueryInformationJobObject.argtypes = [w.HANDLE, c.c_int, w.LPVOID, w.DWORD, w.LPVOID]
        k.QueryInformationJobObject.restype = w.BOOL
        k.ReadFile.argtypes = [w.HANDLE, w.LPVOID, w.DWORD, c.POINTER(w.DWORD), w.LPVOID]
        k.ReadFile.restype = w.BOOL
        k.GetCurrentProcess.argtypes = []
        k.GetCurrentProcess.restype = w.HANDLE
        k.GetCurrentProcessId.argtypes = []
        k.GetCurrentProcessId.restype = w.DWORD
        k.GetProcessTimes.argtypes = [
            w.HANDLE,
            c.POINTER(w.FILETIME),
            c.POINTER(w.FILETIME),
            c.POINTER(w.FILETIME),
            c.POINTER(w.FILETIME),
        ]
        k.GetProcessTimes.restype = w.BOOL
        k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.OpenProcess.restype = w.HANDLE
        k.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
        k.CreateToolhelp32Snapshot.restype = w.HANDLE
        k.Process32FirstW.argtypes = [w.HANDLE, c.POINTER(self.ProcessEntry32)]
        k.Process32FirstW.restype = w.BOOL
        k.Process32NextW.argtypes = [w.HANDLE, c.POINTER(self.ProcessEntry32)]
        k.Process32NextW.restype = w.BOOL

    def current_controller_identity(self) -> tuple[int, int]:
        """Capture this controller's PID and exact creation FILETIME ticks."""
        pid = int(self.kernel.GetCurrentProcessId())
        process = self.kernel.GetCurrentProcess()
        if pid <= 0:
            raise self._error("GetCurrentProcessId")
        if not process:
            raise self._error("GetCurrentProcess")
        return pid, self.process_creation_ticks(process)

    def process_creation_ticks(self, process: int) -> int:
        """Read exact FILETIME creation ticks from a pinned process handle."""
        created = self.wintypes.FILETIME()
        exited = self.wintypes.FILETIME()
        kernel = self.wintypes.FILETIME()
        user = self.wintypes.FILETIME()
        if not self.kernel.GetProcessTimes(
            process,
            self.ctypes.byref(created),
            self.ctypes.byref(exited),
            self.ctypes.byref(kernel),
            self.ctypes.byref(user),
        ):
            raise self._error("GetProcessTimes")
        ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        if ticks <= 0:
            raise OSError("GetProcessTimes returned an invalid creation time")
        return ticks

    def open_process_for_observation(self, pid: int) -> int:
        """Open read-only query and synchronization access to one PID."""
        handle = _handle_value(self.kernel.OpenProcess(0x00101000, False, pid))
        if not handle:
            raise self._error("OpenProcess")
        return handle

    def snapshot_processes(self) -> int:
        """Open one process-list snapshot; the caller closes the returned handle."""
        handle = _handle_value(self.kernel.CreateToolhelp32Snapshot(0x00000002, 0))
        if handle == self.invalid_handle:
            raise self._error("CreateToolhelp32Snapshot")
        return handle

    def first_snapshot_pid(self, snapshot: int) -> int | None:
        return self._snapshot_pid(snapshot, first=True)

    def next_snapshot_pid(self, snapshot: int) -> int | None:
        return self._snapshot_pid(snapshot, first=False)

    def _snapshot_pid(self, snapshot: int, *, first: bool) -> int | None:
        entry = self.ProcessEntry32()
        entry.dwSize = self.ctypes.sizeof(entry)
        self.ctypes.set_last_error(0)
        method = self.kernel.Process32FirstW if first else self.kernel.Process32NextW
        if method(snapshot, self.ctypes.byref(entry)):
            return int(entry.th32ProcessID)
        if self.ctypes.get_last_error() == self.ERROR_NO_MORE_FILES:
            return None
        raise self._error("Process32FirstW" if first else "Process32NextW")

    def create_job(self) -> int:
        handle = _handle_value(self.kernel.CreateJobObjectW(None, None))
        if not handle:
            raise self._error("CreateJobObjectW")
        return handle

    def wsl_executable(self) -> str:
        directory = self.ctypes.create_unicode_buffer(32768)
        length = int(self.kernel.GetSystemDirectoryW(directory, len(directory)))
        if length == 0 or length >= len(directory):
            raise self._error("GetSystemDirectoryW")
        return os.path.join(directory.value, "wsl.exe")

    def configure_kill_on_close(self, handle: int) -> None:
        info = self.ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = 0x00002000
        if not self.kernel.SetInformationJobObject(handle, 9, self.ctypes.byref(info), self.ctypes.sizeof(info)):
            raise self._error("SetInformationJobObject")

    def pipe(self) -> tuple[int, int]:
        read = self.wintypes.HANDLE()
        write = self.wintypes.HANDLE()
        attributes = self.SecurityAttributes(self.ctypes.sizeof(self.SecurityAttributes), None, True)
        if not self.kernel.CreatePipe(
            self.ctypes.byref(read), self.ctypes.byref(write), self.ctypes.byref(attributes), 0
        ):
            raise self._error("CreatePipe")
        return _handle_value(read), _handle_value(write)

    def nul_for_child(self) -> int:
        attributes = self.SecurityAttributes(self.ctypes.sizeof(self.SecurityAttributes), None, True)
        handle = _handle_value(self.kernel.CreateFileW("NUL", 0x40000000, 3, self.ctypes.byref(attributes), 3, 0, None))
        if handle == self.invalid_handle:
            raise self._error("CreateFileW")
        return handle

    def non_inheritable(self, handle: int) -> None:
        if not self.kernel.SetHandleInformation(handle, 1, 0):
            raise self._error("SetHandleInformation")

    def create_process(
        self,
        argv: tuple[str, ...],
        job: int,
        stdin: int,
        stdout: int,
        stderr: int,
        process_info: Any,
    ) -> tuple[int, int]:
        c = self.ctypes
        size = c.c_size_t()
        c.set_last_error(0)
        self.kernel.InitializeProcThreadAttributeList(None, 2, 0, c.byref(size))
        if c.get_last_error() != self.ERROR_INSUFFICIENT_BUFFER or size.value == 0:
            raise self._error("InitializeProcThreadAttributeList")
        attributes = c.create_string_buffer(size.value)
        attributes_pointer = c.cast(attributes, self.wintypes.LPVOID)
        if not self.kernel.InitializeProcThreadAttributeList(attributes_pointer, 2, 0, c.byref(size)):
            raise self._error("InitializeProcThreadAttributeList")
        job_list = (self.wintypes.HANDLE * 1)(job)
        handle_list = (self.wintypes.HANDLE * 3)(stdin, stdout, stderr)
        try:
            if not self.kernel.UpdateProcThreadAttribute(
                attributes_pointer, 0, 0x0002000D, c.byref(job_list), c.sizeof(job_list), None, None
            ):
                raise self._error("UpdateProcThreadAttribute job list")
            if not self.kernel.UpdateProcThreadAttribute(
                attributes_pointer, 0, 0x00020002, c.byref(handle_list), c.sizeof(handle_list), None, None
            ):
                raise self._error("UpdateProcThreadAttribute handle list")
            startup = self.StartupInfoEx()
            startup.StartupInfo.cb = c.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x00000100
            startup.StartupInfo.hStdInput = stdin
            startup.StartupInfo.hStdOutput = stdout
            startup.StartupInfo.hStdError = stderr
            startup.lpAttributeList = attributes_pointer
            command_line = c.create_unicode_buffer(subprocess.list2cmdline(list(argv)))
            if not self.kernel.CreateProcessW(
                argv[0], command_line, None, None, True, 0x00080000, None, None, c.byref(startup), c.byref(process_info)
            ):
                raise self._error("CreateProcessW")
            return _handle_value(process_info.hProcess), _handle_value(process_info.hThread)
        finally:
            self.kernel.DeleteProcThreadAttributeList(attributes_pointer)

    def close_handle(self, handle: int) -> None:
        if not self.kernel.CloseHandle(handle):
            raise self._error("CloseHandle")

    def wait_process(self, handle: int, milliseconds: int) -> int:
        return int(self.kernel.WaitForSingleObject(handle, milliseconds))

    def exit_code(self, handle: int) -> int:
        status = self.wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(handle, self.ctypes.byref(status)):
            raise self._error("GetExitCodeProcess")
        return int(status.value)

    def terminate_job(self, handle: int) -> None:
        if not self.kernel.TerminateJobObject(handle, 1):
            raise self._error("TerminateJobObject")

    def active_processes(self, handle: int) -> int:
        info = self.BasicAccountingInformation()
        if not self.kernel.QueryInformationJobObject(
            handle, 1, self.ctypes.byref(info), self.ctypes.sizeof(info), None
        ):
            raise self._error("QueryInformationJobObject")
        return int(info.ActiveProcesses)

    def read_pipe(self, handle: int, limit: int) -> tuple[bytes | None, int]:
        buffer = self.ctypes.create_string_buffer(limit)
        received = self.wintypes.DWORD()
        if self.kernel.ReadFile(handle, buffer, limit, self.ctypes.byref(received), None):
            return buffer.raw[: received.value], 0
        return None, int(self.ctypes.get_last_error())

    def _error(self, operation: str) -> OSError:
        return OSError(self.ctypes.get_last_error(), f"{operation} failed")


def _handle_value(handle: Any) -> int:
    value = getattr(handle, "value", handle)
    return 0 if value is None else int(value)
