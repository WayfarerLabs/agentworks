"""Service boundary for the durable adjacent Debian VM upgrade."""

from __future__ import annotations

import contextlib
import importlib
import json
import shlex
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, cast

from agentworks import output
from agentworks.db import PID_STOPPED, SessionStatus
from agentworks.errors import StateError, UserAbort, ValidationError
from agentworks.path_rendering import format_host_path
from agentworks.vms.upgrade.engine import UpgradeActionError, UpgradeEngine
from agentworks.vms.upgrade.execution import RemoteUpgradeExecution
from agentworks.vms.upgrade.journal import (
    AttemptOutcome,
    JournalError,
    JournalProgress,
    JournalState,
    UpgradeAction,
    UpgradePair,
)
from agentworks.vms.upgrade.network import (
    predict_interface_names,
    require_stable_interface_names,
    verify_interface_names,
)
from agentworks.vms.upgrade.probe import probe_upgrade_preflight, supported_architectures
from agentworks.vms.upgrade.remote import RemoteJournal

from ._helpers import _guard_failed_vm, _require_vm
from .boundary import gated_vm_boundary

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from agentworks.capabilities.base import RunContext
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.resources import Registry
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.secrets.resolver import Resolver
    from agentworks.transports import Transport
    from agentworks.vms.nodes import LiveVMNode
    from agentworks.vms.upgrade.preflight import UpgradePreflight


class _UpgradePolicy(Protocol):
    source: str
    target: str
    source_suites: tuple[str, ...]
    target_suites: tuple[str, ...]
    minimum_openssh_version: str
    documentation_urls: tuple[str, ...]
    blockers: tuple[str, ...]


class _ReleaseProfile(Protocol):
    release: str
    version_id: str
    upgrade_from_previous: _UpgradePolicy | None


class _DebianModule(Protocol):
    DEBIAN_RELEASES: tuple[_ReleaseProfile, ...]
    CURRENT_DEBIAN_RELEASE: str

    def probe_debian_release(self, target: Transport, *, expected: str | None = None) -> str: ...


class _ReleaseDatabase(Protocol):
    def update_vm_debian_release(
        self,
        name: str,
        release: object,
        *,
        observed_at: str | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class UpgradeResult:
    name: str
    source: str
    target: str
    status: str


@dataclass(frozen=True, slots=True)
class _Transition:
    pair: UpgradePair
    source_profile: _ReleaseProfile
    target_profile: _ReleaseProfile
    policy: _UpgradePolicy


@dataclass(frozen=True, slots=True)
class _EntryInspection:
    transition: _Transition
    journal_pair: UpgradePair | None
    preflight: UpgradePreflight | None
    adoption: bool
    platform_name: str


def upgrade_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    checkpoint: str | None = None,
    interaction: TtyInteractionPolicy,
) -> UpgradeResult:
    """Upgrade one verified previous-release VM to core's current release."""
    try:
        return _upgrade_vm(db, config, name, checkpoint=checkpoint, interaction=interaction)
    except JournalError as error:
        raise StateError(
            f"VM '{name}' has an invalid or unavailable Debian upgrade journal",
            entity_kind="vm",
            entity_name=name,
            hint=str(error),
        ) from error


def _upgrade_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    checkpoint: str | None = None,
    interaction: TtyInteractionPolicy,
) -> UpgradeResult:
    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{name}' has no canonical Tailscale route",
            entity_kind="vm",
            entity_name=name,
            hint="Repair VM connectivity before starting or resuming the Debian upgrade.",
        )

    entry = _inspect_entry(db, config, vm, interaction=interaction)
    vm = _require_vm(db, name)
    if entry.adoption:
        return _adopt_external_upgrade(db, config, vm, entry.transition, interaction=interaction)
    if entry.journal_pair is None and entry.preflight is None:
        current = entry.transition.pair.target
        output.result(f"VM '{name}' already runs Debian {current}; no upgrade is needed.")
        return UpgradeResult(name, current, current, "already-current")

    if entry.journal_pair is None:
        assert entry.preflight is not None
        _require_safe_preflight(entry.preflight, entry.transition)
        _render_plan(entry.preflight, heading="Preliminary Debian upgrade plan")

        from agentworks.vms.backup import backup_vm

        _render_checkpoint_guidance(entry.platform_name)
        checkpoint_ref = _resolve_checkpoint(checkpoint)
        ordinary_backup = backup_vm(db, config, name, interaction=interaction)
        recovery_bundle = _create_recovery_bundle(
            db,
            config,
            vm,
            entry.transition,
            entry.preflight,
            checkpoint_ref,
            interaction=interaction,
        )
        output.warn("Agentworks backups protect data, but they are not bootable VM checkpoints.")
        output.detail(f"VM backup: {format_host_path(ordinary_backup)}")
        output.detail(f"Debian recovery bundle: {format_host_path(recovery_bundle)}")
        if not output.confirm(
            f"Bring Debian {entry.transition.pair.source} fully current within its existing suite?",
            default=False,
        ):
            raise UserAbort("VM upgrade cancelled before package mutation")
        tailscale_auth_key = _initialize_and_update_source(
            db,
            config,
            vm,
            entry.transition,
            entry.preflight,
            checkpoint_ref,
            ordinary_backup,
            recovery_bundle,
            interaction=interaction,
        )
    else:
        tailscale_auth_key = None

    result = _resume_after_source_update(
        db,
        config,
        vm,
        entry.transition,
        tailscale_auth_key=tailscale_auth_key,
        interaction=interaction,
    )
    if result.status == "complete":
        import agentworks.vms.manager as manager

        try:
            manager.reinit_vm(db, config, name, interaction=interaction)
            _require_complete_initialization(db, name)
        except Exception as error:
            db.insert_vm_event(
                name,
                "debian_upgrade_repair_required",
                json.dumps({"stage": "release-aware-initialization", "target": result.target}, sort_keys=True),
            )
            raise StateError(
                f"VM '{name}' runs Debian {result.target}, but release-aware initialization needs repair",
                entity_kind="vm",
                entity_name=name,
                hint="Repair the initialization failure and run vm reinit. Automatic APT timers remain inhibited.",
            ) from error
        try:
            _restore_completed_timer_state(db, config, vm, entry.transition, interaction=interaction)
        except Exception as error:
            db.insert_vm_event(
                name,
                "debian_upgrade_repair_required",
                json.dumps({"stage": "apt-timer-restore", "target": result.target}, sort_keys=True),
            )
            raise StateError(
                f"VM '{name}' is healthy on Debian {result.target}, but automatic APT timers need repair",
                entity_kind="vm",
                entity_name=name,
                hint="Restore apt-daily.timer and apt-daily-upgrade.timer to their recorded states.",
            ) from error
        db.insert_vm_event(name, "debian_upgrade_complete", json.dumps({"target": result.target}, sort_keys=True))
        output.result(f"VM '{name}' upgraded to Debian {result.target}.")
    return result


