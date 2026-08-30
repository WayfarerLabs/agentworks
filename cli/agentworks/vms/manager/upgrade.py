"""Service boundary for the durable adjacent Debian VM upgrade."""

from __future__ import annotations

import contextlib
import json
import shlex
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.db import PID_STOPPED, SessionStatus
from agentworks.debian import (
    CURRENT_DEBIAN_RELEASE,
    DEBIAN_RELEASES,
    DebianRelease,
    DebianReleaseProfile,
    DebianUpgradePolicy,
    probe_debian_release,
)
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
    snapshot_provider_interface_names,
    verify_interface_names,
)
from agentworks.vms.upgrade.preflight import (
    APT_TIMER_ACTIVE_STATES,
    APT_TIMER_ENABLE_STATES,
    APT_TIMER_UNITS,
)
from agentworks.vms.upgrade.probe import (
    NATIVE_PACKAGE_LOCK_COMMAND,
    probe_upgrade_preflight,
    supported_architectures,
    target_source_hygiene_issues,
)
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


@dataclass(frozen=True, slots=True)
class _Transition:
    pair: UpgradePair
    target_profile: DebianReleaseProfile
    policy: DebianUpgradePolicy


@dataclass(frozen=True, slots=True)
class _EntryInspection:
    transition: _Transition
    journal_pair: UpgradePair | None
    preflight: UpgradePreflight | None
    adoption: bool
    platform_name: str
    tailscale_auth_key: str


def upgrade_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    checkpoint: str | None = None,
    interaction: TtyInteractionPolicy,
) -> None:
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
) -> None:
    vm = _require_vm(db, name)
    _guard_failed_vm(vm)

    checkpoint_ref = _validate_checkpoint(checkpoint) if checkpoint is not None else None
    entry = _inspect_entry(db, config, vm, interaction=interaction)
    vm = _require_vm(db, name)
    if entry.adoption:
        _require_interactive_upgrade_authorization()
        _adopt_external_upgrade(
            db,
            config,
            vm,
            entry.transition,
            platform_name=entry.platform_name,
            interaction=interaction,
        )
        return
    if entry.journal_pair is None and entry.preflight is None:
        current = entry.transition.pair.target
        output.result(f"VM '{name}' already runs Debian {current}; no upgrade is needed.")
        return

    if entry.journal_pair is None:
        _require_interactive_upgrade_authorization()
        assert entry.preflight is not None
        _require_safe_preflight(entry.preflight, entry.transition)
        _render_plan(
            entry.preflight,
            entry.transition,
            heading="Preliminary Debian upgrade plan",
        )

        from agentworks.vms.backup import backup_vm

        _render_checkpoint_guidance(entry.platform_name)
        checkpoint_ref = _resolve_checkpoint(checkpoint_ref)
        if not output.confirm(
            "Confirm that the named external recovery artifact exists and console or rescue access was tested?",
            default=False,
        ):
            raise UserAbort("VM upgrade cancelled before local backup because checkpoint creation was not attested")
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
        _initialize_and_update_source(
            db,
            config,
            vm,
            entry.transition,
            entry.preflight,
            checkpoint_ref,
            ordinary_backup,
            recovery_bundle,
            platform_name=entry.platform_name,
            interaction=interaction,
        )

    _resume_after_source_update(
        db,
        config,
        vm,
        entry.transition,
        platform_name=entry.platform_name,
        checkpoint_ref=checkpoint_ref,
        tailscale_auth_key=entry.tailscale_auth_key,
        interaction=interaction,
    )
    target_release = entry.transition.pair.target
    import agentworks.vms.manager as manager

    try:
        manager.reinit_vm(db, config, name, interaction=interaction)
        _require_complete_initialization(db, name)
    except Exception as error:
        db.insert_vm_event(
            name,
            "debian_upgrade_repair_required",
            json.dumps({"stage": "release-aware-initialization", "target": target_release}, sort_keys=True),
        )
        raise StateError(
            f"VM '{name}' runs Debian {target_release}, but release-aware initialization needs repair",
            entity_kind="vm",
            entity_name=name,
            hint=(
                "Repair the initialization failure, run vm reinit, then rerun vm upgrade to finalize. "
                "Automatic APT timers remain inhibited."
            ),
        ) from error
    try:
        _restore_completed_timer_state(db, config, vm, entry.transition, interaction=interaction)
    except Exception as error:
        db.insert_vm_event(
            name,
            "debian_upgrade_repair_required",
            json.dumps({"stage": "apt-timer-restore", "target": target_release}, sort_keys=True),
        )
        raise StateError(
            f"VM '{name}' is healthy on Debian {target_release}, but automatic APT timers need repair",
            entity_kind="vm",
            entity_name=name,
            hint="Restore apt-daily.timer and apt-daily-upgrade.timer to their recorded states.",
        ) from error
    db.insert_vm_event(name, "debian_upgrade_complete", json.dumps({"target": target_release}, sort_keys=True))
    output.result(f"VM '{name}' upgraded to Debian {target_release}.")


