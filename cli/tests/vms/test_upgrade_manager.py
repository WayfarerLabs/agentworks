from __future__ import annotations

import contextlib
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.errors import StateError, ValidationError
from agentworks.ssh import SSHError
from agentworks.vms.manager import upgrade as upgrade_manager
from agentworks.vms.manager.inspect import _project_vm_event_name
from agentworks.vms.manager.upgrade import UpgradeResult
from agentworks.vms.upgrade import UpgradePair


@dataclass(frozen=True)
class _Policy:
    source: str
    target: str
    source_suites: tuple[str, ...]
    target_suites: tuple[str, ...]
    minimum_openssh_version: str
    documentation_urls: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Profile:
    release: str
    version_id: str
    upgrade_from_previous: _Policy | None = None


def _debian(*, probe: str | Exception) -> Any:
    policy = _Policy(
        source="bookworm",
        target="trixie",
        source_suites=("bookworm",),
        target_suites=("trixie",),
        minimum_openssh_version="1:9.2p1",
    )

    def _probe(_target: object, expected: str | None = None) -> str:
        del expected
        if isinstance(probe, Exception):
            raise probe
        return probe

    return SimpleNamespace(
        DEBIAN_RELEASES=(
            _Profile("bookworm", "12"),
            _Profile("trixie", "13", policy),
        ),
        CURRENT_DEBIAN_RELEASE="trixie",
        probe_debian_release=_probe,
    )


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


def _vm(release: str | None) -> Any:
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
    monkeypatch.setattr(upgrade_manager, "_debian", lambda: _debian(probe=AssertionError("unexpected probe")))

    entry = upgrade_manager._inspect_entry(_Database(), object(), _vm("bookworm"), interaction=object())

    assert entry.journal_pair == pair
    assert entry.preflight is None


def test_no_journal_refuses_non_previous_release_before_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_entry_fakes(monkeypatch, journal_pair=None)
    monkeypatch.setattr(upgrade_manager, "_debian", lambda: _debian(probe="bullseye"))
    monkeypatch.setattr(
        upgrade_manager,
        "_probe",
        lambda *args, **kwargs: pytest.fail("legacy release reached upgrade preflight"),
    )

    with pytest.raises(StateError):
        upgrade_manager._inspect_entry(_Database(), object(), _vm("bullseye"), interaction=object())


def test_live_adjacent_target_with_source_row_selects_adoption(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_entry_fakes(monkeypatch, journal_pair=None)
    monkeypatch.setattr(upgrade_manager, "_debian", lambda: _debian(probe="trixie"))

    entry = upgrade_manager._inspect_entry(_Database(), object(), _vm("bookworm"), interaction=object())

    assert entry.adoption is True
    assert entry.journal_pair is None


def test_completed_journal_without_completion_event_resumes_post_reboot_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = UpgradePair("bookworm", "trixie")
    _install_entry_fakes(monkeypatch, journal_pair=None, completed_current=True)
    monkeypatch.setattr(upgrade_manager, "_debian", lambda: _debian(probe="trixie"))

    entry = upgrade_manager._inspect_entry(_Database(), object(), _vm("trixie"), interaction=object())

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
    entry = SimpleNamespace(adoption=False, journal_pair=pair, preflight=None, transition=transition)
    vm = SimpleNamespace(name="box", tailscale_host="100.64.0.9")
    db = _UpgradeDatabase()
    monkeypatch.setattr(upgrade_manager, "_require_vm", lambda *args: vm)
    monkeypatch.setattr(upgrade_manager, "_guard_failed_vm", lambda candidate: None)
    monkeypatch.setattr(upgrade_manager, "_inspect_entry", lambda *args, **kwargs: entry)

    def _resume(*args: object, **kwargs: object) -> UpgradeResult:
        del args, kwargs
        upgrade_manager._persist_release(db, "box", "trixie")
        return UpgradeResult("box", "bookworm", "trixie", "complete")

    def _reinit(*args: object, **kwargs: object) -> None:
        del args, kwargs
        assert calls == ["persist"]
        calls.append("reinit")

    monkeypatch.setattr(upgrade_manager, "_resume_after_source_update", _resume)
    monkeypatch.setattr(upgrade_manager, "_require_complete_initialization", lambda *args: None)
    monkeypatch.setattr(upgrade_manager, "_restore_completed_timer_state", lambda *args, **kwargs: None)
    monkeypatch.setattr("agentworks.vms.manager.reinit_vm", _reinit)

    result = upgrade_manager.upgrade_vm(db, object(), "box", interaction=object())

    assert result.status == "complete"
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

    target_profile = SimpleNamespace(release="trixie")
    transition = SimpleNamespace(pair=UpgradePair("bookworm", "trixie"), target_profile=target_profile)
    vm = SimpleNamespace(name="box")
    db = _Database()
    monkeypatch.setattr(upgrade_manager.output, "confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(upgrade_manager, "gated_vm_boundary", _boundary)
    monkeypatch.setattr(upgrade_manager, "_target_health_failures", lambda *args: [])
    monkeypatch.setattr(upgrade_manager, "_debian", lambda: _debian(probe="trixie"))
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda *args, **kwargs: object())
    monkeypatch.setattr("agentworks.transports.transport", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        "agentworks.vms.manager.reinit_vm",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("init failed")),
    )

    with pytest.raises(StateError):
        upgrade_manager._adopt_external_upgrade(db, object(), vm, transition, interaction=object())

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
