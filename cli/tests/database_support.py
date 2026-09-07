"""Initial database state for backup and migration tests."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from agentworks.db import MIGRATIONS, MigrationContext


def build_schema(path: Path, target_version: int) -> None:
    """Build disposable initial state; close before opening any connection under test."""
    with closing(sqlite3.connect(path)) as connection:
        # Durability is unnecessary during setup; both settings end with this connection.
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute("PRAGMA journal_mode = MEMORY")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "CREATE TABLE schema_version ("
            "version INTEGER NOT NULL, "
            "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))"
        )
        context = MigrationContext()
        for version in range(1, target_version + 1):
            step = MIGRATIONS[version]
            if callable(step):
                step(connection, context)
            else:
                for statement in step.split(";"):
                    if statement.strip():
                        connection.execute(statement)
            connection.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            connection.commit()
