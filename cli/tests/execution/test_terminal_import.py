"""Cross-platform import check for terminal candidate modules."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from agentworks.execution import _terminal_guest as guest
from agentworks.execution._terminal_handoff import prepare_terminal_handoff

pytestmark = pytest.mark.windows


def test_guest_module_import_defers_posix_terminal_modules() -> None:
    script = r"""
import sys

sys.modules["termios"] = None
sys.modules["tty"] = None
import agentworks.execution._terminal_guest
assert sys.modules["termios"] is None
assert sys.modules["tty"] is None
"""
    result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, timeout=20)

    assert result.returncode == 0, result.stderr.decode(errors="replace")


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_host_preparation_is_platform_neutral_and_has_no_terminal_or_dispatch_effects(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    writes: list[bytes] = []

    class Sink:
        @staticmethod
        def try_write(data: memoryview) -> int:
            writes.append(bytes(data))
            return len(data)

    def unexpected_guest_run(_nonce: str) -> int:
        raise AssertionError("host preparation must not run the guest")

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.delattr(os, "memfd_create", raising=False)
    monkeypatch.setattr(guest, "run", unexpected_guest_run)

    prepared = prepare_terminal_handoff((b"/bin/true", b""), {}, b"source", Sink())

    assert prepared.bootstrap.try_read(1) is None
    assert not prepared.handed_off
    assert prepared.failure is None
    assert writes == []
