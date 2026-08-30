"""Database-backed doctor groups using the ordinary read-only connection."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from agentworks.path_rendering import format_host_path

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.config import Config
    from agentworks.db import (
        Database,
        InspectedAppliedStateSlice,
        InspectedDesiredOverlay,
        InstanceRecordMetadata,
        InstanceStateInspection,
        VMRow,
    )
    from agentworks.doctor import HealthGroup, InstanceStateHealthFactType
    from agentworks.ssh_identity import SSHIdentity
    from agentworks.vms.applied_state import SSHAppliedState
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
    from agentworks.doctor import HealthGroup

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
        group.warn("System slug", f"could not check the database: {error}", hint=getattr(error, "hint", None))
    return group


def check_database(config: Config | None = None) -> HealthGroup:
    """Report schema and contents without migrating state."""
    from agentworks.doctor import HealthGroup

    group = HealthGroup("Database")
    try:
        with _current_database() as (exists, current, latest, database):
            if not exists:
                group.ok("Database", "does not exist yet (will be created on first use)")
            elif current == latest:
                group.ok("Schema", f"up to date (version {current})")
                assert database is not None
                with database.snapshot():
                    vms = database.list_vms()
                    counts = _live_resource_counts(database, vms)
                    inspection = database.instance_state.inspect_all_instance_state()
                _report_contents(group, database, vms=vms, counts=counts)
                _report_instance_state(group, config, vms, inspection)
            elif current < latest:
                group.warn(
                    "Schema",
                    f"at version {current}, latest is {latest}; "
                    "a normal Agentworks command will announce migration and offer or automatically "
                    "create a backup first",
                )
            else:
                group.fail(
                    "Schema",
                    f"version {current} is newer than latest {latest} (downgrade?)",
                    hint="Back it up, restore a compatible backup before downgrading, or use a newer release.",
                )
    except Exception as error:
        group.fail("Database", str(error), hint=getattr(error, "hint", None))
    return group


def _live_resource_counts(database: Database, vms: list[VMRow]) -> dict[str, int]:
    return {
        "vm": len(vms),
        "workspace": len(database.list_workspaces()),
        "agent": len(database.list_agents()),
        "session": database.count_sessions(),
        "console": len(database.list_consoles()),
    }


def _report_contents(
    group: HealthGroup,
    database: object,
    *,
    vms: list[VMRow] | None = None,
    counts: dict[str, int] | None = None,
) -> None:
    """Report stored counts and flag VMs in non-complete states."""
    from agentworks.db import Database, InitStatus
    from agentworks.ssh import LOG_DIR

    assert isinstance(database, Database)
    if vms is None:
        vms = database.list_vms()
    if counts is None:
        counts = _live_resource_counts(database, vms)
    group.ok(
        "Contents",
        f"{counts['vm']} VMs, {counts['workspace']} workspaces, {counts['agent']} agents, "
        f"{counts['session']} sessions, {counts['console']} consoles",
    )

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


def _report_instance_state(
    group: HealthGroup,
    config: Config | None,
    vms: list[VMRow],
    inspection: InstanceStateInspection,
) -> None:
    """Append value-free integrity and SSH comparison checks from one fleet read."""
    from agentworks.db import AppliedStateKey, InstanceRecordMetadata
    from agentworks.doctor import (
        HealthCheck,
        InstanceStateHealthFact,
        InstanceStateHealthFactType,
        Status,
    )
    from agentworks.errors import StateError
    from agentworks.instance_specs import UnsupportedStoredOverlayError, decode_stored_overlay
    from agentworks.ssh_identity import SSHIdentityReadError, read_private_ssh_identity
    from agentworks.vms.applied_state import (
        UnsupportedAppliedStateVersionError,
        UnverifiableSSHAppliedState,
        VerifiedSSHAppliedState,
        VMSSHIdentityState,
        compare_vm_ssh_identity_evidence,
        decode_hardware_provenance,
        decode_ssh_identity,
    )

    def add(
        status: Status,
        name: str,
        message: str,
        fact_type: InstanceStateHealthFactType,
        metadata: InstanceRecordMetadata,
        *,
        comparison: str | None = None,
        hint: str | None = None,
    ) -> None:
        group.checks.append(
            HealthCheck(
                name,
                status,
                message,
                hint=hint,
                instance_state=InstanceStateHealthFact(
                    fact_type=fact_type,
                    instance_kind=metadata.instance_kind,
                    instance_name=metadata.instance_name,
                    record_type=metadata.record_type,
                    record_key=metadata.record_key,
                    payload_version=metadata.payload_version,
                    recorded_at=metadata.recorded_at,
                    comparison=comparison,
                    owner_exists=metadata.owner_exists,
                ),
            )
        )

    def desired_metadata(item: InspectedDesiredOverlay) -> InstanceRecordMetadata:
        return item.metadata

    def applied_metadata(item: InspectedAppliedStateSlice) -> InstanceRecordMetadata:
        return item.metadata

    def label(metadata: InstanceRecordMetadata) -> str:
        owner = (
            f"{metadata.instance_kind} {metadata.instance_name!r}"
            if metadata.instance_kind is not None and metadata.instance_name is not None
            else "instance state"
        )
        record = metadata.record_key or metadata.record_type or "record"
        return f"{owner} {record}"

    metadata_rows = [
        *(desired_metadata(item) for item in inspection.desired_overlays),
        *(applied_metadata(item) for item in inspection.applied_slices),
        *(item.metadata for item in inspection.unconsumed_records),
        *(item.metadata for item in inspection.malformed_records),
    ]
    for metadata in metadata_rows:
        if not metadata.owner_exists:
            add(
                Status.FAIL,
                label(metadata),
                "record has no database owner",
                InstanceStateHealthFactType.ORPHAN_RECORD,
                metadata,
                hint="Back up the state database before repairing or removing the orphan record.",
            )

    for malformed_record in inspection.malformed_records:
        add(
            Status.FAIL,
            label(malformed_record.metadata),
            f"record is malformed ({malformed_record.diagnostic.value})",
            InstanceStateHealthFactType.MALFORMED_RECORD,
            malformed_record.metadata,
            hint="Back up the state database before repairing it, or restore a known-good backup.",
        )

    for unconsumed_record in inspection.unconsumed_records:
        add(
            Status.INFO,
            label(unconsumed_record.metadata),
            "record is valid but not understood by this Agentworks release",
            InstanceStateHealthFactType.UNCONSUMED_RECORD,
            unconsumed_record.metadata,
            hint="Use a compatible or newer Agentworks release to inspect this record.",
        )

    for desired_record in inspection.desired_overlays:
        metadata = desired_metadata(desired_record)
        try:
            decode_stored_overlay(desired_record.record)
        except UnsupportedStoredOverlayError:
            add(
                Status.INFO,
                label(metadata),
                "record is not understood by this Agentworks release",
                InstanceStateHealthFactType.UNCONSUMED_RECORD,
                metadata,
                hint="Use a compatible or newer Agentworks release to inspect this record.",
            )
        except StateError:
            add(
                Status.FAIL,
                label(metadata),
                "record payload is malformed",
                InstanceStateHealthFactType.MALFORMED_RECORD,
                metadata,
                hint="Back up the state database before repairing it, or restore a known-good backup.",
            )

    ssh_record_names = {
        item.record.instance_name
        for item in inspection.applied_slices
        if item.record.key is AppliedStateKey.SSH_IDENTITY and item.owner_exists
    }
    ssh_record_names.update(
        item.metadata.instance_name
        for item in inspection.malformed_records
        if item.metadata.instance_kind == "vm"
        and item.metadata.record_type == "applied-state"
        and item.metadata.record_key == AppliedStateKey.SSH_IDENTITY.value
        and item.metadata.owner_exists
        and item.metadata.instance_name is not None
    )
    ssh_evidence: dict[str, tuple[SSHAppliedState, InstanceRecordMetadata]] = {}
    for applied_record in inspection.applied_slices:
        metadata = applied_metadata(applied_record)
        try:
            if applied_record.record.key is AppliedStateKey.HARDWARE_PROVENANCE:
                decode_hardware_provenance(applied_record.record)
            elif applied_record.record.key is AppliedStateKey.SSH_IDENTITY:
                decoded = decode_ssh_identity(applied_record.record)
                if applied_record.owner_exists:
                    ssh_evidence[applied_record.record.instance_name] = (decoded, metadata)
        except UnsupportedAppliedStateVersionError:
            add(
                Status.INFO,
                label(metadata),
                "record uses an unsupported payload version",
                InstanceStateHealthFactType.UNCONSUMED_RECORD,
                metadata,
                hint="Use a compatible or newer Agentworks release to inspect this record.",
            )
        except StateError:
            add(
                Status.FAIL,
                label(metadata),
                "record payload is malformed",
                InstanceStateHealthFactType.MALFORMED_RECORD,
                metadata,
                hint="Back up the state database before repairing it, or restore a known-good backup.",
            )

    current_identity: SSHIdentity | None = None
    identity_error: SSHIdentityReadError | None = None
    needs_current_identity = any(
        isinstance(applied, VerifiedSSHAppliedState) for applied, _metadata in ssh_evidence.values()
    )
    if needs_current_identity and config is not None:
        try:
            current_identity = read_private_ssh_identity(config.operator.ssh_private_key)
        except SSHIdentityReadError as error:
            identity_error = error
    if needs_current_identity and current_identity is None:
        if config is None:
            detail = "configuration is unavailable"
        else:
            assert identity_error is not None
            detail = f"configured identity is {identity_error.kind}"
        group.checks.append(
            HealthCheck(
                "VM SSH identity comparisons",
                Status.WARN,
                f"not checked because {detail}",
                hint=None if identity_error is None else identity_error.detail,
                instance_state=InstanceStateHealthFact(
                    fact_type=InstanceStateHealthFactType.COVERAGE,
                    record_type="applied-state",
                    record_key="ssh-identity",
                ),
            )
        )

    by_name = {vm.name: vm for vm in vms}
    for vm_name in sorted(by_name):
        try:
            missing_metadata = InstanceRecordMetadata(
                "vm",
                vm_name,
                "applied-state",
                "ssh-identity",
                None,
                None,
                True,
            )
        except (TypeError, ValueError):
            add(
                Status.FAIL,
                "VM SSH identity",
                "VM owner identity is malformed",
                InstanceStateHealthFactType.MALFORMED_RECORD,
                InstanceRecordMetadata("vm", None, None, None, None, None, True),
                hint="Back up the state database before repairing it, or restore a known-good backup.",
            )
            continue
        evidence = ssh_evidence.get(vm_name)
        if evidence is None:
            if vm_name in ssh_record_names:
                continue
            add(
                Status.WARN,
                f"VM '{vm_name}' SSH identity",
                "not recorded",
                InstanceStateHealthFactType.APPLIED_COMPARISON,
                missing_metadata,
                comparison=VMSSHIdentityState.NOT_RECORDED.value,
            )
            continue
        applied, metadata = evidence
        if isinstance(applied, UnverifiableSSHAppliedState):
            add(
                Status.INFO,
                f"VM '{vm_name}' SSH identity",
                "recorded identity is unverifiable",
                InstanceStateHealthFactType.APPLIED_COMPARISON,
                metadata,
                comparison=VMSSHIdentityState.UNVERIFIABLE.value,
            )
            continue
        if current_identity is None:
            continue
        comparison = compare_vm_ssh_identity_evidence(vm_name, current_identity, applied)
        disposition = {
            VMSSHIdentityState.MATCH: (Status.OK, "matches recorded identity"),
            VMSSHIdentityState.UNVERIFIABLE: (Status.INFO, "recorded identity is unverifiable"),
            VMSSHIdentityState.DRIFT: (Status.FAIL, "does not match recorded identity"),
            VMSSHIdentityState.NOT_RECORDED: (Status.WARN, "not recorded"),
        }[comparison.state]
        add(
            disposition[0],
            f"VM '{vm_name}' SSH identity",
            disposition[1],
            InstanceStateHealthFactType.APPLIED_COMPARISON,
            metadata,
            comparison=comparison.state.value,
        )


def append_vm_site_database_checks(
    group: HealthGroup,
    *,
    sites: dict[str, VMSiteDecl],
    not_ready: dict[str, str],
) -> None:
    """Append stored VM-to-site checks without migrating state."""
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
        group.warn("VM sites", f"could not check the database: {error}", hint=getattr(error, "hint", None))
