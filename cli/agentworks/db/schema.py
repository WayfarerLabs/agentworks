"""Small, non-migrating checks for the scalar database schema version."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from agentworks.db.migrations import LATEST_VERSION
from agentworks.errors import StateError

if TYPE_CHECKING:
    from pathlib import Path


def read_schema_version(connection: sqlite3.Connection) -> int:
    """Read and validate the current scalar schema version."""
    entry = connection.execute("SELECT type FROM sqlite_master WHERE name = 'schema_version'").fetchone()
    if entry is None:
        return 0
    if entry[0] != "table":
        raise StateError(
            "state database schema is unavailable or malformed",
            hint="Restore the state database from a known-good backup.",
        )
    row = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0]
    if current is None:
        return 0
    if type(current) is not int or current < 0:
        raise StateError(
            "state database schema version is invalid",
            hint="Restore the state database from a known-good backup.",
        )
    return current


def check_schema(path: Path) -> tuple[bool, int, int]:
    """Return existence and scalar version facts without migrating state."""
    if not path.exists():
        return (False, 0, LATEST_VERSION)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(str(path))
        current = read_schema_version(connection)
    except sqlite3.DatabaseError as error:
        raise StateError(
            "state database schema is unavailable or malformed",
            hint="Restore the state database from a known-good backup.",
        ) from error
    finally:
        if connection is not None:
            connection.close()
    return (True, current, LATEST_VERSION)