def _inspect_entry(
    db: Database,
    config: Config,
    vm: VMRow,
    *,
    interaction: TtyInteractionPolicy,
) -> _EntryInspection:
    debian = _debian()
    transitions = _transitions(debian.DEBIAN_RELEASES)
    retained_pairs = tuple(transition.pair for transition in transitions)
    by_pair = {transition.pair: transition for transition in transitions}
    current_transition = transitions[-1]
    if current_transition.pair.target != _release_text(debian.CURRENT_DEBIAN_RELEASE):
        raise StateError("Debian release registry does not end at the declared current release")

    from agentworks.bootstrap import load_request_registry

    registry = load_request_registry(config, live_database=db)
    auth_key_name = _tailscale_auth_key_name(db, registry, vm)
    with gated_vm_boundary(
        db,
        config,
        registry,
        vm,
        secret_names=(auth_key_name,),
        interaction=interaction,
    ) as (vm_node, resolver, ops_ctx):
        platform_name = vm_node.site.platform.name
        target, states = _read_entry_states_with_repair(
            db,
            config,
            vm,
            retained_pairs,
            vm_node=vm_node,
            ops_ctx=ops_ctx,
            tailscale_auth_key=_tailscale_auth_key(resolver, auth_key_name),
        )
        incomplete = [pair for pair, state in states.items() if not state.is_complete]
        if len(incomplete) > 1:
            names = ", ".join(pair.dirname for pair in incomplete)
            raise JournalError(f"multiple incomplete Debian upgrade journals require repair: {names}")
        pair = incomplete[0] if incomplete else None
        if pair is not None:
            transition = by_pair[pair]
            return _EntryInspection(transition, pair, None, False, platform_name)

        pending_completion = [
            pair
            for pair, state in states.items()
            if state.is_complete and not _has_completed_upgrade_event(db, vm.name, pair.target)
        ]
        if len(pending_completion) > 1:
            names = ", ".join(pair.dirname for pair in pending_completion)
            raise JournalError(f"multiple completed Debian upgrades need finalization: {names}")
        if pending_completion:
            pair = pending_completion[0]
            return _EntryInspection(by_pair[pair], pair, None, False, platform_name)

        live_release = debian.probe_debian_release(target)
        recorded = _recorded_release(vm)
        if recorded is None:
            _persist_release(db, vm.name, live_release)
            recorded = live_release
        elif recorded == live_release:
            _persist_release(db, vm.name, live_release)
        elif recorded == current_transition.pair.source and live_release == current_transition.pair.target:
            return _EntryInspection(current_transition, None, None, True, platform_name)
        else:
            raise StateError(
                f"VM '{vm.name}' release disagreement: database={recorded}, guest={live_release}",
                entity_kind="vm",
                entity_name=vm.name,
                hint="Repair the disagreement before a release-sensitive operation.",
            )

        if live_release == current_transition.pair.target:
            return _EntryInspection(current_transition, None, None, False, platform_name)
        if live_release != current_transition.pair.source:
            raise StateError(
                f"VM '{vm.name}' runs Debian {live_release}; only "
                f"{current_transition.pair.source} to {current_transition.pair.target} is supported",
                entity_kind="vm",
                entity_name=vm.name,
                hint=(
                    f"Create a new Debian {current_transition.pair.target} VM and copy the data. "
                    "Multi-hop upgrade is not supported."
                ),
            )
        preflight = _probe(db, vm, target, current_transition, recorded)
        return _EntryInspection(current_transition, None, preflight, False, platform_name)


