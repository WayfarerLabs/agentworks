"""Synthetic character boundaries and comparison protocol, with no console effects."""

from __future__ import annotations

import ctypes
import struct
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from tests.execution.carriers.ssh import windows_console_probe as probe
from tests.execution.carriers.ssh import windows_input_comparison as comparison


def test_private_release_record_uses_win32_abi() -> None:
    record = probe._key_record(0x82A0)
    record.event.key.down = 0
    record.event.key.virtual_key = 0x12
    record.event.key.control = 0x04000000
    assert bytes(record) == struct.pack("<H2xiHHHHI", 1, 0, 1, 0x12, 0, 0x82A0, 0x04000000)
    assert comparison._describe(record) == {
        "kind": 1,
        "down": 0,
        "repeat": 1,
        "vkey": 0x12,
        "unit": 0x82A0,
        "control": 0x04000000,
    }


@pytest.mark.parametrize("private_honored", [False, True])
def test_comparison_reinjects_identical_vectors_and_reports_observations(private_honored: bool) -> None:
    injections: list[tuple[bytes, ...]] = []
    observations: list[dict[str, object]] = []
    references: list[tuple[int, bytes]] = []
    reads: list[int] = []

    class Native:
        def __init__(self) -> None:
            self.kernel = SimpleNamespace(
                ReadConsoleW=Mock(side_effect=self.read),
                MultiByteToWideChar=Mock(side_effect=self.convert),
                SetConsoleMode=self.set_mode,
            )
            self.current_mode = 0x0227
            self.current_pages = (65001, 65001)
            self.vector: list[probe._InputRecord] = []
            self.pending: list[int] = []

        def check(self, value: int) -> None:
            assert value

        def mode(self, handle: int) -> int:
            assert handle == 832
            return self.current_mode

        def set_mode(self, handle: int, mode: int) -> int:
            assert handle == 832
            self.current_mode = mode
            return 1

        def pages(self) -> tuple[int, int]:
            return self.current_pages

        def set_pages(self, pages: tuple[int, int]) -> None:
            self.current_pages = pages

        def inject(self, handle: int, values: list[probe._InputRecord]) -> None:
            assert handle == 832
            assert values[-1].event.key.down == 1
            assert values[-1].event.key.character == 0x5A
            injections.append(tuple(bytes(value) for value in values))
            self.vector = values.copy()
            candidate = values[0].event.key
            # Scripted outcomes exercise observation handling, not an emulator.
            self.pending = []
            if candidate.control and private_honored:
                self.pending = [0x2603]
            elif not candidate.control and (candidate.down or candidate.virtual_key == 0x12):
                self.pending = [0x03A9]
            self.pending.append(0x5A)

        def drain(self, handle: int) -> None:
            assert handle == 832
            self.vector.clear()
            self.pending.clear()

        def poll(self, handle: int) -> tuple[list[probe._InputRecord], float]:
            assert handle == 832
            records, self.vector = self.vector, []
            return records, 0.125

        def read(self, handle: int, buffer: object, capacity: int, count: object, control: object) -> int:
            assert handle == 832 and capacity == 32 and control is None
            units = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint16))
            units[0] = self.pending.pop(0)
            ctypes.cast(count, ctypes.POINTER(ctypes.c_uint32)).contents.value = 1
            reads.append(units[0])
            if not self.pending:
                self.vector.clear()
            return 1

        def convert(self, page: int, flags: int, packed: bytes, length: int, buffer: object, capacity: int) -> int:
            assert flags == 0 and length == len(packed) and capacity == 8
            references.append((page, packed))
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint16))[0] = page
            return 1

    native = Native()
    comparison.compare(cast("probe._Native", native), 832, probe._key_record, observations.append)
    assert len(observations) == 48
    assert len(injections) == 96
    assert all(before == after for before, after in zip(injections[::2], injections[1::2], strict=True))
    assert len(references) == 12
    assert len(reads) == (84 if private_honored else 72)
    assert {(value["input_page"], value["output_page"], value["vt_input"]) for value in observations} == {
        (input_page, output_page, vt)
        for input_page, output_page in ((1252, 437), (932, 437), (437, 1252), (932, 1252), (1252, 932), (437, 932))
        for vt in (False, True)
    }
    for value in observations:
        assert value["phase"] == "input_comparison"
        assert value["low_level_records"] == value["injected"]
        assert value["remaining_records"] == []
        assert value["nowait_seconds"] == 0.125
        assert value["input_mode"] == (0x0227 & ~(7 | 0x0200)) | (0x0200 if value["vt_input"] else 0)
        if value["case"] == "private_flagged_vk_menu_release":
            assert value["read_console_w_batches"] == ([[0x2603], [0x5A]] if private_honored else [[0x5A]])
            assert value["packed_bytes"] == ([0x82, 0xA0] if value["output_page"] == 932 else [0x82])
            assert value["native_page_reference_units"] == {
                str(value["input_page"]): [value["input_page"]],
                str(value["output_page"]): [value["output_page"]],
            }
        elif value["case"] == "unicode_ordinary_release":
            assert value["read_console_w_batches"] == [[0x5A]]
        else:
            assert value["read_console_w_batches"] == [[0x03A9], [0x5A]]


@pytest.mark.parametrize("count", [0, 33])
def test_character_read_rejects_invalid_native_count(count: int) -> None:
    def read(handle: int, buffer: object, capacity: int, result: object, control: object) -> int:
        ctypes.cast(result, ctypes.POINTER(ctypes.c_uint32)).contents.value = count
        return 1

    native = SimpleNamespace(
        kernel=SimpleNamespace(ReadConsoleW=Mock(side_effect=read), MultiByteToWideChar=Mock()),
        check=lambda value: None,
    )
    api = comparison._CharacterAPI(cast("probe._Native", native))
    with pytest.raises(AssertionError):
        api.read_through_sentinel(832)


def test_character_read_bounds_returning_batches_without_sentinel() -> None:
    calls = []

    def read(handle: int, buffer: object, capacity: int, result: object, control: object) -> int:
        calls.append(handle)
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint16))[0] = 0x0041
        ctypes.cast(result, ctypes.POINTER(ctypes.c_uint32)).contents.value = 1
        return 1

    native = SimpleNamespace(
        kernel=SimpleNamespace(ReadConsoleW=Mock(side_effect=read), MultiByteToWideChar=Mock()),
        check=lambda value: None,
    )
    api = comparison._CharacterAPI(cast("probe._Native", native))
    with pytest.raises(AssertionError):
        api.read_through_sentinel(832)
    assert calls == [832] * 32