def _inspect_entry(
    db: Database,
    config: Config,
    vm: VMRow,
    *,
    interaction: TtyInteractionPolicy,
) -> _EntryInspection:
    transitions = _transitions(DEBIAN_RELEASES)
    retained_pairs = tuple(transition.pair for transition in transitions)
    by_pair = {transition.pair: transition for transition in transitions}
    current_transition = transitions[-1]
    if current_transition.pair.target != CURRENT_DEBIAN_RELEASE.value:
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
        tailscale_auth_key = _tailscale_auth_key(resolver, auth_key_name)
        target, states = _read_entry_states_with_repair(
            db,
            config,
            vm,
            retained_pairs,
            vm_node=vm_node,
            ops_ctx=ops_ctx,
            tailscale_auth_key=tailscale_auth_key,
        )
        incomplete = [pair for pair, state in states.items() if not state.is_complete]
        if len(incomplete) > 1:
            names = ", ".join(pair.dirname for pair in incomplete)
            raise JournalError(f"multiple incomplete Debian upgrade journals require repair: {names}")
        pair = incomplete[0] if incomplete else None
        if pair is not None:
            transition = by_pair[pair]
            return _EntryInspection(transition, pair, None, False, platform_name, tailscale_auth_key)

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
            return _EntryInspection(by_pair[pair], pair, None, False, platform_name, tailscale_auth_key)

        live_release = probe_debian_release(target)
        recorded = _recorded_release(vm)
        if recorded is None:
            _persist_release(db, vm.name, live_release)
            recorded = live_release.value
        elif recorded == live_release.value:
            _persist_release(db, vm.name, live_release)
        elif recorded == current_transition.pair.source and live_release.value == current_transition.pair.target:
            return _EntryInspection(current_transition, None, None, True, platform_name, tailscale_auth_key)
        else:
            raise StateError(
                f"VM '{vm.name}' release disagreement: database={recorded}, guest={live_release.value}",
                entity_kind="vm",
                entity_name=vm.name,
                hint="Repair the disagreement before a release-sensitive operation.",
            )

        if live_release.value == current_transition.pair.target:
            return _EntryInspection(current_transition, None, None, False, platform_name, tailscale_auth_key)
        if live_release.value != current_transition.pair.source:
            raise StateError(
                f"VM '{vm.name}' runs Debian {live_release.value}; only "
                f"{current_transition.pair.source} to {current_transition.pair.target} is supported",
                entity_kind="vm",
                entity_name=vm.name,
                hint=(
                    f"Create a new Debian {current_transition.pair.target} VM and copy the data. "
                    "Multi-hop upgrade is not supported."
                ),
            )
        preflight = _probe(
            db,
            vm,
            target,
            current_transition,
            recorded,
            platform_name=platform_name,
        )
        return _EntryInspection(current_transition, None, preflight, False, platform_name, tailscale_auth_key)


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
    platform_name: str,
    interaction: TtyInteractionPolicy,
) -> None:
    from agentworks.bootstrap import load_request_registry
    from agentworks.transports import transport

    registry = load_request_registry(config, live_database=db)
    with gated_vm_boundary(
        db,
        config,
        registry,
        vm,
        interaction=interaction,
    ):
        target = transport(vm, config)
        journal = RemoteJournal(target)
        journal.install()
        if journal.select_incomplete(tuple(item.pair for item in _transitions(DEBIAN_RELEASES))) is not None:
            raise StateError(
                f"VM '{vm.name}' acquired an upgrade journal while backups were being prepared",
                entity_kind="vm",
                entity_name=vm.name,
                hint="Rerun vm upgrade to inspect the existing journal.",
            )
        fresh = _probe(
            db,
            vm,
            target,
            transition,
            transition.pair.source,
            platform_name=platform_name,
        )
        _require_safe_preflight(fresh, transition)
        plan = fresh.to_plan()
        plan.update(
            {
                "checkpoint": checkpoint,
                "ordinary_backup": str(ordinary_backup),
                "recovery_bundle": str(recovery_bundle),
                "preliminary_material_plan": preflight.material_plan(),
            }
        )
        journal.initialize(transition.pair, plan)
        try:
            _inhibit_apt_timers(target)
            db.insert_vm_event(
                vm.name,
                "debian_upgrade_started",
                json.dumps({"checkpoint": checkpoint, "target": transition.pair.target}, sort_keys=True),
            )
            execution = _execution(target, transition)
            execution.install_script()
        except (Exception, KeyboardInterrupt) as error:
            _restore_timers_or_raise_repair(
                db,
                vm,
                target,
                fresh.apt_timer_states,
                transition,
                error,
            )
            raise
        _advance_source_action(
            db,
            vm,
            journal,
            execution,
            transition,
            target,
            fresh.apt_timer_states,
        )