def _initialize_and_update_source(
    db: Database,
    config: Config,
    vm: VMRow,
    transition: _Transition,
    preflight: UpgradePreflight,
    checkpoint: str,
    ordinary_backup: Path,
    recovery_bundle: Path,
    *,
    interaction: TtyInteractionPolicy,
) -> str:
    from agentworks.bootstrap import load_request_registry
    from agentworks.transports import transport

    registry = load_request_registry(config, live_database=db)
    auth_key_name = _tailscale_auth_key_name(db, registry, vm)
    with gated_vm_boundary(
        db,
        config,
        registry,
        vm,
        secret_names=(auth_key_name,),
        interaction=interaction,
    ) as (_vm_node, resolver, _ops_ctx):
        tailscale_auth_key = _tailscale_auth_key(resolver, auth_key_name)
        target = transport(vm, config)
        journal = RemoteJournal(target)
        journal.install()
        if journal.select_incomplete(tuple(item.pair for item in _transitions(_debian().DEBIAN_RELEASES))) is not None:
            raise StateError(
                f"VM '{vm.name}' acquired an upgrade journal while backups were being prepared",
                entity_kind="vm",
                entity_name=vm.name,
                hint="Rerun vm upgrade to inspect the existing journal.",
            )
        fresh = _probe(db, vm, target, transition, transition.pair.source)
        _require_safe_preflight(fresh, transition)
        plan = fresh.to_plan()
        plan.update(
            {
                "checkpoint": checkpoint,
                "ordinary_backup": str(ordinary_backup),
                "recovery_bundle": str(recovery_bundle),
                "preliminary_material_fingerprint": json.loads(json.dumps(preflight.material_fingerprint())),
            }
        )
        journal.initialize(transition.pair, plan)
        _inhibit_apt_timers(target)
        db.insert_vm_event(
            vm.name,
            "debian_upgrade_started",
            json.dumps({"checkpoint": checkpoint, "target": transition.pair.target}, sort_keys=True),
        )
        execution = _execution(target, journal, transition)
        execution.install_script()
        _advance_source_action(journal, execution, transition.pair, target, fresh.apt_timer_states)
    return tailscale_auth_key


def _resume_after_source_update(
    db: Database,
    config: Config,
    vm: VMRow,
    transition: _Transition,
    *,
    tailscale_auth_key: str | None,
    interaction: TtyInteractionPolicy,
) -> UpgradeResult:
    from agentworks.bootstrap import load_request_registry
    from agentworks.transports import transport

    registry = load_request_registry(config, live_database=db)
    auth_key_name = _tailscale_auth_key_name(db, registry, vm)
    required_names = () if tailscale_auth_key is not None else (auth_key_name,)
    with gated_vm_boundary(
        db,
        config,
        registry,
        vm,
        secret_names=required_names,
        interaction=interaction,
    ) as (vm_node, resolver, ops_ctx):
        if tailscale_auth_key is None:
            tailscale_auth_key = _tailscale_auth_key(resolver, auth_key_name)
        target = transport(vm, config)
        journal = RemoteJournal(target)
        journal.install()
        execution = _execution(target, journal, transition)
        execution.install_script()
        state = journal.load(transition.pair)
        if state.last_completed is JournalProgress.PREPARED or state.active_action is UpgradeAction.SOURCE_UPDATE:
            plan = journal.load_plan(transition.pair)
            timers = _timer_states_from_plan(plan)
            _inhibit_apt_timers(target)
            _advance_source_action(journal, execution, transition.pair, target, timers)
            state = journal.load(transition.pair)

        if state.last_completed is JournalProgress.SOURCE_CURRENT and state.active_action is None:
            final = _probe(db, vm, target, transition, transition.pair.source)
            try:
                _require_safe_preflight(final, transition)
            except Exception:
                plan = journal.load_plan(transition.pair)
                _restore_apt_timers(target, _timer_states_from_plan(plan))
                raise
            _render_plan(final, heading="Final Debian suite-switch plan")
            plan = journal.load_plan(transition.pair)
            final_fingerprint = json.loads(json.dumps(final.material_fingerprint()))
            if plan.get("preliminary_material_fingerprint") != final_fingerprint:
                output.warn("The final package, source, conffile, blocker, or space plan changed after source update.")
            output.warn("VM connectivity will be interrupted during service restarts and reboot.")
            if not output.confirm(
                f"Switch Debian suites from {transition.pair.source} to {transition.pair.target}?",
                default=False,
            ):
                plan = journal.load_plan(transition.pair)
                _restore_apt_timers(target, _timer_states_from_plan(plan))
                raise UserAbort("VM upgrade paused before suite switching; rerun vm upgrade to continue")
            plan["final_plan"] = final.to_plan()
            journal.update_plan(transition.pair, plan)
            _inhibit_apt_timers(target)

        for action in (
            UpgradeAction.SWITCH_SOURCES,
            UpgradeAction.MINIMAL_UPGRADE,
            UpgradeAction.FULL_UPGRADE,
        ):
            state = journal.load(transition.pair)
            if _action_pending(state, action):
                _advance_action(journal, execution, transition.pair, action)

        state = journal.load(transition.pair)
        if state.last_completed is JournalProgress.FULL_UPGRADE_COMPLETE and state.active_action is None:
            predictions = predict_interface_names(target)
            require_stable_interface_names(predictions)
            plan = journal.load_plan(transition.pair)
            plan["interface_predictions"] = predictions
            journal.update_plan(transition.pair, plan)
        if _action_pending(state, UpgradeAction.REBOOT):
            _advance_reboot(journal, execution, transition.pair)
            reboot_state = journal.load(transition.pair)
            if reboot_state.boot_id_before is None:
                raise StateError("reboot journal is missing the pre-reboot boot ID")
            reconnected = _reconnect_after_reboot(
                db,
                config,
                vm,
                target,
                prior_boot_id=reboot_state.boot_id_before,
                vm_node=vm_node,
                ops_ctx=ops_ctx,
                tailscale_auth_key=tailscale_auth_key,
            )
            if reconnected is None and _observed_boot_id(target) == reboot_state.boot_id_before:
                output.warn("The guest is still on the pre-reboot boot ID; safely redispatching reboot once.")
                journal.redispatch_reboot(transition.pair, reboot_state.boot_id_before)
                reconnected = _reconnect_after_reboot(
                    db,
                    config,
                    vm,
                    target,
                    prior_boot_id=reboot_state.boot_id_before,
                    vm_node=vm_node,
                    ops_ctx=ops_ctx,
                    tailscale_auth_key=tailscale_auth_key,
                )
            if reconnected is None:
                state = journal.load(transition.pair)
                if state.active_action is UpgradeAction.REBOOT:
                    journal.fail(
                        transition.pair,
                        UpgradeAction.REBOOT,
                        "canonical transport did not return after reboot",
                        repair_required=True,
                    )
                plan = journal.load_plan(transition.pair)
                db.insert_vm_event(
                    vm.name,
                    "debian_upgrade_repair_required",
                    json.dumps({"checkpoint": plan.get("checkpoint"), "stage": "reconnect"}, sort_keys=True),
                )
                raise StateError(
                    f"VM '{vm.name}' did not reconnect after reboot",
                    entity_kind="vm",
                    entity_name=vm.name,
                    hint="Use the platform console or restore the recorded external checkpoint.",
                )
            target = reconnected
            journal = RemoteJournal(target)
            execution = _execution(target, journal, transition)
            result = execution.inspect(UpgradeAction.REBOOT, journal.load(transition.pair))
            if result.disposition.value != "succeeded":
                state = journal.load(transition.pair)
                if state.active_action is UpgradeAction.REBOOT:
                    journal.fail(
                        transition.pair,
                        UpgradeAction.REBOOT,
                        "reboot did not change the guest boot ID",
                        repair_required=True,
                    )
                raise StateError(
                    f"VM '{vm.name}' reconnected without proving a completed reboot",
                    entity_kind="vm",
                    entity_name=vm.name,
                )
            journal.complete(transition.pair, UpgradeAction.REBOOT)

        live = _debian().probe_debian_release(target, expected=transition.target_profile.release)
        _persist_release(db, vm.name, live)
        try:
            verify_interface_names(target, _interface_predictions(journal.load_plan(transition.pair)))
        except StateError:
            plan = journal.load_plan(transition.pair)
            db.insert_vm_event(
                vm.name,
                "debian_upgrade_repair_required",
                json.dumps({"checkpoint": plan.get("checkpoint"), "stage": "interface-names"}, sort_keys=True),
            )
            raise
        _verify_target_health(db, vm, target, transition)
        return UpgradeResult(vm.name, transition.pair.source, transition.pair.target, "complete")


