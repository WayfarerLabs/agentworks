"""SQLite state database for Agentworks.

Database lives at ~/.config/agentworks/agentworks.db. Created automatically on
first use. Schema migrations are forward-only via a version table.

The package is split by concern: ``models`` (enums, row dataclasses, the
``ShellEntry`` TypedDict), ``migrations`` (the forward-only migration
ladder), ``converters`` (sqlite3.Row -> row-dataclass conversion and the
query-building helpers), and ``database`` (the ``Database`` class itself).
This module re-exports the full public surface so ``agentworks.db`` stays
the one import path callers use.
"""

from __future__ import annotations

from agentworks.config import CONFIG_DIR
from agentworks.db.converters import _parse_shells
from agentworks.db.database import Database
from agentworks.db.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    MigrationContext,
    _load_legacy_toml,
)
from agentworks.db.models import (
    PID_STOPPED,
    SYSTEM_SLUG_KEY,
    AgentGrantRow,
    AgentRow,
    ConsoleRow,
    ConsoleSessionRow,
    InitStatus,
    ProvisioningStatus,
    SessionMode,
    SessionRow,
    SessionStatus,
    ShellEntry,
    VMEventRow,
    VMRow,
    VMStatus,
    WorkspaceRow,
)

DB_PATH = CONFIG_DIR / "agentworks.db"

__all__ = [
    "DB_PATH",
    "LATEST_VERSION",
    "MIGRATIONS",
    "PID_STOPPED",
    "SYSTEM_SLUG_KEY",
    "AgentGrantRow",
    "AgentRow",
    "ConsoleRow",
    "ConsoleSessionRow",
    "Database",
    "InitStatus",
    "MigrationContext",
    "ProvisioningStatus",
    "SessionMode",
    "SessionRow",
    "SessionStatus",
    "ShellEntry",
    "VMEventRow",
    "VMRow",
    "VMStatus",
    "WorkspaceRow",
    "_load_legacy_toml",
    "_parse_shells",
]
