"""Windows capture handles that prevent ancestor replacement and reparse edits.

CreateFile's no-reparse flag protects only the final component. Callers must
open ancestors from the volume root down and retain them while using a path.
Sharing read access only prevents both deletion/rename and writes through other
handles, including changing an opened directory into a reparse point. An already
incompatible open causes capture to fail, rather than weakening this boundary.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@contextmanager
def locked_file(path: Path) -> Iterator[int]:
    """Yield a readable descriptor for a non-reparse file or directory."""
    if sys.platform != "win32":
        raise OSError("Windows capture handles are unavailable")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation", wintypes.FILETIME),
            ("access", wintypes.FILETIME),
            ("write", wintypes.FILETIME),
            ("volume", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD),
            ("index_low", wintypes.DWORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileInformation)]
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING,
    # FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT.
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError("could not lock artifact source")
    descriptor: int | None = None
    try:
        info = FileInformation()
        if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)) or info.attributes & 0x400:
            raise OSError("artifact source is unavailable or a reparse point")
        # Ownership transfers to the CRT descriptor only after successful conversion.
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        yield descriptor
    finally:
        if descriptor is None:
            kernel.CloseHandle(handle)
        else:
            os.close(descriptor)
