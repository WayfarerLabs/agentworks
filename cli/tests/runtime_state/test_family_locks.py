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
from tests.runtime_state.test_composition import REFUSE, seed_session, seed_vm
from tests.runtime_state.test_composition import runtime as runtime

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