def _advance_action(
    journal: RemoteJournal,
    execution: RemoteUpgradeExecution,
    pair: UpgradePair,
    action: UpgradeAction,
) -> None:
    try:
        UpgradeEngine(journal, execution).advance_action(pair, action)
    except UpgradeActionError as error:
        raise StateError(
            f"Debian upgrade action failed: {action.value}",
            hint=("Repair forward using the retained log, or restore the external checkpoint. " + error.detail),
        ) from error


def _advance_source_action(
    journal: RemoteJournal,
    execution: RemoteUpgradeExecution,
    pair: UpgradePair,
    target: Transport,
    timer_states: dict[str, tuple[str, str]],
) -> None:
    try:
        _advance_action(journal, execution, pair, UpgradeAction.SOURCE_UPDATE)
    except StateError:
        state = journal.load(pair)
        if state.last_completed is JournalProgress.PREPARED and state.outcome in {
            AttemptOutcome.FAILED,
            AttemptOutcome.REPAIR_REQUIRED,
        }:
            _restore_apt_timers(target, timer_states)
        raise


def _advance_reboot(journal: RemoteJournal, execution: RemoteUpgradeExecution, pair: UpgradePair) -> None:
    state = journal.load(pair)
    if state.active_action is UpgradeAction.REBOOT:
        return
    if state.next_action is not UpgradeAction.REBOOT:
        raise StateError("upgrade journal is not ready to reboot")
    execution.start(UpgradeAction.REBOOT, "reboot-dispatch")


def _action_pending(state: JournalState, action: UpgradeAction) -> bool:
    completed_order = list(JournalProgress)
    action_progress = {
        UpgradeAction.SOURCE_UPDATE: JournalProgress.SOURCE_CURRENT,
        UpgradeAction.SWITCH_SOURCES: JournalProgress.SOURCES_SWITCHED,
        UpgradeAction.MINIMAL_UPGRADE: JournalProgress.MINIMAL_UPGRADE_COMPLETE,
        UpgradeAction.FULL_UPGRADE: JournalProgress.FULL_UPGRADE_COMPLETE,
        UpgradeAction.REBOOT: JournalProgress.REBOOT_COMPLETE,
    }[action]
    return completed_order.index(state.last_completed) < completed_order.index(action_progress)


