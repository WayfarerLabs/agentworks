"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from agentworks.db import Database


@pytest.fixture
def db(tmp_path: Path) -> Generator[Database, None, None]:
    """Provide a fresh database for each test, closed automatically."""
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture
def warnings() -> Generator[list[str], None, None]:
    """Capture warnings emitted via ``agentworks.output.warn``.

    Installs a list-based handler before the test and restores the default
    after. Usage::

        def test_something(warnings):
            do_something_that_warns()
            assert any("expected text" in w for w in warnings)
    """
    from agentworks.output import _default_warn, set_warn_handler

    captured: list[str] = []
    set_warn_handler(captured.append)
    yield captured
    set_warn_handler(_default_warn)
