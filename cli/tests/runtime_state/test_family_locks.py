"""Runtime families' lock sets: per-target refusal, recorded intent and how to restore it, the barrier."""

from __future__ import annotations

from contextlib import ExitStack, suppress

import pytest

from agentworks.capabilities.harness_integration import HarnessLaunchIntent
from agentworks.db import DesiredRuntimeState
from agentworks.errors import StateError
from agentworks.native_locks import LockRequest, native_locks, vm_entity
from agentworks.native_locks.rows import session_entity_for
from agentworks.runtime_state import _families as families
from agentworks.runtime_state import run_session_batch
from agentworks.runtime_state._models import Outcome, RuntimeResult
from tests.runtime_state.runtime_support import REFUSE, seed_session, seed_vm
from tests.runtime_state.runtime_support import runtime as runtime

pytestmark = pytest.mark.windows


def _start(db, config, names, monkeypatch) -> dict[tuple[str, str], RuntimeResult]:
    rendered: list = []
    monkeypatch.setattr("agentworks.runtime_state._service.render_results", lambda rows, **k: rendered.extend(rows))
    sessions = [db.get_session(name) for name in names]
    with suppress(StateError):
        run_session_batch(
            db,
            config,
            sessions,
            operation="start",
            force=False,
            intent=HarnessLaunchIntent.RESUME_OR_NEW,
            running_restart_authorized=frozenset(),
            interaction=REFUSE,
        )
    return {(row.kind, row.name): row for row in rendered}


def _session_lock(db, name: str) -> LockRequest:
    return LockRequest(exclusive=(session_entity_for(db, db.get_session(name)),))


def test_a_target_held_elsewhere_drops_alone_and_its_sibling_starts(db, make_config, runtime, monkeypatch):
    seed_vm(db, "box")
    seed_session(db, "held", "box", target=DesiredRuntimeState.STOPPED)
    seed_session(db, "free", "box", target=DesiredRuntimeState.STOPPED)
    with native_locks(db.path, _session_lock(db, "held")):
        results = _start(db, make_config(), ["held", "free"], monkeypatch)
    assert results[("session", "held")].outcome is Outcome.BLOCKED
    # Refused at the probe, before anything was recorded.
    assert results[("session", "held")].restore_intent is None
    assert db.get_session("held").desired_runtime_state is DesiredRuntimeState.STOPPED
    assert ("session-start", "free") in runtime[2]
    assert ("session-start", "held") not in runtime[2]


def test_a_refused_vm_blocks_its_whole_family(db, make_config, runtime, monkeypatch):
    seed_vm(db, "box")
    seed_session(db, "one", "box", target=DesiredRuntimeState.STOPPED)
    with native_locks(db.path, LockRequest(exclusive=(vm_entity("box"),))):
        results = _start(db, make_config(), ["one"], monkeypatch)
    assert results[("session", "one")].outcome is Outcome.BLOCKED
    assert runtime[2] == []


@pytest.mark.parametrize(
    ("previous", "restore"),
    [
        (DesiredRuntimeState.STOPPED, ("agw", "session", "set-desired-state", "late", "stopped")),
        (None, ()),
    ],
    ids=["stopped", "legacy-null"],
)
def test_a_target_refused_after_its_intent_was_recorded_says_how_to_restore_it(
    db, make_config, runtime, monkeypatch, previous, restore
):
    seed_vm(db, "box")
    seed_session(db, "late", "box", target=DesiredRuntimeState.STOPPED)
    if previous is None:
        db._conn.execute("UPDATE sessions SET desired_runtime_state=NULL WHERE name='late'")
        db._conn.commit()
    import agentworks.vms.manager as vms

    original_prepare = vms.prepare_vm_runtime
    with ExitStack() as other_operation:

        def prepare_then_another_operation_takes_the_session(*args, **kwargs):
            # Between intent acceptance (d) and the final set (f), another operation takes the target.
            other_operation.enter_context(native_locks(db.path, _session_lock(db, "late")))
            return original_prepare(*args, **kwargs)

        monkeypatch.setattr(vms, "prepare_vm_runtime", prepare_then_another_operation_takes_the_session)
        results = _start(db, make_config(), ["late"], monkeypatch)
    result = results[("session", "late")]
    assert result.outcome is Outcome.BLOCKED
    assert result.restore_intent == restore
    # The recorded intent stays accepted.
    assert db.get_session("late").desired_runtime_state is DesiredRuntimeState.RUNNING
    assert ("session-start", "late") not in runtime[2]


