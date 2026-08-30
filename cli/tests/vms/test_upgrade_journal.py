from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from agentworks.vms.upgrade import journal as journal_module
from agentworks.vms.upgrade.engine import (
    ActionDisposition,
    UpgradeActionError,
    UpgradeEngine,
)
from agentworks.vms.upgrade.journal import (
    AttemptOutcome,
    JournalError,
    JournalProgress,
    JournalState,
    JournalStore,
    UpgradeAction,
    UpgradePair,
)
from agentworks.vms.upgrade.remote import RemoteJournal


@dataclass(frozen=True)
class _Result:
    disposition: ActionDisposition
    detail: str | None = None


class _Execution:
    def __init__(
        self,
        *,
        inspected: ActionDisposition = ActionDisposition.RUNNING,
        started: ActionDisposition = ActionDisposition.RUNNING,
    ) -> None:
        self.inspected = inspected
        self.started = started
        self.inspections: list[UpgradeAction] = []
        self.starts: list[tuple[UpgradeAction, str]] = []

    def inspect(self, action: UpgradeAction, state: JournalState) -> _Result:
        self.inspections.append(action)
        return _Result(self.inspected)

    def start(self, action: UpgradeAction, attempt_id: str) -> _Result:
        self.starts.append((action, attempt_id))
        return _Result(self.started)

    def wait(self, action: UpgradeAction, state: JournalState) -> _Result:
        return _Result(self.started)


class _StoreJournal:
    """Test-owned adapter for exercising the coordinator against a local store."""

    def __init__(self, store: JournalStore) -> None:
        self.store = store

    def load(self, pair: UpgradePair) -> JournalState:
        return self.store.load(pair)

    def claim(self, pair: UpgradePair, action: UpgradeAction) -> JournalState:
        with self.store.locked(pair):
            state = self.store.load(pair).claim(action)
            self.store.write_state(pair, state)
            return state

    def complete(self, pair: UpgradePair, action: UpgradeAction, attempt_id: str) -> JournalState:
        with self.store.locked(pair):
            state = self.store.load(pair)
            if state.active_action is not action:
                raise UpgradeActionError(action, "completion mismatch", repair_required=True)
            state = state.complete_active(attempt_id)
            self.store.write_state(pair, state)
            return state

    def fail(
        self,
        pair: UpgradePair,
        action: UpgradeAction,
        attempt_id: str,
        detail: str,
        *,
        repair_required: bool,
    ) -> JournalState:
        with self.store.locked(pair):
            state = self.store.load(pair)
            if state.active_action is not action:
                raise UpgradeActionError(action, "failure mismatch", repair_required=True)
            state = state.fail_active(attempt_id, detail, repair_required=repair_required)
            self.store.write_state(pair, state)
            return state

    def retry(self, pair: UpgradePair, attempt_id: str) -> JournalState:
        with self.store.locked(pair):
            state = self.store.load(pair).retry_active(attempt_id)
            self.store.write_state(pair, state)
            return state


@pytest.fixture
def pair() -> UpgradePair:
    return UpgradePair("bookworm", "trixie")


def test_journal_pair_is_owned_only_by_validated_directory(tmp_path, pair: UpgradePair) -> None:
    store = JournalStore(tmp_path)
    store.initialize(pair, {"removals": [], "checkpoint": "snapshot-17"})

    assert store.path_for(pair).name == "bookworm-to-trixie"
    assert set(store.load_plan(pair)) == {"removals", "checkpoint"}
    state_payload = json.loads((store.path_for(pair) / "state.json").read_text())
    assert "source" not in state_payload
    assert "target" not in state_payload
    assert (store.path_for(pair).stat().st_mode & 0o777) == 0o700
    assert ((store.path_for(pair) / "state.json").stat().st_mode & 0o777) == 0o600


def test_persisted_state_rejects_partial_attempt_fields(tmp_path, pair: UpgradePair) -> None:
    store = JournalStore(tmp_path)
    store.initialize(pair, {})
    path = store.path_for(pair) / "state.json"
    payload = json.loads(path.read_text())
    payload["attempt_id"] = "attempt"
    path.write_text(json.dumps(payload))

    with pytest.raises(JournalError):
        store.load(pair)


def test_multiple_incomplete_remote_journals_fail_before_selection() -> None:
    first = UpgradePair("bookworm", "trixie")
    second = UpgradePair("trixie", "forky")

    class _Target:
        def copy_to(self, source: Path, destination: str) -> None:
            del source, destination

        def run(self, command: str, **kwargs: object) -> object:
            del command, kwargs
            inventory = {
                first.dirname: JournalState.prepared().to_mapping(),
                second.dirname: JournalState.prepared().to_mapping(),
            }
            return type(
                "Result",
                (),
                {"ok": True, "stdout": json.dumps(inventory), "stderr": ""},
            )()

    with pytest.raises(JournalError):
        RemoteJournal(_Target()).select_incomplete((first, second))  # type: ignore[arg-type]


