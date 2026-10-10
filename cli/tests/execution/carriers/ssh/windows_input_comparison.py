"""Injected record/character comparison on the fixture's exclusively owned console.

These are synthetic private-flagged records, never physical Alt-numpad keys.
The production keyboard remains disabled; this module only reports measurements.
ReadConsoleW may block: the retained parent kills/reaps this exact fixture child.
"""

from __future__ import annotations

import ctypes
from time import perf_counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.execution.carriers.ssh.windows_console_probe import _InputRecord, _Native

# Microsoft's win32metadata generation/WinSDK/RecompiledIdlHeaders/um/kbd.h:54.
_PRIVATE_ALTNUMPAD = 0x04000000
_VK_MENU = 0x12
_VT_INPUT = 0x0200
_SENTINEL = 0x005A


class _CharacterAPI:
    """Two native boundaries borrowing the existing fixture's kernel binding."""

    def __init__(self, native: _Native) -> None:
        self.native = native
        kernel = native.kernel
        kernel.ReadConsoleW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        kernel.ReadConsoleW.restype = ctypes.c_int32
        kernel.MultiByteToWideChar.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.c_int32,
        ]
        kernel.MultiByteToWideChar.restype = ctypes.c_int32

    def reference_units(self, page: int, packed: bytes) -> list[int]:
        units = (ctypes.c_uint16 * 8)()
        count = self.native.kernel.MultiByteToWideChar(page, 0, packed, len(packed), units, len(units))
        self.native.check(count)
        assert 0 < count <= len(units)
        return list(units[:count])

    def read_through_sentinel(self, handle: int) -> list[list[int]]:
        reads = []
        # Every injected vector ends with an ordinary keydown Z. An ignored
        # release must still have a queued character to complete the native read.
        for _ in range(32):
            units = (ctypes.c_uint16 * 32)()
            count = ctypes.c_uint32()
            self.native.check(self.native.kernel.ReadConsoleW(handle, units, len(units), ctypes.byref(count), None))
            assert 0 < count.value <= len(units)
            reads.append(list(units[: count.value]))
            if _SENTINEL in reads[-1]:
                return reads
        raise AssertionError("Owned fixture character read exceeded its batch budget")


def _describe(record: _InputRecord) -> dict[str, int]:
    key = record.event.key
    return {
        "kind": record.kind,
        "down": key.down,
        "repeat": key.repeat,
        "vkey": key.virtual_key,
        "unit": key.character,
        "control": key.control,
    }


def compare(
    native: _Native,
    handle: int,
    make_key: Callable[[int], _InputRecord],
    emit: Callable[[dict[str, object]], None],
) -> None:
    """Emit each completed measurement before the next potentially blocking call.

    Terminal dc8ae365847d41f62d1ebca93815bb00959bc12f, host/stream.cpp:170-198
    and host/misc.cpp:19-28 motivate the private probes and output-page boundary.
    That source does not establish this Windows binary's behavior. Record reads
    and character reads consume separate injections of byte-identical vectors.
    """
    api = _CharacterAPI(native)
    saved_mode = native.mode(handle)
    # The outer fixture restores original modes/pages after this worker settles.
    # All mutations here target only its freshly created exclusive console.
    for input_page, output_page in ((1252, 437), (932, 437), (437, 1252), (932, 1252), (1252, 932), (437, 932)):
        native.set_pages((input_page, output_page))
        assert native.pages() == (input_page, output_page)
        # The packed DBCS probe belongs only on DBCS output. The pinned host can
        # fail fast for a two-byte probe whose first byte is not a DBCS lead byte.
        packed = b"\x82\xa0" if output_page == 932 else b"\x82"
        private_unit = 0x82A0 if output_page == 932 else 0x0082
        references = {str(page): api.reference_units(page, packed) for page in (input_page, output_page)}
        for vt in (False, True):
            mode = (saved_mode & ~(7 | _VT_INPUT)) | (_VT_INPUT if vt else 0)
            native.check(native.kernel.SetConsoleMode(handle, mode))
            assert native.mode(handle) == mode
            for name, down, vkey, unit, control in (
                ("private_flagged_vk_menu_release", 0, _VK_MENU, private_unit, _PRIVATE_ALTNUMPAD),
                ("unicode_vk_menu_release", 0, _VK_MENU, 0x03A9, 0),
                ("unicode_ordinary_release", 0, 0x41, 0x03A9, 0),
                ("unicode_keydown", 1, 0x41, 0x03A9, 0),
            ):
                candidate, sentinel = make_key(unit), make_key(_SENTINEL)
                candidate.event.key.down = down
                candidate.event.key.virtual_key = vkey
                candidate.event.key.control = control
                sentinel.event.key.virtual_key = 0x5A
                vector = [candidate, sentinel]
                native.drain(handle)
                native.inject(handle, vector)
                records, seconds = native.poll(handle)
                record_data = [_describe(record) for record in records]
                native.drain(handle)
                native.inject(handle, vector)
                start = perf_counter()
                reads = api.read_through_sentinel(handle)
                character_seconds = perf_counter() - start
                if name == "unicode_keydown":
                    assert [unit for batch in reads for unit in batch] == [0x03A9, _SENTINEL]
                remaining, _ = native.poll(handle)
                emit(
                    {
                        "phase": "input_comparison",
                        "case": name,
                        "input_page": input_page,
                        "output_page": output_page,
                        "input_mode": mode,
                        "vt_input": vt,
                        "injected": [_describe(record) for record in vector],
                        "packed_bytes": list(packed) if control else None,
                        "native_page_reference_units": references if control else None,
                        "low_level_records": record_data,
                        "nowait_seconds": seconds,
                        "read_console_w_batches": reads,
                        "read_console_w_seconds": character_seconds,
                        "remaining_records": [_describe(record) for record in remaining],
                    }
                )