def _probe(
    db: Database,
    vm: VMRow,
    target: Transport,
    transition: _Transition,
    recorded: str,
) -> UpgradePreflight:
    from agentworks.sessions.manager import batch_check_status

    sessions = db.list_sessions(vm_name=vm.name)
    statuses = batch_check_status(sessions, target=target)
    unsafe: list[str] = []
    for session in sessions:
        if session.pid == PID_STOPPED:
            continue
        status = statuses.get(session.name, SessionStatus.UNKNOWN)
        if status is not SessionStatus.STOPPED:
            unsafe.append(f"{session.name}:{status.value}")
    live = _release_text(_debian().probe_debian_release(target))
    return probe_upgrade_preflight(
        target,
        database_release=recorded,
        live_release=live,
        source_suites=transition.policy.source_suites,
        minimum_openssh_version=transition.policy.minimum_openssh_version,
        blocker_hooks=transition.policy.blockers,
        non_quiescent_sessions=unsafe,
    )


def _require_safe_preflight(preflight: UpgradePreflight, transition: _Transition) -> None:
    issues = preflight.issues(
        expected_source=transition.pair.source,
        supported_architectures=supported_architectures(),
    )
    if issues:
        raise StateError(
            f"Debian upgrade preflight found {len(issues)} unsafe condition(s)",
            hint="Correct and rerun: " + ", ".join(issue.value for issue in issues),
        )


def _render_plan(preflight: UpgradePreflight, *, heading: str) -> None:
    with output.section(heading):
        output.detail(f"Architecture: {preflight.architecture}; kernel: {preflight.kernel}")
        output.detail(f"Packages apt would remove: {', '.join(preflight.removals) or 'none'}")
        output.detail(f"Third-party sources to disable: {', '.join(preflight.third_party_sources) or 'none'}")
        output.detail(f"Installed non-Debian packages: {', '.join(preflight.non_debian_packages) or 'none'}")
        output.detail(f"Obsolete packages: {', '.join(preflight.obsolete_packages) or 'none'}")
        output.detail(f"Modified conffiles: {', '.join(preflight.modified_conffiles) or 'none'}")


def _resolve_checkpoint(value: str | None) -> str:
    if value is None:
        if not output.is_interactive():
            raise ValidationError("--checkpoint is required when vm upgrade cannot prompt")
        output.warn("Create a bootable provider snapshot, WSL export, Proxmox backup, or equivalent before continuing.")
        value = output.prompt("External recovery artifact reference").strip()
    if value != value.strip() or not value or len(value) > 512 or "\n" in value or "\r" in value:
        raise ValidationError("checkpoint reference must be a non-blank, bounded single line")
    return value


def _render_checkpoint_guidance(platform_name: str) -> None:
    guidance = {
        "azure-vm": "Create an Azure managed-disk snapshot and verify Azure serial-console access.",
        "aws-ec2": "Create an EBS snapshot or recoverable AMI and verify EC2 console access.",
        "gcp-gce": "Create a persistent-disk snapshot and verify Google Cloud serial-console access.",
        "lima": "Create a recoverable Lima instance or disk copy and verify limactl shell access.",
        "proxmox": "Create a Proxmox backup or snapshot and verify VM console access.",
        "wsl2": "Export the WSL distribution and verify that the export can be imported on this host.",
    }.get(
        platform_name,
        f"Create a recoverable {platform_name} VM checkpoint and verify provider console access.",
    )
    output.warn(guidance)


def _create_recovery_bundle(
    db: Database,
    config: Config,
    vm: VMRow,
    transition: _Transition,
    preflight: UpgradePreflight,
    checkpoint: str,
    *,
    interaction: TtyInteractionPolicy,
) -> Path:
    from agentworks.bootstrap import load_request_registry
    from agentworks.transports import transport

    registry = load_request_registry(config, live_database=db)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = config.paths.backups / f"{vm.name}-debian-upgrade-{timestamp}"
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    destination.chmod(0o700)
    local_archive = destination / "debian-recovery.tar.gz"
    remote_dir = f"/var/tmp/agentworks-debian-recovery-{uuid.uuid4().hex}"
    q_remote = shlex.quote(remote_dir)
    with gated_vm_boundary(db, config, registry, vm, interaction=interaction):
        target = transport(vm, config)
        try:
            target.run(f"install -d -m 0700 {q_remote}", sudo=True)
            target.run(f"dpkg --get-selections > {q_remote}/dpkg-selections", sudo=True)
            target.run(f"apt-mark showmanual > {q_remote}/apt-manual", sudo=True)
            manifest = {
                "vm": vm.name,
                "source": transition.pair.source,
                "target": transition.pair.target,
                "checkpoint": checkpoint,
                "created_at": datetime.now(UTC).isoformat(),
                "preflight": preflight.to_plan(),
            }
            write_manifest = "import json,sys; json.dump(json.load(sys.stdin), open(sys.argv[1], 'w'), indent=2)"
            target.run(
                f"python3 -c {shlex.quote(write_manifest)} {q_remote}/manifest.json",
                sudo=True,
                input_text=json.dumps(manifest),
                tty=False,
            )
            archive = f"{remote_dir}/debian-recovery.tar.gz"
            target.run(
                f"tar -czf {shlex.quote(archive)} "
                f"-C / etc var/lib/dpkg var/lib/apt/extended_states "
                f"-C {q_remote} dpkg-selections apt-manual manifest.json",
                sudo=True,
            )
            target.run(
                f"chown {shlex.quote(vm.admin_username)} {shlex.quote(archive)}",
                sudo=True,
            )
            target.copy_from(archive, local_archive)
            local_archive.chmod(0o600)
        except Exception:
            output.warn(f"Incomplete Debian recovery bundle retained at {format_host_path(destination)}")
            raise
        finally:
            target.run(f"rm -rf {q_remote}", sudo=True, check=False)
    return local_archive