def test_no_family_takes_its_final_set_before_every_family_prepared_its_infrastructure(
    db, make_config, runtime, monkeypatch
):
    seed_vm(db, "a")
    seed_vm(db, "b")
    seed_session(db, "on-a", "a", target=DesiredRuntimeState.STOPPED)
    seed_session(db, "on-b", "b", target=DesiredRuntimeState.STOPPED)
    import agentworks.vms.manager as vms

    order: list[str] = []
    original_prepare = vms.prepare_vm_runtime
    original_final = families.acquire_final_sets

    def prepare(*args, **kwargs):
        order.append(f"prepare-{args[3].name}")
        return original_prepare(*args, **kwargs)

    def final(invocation):
        order.append("final-sets")
        return original_final(invocation)

    monkeypatch.setattr(vms, "prepare_vm_runtime", prepare)
    monkeypatch.setattr("agentworks.runtime_state._service.acquire_final_sets", final)
    _start(db, make_config(), ["on-a", "on-b"], monkeypatch)
    assert order == ["prepare-a", "prepare-b", "final-sets"]
    assert {name for action, name in runtime[2] if action == "session-start"} == {"on-a", "on-b"}


def test_a_target_whose_intent_another_command_replaced_gives_no_restore_words(db, make_config, runtime, monkeypatch):
    seed_vm(db, "box")
    seed_session(db, "late", "box", target=DesiredRuntimeState.STOPPED)
    import agentworks.vms.manager as vms

    original_prepare = vms.prepare_vm_runtime

    def prepare_then_another_command_records_the_old_intent(*args, **kwargs):
        # Between intent acceptance (d) and the final set, another command puts the old intent back.
        late = db.get_session("late")
        db.set_session_desired_runtime_state(
            "late", DesiredRuntimeState.STOPPED, expected_session_uuid=late.session_uuid
        )
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(vms, "prepare_vm_runtime", prepare_then_another_command_records_the_old_intent)
    results = _start(db, make_config(), ["late"], monkeypatch)
    result = results[("session", "late")]
    assert result.outcome is Outcome.BLOCKED
    assert result.restore_intent is None
    assert ("session-start", "late") not in runtime[2]


def test_a_target_whose_declaration_changed_after_acceptance_fails_alone(db, make_config, runtime, monkeypatch):
    seed_vm(db, "box")
    seed_session(db, "changed", "box", target=DesiredRuntimeState.STOPPED)
    seed_session(db, "kept", "box", target=DesiredRuntimeState.STOPPED)
    import agentworks.vms.manager as vms
    from agentworks.instance_specs import parse_instance_spec

    original_prepare = vms.prepare_vm_runtime

    def prepare_then_replace_the_layer(*args, **kwargs):
        # A later restart accepts another overlay between this one's acceptance and its final set.
        overlay = parse_instance_spec("session", '{"env":{"LATER":"y"}}')
        db.instance_state.put_desired_overlay("session", "changed", overlay.payload)
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(vms, "prepare_vm_runtime", prepare_then_replace_the_layer)
    results = _start(db, make_config(), ["changed", "kept"], monkeypatch)
    assert results[("session", "changed")].outcome is Outcome.BLOCKED
    assert ("session-start", "changed") not in runtime[2]
    assert ("session-start", "kept") in runtime[2]


