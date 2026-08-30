from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agentworks.debian import (
    DEBIAN_RELEASES,
    DebianRelease,
    DebianReleaseProfile,
    DebianUpgradePolicy,
)
from agentworks.errors import StateError, UserAbort, ValidationError
from agentworks.ssh import SSHError
from agentworks.vms.manager import upgrade as upgrade_manager
from agentworks.vms.manager.inspect import _project_vm_event_name
from agentworks.vms.upgrade.journal import (
    AttemptOutcome,
    JournalProgress,
    JournalState,
    UpgradeAction,
    UpgradePair,
)


def _install_release_probe(monkeypatch: pytest.MonkeyPatch, probe: DebianRelease | Exception) -> None:
    def _probe(_target: object, expected: DebianRelease | None = None) -> DebianRelease:
        if isinstance(probe, Exception):
            raise probe
        if expected is not None and probe is not expected:
            raise StateError("unexpected release")
        return probe

    monkeypatch.setattr(upgrade_manager, "probe_debian_release", _probe)


class _Database:
    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []
        self.events: list[tuple[str, str, str | None]] = []

    def update_vm_debian_release(self, name: str, release: object) -> None:
        self.updates.append((name, release))

    def list_vm_events(self, name: str) -> list[object]:
        del name
        return []

    def insert_vm_event(self, name: str, event: str, detail: str | None = None) -> None:
        self.events.append((name, event, detail))


def _vm(release: DebianRelease | None) -> Any:
    return SimpleNamespace(name="box", debian_release=release)


def _install_entry_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    journal_pair: UpgradePair | None,
    completed_current: bool = False,
) -> None:
    @contextlib.contextmanager
    def _boundary(*args: object, **kwargs: object):
        del args, kwargs
        vm_node = SimpleNamespace(site=SimpleNamespace(platform=SimpleNamespace(name="lima")))
        yield vm_node, object(), object()

    monkeypatch.setattr(upgrade_manager, "gated_vm_boundary", _boundary)
    monkeypatch.setattr(upgrade_manager, "_tailscale_auth_key_name", lambda *args: "tailscale-key")
    monkeypatch.setattr(upgrade_manager, "_tailscale_auth_key", lambda *args: "ts-key")

    def _states(*args: object, **kwargs: object) -> tuple[object, dict[UpgradePair, object]]:
        del args, kwargs
        if journal_pair is not None:
            return object(), {journal_pair: SimpleNamespace(is_complete=False)}
        if completed_current:
            pair = UpgradePair("bookworm", "trixie")
            return object(), {pair: SimpleNamespace(is_complete=True)}
        return object(), {}

    monkeypatch.setattr(upgrade_manager, "_read_entry_states_with_repair", _states)
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda *args, **kwargs: object())


def test_incomplete_journal_precedes_live_release_and_new_eligibility(monkeypatch: pytest.MonkeyPatch) -> None:
    pair = UpgradePair("bookworm", "trixie")
    _install_entry_fakes(monkeypatch, journal_pair=pair)
    _install_release_probe(monkeypatch, AssertionError("unexpected probe"))

    entry = upgrade_manager._inspect_entry(_Database(), object(), _vm(DebianRelease.BOOKWORM), interaction=object())

    assert entry.journal_pair == pair
    assert entry.preflight is None


def test_no_journal_refuses_non_previous_release_before_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    class CandidateRelease(StrEnum):
        FORKY = "forky"

    forky = cast("DebianRelease", CandidateRelease.FORKY)
    policy = DebianUpgradePolicy(
        source_suites=("trixie",),
        target_suites=("forky",),
        minimum_openssh_version="candidate",
        documentation_urls=(),
    )
    profiles = (*DEBIAN_RELEASES, DebianReleaseProfile(forky, "14", policy))
    _install_entry_fakes(monkeypatch, journal_pair=None)
    monkeypatch.setattr(upgrade_manager, "DEBIAN_RELEASES", profiles)
    monkeypatch.setattr(upgrade_manager, "CURRENT_DEBIAN_RELEASE", forky)
    _install_release_probe(monkeypatch, DebianRelease.BOOKWORM)
    monkeypatch.setattr(
        upgrade_manager,
        "_probe",
        lambda *args, **kwargs: pytest.fail("legacy release reached upgrade preflight"),
    )

    with pytest.raises(StateError):
        upgrade_manager._inspect_entry(_Database(), object(), _vm(DebianRelease.BOOKWORM), interaction=object())


