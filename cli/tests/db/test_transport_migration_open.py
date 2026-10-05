"""Canonical transport migration versions remain openable."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database, SchemaState, inspect_schema
from tests.database_support import build_schema

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.windows


@pytest.mark.parametrize("version", (39, 40, 41))
def test_canonical_transport_schemas_remain_openable(tmp_path: Path, version: int) -> None:
    path = tmp_path / "state.db"
    build_schema(path, version)

    expected = SchemaState.CURRENT if version == 41 else SchemaState.STALE
    assert inspect_schema(path).state is expected
    database = Database(path)
    try:
        assert database._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 41  # noqa: SLF001
    finally:
        database.close()