def _resume_after_source_update(
    db: Database,
    config: Config,
    vm: VMRow,
    transition: _Transition,
    *,
    platform_name: str,
    checkpoint_ref: str | None,
    tailscale_auth_key: str,
    interaction: TtyInteractionPolicy,
) -> None:
    from agentworks.bootstrap import load_request_registry
    from agentworks.transports import transport

    registry = load_request_registry(config, live_database=db)
    with gated_vm_boundary(
        db,
        config,
        registry,
        vm,
        interaction=interaction,
    ):
        target = transport(vm, config)
        journal = RemoteJournal(target)
        journal.install()
        state = journal.load(transition.pair)
        plan = journal.load_plan(transition.pair)
        _require_matching_checkpoint(checkpoint_ref, plan)
        execution = _execution(target, transition)
        execution.install_script()
        if state.last_completed is JournalProgress.PREPARED or state.active_action is UpgradeAction.SOURCE_UPDATE:
            timers = _timer_states_from_plan(plan)
            try:
                _inhibit_apt_timers(target)
            except (Exception, KeyboardInterrupt) as error:
                _restore_timers_or_raise_repair(db, vm, target, timers, transition, error)
                raise
            _advance_source_action(db, vm, journal, execution, transition, target, timers)
            state = journal.load(transition.pair)

        if state.last_completed is JournalProgress.SOURCE_CURRENT and state.active_action is None:
            timer_states = _timer_states_from_plan(plan)
            try:
                _require_interactive_upgrade_authorization()
                final = _probe(
                    db,
                    vm,
                    target,
                    transition,
                    transition.pair.source,
                    platform_name=platform_name,
                )
                _require_safe_preflight(final, transition)
                _render_plan(final, transition, heading="Final Debian suite-switch plan")
                if plan.get("preliminary_material_plan") != final.material_plan():
                    output.warn(
                        "The final package, source, conffile, blocker, or space plan changed after source update."
                    )
                output.warn("VM connectivity will be interrupted during service restarts and reboot.")
                if not output.confirm(
                    f"Switch Debian suites from {transition.pair.source} to {transition.pair.target}?",
                    default=False,
                ):
                    raise UserAbort("VM upgrade paused before suite switching; rerun vm upgrade to continue")
                plan["final_plan"] = final.to_plan()
                journal.update_plan(transition.pair, plan)
                _inhibit_apt_timers(target)
            except (Exception, KeyboardInterrupt) as error:
                _restore_timers_or_raise_repair(db, vm, target, timer_states, transition, error)
                raise

        for action in (
            UpgradeAction.SWITCH_SOURCES,
            UpgradeAction.MINIMAL_UPGRADE,
            UpgradeAction.FULL_UPGRADE,
        ):
            state = journal.load(transition.pair)
            if state.active_action is action or state.next_action is action:
                _advance_action(journal, execution, transition.pair, action)

        state = journal.load(transition.pair)
        if state.last_completed is JournalProgress.FULL_UPGRADE_COMPLETE and state.active_action is None:
            predictions = (
                snapshot_provider_interface_names(target)
                if platform_name == "wsl2"
                else predict_interface_names(target)
            )
            require_stable_interface_names(predictions)
            plan = journal.load_plan(transition.pair)
            plan["interface_predictions"] = predictions
            journal.update_plan(transition.pair, plan)
        checkpoint_value = plan.get("checkpoint")
        checkpoint = checkpoint_value if isinstance(checkpoint_value, str) else None
        if state.active_action is UpgradeAction.REBOOT or state.next_action is UpgradeAction.REBOOT:
            prior_boot_id: str | None = _advance_reboot(journal, execution, transition.pair)
        elif state.last_completed is JournalProgress.REBOOT_COMPLETE:
            prior_boot_id = None
        else:
            raise StateError("upgrade journal did not reach reboot dispatch")

    _finish_after_reboot(
        db,
        config,
        vm,
        transition,
        platform_name=platform_name,
        prior_boot_id=prior_boot_id,
        checkpoint=checkpoint,
        tailscale_auth_key=tailscale_auth_key,
        interaction=interaction,
    )