def test_live_adjacent_target_with_source_row_selects_adoption(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_entry_fakes(monkeypatch, journal_pair=None)
    _install_release_probe(monkeypatch, DebianRelease.TRIXIE)

    entry = upgrade_manager._inspect_entry(_Database(), object(), _vm(DebianRelease.BOOKWORM), interaction=object())

    assert entry.adoption is True
    assert entry.journal_pair is None


def test_completed_journal_without_completion_event_resumes_post_reboot_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = UpgradePair("bookworm", "trixie")
    _install_entry_fakes(monkeypatch, journal_pair=None, completed_current=True)
    _install_release_probe(monkeypatch, DebianRelease.TRIXIE)

    entry = upgrade_manager._inspect_entry(_Database(), object(), _vm(DebianRelease.TRIXIE), interaction=object())

    assert entry.journal_pair == pair
    assert entry.adoption is False


def test_entry_inspection_resolves_the_tailscale_key_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_entry_fakes(monkeypatch, journal_pair=None)
    _install_release_probe(monkeypatch, DebianRelease.TRIXIE)
    resolutions: list[object] = []

    def _resolve(resolver: object, name: str) -> str:
        resolutions.append((resolver, name))
        return "resolved-key"

    monkeypatch.setattr(upgrade_manager, "_tailscale_auth_key", _resolve)

    entry = upgrade_manager._inspect_entry(_Database(), object(), _vm(DebianRelease.TRIXIE), interaction=object())

    assert entry.tailscale_auth_key == "resolved-key"
    assert len(resolutions) == 1


def test_proved_target_is_persisted_before_release_aware_reinit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class _UpgradeDatabase(_Database):
        def update_vm_debian_release(
            self,
            name: str,
            release: object,
        ) -> None:
            super().update_vm_debian_release(name, release)
            calls.append("persist")

        def insert_vm_event(self, name: str, event: str, detail: str | None = None) -> None:
            del name, event, detail

    pair = UpgradePair("bookworm", "trixie")
    transition = SimpleNamespace(pair=pair)
    entry = SimpleNamespace(
        adoption=False,
        journal_pair=pair,
        preflight=None,
        transition=transition,
        platform_name="lima",
        tailscale_auth_key="ts-key",
    )
    vm = SimpleNamespace(name="box", tailscale_host="100.64.0.9")
    db = _UpgradeDatabase()
    monkeypatch.setattr(upgrade_manager, "_require_vm", lambda *args: vm)
    monkeypatch.setattr(upgrade_manager, "_guard_failed_vm", lambda candidate: None)
    monkeypatch.setattr(upgrade_manager, "_inspect_entry", lambda *args, **kwargs: entry)

    def _resume(*args: object, **kwargs: object) -> None:
        del args, kwargs
        upgrade_manager._persist_release(db, "box", DebianRelease.TRIXIE)

    def _reinit(*args: object, **kwargs: object) -> None:
        del args, kwargs
        assert calls == ["persist"]
        calls.append("reinit")

    monkeypatch.setattr(upgrade_manager, "_resume_after_source_update", _resume)
    monkeypatch.setattr(upgrade_manager, "_require_complete_initialization", lambda *args: None)
    monkeypatch.setattr(upgrade_manager, "_restore_completed_timer_state", lambda *args, **kwargs: None)
    monkeypatch.setattr("agentworks.vms.manager.reinit_vm", _reinit)

    result = upgrade_manager.upgrade_vm(db, object(), "box", interaction=object())

    assert result is None
    assert calls == ["persist", "reinit"]


def test_native_route_rejoins_tailscale_after_canonical_reconnect_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = object()
    native = SimpleNamespace(logger=None)
    repaired = object()
    vm = SimpleNamespace(name="box")
    refreshed = SimpleNamespace(name="box")
    platform = object()
    vm_node = SimpleNamespace(site=SimpleNamespace(platform=platform))
    rejoined: list[tuple[object, str]] = []

    def _wait(target: object, **kwargs: object) -> bool:
        del kwargs
        return target is not canonical

    monkeypatch.setattr(upgrade_manager, "_wait_for_strict_reconnect", _wait)
    monkeypatch.setattr(upgrade_manager, "_observed_boot_id", lambda target: None)
    monkeypatch.setattr("agentworks.transports.native_transport", lambda *args, **kwargs: native)
    monkeypatch.setattr("agentworks.transports.transport", lambda *args, **kwargs: repaired)
    monkeypatch.setattr(upgrade_manager, "_require_vm", lambda *args: refreshed)
    monkeypatch.setattr("agentworks.vms.manager.verify_tailscale_available", lambda: None)
    monkeypatch.setattr(
        "agentworks.vms.manager.rejoin_tailscale",
        lambda db, name, target, *, auth_key: rejoined.append((target, auth_key)),
    )

    result = upgrade_manager._reconnect_after_reboot(
        object(),
        object(),
        vm,
        canonical,
        prior_boot_id="old",
        vm_node=vm_node,
        ops_ctx=object(),
        tailscale_auth_key="ts-key",
    )

    assert result is repaired
    assert rejoined == [(native, "ts-key")]


def test_reboot_closes_pre_upgrade_gate_before_fresh_post_reboot_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    pair = UpgradePair("bookworm", "trixie")
    transition = upgrade_manager._transitions(DEBIAN_RELEASES)[-1]
    state = JournalState(
        version=1,
        attempt_id=None,
        last_completed=JournalProgress.FULL_UPGRADE_COMPLETE,
        active_action=None,
        active_started_at=None,
        boot_id_before=None,
        outcome=AttemptOutcome.SUCCEEDED,
        failure=None,
    )
    plan: dict[str, object] = {"interface_predictions": {"eth0": "eth0"}}
    target = object()

    class _Journal:
        def __init__(self, _target: object) -> None:
            pass

        def install(self) -> None:
            pass

        def load(self, requested: UpgradePair) -> JournalState:
            assert requested == pair
            return state_holder[0]

        def load_plan(self, requested: UpgradePair) -> dict[str, object]:
            assert requested == pair
            return plan

        def update_plan(self, requested: UpgradePair, updated: dict[str, object]) -> None:
            assert requested == pair
            plan.update(updated)

        def complete(self, requested: UpgradePair, action: UpgradeAction, attempt_id: str) -> None:
            assert requested == pair
            assert action is UpgradeAction.REBOOT
            state_holder[0] = state_holder[0].complete_active(attempt_id)

    state_holder = [state]

    class _Execution:
        def install_script(self) -> None:
            pass

        def inspect(self, action: UpgradeAction, current: JournalState) -> object:
            assert action is UpgradeAction.REBOOT
            assert current.active_action is UpgradeAction.REBOOT
            return SimpleNamespace(disposition=SimpleNamespace(value="succeeded"))

    @contextlib.contextmanager
    def _boundary(*args: object, **kwargs: object):
        del args, kwargs
        number = sum(item.startswith("enter") for item in events) + 1
        events.append(f"enter{number}")
        yield SimpleNamespace(site=SimpleNamespace(platform=object())), object(), object()
        events.append(f"exit{number}")

    def _dispatch(_journal: object, _execution: object, requested: UpgradePair) -> str:
        assert requested == pair
        state_holder[0] = state_holder[0].claim(UpgradeAction.REBOOT, boot_id_before="old-boot")
        return "old-boot"

    def _reconnect(*args: object, **kwargs: object) -> object:
        del args, kwargs
        assert events == ["enter1", "exit1", "enter2"]
        return target

    db = _Database()
    vm = SimpleNamespace(name="box", admin_username="operator")
    monkeypatch.setattr(upgrade_manager, "gated_vm_boundary", _boundary)
    monkeypatch.setattr(upgrade_manager, "RemoteJournal", _Journal)
    monkeypatch.setattr(upgrade_manager, "_execution", lambda *args: _Execution())
    monkeypatch.setattr(upgrade_manager, "_tailscale_auth_key_name", lambda *args: "tailscale-key")
    monkeypatch.setattr(upgrade_manager, "_advance_reboot", _dispatch)
    monkeypatch.setattr(upgrade_manager, "_reconnect_after_reboot", _reconnect)
    monkeypatch.setattr(upgrade_manager, "predict_interface_names", lambda target: {"eth0": "eth0"})
    monkeypatch.setattr(upgrade_manager, "require_stable_interface_names", lambda predictions: None)
    monkeypatch.setattr(upgrade_manager, "verify_interface_names", lambda *args: None)
    monkeypatch.setattr(upgrade_manager, "_verify_target_health", lambda *args, **kwargs: None)
    _install_release_probe(monkeypatch, DebianRelease.TRIXIE)
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda *args, **kwargs: object())
    monkeypatch.setattr("agentworks.transports.transport", lambda *args, **kwargs: target)

    result = upgrade_manager._resume_after_source_update(
        db,
        object(),
        vm,
        transition,
        platform_name="lima",
        checkpoint_ref=None,
        tailscale_auth_key="ts-key",
        interaction=object(),
    )

    assert result is None
    assert events == ["enter1", "exit1", "enter2", "exit2"]
    assert state_holder[0].last_completed is JournalProgress.REBOOT_COMPLETE


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class _PackageStateTarget:
    def __init__(self, *, audit: str = "", lock_returncode: int = 1) -> None:
        self.audit = audit
        self.lock_returncode = lock_returncode

    def run(self, command: str, **kwargs: object) -> _CommandResult:
        del kwargs
        if command == "dpkg --audit":
            return _CommandResult(0, self.audit)
        if "fuser /var/lib/dpkg/lock" in command:
            return _CommandResult(self.lock_returncode)
        raise AssertionError(command)


