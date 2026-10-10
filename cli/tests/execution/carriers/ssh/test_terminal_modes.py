"""Portable mode comparisons still reject lost settings and raw terminals."""

from __future__ import annotations

import os
import sys
from copy import deepcopy

import pytest

from tests.execution.carriers.ssh._terminal_modes import assert_preserved_terminal_mode

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Requires POSIX terminal modes")


@pytest.fixture
def canonical_mode() -> list:
    import termios

    master, slave = os.openpty()
    try:
        mode = termios.tcgetattr(slave)
    finally:
        os.close(slave)
        os.close(master)
    assert mode[3] & termios.ICANON
    mode[3] &= ~termios.PENDIN
    return mode


@pytest.mark.parametrize(
    "platform,canonical,accepted", [("darwin", True, True), ("linux", True, False), ("darwin", False, False)]
)
def test_only_darwin_canonical_pendin_addition_is_accepted(
    canonical_mode: list, monkeypatch: pytest.MonkeyPatch, platform: str, canonical: bool, accepted: bool
) -> None:
    import termios

    monkeypatch.setattr(sys, "platform", platform)
    if not canonical:
        canonical_mode[3] &= ~termios.ICANON
    actual = deepcopy(canonical_mode)
    actual[3] |= termios.PENDIN
    original = deepcopy(actual)
    if accepted:
        assert_preserved_terminal_mode(actual, canonical_mode)
    else:
        with pytest.raises(AssertionError):
            assert_preserved_terminal_mode(actual, canonical_mode)
    assert actual == original


@pytest.mark.parametrize("field", range(7))
def test_pendin_does_not_hide_other_mode_changes(
    canonical_mode: list, monkeypatch: pytest.MonkeyPatch, field: int
) -> None:
    import termios

    monkeypatch.setattr(sys, "platform", "darwin")
    actual = deepcopy(canonical_mode)
    actual[3] |= termios.PENDIN
    if field == 6:
        actual[6][termios.VINTR] = b"\x07" if actual[6][termios.VINTR] != b"\x07" else b"\x03"
    else:
        actual[field] ^= termios.ECHO if field == 3 else 1
    with pytest.raises(AssertionError):
        assert_preserved_terminal_mode(actual, canonical_mode)


def test_saved_pendin_cannot_be_removed(canonical_mode: list, monkeypatch: pytest.MonkeyPatch) -> None:
    import termios

    monkeypatch.setattr(sys, "platform", "darwin")
    actual = deepcopy(canonical_mode)
    canonical_mode[3] |= termios.PENDIN
    with pytest.raises(AssertionError):
        assert_preserved_terminal_mode(actual, canonical_mode)


def test_omitted_restoration_still_fails() -> None:
    import termios
    import tty

    master, slave = os.openpty()
    saved = termios.tcgetattr(slave)
    try:
        tty.setraw(slave, termios.TCSANOW)
        actual = termios.tcgetattr(slave)
        assert not actual[3] & (termios.ICANON | termios.ECHO | termios.ISIG)
        with pytest.raises(AssertionError):
            assert_preserved_terminal_mode(actual, saved)
    finally:
        termios.tcsetattr(slave, termios.TCSANOW, saved)
        try:
            assert_preserved_terminal_mode(termios.tcgetattr(slave), saved)
        finally:
            os.close(slave)
            os.close(master)
