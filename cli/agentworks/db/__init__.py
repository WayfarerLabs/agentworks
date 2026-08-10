"""SQLite state database for Agentworks.

Database lives at ~/.config/agentworks/agentworks.db. Created automatically on
first use. Schema migrations are forward-only via a version table.

The package is split by concern: ``models`` (enums, row dataclasses, the
``ShellEntry`` TypedDict), ``migrations`` (the forward-only migration
ladder), ``backup`` (direct SQLite backup and restore), ``converters``
(sqlite3.Row -> row-dataclass conversion and the query-building helpers),
and ``database`` (the ``Database`` class itself). This module re-exports the
full public surface so ``agentworks.db`` stays the one import path callers
use.
"""

from __future__ import annotations

from agentworks.config import CONFIG_DIR
from agentworks.db.backup import (
    AUTOMATIC_BACKUP_LIMIT,
    BACKUP_DEADLINE_SECONDS,
    AutomaticBackupResult,
    RetentionCleanupFailure,
    backup_directory,
    create_manual_backup,
    create_pre_migration_backup,
    restore_backup,
    validate_restore_source,
)
from agentworks.db.converters import _parse_shells
from agentworks.db.database import Database, DatabaseDriverError
from agentworks.db.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    SCHEMA_SENTINELS,
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
    "AUTOMATIC_BACKUP_LIMIT",
    "BACKUP_DEADLINE_SECONDS",
    "LATEST_VERSION",
    "MIGRATIONS",
    "SCHEMA_SENTINELS",
    "PID_STOPPED",
    "SYSTEM_SLUG_KEY",
    "AgentGrantRow",
    "AgentRow",
    "AutomaticBackupResult",
    "ConsoleRow",
    "ConsoleSessionRow",
    "Database",
    "DatabaseDriverError",
    "InitStatus",
    "MigrationContext",
    "ProvisioningStatus",
    "RetentionCleanupFailure",
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
    "backup_directory",
    "create_manual_backup",
    "create_pre_migration_backup",
    "restore_backup",
    "validate_restore_source",
]