def test_source_safe_abort_restores_timers_only_after_package_health_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transition = upgrade_manager._transitions(DEBIAN_RELEASES)[-1]
    restored: list[object] = []
    _install_release_probe(monkeypatch, DebianRelease.BOOKWORM)
    monkeypatch.setattr(upgrade_manager, "_restore_apt_timers", lambda target, states: restored.append(target))

    unsafe = _PackageStateTarget(audit="half-configured package\n")
    assert upgrade_manager._restore_timers_after_source_safe_failure(unsafe, {}, transition) is False
    assert restored == []

    safe = _PackageStateTarget()
    assert upgrade_manager._restore_timers_after_source_safe_failure(safe, {}, transition) is True
    assert restored == [safe]


def test_source_safe_abort_keeps_timers_inhibited_when_native_lock_is_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transition = upgrade_manager._transitions(DEBIAN_RELEASES)[-1]
    _install_release_probe(monkeypatch, DebianRelease.BOOKWORM)
    monkeypatch.setattr(
        upgrade_manager,
        "_restore_apt_timers",
        lambda *args: pytest.fail("busy package state restored automatic timers"),
    )
    assert (
        upgrade_manager._restore_timers_after_source_safe_failure(
            _PackageStateTarget(lock_returncode=0),
            {},
            transition,
        )
        is False
    )