def test_completed_historic_journal_does_not_block_new_pair(tmp_path) -> None:
    first = UpgradePair("bookworm", "trixie")
    second = UpgradePair("trixie", "forky")
    store = JournalStore(tmp_path)
    store.initialize(first, {})
    state = store.load(first)
    for action in UpgradeAction:
        boot_id = "boot-a" if action is UpgradeAction.REBOOT else None
        state = state.claim(action, boot_id_before=boot_id)
        assert state.attempt_id is not None
        state = state.complete_active(state.attempt_id)
    store.write_state(first, state)
    store.initialize(second, {})

    assert store.scan_incomplete((first, second)) == [second]
    assert set(store.read_states((first, second))) == {first, second}


def test_absent_local_journal_inventory_does_not_create_the_root(tmp_path, pair: UpgradePair) -> None:
    root = tmp_path / "absent"

    assert JournalStore(root).read_states((pair,)) == {}
    assert not root.exists()


def test_inventory_rejects_a_dangling_symlink_root(tmp_path, pair: UpgradePair) -> None:
    root = tmp_path / "linked-root"
    root.symlink_to(tmp_path / "missing", target_is_directory=True)

    with pytest.raises(JournalError):
        JournalStore(root).read_states((pair,))


def test_one_nonblocking_lock_excludes_every_other_writer(tmp_path, pair: UpgradePair) -> None:
    store = JournalStore(tmp_path)
    store.initialize(pair, {})

    with (
        store.locked(pair),
        pytest.raises(JournalError),
        store.locked(pair),
    ):
        pytest.fail("second lock unexpectedly acquired")


def test_engine_records_intent_before_dispatch(tmp_path, pair: UpgradePair) -> None:
    store = JournalStore(tmp_path)
    store.initialize(pair, {})

    class _InterruptingExecution(_Execution):
        def start(self, action: UpgradeAction, attempt_id: str) -> _Result:
            state = store.load(pair)
            assert state.active_action is action
            assert state.attempt_id == attempt_id
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        UpgradeEngine(_StoreJournal(store), _InterruptingExecution()).advance_action(pair, UpgradeAction.SOURCE_UPDATE)

    state = store.load(pair)
    assert state.active_action is UpgradeAction.SOURCE_UPDATE
    assert state.outcome is AttemptOutcome.RUNNING


def test_stale_coordinator_cannot_complete_a_retried_attempt(tmp_path, pair: UpgradePair) -> None:
    store = JournalStore(tmp_path)
    store.initialize(pair, {})
    original = store.load(pair).claim(UpgradeAction.SOURCE_UPDATE)
    assert original.attempt_id is not None
    failed = original.fail_active(original.attempt_id, "dispatch not observed", repair_required=False)
    replacement = failed.retry_active(original.attempt_id)
    store.write_state(pair, replacement)

    with store.locked(pair), pytest.raises(JournalError):
        store.load(pair).complete_active(original.attempt_id)

    assert store.load(pair).attempt_id == replacement.attempt_id


def test_journal_root_rejects_symlink_and_wrong_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pair: UpgradePair,
) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    symlink_root = tmp_path / "linked-root"
    symlink_root.symlink_to(target, target_is_directory=True)
    with pytest.raises(JournalError):
        JournalStore(symlink_root).initialize(pair, {})

    owned_root = tmp_path / "owned-root"
    owned_root.mkdir(mode=0o700)
    monkeypatch.setattr("agentworks.vms.upgrade.journal.os.geteuid", lambda: owned_root.stat().st_uid + 1)
    with pytest.raises(JournalError):
        JournalStore(owned_root).initialize(pair, {})


def test_journal_load_rejects_a_symlinked_pair_directory(tmp_path, pair: UpgradePair) -> None:
    store = JournalStore(tmp_path)
    store.initialize(pair, {})
    directory = store.path_for(pair)
    moved = tmp_path / "moved-pair"
    directory.rename(moved)
    directory.symlink_to(moved, target_is_directory=True)

    with pytest.raises(JournalError):
        store.load(pair)


def test_journal_write_rejects_an_unsafe_existing_state_file(tmp_path, pair: UpgradePair) -> None:
    store = JournalStore(tmp_path)
    store.initialize(pair, {})
    state = store.load(pair)
    (store.path_for(pair) / "state.json").chmod(0o644)

    with pytest.raises(JournalError):
        store.write_state(pair, state)


