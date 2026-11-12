"""Provider inactivity qualifies an observation without replacing domain outcomes."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentworks.capabilities.harness_integration import HarnessLaunchIntent
from agentworks.db import DesiredRuntimeState, SessionRuntimeSnapshot, SessionStatus, VMStatus
from agentworks.errors import StateError, UserAbort
from agentworks.runtime_state import reconcile_console, reconcile_session
from agentworks.runtime_state._execute import final_observations
from agentworks.runtime_state._models import Outcome, RuntimeInvocation, RuntimeResult, RuntimeSelection
from agentworks.runtime_state._prepare import _guest_stop_failure
from agentworks.sessions import manager
from agentworks.sessions import multi_console as mc
from agentworks.sessions.multi_console._status import ConsoleStatus
from tests.console_helpers import create_console_record
from tests.runtime_state.test_composition import REFUSE, seed_session, seed_vm
from tests.runtime_state.test_composition import runtime as runtime


@pytest.fixture
def invocation(db, make_config):
    seed_vm(db, "box")
    seed_session(db, "work", "box", target=DesiredRuntimeState.STOPPED)
    create_console_record(db, name="view", vm_name="box", session_specs=[], add_admin_shell=True)
    db.set_console_desired_runtime_state("view", DesiredRuntimeState.STOPPED)
    vm = db.get_vm("box")
    session = db.get_session("work")
    console = db.get_console("view")
    contexts = {"box": SimpleNamespace(vm=vm)}
    stops = {"work": SimpleNamespace(admin_target=object())}
    console_stops = {"view": SimpleNamespace()}
    with ExitStack() as stack:
        result = RuntimeInvocation(
            db=db,
            config=make_config(),
            registry=None,
            resolver=None,
            interaction=REFUSE,
            selection=RuntimeSelection(vms=(vm,), sessions=(session,), session_routes=(("work", "box"),)),
            stack=stack,
            intent=HarnessLaunchIntent.RESUME_OR_NEW,
            vms={"box": vm},
            sessions={"work": session},
            consoles={"view": console},
            contexts=contexts,
            stops=stops,
            console_stops=console_stops,
            results={
                ("session", "work"): RuntimeResult(
                    kind="session", name="work", desired_runtime_state=DesiredRuntimeState.STOPPED
                ),
                ("console", "view"): RuntimeResult(
                    kind="console", name="view", desired_runtime_state=DesiredRuntimeState.STOPPED
                ),
            },
        )
        yield result


def test_observation_provenance_survives_metadata_and_resets_on_same_status(invocation):
    invocation.record("session", "work", observation=SessionStatus.STOPPED, observation_from_parent=True)
    invocation.record("session", "work", action="stop", outcome=Outcome.CHANGED)
    assert invocation.results[("session", "work")].observation_from_parent
    error = StateError("external execution failure")
    invocation.fail("session", "work", error)
    failed = invocation.results[("session", "work")]
    assert failed.observation_from_parent and failed.outcome is Outcome.FAILED
    invocation.record("session", "work", observation=SessionStatus.STOPPED)
    assert invocation.results[("session", "work")] == replace(failed, observation_from_parent=False)


@pytest.mark.parametrize(
    "kind,name,status", [("session", "work", SessionStatus.STOPPED), ("console", "view", ConsoleStatus.STOPPED)]
)
@pytest.mark.parametrize("failed", [False, True])
def test_off_fallback_pairs_observation_without_erasing_prior_failure(
    invocation, monkeypatch, kind, name, status, failed
):
    monkeypatch.setattr("agentworks.vms.manager.observe_vm_runtime", lambda context: VMStatus.STOPPED)
    if failed:
        invocation.record(kind, name, action="stop")
        invocation.fail(kind, name, StateError("original native failure"))
    before = invocation.results[(kind, name)]
    _guest_stop_failure(invocation, kind, name, "box", StateError("later transport failure"))
    result = invocation.results[(kind, name)]
    assert result.observation is status and result.observation_from_parent
    if failed:
        assert result.outcome is before.outcome and result.reason == before.reason and result.action == before.action
    else:
        assert result.outcome is Outcome.UNCHANGED and result.reason is None


@pytest.mark.parametrize("power", [VMStatus.RUNNING, VMStatus.STOPPED, VMStatus.UNKNOWN])
@pytest.mark.parametrize("failed", [False, True])
def test_final_observation_replaces_provenance_and_preserves_execution_failure(invocation, monkeypatch, power, failed):
    before = {row.name: SessionRuntimeSnapshot.from_session(row) for row in invocation.db.list_sessions()}
    monkeypatch.setattr("agentworks.vms.manager.observe_vm_runtime", lambda context: power)
    monkeypatch.setattr(manager, "observe_session_runtime", lambda *a, **k: SessionStatus.STOPPED)
    monkeypatch.setattr(mc, "observe_prepared_console", lambda *a: ConsoleStatus.STOPPED)
    for kind, name in invocation.results:
        status = SessionStatus.STOPPED if kind == "session" else ConsoleStatus.STOPPED
        invocation.record(
            kind, name, observation=status, observation_from_parent=power is not VMStatus.STOPPED, action="stop"
        )
        if failed:
            invocation.fail(kind, name, StateError("original execution failure"))
    previous = dict(invocation.results)
    final_observations(invocation)
    for key, result in invocation.results.items():
        kind, _ = key
        expected = SessionStatus if kind == "session" else ConsoleStatus
        assert result.observation is (expected.UNKNOWN if power is VMStatus.UNKNOWN else expected.STOPPED)
        assert result.observation_from_parent == (power is VMStatus.STOPPED)
        if failed:
            assert result.outcome is Outcome.FAILED and result.reason == previous[key].reason
        elif power is VMStatus.UNKNOWN:
            assert result.outcome is Outcome.BLOCKED
    assert {row.name: SessionRuntimeSnapshot.from_session(row) for row in invocation.db.list_sessions()} == before


@pytest.mark.parametrize("failed", [False, True])
def test_failed_guest_probe_preserves_failure_and_honest_observation_source(invocation, monkeypatch, failed):
    monkeypatch.setattr("agentworks.vms.manager.observe_vm_runtime", lambda context: VMStatus.RUNNING)

    def refuse(*args, **kwargs):
        raise StateError("guest inspection failed")

    monkeypatch.setattr(manager, "observe_session_runtime", refuse)
    monkeypatch.setattr(mc, "observe_prepared_console", refuse)
    for kind, name in invocation.results:
        status = SessionStatus.STOPPED if kind == "session" else ConsoleStatus.STOPPED
        invocation.record(kind, name, observation=status, observation_from_parent=True)
        if failed:
            invocation.fail(kind, name, StateError("original execution failure"))
    before = dict(invocation.results)
    final_observations(invocation)
    for key, result in invocation.results.items():
        assert result.outcome is (Outcome.FAILED if failed else Outcome.BLOCKED)
        assert result.observation_from_parent == (not failed)
        if failed:
            assert result.observation is (SessionStatus.UNKNOWN if key[0] == "session" else ConsoleStatus.UNKNOWN)
            assert result.reason == before[key].reason
        else:
            assert result.observation == before[key].observation


@pytest.mark.parametrize("kind", ["session", "console"])
@pytest.mark.parametrize("target", [DesiredRuntimeState.STOPPED, DesiredRuntimeState.RUNNING])
def test_preview_off_observation_is_qualified_for_both_targets(db, make_config, runtime, monkeypatch, kind, target):
    seed_vm(db, "box")
    seed_session(db, "work", "box", target=target)
    create_console_record(db, name="view", vm_name="box", session_specs=[], add_admin_shell=True)
    db.set_console_desired_runtime_state("view", target)
    runtime[0]["box"] = VMStatus.STOPPED
    before = (db.get_session("work"), db.get_console("view"), db.get_vm("box"))
    monkeypatch.setattr(manager, "observe_session_statuses", lambda *a, **k: pytest.fail("off guest inspected"))
    monkeypatch.setattr("agentworks.transports.transport", lambda *a, **k: pytest.fail("off guest transport"))
    rendered = []
    monkeypatch.setattr("agentworks.runtime_state._preview.render_results", lambda rows, **k: rendered.extend(rows))
    if kind == "session":
        reconcile_session(db, make_config(), name="work", dry_run=True, interaction=REFUSE)
    else:
        reconcile_console(db, make_config(), name="view", dry_run=True, interaction=REFUSE)
    result = next(row for row in rendered if row.kind == kind)
    assert result.observation_from_parent
    assert result.observation is (SessionStatus.STOPPED if kind == "session" else ConsoleStatus.STOPPED)
    assert result.desired_runtime_state is target
    assert result.outcome is (Outcome.CHANGED if target is DesiredRuntimeState.RUNNING else Outcome.UNCHANGED)
    assert (db.get_session("work"), db.get_console("view"), db.get_vm("box")) == before
    assert not runtime[2]


@pytest.mark.parametrize("cancel", [False, True])
def test_off_console_reconciliation_qualifies_guest_free_result_before_final(
    db, make_config, runtime, monkeypatch, cancel
):
    seed_vm(db, "box")
    create_console_record(db, name="view", vm_name="box", session_specs=[], add_admin_shell=True)
    db.set_console_desired_runtime_state("view", DesiredRuntimeState.STOPPED)
    runtime[0]["box"] = VMStatus.STOPPED
    before = db.get_console("view")
    monkeypatch.setattr("agentworks.transports.transport", lambda *a, **k: pytest.fail("off guest transport"))
    monkeypatch.setattr(mc, "_console_runtime_presence", lambda *a, **k: pytest.fail("off guest inspected"))
    rendered = []
    monkeypatch.setattr("agentworks.runtime_state._service.render_results", lambda rows: rendered.extend(rows))
    if cancel:

        def abort(invocation):
            raise UserAbort("cancelled before deferred work")

        monkeypatch.setattr("agentworks.runtime_state._service.resolve_workload_boundary", abort)
        with pytest.raises(UserAbort):
            reconcile_console(db, make_config(), name="view", interaction=REFUSE)
    else:
        reconcile_console(db, make_config(), name="view", interaction=REFUSE)
    result = next(row for row in rendered if row.kind == "console")
    assert result.observation is ConsoleStatus.STOPPED and result.observation_from_parent
    assert result.outcome is (Outcome.BLOCKED if cancel else Outcome.UNCHANGED)
    assert db.get_console("view") == before and not runtime[2]


@pytest.mark.parametrize("scenario", ["off", "live", "off-then-live", "prepare-fallback", "execute-fallback"])
def test_public_console_reports_the_final_stop_path(db, make_config, runtime, monkeypatch, scenario):
    from agentworks.sessions.multi_console import lifecycle
    from agentworks.sessions.tmux import ProbeStatus

    seed_vm(db, "box")
    create_console_record(db, name="view", vm_name="box", session_specs=[], add_admin_shell=True)
    before = db.get_console("view")
    powers, _, events = runtime
    powers["box"] = VMStatus.STOPPED if scenario in {"off", "off-then-live"} else VMStatus.RUNNING
    reports = []
    report = lifecycle._report_console_stop

    def capture(name, *, observation_from_parent):
        reports.append((name, observation_from_parent))
        report(name, observation_from_parent=observation_from_parent)

    monkeypatch.setattr(lifecycle, "_report_console_stop", capture)
    present = True
    probes = []
    teardowns = []

    def presence(target, name):
        probes.append(name)
        if scenario == "prepare-fallback":
            powers["box"] = VMStatus.STOPPED
            raise StateError("external transport lost")
        return (ProbeStatus.PRESENT if present else ProbeStatus.ABSENT), ProbeStatus.ABSENT

    monkeypatch.setattr(mc, "_console_runtime_presence", presence)
    prepare = mc.prepare_console_stop
    prepared_targets = []

    def observe_preparation(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        prepared_targets.append(prepared.target)
        if scenario == "off-then-live":
            powers["box"] = VMStatus.RUNNING
        return prepared

    monkeypatch.setattr(mc, "prepare_console_stop", observe_preparation)

    def teardown(target, name):
        nonlocal present
        teardowns.append(name)
        if scenario == "execute-fallback":
            powers["box"] = VMStatus.STOPPED
            raise StateError("external execution lost")
        present = False

    monkeypatch.setattr(mc, "_teardown_console_tmux", teardown)
    mc.stop_console(db, make_config(), name="view", interaction=REFUSE)
    assert reports == [("view", scenario in {"off", "prepare-fallback", "execute-fallback"})]
    assert bool(probes) == (scenario != "off")
    assert bool(teardowns) == (scenario in {"live", "off-then-live", "execute-fallback"})
    if scenario == "off-then-live":
        assert len(prepared_targets) == 2 and prepared_targets[0] is None and prepared_targets[1] is not None
    current = db.get_console("view")
    assert current == replace(before, desired_runtime_state=DesiredRuntimeState.STOPPED, updated_at=current.updated_at)
    assert not events


@pytest.mark.parametrize("operation", ["named", "bulk", "reconcile"])
def test_cancellation_after_off_session_stop_keeps_qualified_completion(
    db, make_config, runtime, monkeypatch, operation
):
    seed_vm(db, "box")
    seed_session(db, "work", "box", target=DesiredRuntimeState.STOPPED)
    runtime[0]["box"] = VMStatus.STOPPED
    before = SessionRuntimeSnapshot.from_session(db.get_session("work"))
    rendered = []
    monkeypatch.setattr("agentworks.runtime_state._service.render_results", lambda rows: rendered.extend(rows))

    def abort(invocation):
        raise UserAbort("cancelled before final observations")

    monkeypatch.setattr("agentworks.runtime_state._service.final_observations", abort)
    with pytest.raises(UserAbort):
        if operation == "named":
            manager.stop_session(db, make_config(), name="work", interaction=REFUSE)
        elif operation == "bulk":
            manager.stop_all_sessions(db, make_config(), interaction=REFUSE)
        else:
            reconcile_session(db, make_config(), name="work", interaction=REFUSE)
    result = next(row for row in rendered if row.kind == "session")
    assert result.observation is SessionStatus.STOPPED and result.observation_from_parent
    assert result.outcome is Outcome.UNCHANGED
    assert SessionRuntimeSnapshot.from_session(db.get_session("work")) == before
    assert not runtime[2]