def _inhibit_apt_timers(target: Transport) -> None:
    target.run(
        "systemctl stop apt-daily.timer apt-daily-upgrade.timer && "
        "systemctl mask apt-daily.timer apt-daily-upgrade.timer",
        sudo=True,
    )


def _restore_apt_timers(target: Transport, states: dict[str, tuple[str, str]]) -> None:
    for name, (enabled, active) in states.items():
        q_name = shlex.quote(name)
        target.run(f"systemctl unmask {q_name}", sudo=True)
        if enabled == "enabled":
            target.run(f"systemctl enable {q_name}", sudo=True)
        elif enabled == "enabled-runtime":
            target.run(f"systemctl enable --runtime {q_name}", sudo=True)
        elif enabled == "disabled":
            target.run(f"systemctl disable {q_name}", sudo=True)
        elif enabled == "masked":
            target.run(f"systemctl mask {q_name}", sudo=True)
        elif enabled == "masked-runtime":
            target.run(f"systemctl mask --runtime {q_name}", sudo=True)
        if active == "active" and enabled not in {"masked", "masked-runtime"}:
            target.run(f"systemctl start {q_name}", sudo=True)
        else:
            target.run(f"systemctl stop {q_name}", sudo=True)
        observed_enabled = target.run(f"systemctl is-enabled {q_name}", check=False).stdout.strip()
        observed_active = target.run(f"systemctl is-active {q_name}", check=False).stdout.strip()
        if (observed_enabled, observed_active) != (enabled, active):
            raise StateError(f"automatic APT timer {name} did not return to its recorded state")


def _restore_completed_timer_state(
    db: Database,
    config: Config,
    vm: VMRow,
    transition: _Transition,
    *,
    interaction: TtyInteractionPolicy,
) -> None:
    from agentworks.bootstrap import load_request_registry
    from agentworks.transports import transport

    registry = load_request_registry(config, live_database=db)
    with gated_vm_boundary(db, config, registry, vm, interaction=interaction):
        target = transport(vm, config)
        journal = RemoteJournal(target)
        journal.install()
        plan = journal.load_plan(transition.pair)
        _restore_apt_timers(target, _timer_states_from_plan(plan))


def _verify_target_health(
    db: Database,
    vm: VMRow,
    target: Transport,
    transition: _Transition,
) -> None:
    failed = _target_health_failures(db, vm, target)
    if not failed:
        return
    plan = RemoteJournal(target).load_plan(transition.pair)
    db.insert_vm_event(
        vm.name,
        "debian_upgrade_repair_required",
        json.dumps(
            {"checkpoint": plan.get("checkpoint"), "failed_checks": failed, "stage": "target-health"},
            sort_keys=True,
        ),
    )
    raise StateError(
        f"VM '{vm.name}' proved Debian {transition.pair.target}, but target health checks failed",
        entity_kind="vm",
        entity_name=vm.name,
        hint="Repair the target release before reinitialization: " + ", ".join(failed),
    )


def _target_health_failures(db: Database, vm: VMRow, target: Transport) -> list[str]:
    checks = {
        "package database": 'test -z "$(dpkg --audit)"',
        "running kernel package": (
            "dpkg-query -W -f='${db:Status-Status}' \"linux-image-$(uname -r)\" 2>/dev/null | grep -qx installed"
        ),
        "systemd": 'test "$(cat /proc/1/comm)" = systemd',
        "sshd": "systemctl is-active --quiet ssh",
        "Tailscale": "tailscale status --json >/dev/null",
        "admin identity": f"id {shlex.quote(vm.admin_username)} >/dev/null",
        "Agentworks state": "test -d /var/lib/agentworks",
    }
    checks.update(
        {
            f"agent identity {agent.name}": f"id {shlex.quote(agent.linux_user)} >/dev/null"
            for agent in db.list_agents(vm_name=vm.name)
        }
    )
    checks.update(
        {
            f"workspace path {workspace.name}": f"test -d {shlex.quote(workspace.workspace_path)}"
            for workspace in db.list_workspaces(vm_name=vm.name)
        }
    )
    return [name for name, command in checks.items() if not target.run(command, sudo=True, check=False).ok]


def _timer_states_from_plan(plan: dict[str, object]) -> dict[str, tuple[str, str]]:
    value = plan.get("apt_timer_states")
    if not isinstance(value, dict):
        raise StateError("upgrade plan is missing automatic APT timer state")
    result: dict[str, tuple[str, str]] = {}
    for name, state in value.items():
        if not isinstance(name, str) or not isinstance(state, list) or len(state) != 2:
            raise StateError("upgrade plan has invalid automatic APT timer state")
        enabled, active = state
        if not isinstance(enabled, str) or not isinstance(active, str):
            raise StateError("upgrade plan has invalid automatic APT timer state")
        result[name] = (enabled, active)
    return result