def test_failed_source_safe_timer_restore_records_durable_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transition = upgrade_manager._transitions(DEBIAN_RELEASES)[-1]
    db = _Database()
    vm = SimpleNamespace(name="box")
    _install_release_probe(monkeypatch, DebianRelease.BOOKWORM)
    monkeypatch.setattr(
        upgrade_manager,
        "_restore_apt_timers",
        lambda *args: (_ for _ in ()).throw(StateError("restore failed")),
    )

    restored = upgrade_manager._restore_timers_or_record(
        db,
        vm,
        _PackageStateTarget(),
        {},
        transition,
    )

    assert restored is False
    assert len(db.events) == 1
    assert db.events[0][1] == "debian_upgrade_repair_required"
    assert json.loads(db.events[0][2] or "null")["stage"] == "source-safe-apt-timer-restore"


def test_timer_restore_keeps_all_timers_stopped_until_configuration_succeeds() -> None:
    commands: list[str] = []

    class _Target:
        def run(self, command: str, **kwargs: object) -> _CommandResult:
            del kwargs
            commands.append(command)
            if command == "systemctl unmask apt-daily-upgrade.timer":
                raise StateError("configuration failed")
            return _CommandResult(0)

    with pytest.raises(StateError):
        upgrade_manager._restore_apt_timers(
            _Target(),
            {
                "apt-daily.timer": ("enabled", "active"),
                "apt-daily-upgrade.timer": ("enabled", "active"),
            },
        )

    assert not any(command.startswith("systemctl start ") for command in commands)