def test_a_target_whose_layer_was_replaced_after_its_definition_was_built_fails_alone(
    db, make_config, runtime, monkeypatch
):
    # Running intent already recorded, so acceptance records nothing new and does not rebuild the definition.
    seed_vm(db, "box")
    seed_session(db, "built", "box", target=DesiredRuntimeState.RUNNING)
    seed_session(db, "kept", "box", target=DesiredRuntimeState.RUNNING)
    from agentworks.instance_specs import parse_instance_spec
    from agentworks.sessions import manager

    observe = manager.observe_session_statuses

    def observe_while_a_spec_start_lands(*args, **kwargs):
        # After this start built its definitions and before its intent acceptance, a `--spec` start
        # of the same session replaces its layer.
        overlay = parse_instance_spec("session", '{"env":{"LATER":"y"}}')
        db.instance_state.put_desired_overlay("session", "built", overlay.payload)
        return observe(*args, **kwargs)

    monkeypatch.setattr(manager, "observe_session_statuses", observe_while_a_spec_start_lands)
    results = _start(db, make_config(), ["built", "kept"], monkeypatch)
    assert results[("session", "built")].outcome is Outcome.BLOCKED
    assert ("session-start", "built") not in runtime[2]
    assert ("session-start", "kept") in runtime[2]


def _reconcile_rows(db, config, monkeypatch, reconcile) -> dict[tuple[str, str], RuntimeResult]:
    rendered: list = []
    monkeypatch.setattr("agentworks.runtime_state._service.render_results", lambda rows, **k: rendered.extend(rows))
    with suppress(StateError):
        reconcile()
    return {(row.kind, row.name): row for row in rendered}


def test_a_target_held_in_a_weaker_mode_survives_when_only_its_sibling_needed_it_stronger(
    db, make_config, runtime, monkeypatch
):
    from agentworks.runtime_state import reconcile_vm
    from agentworks.sessions.multi_console._helpers import console_request
    from tests.console_helpers import create_console_record

    seed_vm(db, "box")
    seed_session(db, "m", "box", target=DesiredRuntimeState.STOPPED)
    create_console_record(db, name="c", vm_name="box", session_specs=["m"])
    create_console_record(db, name="d", vm_name="box", session_specs=["m"])
    db.set_console_desired_runtime_state("c", DesiredRuntimeState.STOPPED)
    from agentworks.sessions import multi_console
    from agentworks.sessions.tmux import ProbeStatus

    # Console c's tmux session is live until torn down, so reconciling it to stopped tears it down.
    torn_down: list[str] = []
    monkeypatch.setattr(
        multi_console,
        "_console_runtime_presence",
        lambda admin, name: (ProbeStatus.ABSENT if name in torn_down else ProbeStatus.PRESENT, ProbeStatus.ABSENT),
    )
    monkeypatch.setattr(multi_console, "_teardown_console_tmux", lambda admin, name: torn_down.append(name))
    config = make_config()
    # Console d's operation holds d exclusive, and so its member m shared.
    with native_locks(db.path, console_request(db, db.get_console("d"))):
        results = _reconcile_rows(
            db, config, monkeypatch, lambda: reconcile_vm(db, config, name="box", interaction=REFUSE)
        )
    # d is held, and m needs itself exclusive, so both are dropped; c needs m only shared, so the retry
    # without them reconciles c.
    assert results[("console", "d")].outcome is Outcome.BLOCKED
    assert results[("session", "m")].outcome is Outcome.BLOCKED
    assert results[("console", "c")].outcome not in {Outcome.BLOCKED, Outcome.FAILED}
    assert torn_down == ["c"]


@pytest.mark.parametrize(
    ("target", "workspace_create_proceeds"),
    [(DesiredRuntimeState.RUNNING, True), (DesiredRuntimeState.STOPPED, False)],
    ids=["running-target", "stopped-target"],
)
def test_vm_reconcile_holds_the_vm_shared_unless_it_stops_the_vm(
    db, make_config, runtime, monkeypatch, target, workspace_create_proceeds
):
    from agentworks.native_locks import NativeLockBusyError, probe_locks
    from agentworks.runtime_state import _service, reconcile_vm
    from agentworks.workspaces.locks import workspace_create_request

    seed_vm(db, "box", target=target)
    seed_session(db, "s", "box", target=target)
    config = make_config()
    beside: list[bool] = []
    from agentworks.runtime_state._prepare import prepare_guest_workloads as guest_work

    def under_the_final_sets(invocation):
        # An unrelated workspace create on the VM, probed while the reconcile holds its final set.
        try:
            probe_locks(db.path, workspace_create_request(db, db.get_vm("box"), "unrelated"))
            beside.append(True)
        except NativeLockBusyError:
            beside.append(False)
        return guest_work(invocation)

    monkeypatch.setattr(_service, "prepare_guest_workloads", under_the_final_sets)
    _reconcile_rows(db, config, monkeypatch, lambda: reconcile_vm(db, config, name="box", interaction=REFUSE))
    assert beside == [workspace_create_proceeds]