def test_resume_proves_completed_active_action_without_replay(tmp_path, pair: UpgradePair) -> None:
    store = JournalStore(tmp_path)
    store.initialize(pair, {})
    store.write_state(pair, store.load(pair).claim(UpgradeAction.SOURCE_UPDATE))
    execution = _Execution(inspected=ActionDisposition.SUCCEEDED, started=ActionDisposition.RUNNING)

    result = UpgradeEngine(_StoreJournal(store), execution).advance_action(pair, UpgradeAction.SOURCE_UPDATE)

    assert result.last_completed is JournalProgress.SOURCE_CURRENT
    assert result.active_action is None
    assert execution.inspections == [UpgradeAction.SOURCE_UPDATE]
    assert execution.starts == []


def test_repair_required_attempt_is_not_retried(tmp_path, pair: UpgradePair) -> None:
    store = JournalStore(tmp_path)
    store.initialize(pair, {})
    state = store.load(pair).claim(UpgradeAction.SOURCE_UPDATE)
    assert state.attempt_id is not None
    state = state.fail_active(state.attempt_id, "manual package repair needed", repair_required=True)
    store.write_state(pair, state)
    execution = _Execution(inspected=ActionDisposition.REPAIR_REQUIRED)

    with pytest.raises(UpgradeActionError) as raised:
        UpgradeEngine(_StoreJournal(store), execution).advance_action(pair, UpgradeAction.SOURCE_UPDATE)

    assert raised.value.repair_required is True
    assert execution.starts == []


def test_state_rejects_out_of_order_action(pair: UpgradePair) -> None:
    del pair
    with pytest.raises(JournalError):
        JournalState.prepared().claim(UpgradeAction.FULL_UPGRADE)


def test_state_rejects_pair_fields_at_persisted_boundary() -> None:
    payload = JournalState.prepared().to_mapping()
    payload["source"] = "bookworm"
    with pytest.raises(JournalError):
        JournalState.from_mapping(payload)


def test_complete_state_cannot_retain_active_attempt() -> None:
    state = JournalState.prepared()
    for action in UpgradeAction:
        state = state.claim(action, boot_id_before="old" if action is UpgradeAction.REBOOT else None)
        assert state.attempt_id is not None
        state = state.complete_active(state.attempt_id)
    with pytest.raises(JournalError):
        replace(
            state,
            attempt_id="attempt",
            active_action=UpgradeAction.REBOOT,
            active_started_at="now",
            boot_id_before="old",
            outcome=AttemptOutcome.RUNNING,
        ).validate()


def test_reboot_redispatch_requires_the_unchanged_recorded_boot() -> None:
    state = JournalState.prepared()
    for action in (
        UpgradeAction.SOURCE_UPDATE,
        UpgradeAction.SWITCH_SOURCES,
        UpgradeAction.MINIMAL_UPGRADE,
        UpgradeAction.FULL_UPGRADE,
    ):
        state = state.claim(action)
        assert state.attempt_id is not None
        state = state.complete_active(state.attempt_id)
    state = state.claim(UpgradeAction.REBOOT, boot_id_before="boot-a")
    assert state.attempt_id is not None

    redispatched = state.redispatch_reboot("boot-a", state.attempt_id)

    assert redispatched.boot_id_before == "boot-a"
    assert redispatched.attempt_id != state.attempt_id
    with pytest.raises(JournalError):
        state.redispatch_reboot("boot-b", state.attempt_id)


def test_absent_remote_journal_scan_is_read_only(pair: UpgradePair) -> None:
    class _Target:
        def __init__(self) -> None:
            self.copy_destinations: list[str] = []
            self.commands: list[str] = []

        def run(self, command: str, **kwargs: object) -> object:
            del kwargs
            self.commands.append(command)
            return type("Result", (), {"ok": True, "stdout": "{}", "stderr": ""})()

        def copy_to(self, source: Path, destination: str) -> None:
            assert source.name == "journal.py"
            self.copy_destinations.append(destination)

    target = _Target()

    assert RemoteJournal(target).read_states((pair,)) == {}  # type: ignore[arg-type]
    assert len(target.copy_destinations) == 1
    assert " ensure-root" not in target.commands[0]
    assert " install " not in target.commands[0]
    assert " scan " in target.commands[0]
    assert target.commands[-1].startswith("rm -f /var/tmp/agentworks-journal-scan-")


def test_reboot_lock_proof_falls_back_to_lslocks_when_fuser_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agentworks.vms.upgrade.journal.shutil.which",
        lambda command: None if command == "fuser" else "/usr/bin/lslocks",
    )
    monkeypatch.setattr(
        "agentworks.vms.upgrade.journal.subprocess.run",
        lambda *args, **kwargs: type("Result", (), {"returncode": 0, "stdout": ""})(),
    )

    assert journal_module._package_locks_quiescent() is True