@pytest.mark.parametrize(
    ("failure_stage", "restoration_proved"),
    (
        ("decline", False),
        ("prompt", True),
        ("plan", True),
        ("keyboard", True),
        ("keyboard-unproved", False),
    ),
)
def test_source_safe_planning_exit_restores_timers_or_records_repair(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    restoration_proved: bool,
) -> None:
    transition = upgrade_manager._transitions(DEBIAN_RELEASES)[-1]
    state = JournalState(
        version=1,
        attempt_id=None,
        last_completed=JournalProgress.SOURCE_CURRENT,
        active_action=None,
        active_started_at=None,
        boot_id_before=None,
        outcome=AttemptOutcome.SUCCEEDED,
        failure=None,
    )
    plan: dict[str, object] = {
        "apt_timer_states": {
            "apt-daily.timer": ["enabled", "active"],
            "apt-daily-upgrade.timer": ["enabled", "active"],
        },
        "preliminary_material_plan": {},
    }
    target = object()
    original_error: BaseException | None = None
    if failure_stage == "prompt":
        original_error = UserAbort("interrupted")
    elif failure_stage == "plan":
        original_error = StateError("plan update failed")
    elif failure_stage.startswith("keyboard"):
        original_error = KeyboardInterrupt("interrupted")

    class _Journal:
        def __init__(self, candidate: object) -> None:
            assert candidate is target

        def install(self) -> None:
            pass

        def load(self, pair: UpgradePair) -> JournalState:
            assert pair == transition.pair
            return state

        def load_plan(self, pair: UpgradePair) -> dict[str, object]:
            assert pair == transition.pair
            return plan

        def update_plan(self, pair: UpgradePair, updated: dict[str, object]) -> None:
            assert pair == transition.pair
            assert updated is plan
            assert original_error is not None
            raise original_error

    class _Execution:
        def install_script(self) -> None:
            pass

    @contextlib.contextmanager
    def _boundary(*args: object, **kwargs: object):
        del args, kwargs
        yield object(), object(), object()

    db = _Database()
    vm = SimpleNamespace(name="box")
    final = SimpleNamespace(material_plan=lambda: {}, to_plan=lambda: {})
    restore_attempts: list[object] = []

    def _probe(*args: object, **kwargs: object) -> object:
        del args, kwargs
        if failure_stage.startswith("keyboard"):
            assert original_error is not None
            raise original_error
        return final

    def _restore(*args: object) -> bool:
        restore_attempts.append(args)
        return restoration_proved

    def _confirm(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        if failure_stage == "prompt":
            assert original_error is not None
            raise original_error
        return failure_stage == "plan"

    monkeypatch.setattr(upgrade_manager, "gated_vm_boundary", _boundary)
    monkeypatch.setattr(upgrade_manager, "RemoteJournal", _Journal)
    monkeypatch.setattr(upgrade_manager, "_execution", lambda *args: _Execution())
    monkeypatch.setattr(upgrade_manager, "_tailscale_auth_key_name", lambda *args: "tailscale-key")
    monkeypatch.setattr(upgrade_manager, "_probe", _probe)
    monkeypatch.setattr(upgrade_manager, "_require_safe_preflight", lambda *args: None)
    monkeypatch.setattr(upgrade_manager, "_render_plan", lambda *args, **kwargs: None)
    monkeypatch.setattr(upgrade_manager, "_restore_timers_after_source_safe_failure", _restore)
    monkeypatch.setattr("agentworks.output.confirm", _confirm)
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda *args, **kwargs: object())
    monkeypatch.setattr("agentworks.transports.transport", lambda *args, **kwargs: target)

    expected_error: type[BaseException]
    if failure_stage == "prompt":
        expected_error = UserAbort
    elif failure_stage == "keyboard" and restoration_proved:
        expected_error = KeyboardInterrupt
    else:
        expected_error = StateError
    with pytest.raises(expected_error) as raised:
        upgrade_manager._resume_after_source_update(
            db,
            object(),
            vm,
            transition,
            platform_name="lima",
            checkpoint_ref=None,
            tailscale_auth_key="ts-key",
            interaction=object(),
        )

    assert len(restore_attempts) == 1
    if restoration_proved:
        assert raised.value is original_error
        assert db.events == []
    else:
        assert len(db.events) == 1
        assert db.events[0][1] == "debian_upgrade_repair_required"
        assert json.loads(db.events[0][2] or "null")["stage"] == "source-safe-apt-timer-restore"