def _finish_after_reboot(
    db: Database,
    config: Config,
    vm: VMRow,
    transition: _Transition,
    *,
    platform_name: str,
    prior_boot_id: str | None,
    checkpoint: str | None,
    tailscale_auth_key: str,
    interaction: TtyInteractionPolicy,
) -> None:
    """Reconnect and verify inside a fresh post-reboot orchestration span."""
    from agentworks.bootstrap import load_request_registry
    from agentworks.transports import transport

    registry = load_request_registry(config, live_database=db)
    with gated_vm_boundary(
        db,
        config,
        registry,
        vm,
        interaction=interaction,
    ) as (vm_node, _resolver, ops_ctx):
        target = transport(vm, config)
        if prior_boot_id is not None:
            journal = RemoteJournal(target)
            reconnected = _reconnect_after_reboot(
                db,
                config,
                vm,
                target,
                prior_boot_id=prior_boot_id,
                vm_node=vm_node,
                ops_ctx=ops_ctx,
                tailscale_auth_key=tailscale_auth_key,
            )
            if reconnected is None and _observed_boot_id(target) == prior_boot_id:
                output.warn("The guest is still on the pre-reboot boot ID; safely dispatching reboot once more.")
                reboot_state = journal.load(transition.pair)
                _dispatch_reboot_once(journal, transition.pair, reboot_state, prior_boot_id)
                reconnected = _reconnect_after_reboot(
                    db,
                    config,
                    vm,
                    target,
                    prior_boot_id=prior_boot_id,
                    vm_node=vm_node,
                    ops_ctx=ops_ctx,
                    tailscale_auth_key=tailscale_auth_key,
                )
            if reconnected is None:
                db.insert_vm_event(
                    vm.name,
                    "debian_upgrade_repair_required",
                    json.dumps({"checkpoint": checkpoint, "stage": "reconnect"}, sort_keys=True),
                )
                raise StateError(
                    f"VM '{vm.name}' did not reconnect after reboot",
                    entity_kind="vm",
                    entity_name=vm.name,
                    hint="Use the platform console or restore the recorded external checkpoint.",
                )
            target = reconnected
            journal = RemoteJournal(target)
            execution = _execution(target, transition)
            reboot_state = journal.load(transition.pair)
            result = execution.inspect(UpgradeAction.REBOOT, reboot_state)
            if result.disposition.value != "succeeded":
                if reboot_state.active_action is UpgradeAction.REBOOT:
                    assert reboot_state.attempt_id is not None
                    journal.fail(
                        transition.pair,
                        UpgradeAction.REBOOT,
                        reboot_state.attempt_id,
                        "reboot did not change the guest boot ID",
                        repair_required=True,
                    )
                raise StateError(
                    f"VM '{vm.name}' reconnected without proving a completed reboot",
                    entity_kind="vm",
                    entity_name=vm.name,
                )
            if reboot_state.attempt_id is None:
                raise StateError("reboot journal is missing its active attempt identity")
            journal.complete(
                transition.pair,
                UpgradeAction.REBOOT,
                reboot_state.attempt_id,
            )
        else:
            journal = RemoteJournal(target)

        live = probe_debian_release(target, expected=transition.target_profile.release)
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
        _verify_target_health(db, vm, target, transition, platform_name=platform_name)


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
    db: Database,
    vm: VMRow,
    journal: RemoteJournal,
    execution: RemoteUpgradeExecution,
    transition: _Transition,
    target: Transport,
    timer_states: dict[str, tuple[str, str]],
) -> None:
    pair = transition.pair
    try:
        _advance_action(journal, execution, pair, UpgradeAction.SOURCE_UPDATE)
    except StateError:
        try:
            state = journal.load(pair)
        except Exception:
            output.warn("Automatic APT timers remain inhibited because upgrade state could not be inspected.")
        else:
            if state.last_completed is JournalProgress.PREPARED and state.outcome in {
                AttemptOutcome.FAILED,
                AttemptOutcome.REPAIR_REQUIRED,
            }:
                _restore_timers_or_record(db, vm, target, timer_states, transition)
        raise