def test_a_target_whose_agent_row_went_away_fails_alone(db, make_config, runtime, monkeypatch):
    from agentworks.db import SessionMode

    seed_vm(db, "box")
    seed_session(db, "kept", "box", target=DesiredRuntimeState.STOPPED)
    db.insert_agent("gone", "box", "agt-gone")
    db.insert_session(
        "orphan",
        "ws-box",
        "default",
        SessionMode.AGENT,
        agent_name="gone",
        socket_path="/run/agentworks/agent-tmux-sockets/agt-gone/orphan.sock",
        desired_runtime_state=DesiredRuntimeState.STOPPED,
    )
    from agentworks.runtime_state import _selection

    read = _selection.read_families

    def read_then_the_agent_row_goes(invocation):
        read(invocation)
        # Another command removed the agent's row after this one read its targets.
        db._conn.execute("PRAGMA foreign_keys = OFF")
        db._conn.execute("DELETE FROM agents WHERE name = 'gone'")
        db._conn.commit()

    monkeypatch.setattr("agentworks.runtime_state._service.read_families", read_then_the_agent_row_goes)
    results = _start(db, make_config(), ["orphan", "kept"], monkeypatch)
    assert results[("session", "orphan")].outcome is Outcome.FAILED
    assert ("session-start", "kept") in runtime[2]


def test_a_console_whose_members_changed_as_its_final_set_was_taken_fails_alone(db, make_config, runtime, monkeypatch):
    from agentworks.runtime_state import reconcile_vm
    from agentworks.sessions import multi_console
    from agentworks.sessions.tmux import ProbeStatus
    from tests.console_helpers import create_console_record

    # Console c's tmux session is live until torn down, so reconciling it to stopped would tear it down.
    torn_down: list[str] = []
    monkeypatch.setattr(
        multi_console,
        "_console_runtime_presence",
        lambda admin, name: (ProbeStatus.ABSENT if name in torn_down else ProbeStatus.PRESENT, ProbeStatus.ABSENT),
    )
    monkeypatch.setattr(multi_console, "_teardown_console_tmux", lambda admin, name: torn_down.append(name))
    seed_vm(db, "box")
    seed_session(db, "m", "box", target=DesiredRuntimeState.STOPPED)
    seed_session(db, "late", "box", target=DesiredRuntimeState.STOPPED)
    create_console_record(db, name="c", vm_name="box", session_specs=["m"])
    db.set_console_desired_runtime_state("c", DesiredRuntimeState.STOPPED)
    request = families._request
    final_sets: list[bool] = []

    def acquire_final_sets(invocation):
        final_sets.append(True)
        return original_final(invocation)

    def built_then_a_member_is_added(invocation, vm_name, targets):
        # Between building the family's final request and holding it, another edit adds a member to c.
        built = request(invocation, vm_name, targets)
        if final_sets and db.get_console_session("c", "late") is None:
            db.add_console_session("c", "late", [])
        return built

    original_final = families.acquire_final_sets
    monkeypatch.setattr("agentworks.runtime_state._service.acquire_final_sets", acquire_final_sets)
    monkeypatch.setattr(families, "_request", built_then_a_member_is_added)
    config = make_config()
    results = _reconcile_rows(db, config, monkeypatch, lambda: reconcile_vm(db, config, name="box", interaction=REFUSE))
    assert results[("console", "c")].outcome is Outcome.BLOCKED
    assert torn_down == []
    assert results[("session", "m")].outcome not in {Outcome.BLOCKED, Outcome.FAILED}
