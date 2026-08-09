"""One report-scoped database fact collection and its doctor projections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.db import VMRow
    from agentworks.doctor import HealthGroup


@dataclass(frozen=True)
class DoctorDatabaseFacts:
    """Database facts collected from one verified inspection snapshot."""

    exists: bool
    current: int
    latest: int
    system_slug: str | None = None
    vms: tuple[VMRow, ...] = ()
    workspace_count: int = 0
    inspection_unavailable: bool = False
    error: Exception | None = None


def collect_database_facts() -> DoctorDatabaseFacts:
    """Read every database-backed doctor fact from one source generation."""
    from agentworks.db import SYSTEM_SLUG_KEY, Database
    from agentworks.errors import DatabaseInspectionUnavailable

    try:
        with Database.inspection_snapshot() as (exists, current, latest, database):
            if not exists or current != latest:
                return DoctorDatabaseFacts(exists=exists, current=current, latest=latest)
            assert database is not None
            return DoctorDatabaseFacts(
                exists=True,
                current=current,
                latest=latest,
                system_slug=database.get_setting(SYSTEM_SLUG_KEY),
                vms=tuple(database.list_vms()),
                workspace_count=len(database.list_workspaces()),
            )
    except DatabaseInspectionUnavailable:
        return DoctorDatabaseFacts(
            exists=False,
            current=0,
            latest=0,
            inspection_unavailable=True,
        )
    except Exception as error:
        return DoctorDatabaseFacts(exists=True, current=0, latest=0, error=error)


def check_system(facts: DoctorDatabaseFacts) -> HealthGroup:
    """Project the install-level system slug from shared database facts."""
    from agentworks.doctor import HealthGroup, MachineDiagnostic
    from agentworks.errors import DatabaseInspectionUnavailable

    group = HealthGroup("System")
    if facts.inspection_unavailable:
        group.unavailable(
            "System slug",
            DatabaseInspectionUnavailable.MESSAGE,
            machine_diagnostic=MachineDiagnostic.DATABASE_INSPECTION_UNAVAILABLE,
        )
    elif facts.error is not None:
        group.warn(
            "System slug",
            f"could not check the database: {facts.error}",
            machine_diagnostic=MachineDiagnostic.DATABASE_UNAVAILABLE,
        )
    elif not facts.exists:
        group.info("System slug", "unset (will ask at first vm create)")
    elif facts.current != facts.latest:
        group.info("System slug", "pending database migration (see the Database group)")
    elif facts.system_slug:
        group.ok("System slug", facts.system_slug)
    elif facts.system_slug == "":
        group.info("System slug", "declined (asked at first vm create)")
    else:
        group.info("System slug", "unset (will ask at first vm create)")
    return group


def append_vm_site_checks(
    group: HealthGroup,
    facts: DoctorDatabaseFacts,
    *,
    sites: Mapping[str, object],
    not_ready: Mapping[str, str],
) -> None:
    """Project stored VM-to-site consistency from shared database facts."""
    from agentworks.doctor import MachineDiagnostic
    from agentworks.errors import DatabaseInspectionUnavailable
    from agentworks.vms.sites import site_manifest_hint

    if facts.inspection_unavailable:
        group.unavailable(
            "VM sites",
            DatabaseInspectionUnavailable.MESSAGE,
            machine_diagnostic=MachineDiagnostic.DATABASE_INSPECTION_UNAVAILABLE,
        )
        return
    if facts.error is not None:
        group.warn(
            "VM sites",
            f"could not check the database: {facts.error}",
            machine_diagnostic=MachineDiagnostic.DATABASE_UNAVAILABLE,
        )
        return
    if not facts.exists:
        return
    if facts.current != facts.latest:
        group.info(
            "VM sites",
            "pending database migration (see the Database group); re-run doctor after migrating for the full report",
        )
        return
    for vm in facts.vms:
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


def check_database(facts: DoctorDatabaseFacts) -> HealthGroup:
    """Project schema and contents checks from shared database facts."""
    from agentworks.doctor import HealthGroup, MachineDiagnostic
    from agentworks.errors import DatabaseInspectionUnavailable

    group = HealthGroup("Database")
    if facts.inspection_unavailable:
        group.unavailable(
            "Database",
            DatabaseInspectionUnavailable.MESSAGE,
            machine_diagnostic=MachineDiagnostic.DATABASE_INSPECTION_UNAVAILABLE,
        )
    elif facts.error is not None:
        group.fail(
            "Database",
            str(facts.error),
            machine_diagnostic=MachineDiagnostic.DATABASE_UNAVAILABLE,
        )
    elif not facts.exists:
        group.ok("Database", "does not exist yet (will be created on first use)")
    elif facts.current == facts.latest:
        group.ok("Schema", f"up to date (version {facts.current})")
        _report_db_contents(group, facts)
    elif facts.current < facts.latest:
        group.warn(
            "Schema",
            f"at version {facts.current}, latest is {facts.latest}; "
            "a normal Agentworks command that opens state will migrate it",
        )
    else:
        group.fail(
            "Schema",
            f"version {facts.current} is newer than latest {facts.latest} (downgrade?)",
        )
    return group


def _report_db_contents(group: HealthGroup, facts: DoctorDatabaseFacts) -> None:
    """Report snapshot contents and flag VMs in non-complete states."""
    from agentworks.db import InitStatus
    from agentworks.path_rendering import format_host_path
    from agentworks.ssh import LOG_DIR

    group.ok("Contents", f"{len(facts.vms)} VMs, {facts.workspace_count} workspaces")

    def log_hint(vm_name: str) -> str:
        if not LOG_DIR.exists():
            return ""
        logs = sorted(LOG_DIR.glob(f"{vm_name}-*.log"), reverse=True)
        return f" Log: {format_host_path(logs[0])}" if logs else ""

    for vm in facts.vms:
        if vm.init_status == InitStatus.FAILED.value:
            group.warn(f"VM '{vm.name}'", f"failed state (only delete supported).{log_hint(vm.name)}")
        elif vm.init_status == InitStatus.PARTIAL.value:
            group.warn(f"VM '{vm.name}'", f"initialized with warnings.{log_hint(vm.name)}")
        elif vm.init_status not in (InitStatus.COMPLETE.value, InitStatus.PENDING.value):
            group.warn(f"VM '{vm.name}'", f"unexpected init status: {vm.init_status}")