def _advance_reboot(journal: RemoteJournal, execution: RemoteUpgradeExecution, pair: UpgradePair) -> str:
    state = journal.load(pair)
    if state.active_action is UpgradeAction.REBOOT:
        if state.boot_id_before is None:
            raise StateError("reboot journal is missing the pre-reboot boot ID")
        return state.boot_id_before
    if state.next_action is not UpgradeAction.REBOOT:
        raise StateError("upgrade journal is not ready to reboot")
    boot_id = execution.current_boot_id()
    journal.dispatch_reboot(pair, boot_id)
    return boot_id


def _dispatch_reboot_once(
    journal: RemoteJournal,
    pair: UpgradePair,
    state: JournalState,
    boot_id: str,
) -> None:
    if state.active_action is UpgradeAction.REBOOT:
        if state.attempt_id is None:
            raise StateError("reboot journal is missing its active attempt identity")
        journal.redispatch_reboot(pair, boot_id, state.attempt_id)
        return
    if state.next_action is UpgradeAction.REBOOT:
        journal.dispatch_reboot(pair, boot_id)
        return
    raise StateError("upgrade journal is not ready to retry reboot dispatch")


def _probe(
    db: Database,
    vm: VMRow,
    target: Transport,
    transition: _Transition,
    recorded: str,
    *,
    platform_name: str,
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
    live = probe_debian_release(target).value
    return probe_upgrade_preflight(
        target,
        database_release=recorded,
        live_release=live,
        source_suites=transition.policy.source_suites,
        target_suites=transition.policy.target_suites,
        guest_kernel_required=platform_name != "wsl2",
        minimum_openssh_version=transition.policy.minimum_openssh_version,
        blocker_probe=transition.policy.blocker_probe,
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


def _render_plan(preflight: UpgradePreflight, transition: _Transition, *, heading: str) -> None:
    with output.section(heading):
        output.detail(f"Architecture: {preflight.architecture}; kernel: {preflight.kernel}")
        output.detail(f"Packages apt would remove: {', '.join(preflight.removals) or 'none'}")
        output.detail(f"Third-party sources to disable: {', '.join(preflight.third_party_sources) or 'none'}")
        output.detail(f"Installed non-Debian packages: {', '.join(preflight.non_debian_packages) or 'none'}")
        output.detail(f"Obsolete packages: {', '.join(preflight.obsolete_packages) or 'none'}")
        output.detail(f"Modified conffiles: {', '.join(preflight.modified_conffiles) or 'none'}")
        output.detail(
            f"Estimated download: {preflight.apt_download_bytes} bytes; "
            f"installed growth: {preflight.apt_installed_growth_bytes} bytes"
        )
        output.detail(
            f"Free space: / ({preflight.root_filesystem})={preflight.root_free_bytes}, "
            f"/var ({preflight.var_filesystem})={preflight.var_free_bytes}, "
            f"apt-cache ({preflight.cache_filesystem})={preflight.cache_free_bytes}, "
            f"/boot ({preflight.boot_filesystem})={preflight.boot_free_bytes} bytes"
        )
        output.detail(
            f"Required free space: /={preflight.root_required_bytes}, "
            f"/var={preflight.var_required_bytes}, apt-cache={preflight.cache_required_bytes}, "
            f"/boot={preflight.boot_required_bytes} bytes; shared filesystems are aggregated"
        )
        for url in transition.policy.documentation_urls:
            output.detail(f"Debian release note: {url}")


def _resolve_checkpoint(value: str | None) -> str:
    if value is None:
        output.warn("Create a bootable provider snapshot, WSL export, Proxmox backup, or equivalent before continuing.")
        value = output.prompt("External recovery artifact reference").strip()
    return _validate_checkpoint(value)


def _validate_checkpoint(value: str) -> str:
    if value != value.strip() or not value or len(value) > 512 or "\n" in value or "\r" in value:
        raise ValidationError("checkpoint reference must be a non-blank, bounded single line")
    return value


def _require_interactive_upgrade_authorization() -> None:
    if not output.is_interactive():
        raise ValidationError("vm upgrade requires an interactive terminal at each pending authorization boundary")


def _require_matching_checkpoint(value: str | None, plan: dict[str, object]) -> None:
    if value is None:
        return
    recorded = plan.get("checkpoint")
    if not isinstance(recorded, str):
        raise StateError("Debian upgrade plan has no valid external checkpoint reference")
    if value != recorded:
        raise ValidationError(
            "--checkpoint cannot replace the attested pre-upgrade recovery artifact on resume; "
            "omit it or pass the originally recorded reference"
        )


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
                f"chown {shlex.quote(vm.admin_username)} {q_remote} {shlex.quote(archive)}",
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


def _restore_timers_after_source_safe_failure(
    target: Transport,
    states: dict[str, tuple[str, str]],
    transition: _Transition,
) -> bool:
    """Restore timers only when the source release is provably package-safe."""
    if not _source_state_is_safe_for_timer_restore(target, transition):
        output.warn("Automatic APT timers remain inhibited because package state is not provably safe.")
        return False
    try:
        _restore_apt_timers(target, states)
    except Exception as error:
        output.warn(
            "Automatic APT timer restoration failed after a source-safe abort "
            f"({type(error).__name__}); repair the recorded timer states manually."
        )
        return False
    return True


def _restore_timers_or_record(
    db: Database,
    vm: VMRow,
    target: Transport,
    states: dict[str, tuple[str, str]],
    transition: _Transition,
) -> bool:
    restored = _restore_timers_after_source_safe_failure(target, states, transition)
    if restored:
        return True
    try:
        db.insert_vm_event(
            vm.name,
            "debian_upgrade_repair_required",
            json.dumps(
                {
                    "stage": "source-safe-apt-timer-restore",
                    "target": transition.pair.target,
                },
                sort_keys=True,
            ),
        )
    except Exception as error:
        output.warn(f"Could not record the automatic APT timer repair requirement ({type(error).__name__}).")
    return False


def _restore_timers_or_raise_repair(
    db: Database,
    vm: VMRow,
    target: Transport,
    states: dict[str, tuple[str, str]],
    transition: _Transition,
    error: BaseException,
) -> None:
    if _restore_timers_or_record(db, vm, target, states, transition):
        return
    raise StateError(
        "Automatic APT timer state needs repair before the Debian upgrade can pause",
        entity_kind="vm",
        entity_name=vm.name,
        hint="Restore apt-daily.timer and apt-daily-upgrade.timer to their recorded states.",
    ) from error


def _source_state_is_safe_for_timer_restore(
    target: Transport,
    transition: _Transition,
) -> bool:
    try:
        probe_debian_release(target, expected=DebianRelease(transition.pair.source))
        audit = target.run("dpkg --audit", sudo=True, check=False)
        locks = target.run(NATIVE_PACKAGE_LOCK_COMMAND, sudo=True, check=False)
    except Exception:
        return False
    return audit.ok and not (audit.stdout or "").strip() and locks.returncode == 1


def _restore_apt_timers(target: Transport, states: dict[str, tuple[str, str]]) -> None:
    quoted = {name: shlex.quote(name) for name in states}
    for q_name in quoted.values():
        target.run(f"systemctl stop {q_name}", sudo=True)
    for name, (enabled, _active) in states.items():
        q_name = quoted[name]
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
    for name, (enabled, active) in states.items():
        q_name = quoted[name]
        if active == "active" and enabled not in {"masked", "masked-runtime"}:
            target.run(f"systemctl start {q_name}", sudo=True)
        else:
            target.run(f"systemctl stop {q_name}", sudo=True)
    for name, (enabled, active) in states.items():
        q_name = quoted[name]
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
    *,
    platform_name: str,
) -> None:
    failed = _target_health_failures(
        db,
        vm,
        target,
        platform_name=platform_name,
        target_suites=transition.policy.target_suites,
    )
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


def _target_health_failures(
    db: Database,
    vm: VMRow,
    target: Transport,
    *,
    platform_name: str,
    target_suites: Sequence[str],
) -> list[str]:
    checks = {
        "package database": 'test -z "$(dpkg --audit)"',
        "target package convergence": (
            "simulation=\"$(LC_ALL=C apt-get -s full-upgrade)\" && ! printf '%s\\n' \"$simulation\" | grep -q '^Inst '"
        ),
        "systemd": 'test "$(cat /proc/1/comm)" = systemd',
        "sshd": "systemctl is-active --quiet ssh",
        "Tailscale": "tailscale status --json >/dev/null",
        "admin identity": f"id {shlex.quote(vm.admin_username)} >/dev/null",
        "Agentworks state": "test -d /var/lib/agentworks",
    }
    if platform_name == "wsl2":
        checks["WSL2 provider kernel"] = "uname -r | grep -qi microsoft"
    else:
        checks["running target kernel"] = (
            "meta=; for candidate in linux-image-amd64 linux-image-cloud-amd64 "
            "linux-image-arm64 linux-image-cloud-arm64; do "
            "dpkg-query -W -f='${db:Status-Status}' \"$candidate\" 2>/dev/null | grep -qx installed "
            "&& meta=$candidate && break; done; "
            'test -n "$meta" && '
            'dpkg-query -W -f=\'${Depends}\' "$meta" | grep -Fq "linux-image-$(uname -r)"'
        )
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
    failed = [name for name, command in checks.items() if not target.run(command, sudo=True, check=False).ok]
    if target_source_hygiene_issues(target, target_suites):
        failed.append("target APT sources")
    return failed


def _timer_states_from_plan(plan: dict[str, object]) -> dict[str, tuple[str, str]]:
    value = plan.get("apt_timer_states")
    if not isinstance(value, dict) or set(value) != set(APT_TIMER_UNITS):
        raise StateError("upgrade plan is missing automatic APT timer state")
    result: dict[str, tuple[str, str]] = {}
    for name in APT_TIMER_UNITS:
        state = value[name]
        if not isinstance(state, list) or len(state) != 2:
            raise StateError("upgrade plan has invalid automatic APT timer state")
        enabled, active = state
        if (
            not isinstance(enabled, str)
            or enabled not in APT_TIMER_ENABLE_STATES
            or not isinstance(active, str)
            or active not in APT_TIMER_ACTIVE_STATES
            or (enabled in {"masked", "masked-runtime"} and active == "active")
        ):
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
    platform_name: str,
    interaction: TtyInteractionPolicy,
) -> None:
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
        live = probe_debian_release(target, expected=transition.target_profile.release)
        failed = _target_health_failures(
            db,
            vm,
            target,
            platform_name=platform_name,
            target_suites=transition.policy.target_suites,
        )
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

    try:
        canonical = transport(vm, config)
        return canonical, RemoteJournal(canonical).read_states(retained_pairs)
    except (SSHError, StateError):
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
    transition: _Transition,
) -> RemoteUpgradeExecution:
    return RemoteUpgradeExecution(
        target,
        transition.pair,
        target_version_id=transition.target_profile.version_id,
        target_suites=transition.policy.target_suites,
    )


def _transitions(profiles: Sequence[DebianReleaseProfile]) -> tuple[_Transition, ...]:
    transitions: list[_Transition] = []
    for source, target in zip(profiles, profiles[1:], strict=False):
        policy = target.upgrade_from_previous
        if policy is None:
            raise StateError(f"Debian profile {target.release.value} has no adjacent upgrade policy")
        pair = UpgradePair(source.release.value, target.release.value)
        transitions.append(_Transition(pair, target, policy))
    if not transitions:
        raise StateError("Debian release registry has no adjacent upgrade transition")
    return tuple(transitions)


def _recorded_release(vm: VMRow) -> str | None:
    return None if vm.debian_release is None else vm.debian_release.value


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


def _persist_release(db: Database, name: str, release: DebianRelease) -> None:
    db.update_vm_debian_release(name, release)