def _interface_predictions(plan: dict[str, object]) -> dict[str, str]:
    value = plan.get("interface_predictions")
    if not isinstance(value, dict) or not value:
        raise StateError("upgrade plan is missing pre-reboot interface-name predictions")
    result: dict[str, str] = {}
    for current, predicted in value.items():
        if not isinstance(current, str) or not current or not isinstance(predicted, str) or not predicted:
            raise StateError("upgrade plan has invalid interface-name predictions")
        result[current] = predicted
    return result


def _adopt_external_upgrade(
    db: Database,
    config: Config,
    vm: VMRow,
    transition: _Transition,
    *,
    interaction: TtyInteractionPolicy,
) -> UpgradeResult:
    output.warn(
        f"VM '{vm.name}' already runs Debian {transition.pair.target}, "
        f"while the database records {transition.pair.source}."
    )
    if not output.confirm("Adopt the verified guest release and run release-aware initialization?", default=False):
        raise UserAbort("external Debian upgrade adoption cancelled")
    from agentworks.bootstrap import load_request_registry
    from agentworks.transports import transport

    registry = load_request_registry(config, live_database=db)
    with gated_vm_boundary(db, config, registry, vm, interaction=interaction):
        target = transport(vm, config)
        live = _debian().probe_debian_release(target, expected=transition.target_profile.release)
        failed = _target_health_failures(db, vm, target)
        if failed:
            raise StateError(
                f"VM '{vm.name}' is not healthy enough to adopt as Debian {transition.pair.target}",
                entity_kind="vm",
                entity_name=vm.name,
                hint="Repair before adoption: " + ", ".join(failed),
            )
        _persist_release(db, vm.name, live)
    import agentworks.vms.manager as manager

    try:
        manager.reinit_vm(db, config, vm.name, interaction=interaction)
        _require_complete_initialization(db, vm.name)
    except Exception as error:
        db.insert_vm_event(
            vm.name,
            "debian_upgrade_repair_required",
            json.dumps(
                {"stage": "adoption-initialization", "target": transition.pair.target},
                sort_keys=True,
            ),
        )
        raise StateError(
            f"VM '{vm.name}' was adopted as Debian {transition.pair.target}, but initialization needs repair",
            entity_kind="vm",
            entity_name=vm.name,
            hint=f"Run vm reinit {vm.name} after correcting the initialization failure.",
        ) from error
    db.insert_vm_event(
        vm.name,
        "debian_upgrade_adopted",
        json.dumps({"target": transition.pair.target}, sort_keys=True),
    )
    return UpgradeResult(vm.name, transition.pair.source, transition.pair.target, "adopted")


def _wait_for_strict_reconnect(
    target: Transport,
    *,
    prior_boot_id: str,
    attempts: int = 48,
) -> bool:
    from agentworks.ssh import SSHError

    output.detail("Waiting for the canonical VM route to return...")
    for attempt in range(attempts):
        try:
            current = target.run("cat /proc/sys/kernel/random/boot_id", timeout=10).stdout.strip()
            if current != prior_boot_id:
                time.sleep(2)
                verified = target.run("cat /proc/sys/kernel/random/boot_id", timeout=10).stdout.strip()
                return verified == current
        except SSHError:
            pass
        if attempt + 1 < attempts:
            time.sleep(5)
    return False


def _observed_boot_id(target: Transport) -> str | None:
    from agentworks.ssh import SSHError

    try:
        value = target.run("cat /proc/sys/kernel/random/boot_id", timeout=10).stdout.strip()
    except SSHError:
        return None
    return value or None


def _reconnect_after_reboot(
    db: Database,
    config: Config,
    vm: VMRow,
    canonical_target: Transport,
    *,
    prior_boot_id: str,
    vm_node: LiveVMNode,
    ops_ctx: RunContext,
    tailscale_auth_key: str,
) -> Transport | None:
    """Return a boot-proved canonical route, using native repair if needed."""
    if _wait_for_strict_reconnect(canonical_target, prior_boot_id=prior_boot_id):
        return canonical_target
    if _observed_boot_id(canonical_target) == prior_boot_id:
        return None

    output.warn("The canonical Tailscale route did not return; trying the platform-native route.")
    try:
        from agentworks.transports import native_transport, transport

        with contextlib.ExitStack() as stack:
            native_target = native_transport(
                vm,
                vm_node.site.platform,
                config,
                ctx=ops_ctx,
                stack=stack,
            )
            if not _wait_for_strict_reconnect(native_target, prior_boot_id=prior_boot_id, attempts=24):
                return None
            if native_target.logger is not None:
                raise StateError("Tailscale repair transport unexpectedly has an operation logger")

            import agentworks.vms.manager as manager

            manager.verify_tailscale_available()
            manager.rejoin_tailscale(db, vm.name, native_target, auth_key=tailscale_auth_key)

        refreshed = _require_vm(db, vm.name)
        repaired = transport(refreshed, config)
        if _wait_for_strict_reconnect(repaired, prior_boot_id=prior_boot_id, attempts=12):
            return repaired
    except Exception as error:
        output.warn(f"Platform-native Tailscale repair failed ({type(error).__name__}).")
    return None


