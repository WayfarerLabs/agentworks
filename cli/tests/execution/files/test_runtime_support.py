"""Portable synthetic runtime-selection checks for file-exchange tests."""

from __future__ import annotations

import importlib
import sys

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._runtime_prerequisite import RuntimeTargetOS
from tests.execution.files import _runtime_support


def test_synthetic_selection_is_posix_when_workstation_interpreter_is_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = sys.executable
    monkeypatch.setattr(sys, "executable", r"C:\Python313\python.exe")
    support = importlib.reload(_runtime_support)
    try:
        selection = support.runtime_selection()

        assert selection.target_os is RuntimeTargetOS.LINUX
        assert selection.explicit_path == "/usr/bin/python3"
        with pytest.raises(ValidationError):
            support.runtime_selection(sys.executable)
    finally:
        monkeypatch.setattr(sys, "executable", original)
        importlib.reload(_runtime_support)
