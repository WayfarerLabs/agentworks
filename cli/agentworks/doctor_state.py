"""Database-backed doctor groups using the ordinary read-only connection."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from agentworks.path_rendering import format_host_path

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.db import Database
    from agentworks.doctor import HealthGroup
    from agentworks.vms.sites import VMSiteDecl


@contextmanager
def _current_database() -> Iterator[tuple[bool, int, int, Database | None]]:
    """Open current state read-only, or report its absent/stale version."""
    from agentworks.db import Database

    exists, current, latest = Database.check_schema()
    database = Database(read_only=True) if exists and current == latest else None
    try:
        yield exists, current, latest, database
    finally:
        if database is not None:
            database.close()


def check_system() -> HealthGroup:
    """Report the install-level system slug without migrating state."""
    from agentworks.db import SYSTEM_SLUG_KEY
    from agentworks.doctor import HealthGroup, MachineDiagnostic

    group = HealthGroup("System")
    try:
        with _current_database() as (db_exists, current, latest, database):
            if not db_exists:
                group.info("System slug", "unset (will ask at first vm create)")
                return group
            if current != latest:
                group.info(
                    "System slug",
                    "pending database migration (see the Database group)",
                )
                return group
            assert database is not None
            slug = database.get_setting(SYSTEM_SLUG_KEY)
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
    from agentworks.doctor import HealthGroup, MachineDiagnostic

    group = HealthGroup("Database")
    try:
        with _current_database() as (exists, current, latest, database):
            if not exists:
                group.ok("Database", "does not exist yet (will be created on first use)")
            elif current == latest:
                group.ok("Schema", f"up to date (version {current})")
                assert database is not None
                _report_contents(group, database)
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
        elif vm.init_status not in (InitStatus.COMPLETE.value, InitStatus.PENDING.value):
            group.warn(f"VM '{vm.name}'", "unexpected initialization state")


def append_vm_site_database_checks(
    group: HealthGroup,
    *,
    sites: dict[str, VMSiteDecl],
    not_ready: dict[str, str],
) -> None:
    """Append stored VM-to-site checks without migrating state."""
    from agentworks.doctor import MachineDiagnostic
    from agentworks.vms.sites import site_manifest_hint

    try:
        with _current_database() as (exists, current, latest, database):
            if not exists:
                return
            if current != latest:
                group.info(
                    "VM sites",
                    "pending database migration (see the Database group); "
                    "re-run doctor after migrating for the full report",
                )
                return
            assert database is not None
            for vm in database.list_vms():
                if vm.site in not_ready:
                    group.warn(
                        f"VM '{vm.name}'",
                        f"site '{vm.site}' is not ready: {not_ready[vm.site]}",
                    )
                elif vm.site not in sites:
                    group.fail(
                        f"VM '{vm.name}'",
                        f"site '{vm.site}' is not declared",
                        hint=site_manifest_hint(vm.site),
                    )
    except Exception as error:
        group.warn(
            "VM sites",
            f"could not check the database: {error}",
            machine_diagnostic=MachineDiagnostic.DATABASE_UNAVAILABLE,
        )