def test_timer_plan_state_requires_exact_owned_units_and_valid_states() -> None:
    valid: dict[str, object] = {
        "apt_timer_states": {
            "apt-daily.timer": ["enabled", "active"],
            "apt-daily-upgrade.timer": ["disabled", "inactive"],
        }
    }
    assert upgrade_manager._timer_states_from_plan(valid) == {
        "apt-daily.timer": ("enabled", "active"),
        "apt-daily-upgrade.timer": ("disabled", "inactive"),
    }

    invalid_values = (
        {"apt-daily.timer": ["enabled", "active"]},
        {
            "apt-daily.timer": ["enabled", "active"],
            "apt-daily-upgrade.timer": ["disabled", "inactive"],
            "ssh.service": ["enabled", "active"],
        },
        {
            "apt-daily.timer": ["unknown", "active"],
            "apt-daily-upgrade.timer": ["disabled", "inactive"],
        },
        {
            "apt-daily.timer": ["enabled", "activating"],
            "apt-daily-upgrade.timer": ["disabled", "inactive"],
        },
        {
            "apt-daily.timer": ["masked", "active"],
            "apt-daily-upgrade.timer": ["disabled", "inactive"],
        },
    )
    for states in invalid_values:
        with pytest.raises(StateError):
            upgrade_manager._timer_states_from_plan({"apt_timer_states": states})


def test_wsl_target_health_uses_provider_kernel_instead_of_guest_kernel_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[str] = []

    class _Target:
        def run(self, command: str, **kwargs: object) -> _CommandResult:
            del kwargs
            commands.append(command)
            return _CommandResult(0)

    db = SimpleNamespace(list_agents=lambda **kwargs: [], list_workspaces=lambda **kwargs: [])
    vm = SimpleNamespace(name="box", admin_username="operator")
    monkeypatch.setattr(upgrade_manager, "target_source_hygiene_issues", lambda *args: ())

    failed = upgrade_manager._target_health_failures(
        db,
        vm,
        _Target(),
        platform_name="wsl2",
        target_suites=("trixie",),
    )

    assert failed == []
    assert any("grep -qi microsoft" in command for command in commands)
    assert not any("linux-image-$(uname -r)" in command for command in commands)


def test_unreachable_post_reboot_guest_preserves_remote_journal_and_records_local_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = object()
    db = _Database()
    vm = SimpleNamespace(name="box")
    transition = upgrade_manager._transitions(DEBIAN_RELEASES)[-1]

    class _Journal:
        def __init__(self, candidate: object) -> None:
            assert candidate is target

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"unreachable guest journal method called: {name}")

    @contextlib.contextmanager
    def _boundary(*args: object, **kwargs: object):
        del args, kwargs
        node = SimpleNamespace(site=SimpleNamespace(platform=object()))
        yield node, object(), object()

    monkeypatch.setattr(upgrade_manager, "gated_vm_boundary", _boundary)
    monkeypatch.setattr(upgrade_manager, "RemoteJournal", _Journal)
    monkeypatch.setattr(upgrade_manager, "_reconnect_after_reboot", lambda *args, **kwargs: None)
    monkeypatch.setattr(upgrade_manager, "_observed_boot_id", lambda *args: None)
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda *args, **kwargs: object())
    monkeypatch.setattr("agentworks.transports.transport", lambda *args, **kwargs: target)

    with pytest.raises(StateError):
        upgrade_manager._finish_after_reboot(
            db,
            object(),
            vm,
            transition,
            platform_name="lima",
            prior_boot_id="old-boot",
            checkpoint="snapshot-1",
            tailscale_auth_key="ts-key",
            interaction=object(),
        )

    assert db.events == [
        (
            "box",
            "debian_upgrade_repair_required",
            '{"checkpoint": "snapshot-1", "stage": "reconnect"}',
        )
    ]


def test_recovery_bundle_staging_is_traversable_by_admin_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands: list[str] = []

    class _Target:
        def run(self, command: str, **kwargs: object) -> _CommandResult:
            del kwargs
            commands.append(command)
            return _CommandResult(0)

        def copy_from(self, source: str, destination: Path) -> None:
            del source
            destination.write_bytes(b"bundle")

    target = _Target()

    @contextlib.contextmanager
    def _boundary(*args: object, **kwargs: object):
        del args, kwargs
        yield object(), object(), object()

    monkeypatch.setattr(upgrade_manager, "gated_vm_boundary", _boundary)
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda *args, **kwargs: object())
    monkeypatch.setattr("agentworks.transports.transport", lambda *args, **kwargs: target)
    transition = upgrade_manager._transitions(DEBIAN_RELEASES)[-1]
    preflight = SimpleNamespace(to_plan=lambda: {})
    vm = SimpleNamespace(name="box", admin_username="operator")
    config = SimpleNamespace(paths=SimpleNamespace(backups=tmp_path))

    destination = upgrade_manager._create_recovery_bundle(
        object(),
        config,
        vm,
        transition,
        preflight,
        "snapshot-1",
        interaction=object(),
    )

    assert destination.read_bytes() == b"bundle"
    chown = next(command for command in commands if command.startswith("chown operator "))
    assert "agentworks-debian-recovery-" in chown
    assert "debian-recovery.tar.gz" in chown


