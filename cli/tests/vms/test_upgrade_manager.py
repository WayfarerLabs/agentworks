from __future__ import annotations

import contextlib
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
from agentworks.errors import StateError, ValidationError
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

    def update_vm_debian_release(self, name: str, release: object, *, observed_at: str | None = None) -> None:
        del observed_at
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


def test_proved_target_is_persisted_before_release_aware_reinit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class _UpgradeDatabase(_Database):
        def update_vm_debian_release(
            self,
            name: str,
            release: object,
            *,
            observed_at: str | None = None,
        ) -> None:
            super().update_vm_debian_release(name, release, observed_at=observed_at)
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

        def complete(self, requested: UpgradePair, action: UpgradeAction) -> None:
            assert requested == pair
            assert action is UpgradeAction.REBOOT
            state_holder[0] = state_holder[0].complete_active()

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

    def _dispatch(_journal: object, _execution: object, requested: UpgradePair) -> None:
        assert requested == pair
        state_holder[0] = state_holder[0].claim(UpgradeAction.REBOOT, boot_id_before="old-boot")

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
