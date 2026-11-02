"""Request helpers whose behavior depends on a real shell."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from agentworks.execution.requests import shell_command_line


@pytest.mark.skipif(shutil.which("zsh") is None, reason="needs zsh")
def test_a_word_starting_with_equals_reaches_zsh_literally() -> None:
    # zsh expands a bare ``=ls`` to the path of ls, whatever the runner's own login shell is.
    zsh = shutil.which("zsh")
    assert zsh is not None
    line = shell_command_line(["printf", "%s", "=ls"])

    out = subprocess.run([zsh, "-f", "-c", line], capture_output=True, check=True, timeout=10).stdout

    assert out == b"=ls"