def _read_entry_states_with_repair(
    db: Database,
    config: Config,
    vm: VMRow,
    retained_pairs: Sequence[UpgradePair],
    *,
    vm_node: LiveVMNode,
    ops_ctx: RunContext,
    tailscale_auth_key: str,
) -> tuple[Transport, dict[UpgradePair, JournalState]]:
    """Read the journal, repairing canonical reachability through native transport once."""
    from agentworks.ssh import SSHError
    from agentworks.transports import native_transport, transport

    canonical = transport(vm, config)
    try:
        return canonical, RemoteJournal(canonical).read_states(retained_pairs)
    except SSHError:
        output.warn("The canonical route is unavailable; reading upgrade recovery state over the native route.")

    with contextlib.ExitStack() as stack:
        native = native_transport(
            vm,
            vm_node.site.platform,
            config,
            ctx=ops_ctx,
            stack=stack,
        )
        native_states = RemoteJournal(native).read_states(retained_pairs)
        needs_recovery = any(
            not state.is_complete or not _has_completed_upgrade_event(db, vm.name, pair.target)
            for pair, state in native_states.items()
        )
        if not needs_recovery:
            raise StateError(
                f"VM '{vm.name}' has no canonical Tailscale route",
                entity_kind="vm",
                entity_name=vm.name,
                hint="Repair VM connectivity before checking new Debian upgrade eligibility.",
            )
        if native.logger is not None:
            raise StateError("Tailscale repair transport unexpectedly has an operation logger")

        import agentworks.vms.manager as manager

        manager.verify_tailscale_available()
        manager.rejoin_tailscale(db, vm.name, native, auth_key=tailscale_auth_key)

    refreshed = _require_vm(db, vm.name)
    repaired = transport(refreshed, config)
    if not _wait_for_stable_route(repaired):
        raise StateError(
            f"VM '{vm.name}' did not recover its canonical Tailscale route",
            entity_kind="vm",
            entity_name=vm.name,
            hint="Use the platform console and recorded external checkpoint before resuming the upgrade.",
        )
    return repaired, RemoteJournal(repaired).read_states(retained_pairs)


def _wait_for_stable_route(target: Transport, *, attempts: int = 12) -> bool:
    from agentworks.ssh import SSHError

    for attempt in range(attempts):
        try:
            first = target.run("cat /proc/sys/kernel/random/boot_id", timeout=10).stdout.strip()
            second = target.run("cat /proc/sys/kernel/random/boot_id", timeout=10).stdout.strip()
            if first and first == second:
                return True
        except SSHError:
            pass
        if attempt + 1 < attempts:
            time.sleep(5)
    return False


def _tailscale_auth_key_name(db: Database, registry: Registry, vm: VMRow) -> str:
    from agentworks.vms.templates import resolve_live_template

    return resolve_live_template(db, registry, vm.name, vm.template).tailscale_auth_key


def _require_complete_initialization(db: Database, name: str) -> None:
    from agentworks.db import InitStatus

    vm = _require_vm(db, name)
    if vm.init_status != InitStatus.COMPLETE.value:
        raise StateError(f"VM '{name}' initialization completed with status {vm.init_status}")


def _tailscale_auth_key(resolver: Resolver, name: str) -> str:
    from agentworks.secrets.line_safety import LineOrientedSecretUse, require_line_safe_secret

    return require_line_safe_secret(
        resolver.get(name),
        use=LineOrientedSecretUse.TAILSCALE,
        secret_name=name,
    )


def _execution(
    target: Transport,
    journal: RemoteJournal,
    transition: _Transition,
) -> RemoteUpgradeExecution:
    return RemoteUpgradeExecution(
        target,
        journal,
        transition.pair,
        target_version_id=transition.target_profile.version_id,
        target_suites=transition.policy.target_suites,
    )


def _transitions(profiles: Sequence[_ReleaseProfile]) -> tuple[_Transition, ...]:
    transitions: list[_Transition] = []
    for source, target in zip(profiles, profiles[1:], strict=False):
        policy = target.upgrade_from_previous
        if policy is None:
            raise StateError(f"Debian profile {_release_text(target.release)} has no adjacent upgrade policy")
        pair = UpgradePair(_release_text(source.release), _release_text(target.release))
        transitions.append(_Transition(pair, source, target, policy))
    if not transitions:
        raise StateError("Debian release registry has no adjacent upgrade transition")
    return tuple(transitions)


def _recorded_release(vm: VMRow) -> str | None:
    value = getattr(vm, "debian_release", None)
    return None if value is None else _release_text(value)


def _has_completed_upgrade_event(db: Database, name: str, target: str) -> bool:
    for event in reversed(db.list_vm_events(name)):
        if event.event != "debian_upgrade_complete" or event.detail is None:
            continue
        try:
            detail = json.loads(event.detail)
        except json.JSONDecodeError:
            continue
        if isinstance(detail, dict) and detail.get("target") == target:
            return True
    return False


def _persist_release(db: Database, name: str, release: object) -> None:
    cast("_ReleaseDatabase", db).update_vm_debian_release(name, release)


def _release_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise StateError("Debian release registry returned an invalid release value")
    return value


def _debian() -> _DebianModule:
    return cast("_DebianModule", importlib.import_module("agentworks.debian"))
