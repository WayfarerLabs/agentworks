"""Cross-platform import check for terminal candidate modules."""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.windows


def test_guest_module_import_defers_posix_terminal_modules() -> None:
    script = r"""
import importlib.abc
import sys

class BlockTerminalModules(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"termios", "tty"}:
            raise ImportError("POSIX terminal module is unavailable")

sys.meta_path.insert(0, BlockTerminalModules())
import agentworks.execution._terminal_guest
assert "termios" not in sys.modules
assert "tty" not in sys.modules
"""
    result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, timeout=20)

    assert result.returncode == 0, result.stderr.decode(errors="replace")
