"""Compare owned terminal snapshots without hiding unrelated mode changes."""

from __future__ import annotations

from inspect import getsource


def assert_preserved_terminal_mode(actual: list, expected: list) -> None:
    """Allow only Darwin's added PENDIN when restoring or copying canonical modes."""
    import sys
    import termios

    compared = actual
    # Darwin can mark queued input for reprocessing on a canonical tcsetattr.
    # Preserve every other field, and never forgive removal of a saved PENDIN.
    if sys.platform == "darwin" and expected[3] & termios.ICANON and actual[3] == expected[3] | termios.PENDIN:
        compared = actual.copy()
        compared[3] = expected[3]
    assert compared == expected, (actual, expected)


def with_terminal_mode_assertion(code: str) -> str:
    """Give owned child probes the same comparison, including isolated Python."""
    return getsource(assert_preserved_terminal_mode) + "\n" + code
