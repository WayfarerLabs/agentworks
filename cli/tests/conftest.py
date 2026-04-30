"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agentworks.db import Database


@pytest.fixture
def db(tmp_path: Path) -> Generator[Database, None, None]:
    """Provide a fresh database for each test, closed automatically."""
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


# ---------------------------------------------------------------------------
# Output capturing
# ---------------------------------------------------------------------------


@dataclass
class _CapturedProgress:
    label: str
    updates: list[tuple[int | None, str | None]] = field(default_factory=list)
    completed: bool = False
    done_message: str | None = None

    def update(self, current: int | None = None, message: str | None = None) -> None:
        self.updates.append((current, message))

    def done(self, message: str | None = None) -> None:
        self.completed = True
        self.done_message = message


@dataclass
class CapturedOutput:
    """All output captured during a test."""

    info: list[str] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    progress_items: list[_CapturedProgress] = field(default_factory=list)


class _TestHandler:
    def __init__(self, captured: CapturedOutput) -> None:
        self._captured = captured

    def info(self, message: str) -> None:
        self._captured.info.append(message)

    def detail(self, message: str) -> None:
        self._captured.detail.append(message)

    def warn(self, message: str) -> None:
        self._captured.warnings.append(message)

    def progress(self, label: str, total: int | None = None) -> _CapturedProgress:
        p = _CapturedProgress(label=label)
        self._captured.progress_items.append(p)
        return p


@pytest.fixture
def captured_output() -> Generator[CapturedOutput, None, None]:
    """Capture all output emitted via agentworks.output.

    Usage::

        def test_something(captured_output):
            do_something()
            assert any("expected" in m for m in captured_output.info)
            assert len(captured_output.warnings) == 0
    """
    from agentworks.output import get_handler, set_handler

    previous = get_handler()
    captured = CapturedOutput()
    set_handler(_TestHandler(captured))
    yield captured
    set_handler(previous)


@pytest.fixture
def warnings() -> Generator[list[str], None, None]:
    """Capture warnings emitted via ``agentworks.output.warn``.

    Convenience wrapper for tests that only care about warnings.
    Installs a test handler and yields just the warnings list.
    """
    from agentworks.output import get_handler, set_handler

    previous = get_handler()
    captured = CapturedOutput()
    set_handler(_TestHandler(captured))
    yield captured.warnings
    set_handler(previous)