def test_fresh_resume_repairs_canonical_route_only_after_native_scan_finds_incomplete_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = UpgradePair("bookworm", "trixie")
    canonical = object()
    native = SimpleNamespace(logger=None)
    repaired = object()
    vm = SimpleNamespace(name="box")
    refreshed = SimpleNamespace(name="box")
    vm_node = SimpleNamespace(site=SimpleNamespace(platform=object()))
    rejoined: list[str] = []
    transport_calls = iter((canonical, repaired))

    class _Journal:
        def __init__(self, target: object) -> None:
            self.target = target

        def read_states(self, retained: object) -> dict[UpgradePair, object]:
            del retained
            if self.target is canonical:
                raise SSHError("canonical down")
            return {pair: SimpleNamespace(is_complete=False)}

    monkeypatch.setattr(upgrade_manager, "RemoteJournal", _Journal)
    monkeypatch.setattr("agentworks.transports.transport", lambda *args, **kwargs: next(transport_calls))
    monkeypatch.setattr("agentworks.transports.native_transport", lambda *args, **kwargs: native)
    monkeypatch.setattr(upgrade_manager, "_require_vm", lambda *args: refreshed)
    monkeypatch.setattr(upgrade_manager, "_wait_for_stable_route", lambda *args, **kwargs: True)
    monkeypatch.setattr("agentworks.vms.manager.verify_tailscale_available", lambda: None)
    monkeypatch.setattr(
        "agentworks.vms.manager.rejoin_tailscale",
        lambda db, name, target, *, auth_key: rejoined.append(auth_key),
    )

    target, states = upgrade_manager._read_entry_states_with_repair(
        object(),
        object(),
        vm,
        (pair,),
        vm_node=vm_node,
        ops_ctx=object(),
        tailscale_auth_key="ts-key",
    )

    assert target is repaired
    assert states[pair].is_complete is False
    assert rejoined == ["ts-key"]


def test_missing_canonical_host_still_scans_incomplete_journal_over_native_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = UpgradePair("bookworm", "trixie")
    native = SimpleNamespace(logger=None)
    repaired = object()
    vm = SimpleNamespace(name="box", tailscale_host=None)
    refreshed = SimpleNamespace(name="box", tailscale_host="100.64.0.8")
    vm_node = SimpleNamespace(site=SimpleNamespace(platform=object()))

    class _Journal:
        def __init__(self, target: object) -> None:
            self.target = target

        def read_states(self, retained: object) -> dict[UpgradePair, object]:
            del retained
            assert self.target is native or self.target is repaired
            return {pair: SimpleNamespace(is_complete=False)}

    def _transport(candidate: object, config: object) -> object:
        del config
        if candidate is vm:
            raise StateError("no canonical host")
        assert candidate is refreshed
        return repaired

    monkeypatch.setattr(upgrade_manager, "RemoteJournal", _Journal)
    monkeypatch.setattr("agentworks.transports.transport", _transport)
    monkeypatch.setattr("agentworks.transports.native_transport", lambda *args, **kwargs: native)
    monkeypatch.setattr(upgrade_manager, "_require_vm", lambda *args: refreshed)
    monkeypatch.setattr(upgrade_manager, "_wait_for_stable_route", lambda *args, **kwargs: True)
    monkeypatch.setattr("agentworks.vms.manager.verify_tailscale_available", lambda: None)
    monkeypatch.setattr("agentworks.vms.manager.rejoin_tailscale", lambda *args, **kwargs: None)

    target, states = upgrade_manager._read_entry_states_with_repair(
        object(),
        object(),
        vm,
        (pair,),
        vm_node=vm_node,
        ops_ctx=object(),
        tailscale_auth_key="ts-key",
    )

    assert target is repaired
    assert states[pair].is_complete is False


