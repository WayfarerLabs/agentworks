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
    MIGRATION_LOCK_NAME,
    MIGRATION_LOCK_TIMEOUT_SECONDS,
    AutomaticBackupResult,
    DatabaseOpenPlan,
    RetentionCleanupFailure,
    SafeOpenResult,
    SchemaInspection,
    SchemaState,
    backup_directory,
    create_manual_backup,
    create_pre_migration_backup,
    inspect_schema,
    open_completion_database,
    open_database_safely,
    prepare_database_open,
    render_restore_command,
    restore_backup,
    validate_restore_source,
)
from agentworks.db.converters import _parse_shells
from agentworks.db.database import Database, DatabaseDriverError
from agentworks.db.instance_state import (
    AppliedStateSlice,
    DesiredOverlayRecord,
    InstanceKind,
    VersionedPayload,
)
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
    "MIGRATION_LOCK_NAME",
    "MIGRATION_LOCK_TIMEOUT_SECONDS",
    "LATEST_VERSION",
    "MIGRATIONS",
    "SCHEMA_SENTINELS",
    "PID_STOPPED",
    "SYSTEM_SLUG_KEY",
    "AgentGrantRow",
    "AgentRow",
    "AppliedStateSlice",
    "AutomaticBackupResult",
    "ConsoleRow",
    "ConsoleSessionRow",
    "Database",
    "DatabaseOpenPlan",
    "DatabaseDriverError",
    "DesiredOverlayRecord",
    "InitStatus",
    "InstanceKind",
    "MigrationContext",
    "ProvisioningStatus",
    "RetentionCleanupFailure",
    "SafeOpenResult",
    "SchemaInspection",
    "SchemaState",
    "SessionMode",
    "SessionRow",
    "SessionStatus",
    "ShellEntry",
    "VMEventRow",
    "VMRow",
    "VMStatus",
    "VersionedPayload",
    "WorkspaceRow",
    "_load_legacy_toml",
    "_parse_shells",
    "backup_directory",
    "create_manual_backup",
    "create_pre_migration_backup",
    "inspect_schema",
    "open_completion_database",
    "open_database_safely",
    "prepare_database_open",
    "render_restore_command",
    "restore_backup",
    "validate_restore_source",
]
