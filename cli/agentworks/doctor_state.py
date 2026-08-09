"""Database-backed doctor groups using the ordinary read-only connection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.path_rendering import format_host_path

if TYPE_CHECKING:
    from agentworks.doctor import HealthGroup


def check_system() -> HealthGroup:
    """Report the install-level system slug without migrating state."""
    from agentworks.db import SYSTEM_SLUG_KEY, Database
    from agentworks.doctor import HealthGroup, MachineDiagnostic

    group = HealthGroup("System")
    try:
        db_exists, current, latest = Database.check_schema()
        if not db_exists:
            group.info("System slug", "unset (will ask at first vm create)")
            return group
        if current != latest:
            group.info(
                "System slug",
                "pending database migration (see the Database group)",
            )
            return group
        database = Database(read_only=True)
        try:
            slug = database.get_setting(SYSTEM_SLUG_KEY)
        finally:
            database.close()
        if slug:
            group.ok("System slug", slug)
        elif slug == "":
            group.info("System slug", "declined (asked at first vm create)")
        else:
            group.info("System slug", "unset (will ask at first vm create)")
    except Exception as error:
        group.warn(
            "System slug",
            f"could not check the database: {error}",
            machine_diagnostic=MachineDiagnostic.DATABASE_UNAVAILABLE,
        )
    return group


def check_database() -> HealthGroup:
    """Report schema and contents without migrating state."""
    from agentworks.db import Database
    from agentworks.doctor import HealthGroup, MachineDiagnostic

    group = HealthGroup("Database")
    try:
        exists, current, latest = Database.check_schema()
        if not exists:
            group.ok("Database", "does not exist yet (will be created on first use)")
        elif current == latest:
            group.ok("Schema", f"up to date (version {current})")
            database = Database(read_only=True)
            try:
                _report_contents(group, database)
            finally:
                database.close()
        elif current < latest:
            group.warn(
                "Schema",
                f"at version {current}, latest is {latest}; "
                "a normal Agentworks command that opens state will migrate it",
            )
        else:
            group.fail("Schema", f"version {current} is newer than latest {latest} (downgrade?)")
    except Exception as error:
        group.fail(
            "Database",
            str(error),
            machine_diagnostic=MachineDiagnostic.DATABASE_UNAVAILABLE,
        )
    return group


def _report_contents(group: HealthGroup, database: object) -> None:
    """Report stored counts and flag VMs in non-complete states."""
    from agentworks.db import Database, InitStatus
    from agentworks.ssh import LOG_DIR

    assert isinstance(database, Database)
    vms = database.list_vms()
    workspace_count = len(database.list_workspaces())
    group.ok("Contents", f"{len(vms)} VMs, {workspace_count} workspaces")

    def log_hint(vm_name: str) -> str:
        if not LOG_DIR.exists():
            return ""
        logs = sorted(LOG_DIR.glob(f"{vm_name}-*.log"), reverse=True)
        return f" Log: {format_host_path(logs[0])}" if logs else ""

    for vm in vms:
        if vm.init_status == InitStatus.FAILED.value:
            group.warn(f"VM '{vm.name}'", f"failed state (only delete supported).{log_hint(vm.name)}")
        elif vm.init_status == InitStatus.PARTIAL.value:
            group.warn(f"VM '{vm.name}'", f"initialized with warnings.{log_hint(vm.name)}")