def test_canonical_loss_without_incomplete_journal_does_not_mutate_tailscale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = object()
    native = SimpleNamespace(logger=None)
    vm = SimpleNamespace(name="box")
    vm_node = SimpleNamespace(site=SimpleNamespace(platform=object()))

    class _Journal:
        def __init__(self, target: object) -> None:
            self.target = target

        def read_states(self, retained: object) -> dict[UpgradePair, object]:
            del retained
            if self.target is canonical:
                raise SSHError("canonical down")
            return {}

    monkeypatch.setattr(upgrade_manager, "RemoteJournal", _Journal)
    monkeypatch.setattr("agentworks.transports.transport", lambda *args, **kwargs: canonical)
    monkeypatch.setattr("agentworks.transports.native_transport", lambda *args, **kwargs: native)
    monkeypatch.setattr(
        "agentworks.vms.manager.rejoin_tailscale",
        lambda *args, **kwargs: pytest.fail("new-upgrade entry attempted a Tailscale mutation"),
    )

    with pytest.raises(StateError):
        upgrade_manager._read_entry_states_with_repair(
            object(),
            object(),
            vm,
            (UpgradePair("bookworm", "trixie"),),
            vm_node=vm_node,
            ops_ctx=object(),
            tailscale_auth_key="ts-key",
        )


def test_adoption_keeps_persisted_target_and_records_repair_when_reinit_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextlib.contextmanager
    def _boundary(*args: object, **kwargs: object):
        del args, kwargs
        yield object(), object(), object()

    transition = upgrade_manager._transitions(DEBIAN_RELEASES)[-1]
    vm = SimpleNamespace(name="box")
    db = _Database()
    monkeypatch.setattr("agentworks.output.confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(upgrade_manager, "gated_vm_boundary", _boundary)
    monkeypatch.setattr(upgrade_manager, "_target_health_failures", lambda *args, **kwargs: [])
    _install_release_probe(monkeypatch, DebianRelease.TRIXIE)
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda *args, **kwargs: object())
    monkeypatch.setattr("agentworks.transports.transport", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        "agentworks.vms.manager.reinit_vm",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("init failed")),
    )

    with pytest.raises(StateError):
        upgrade_manager._adopt_external_upgrade(
            db,
            object(),
            vm,
            transition,
            platform_name="lima",
            interaction=object(),
        )

    assert db.updates == [("box", "trixie")]
    assert db.events[-1][1] == "debian_upgrade_repair_required"


@pytest.mark.parametrize("value", ["", "  ", " leading", "trailing ", "line\nbreak", "x" * 513])
def test_checkpoint_boundary_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValidationError):
        upgrade_manager._resolve_checkpoint(value)


def test_resume_checkpoint_must_match_the_attested_plan_reference() -> None:
    plan: dict[str, object] = {"checkpoint": "snapshot-before-upgrade"}

    upgrade_manager._require_matching_checkpoint(None, plan)
    upgrade_manager._require_matching_checkpoint("snapshot-before-upgrade", plan)
    with pytest.raises(ValidationError):
        upgrade_manager._require_matching_checkpoint("snapshot-after-mutation", plan)


def test_reboot_dispatch_has_no_guest_read_after_dispatch() -> None:
    pair = UpgradePair("bookworm", "trixie")
    state = JournalState(
        version=1,
        attempt_id=None,
        last_completed=JournalProgress.FULL_UPGRADE_COMPLETE,
        active_action=None,
        active_started_at=None,
        boot_id_before=None,
        outcome=AttemptOutcome.SUCCEEDED,
        failure=None,
    )
    calls: list[str] = []

    class _Journal:
        def load(self, requested: UpgradePair) -> JournalState:
            assert requested == pair
            calls.append("load")
            return state

        def dispatch_reboot(self, requested: UpgradePair, boot_id: str) -> None:
            assert requested == pair
            assert boot_id == "boot-before"
            calls.append("dispatch")

    class _Execution:
        def current_boot_id(self) -> str:
            calls.append("boot-id")
            return "boot-before"

    result = upgrade_manager._advance_reboot(_Journal(), _Execution(), pair)  # type: ignore[arg-type]

    assert result == "boot-before"
    assert calls == ["load", "boot-id", "dispatch"]


@pytest.mark.parametrize(
    "event",
    [
        "debian_upgrade_started",
        "debian_upgrade_complete",
        "debian_upgrade_adopted",
        "debian_upgrade_repair_required",
    ],
)
def test_upgrade_events_survive_json_projection(event: str) -> None:
    assert _project_vm_event_name(event) == event
