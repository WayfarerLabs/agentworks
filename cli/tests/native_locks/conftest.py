"""Fixtures for the native lock primitive's tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def database(tmp_path: Path) -> Path:
    """A database path whose lock directory lives in this test's own directory."""
    return tmp_path / "state.db"
