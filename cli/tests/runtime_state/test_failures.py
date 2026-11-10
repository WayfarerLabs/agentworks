"""Cross-family barriers, cancellation, and final observation retain truthful state."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from agentworks.db import DesiredRuntimeState, SessionStatus, VMStatus
from agentworks.errors import AgentworksError, StateError, UserAbort
from agentworks.harness_setup.locking import native_mutation_guard
from agentworks.runtime_state import reconcile_session, reconcile_vm
from agentworks.runtime_state._models import Outcome
from agentworks.secrets import SecretTarget
from agentworks.secrets.resolver import Resolver
from agentworks.sessions import manager, tmux
from agentworks.sessions import multi_console as mc
from agentworks.sessions.tmux import ProbeStatus
from agentworks.vms.admin import AdminConfig
from tests.conftest import ManifestDoc
from tests.console_helpers import create_console_record
from tests.runtime_state.test_composition import REFUSE, seed_session, seed_vm
from tests.runtime_state.test_composition import runtime as runtime


def required_template() -> ManifestDoc:
    return ManifestDoc(
        "session-template",
        "requires-token",
        {"harness_integration": {"name": "shell"}, "env": {"TOKEN": {"secret": "workload-key"}}},
    )


def test_bad_composed_console_env_blocks_sessions_cleanup_and_provider_shutdown(db, make_config, runtime, monkeypatch):
    _, _, events = runtime
    seed_vm(db, "work")
    seed_session(db, "launch", "work")
    seed_vm(db, "shutdown", target=DesiredRuntimeState.STOPPED)
    create_console_record(db, name="view", vm_name="work", session_specs=[], add_admin_shell=True)
    config = make_config()
    admin = AdminConfig.model_validate(
        {"env": {"TOKEN": {"secret": "workload-key"}, "LOCATION": "${agw:session_name}"}}
    )
    monkeypatch.setattr("agentworks.vms.admin_templates.resolve_live_template", lambda *a: admin)
    monkeypatch.setenv("AW_SECRET_WORKLOAD_KEY", "line-safe-supplied-value")
    target = SecretTarget(vm={"TOKEN": admin.env["TOKEN"]})
    monkeypatch.setattr(mc, "_console_build_secret_targets", lambda *a, **k: [target])
    monkeypatch.setattr(mc, "_console_runtime_presence", lambda *a: (ProbeStatus.ABSENT, ProbeStatus.PRESENT))
    monkeypatch.setattr(
        "agentworks.sessions.multi_console.attach._teardown_console_staging",
        lambda *a: pytest.fail("cleaned staging before all uses validated"),
    )
    with pytest.raises(AgentworksError):
        reconcile_vm(db, config, all_vms=True, interaction=REFUSE)
    assert not any(action in {"session-start", "vm-stop"} for action, _ in events)


def test_final_absence_after_start_is_failure_with_identity_retained(db, make_config, runtime, monkeypatch):
    seed_vm(db, "box")
    seed_session(db, "launch", "box")
    selected = db.get_session("launch")
    rendered = []
    monkeypatch.setattr("agentworks.runtime_state._service.render_results", lambda rows, **k: rendered.extend(rows))
    monkeypatch.setattr(
        tmux, "finalize_session_start", lambda name, **k: runtime[1].update({name: SessionStatus.STOPPED})
    )
    with pytest.raises(StateError):
        reconcile_session(db, make_config(), name="launch", interaction=REFUSE)
    result = next(row for row in rendered if row.kind == "session")
    assert result.outcome is Outcome.FAILED and result.observation is SessionStatus.STOPPED
    assert db.get_session("launch").session_uuid == selected.session_uuid
    assert db.get_session("launch").desired_runtime_state is DesiredRuntimeState.RUNNING
    assert runtime[2].count(("session-start", "launch")) == 1


def test_accepted_start_retains_running_intent_after_secret_failure(db, make_config, runtime):
    seed_vm(db, "box", target=DesiredRuntimeState.ON_DEMAND)
    seed_session(db, "launch", "box", template="requires-token", target=DesiredRuntimeState.STOPPED)
    config = make_config(manifests=[required_template()])
    with pytest.raises(StateError):
        manager.start_session(db, config, name="launch", interaction=REFUSE)
    assert db.get_session("launch").desired_runtime_state is DesiredRuntimeState.RUNNING
    assert not any(action == "session-start" for action, _ in runtime[2])


@pytest.mark.parametrize("control_signal", [UserAbort, KeyboardInterrupt])
def test_cancellation_retains_accepted_target_and_completed_activation_and_disposes_values(
    db, make_config, runtime, monkeypatch, control_signal
):
    import agentworks.vms.manager as vms
    from agentworks.secrets import resolve as resolution

    powers, _, events = runtime
    seed_vm(db, "box", target=DesiredRuntimeState.ON_DEMAND)
    seed_session(db, "launch", "box", template="requires-token", target=DesiredRuntimeState.STOPPED)
    powers["box"] = VMStatus.STOPPED
    failure = control_signal("cancelled at workload boundary")
    original = resolution.resolve_batch

    def cancel(declarations, *args, **kwargs):
        if any(row.name == "workload-key" for row in declarations):
            raise failure
        return original(declarations, *args, **kwargs)

    monkeypatch.setattr(resolution, "resolve_batch", cancel)
    original_close = Resolver.close
    closed = []

    def close(self):
        original_close(self)
        closed.append(self)

    monkeypatch.setattr(Resolver, "close", close)
    original_prepare = vms.prepare_vm_runtime
    contexts = []

    def prepare(*args, **kwargs):
        context = original_prepare(*args, **kwargs)
        contexts.append(context)
        return context

    monkeypatch.setattr(vms, "prepare_vm_runtime", prepare)
    rendered = []
    monkeypatch.setattr("agentworks.runtime_state._service.render_results", lambda rows, **k: rendered.extend(rows))
    with pytest.raises((UserAbort, KeyboardInterrupt)) as caught:
        manager.start_session(db, make_config(manifests=[required_template()]), name="launch", interaction=REFUSE)
    assert caught.value is failure
    assert db.get_session("launch").desired_runtime_state is DesiredRuntimeState.RUNNING
    assert ("vm-start", "box") in events and not any(action == "session-start" for action, _ in events)
    assert any(row.kind == "vm" and row.outcome is Outcome.CHANGED for row in rendered)
    assert closed and all(not context._provider_values for context in contexts)
    with pytest.raises(StateError):
        closed[0].get("proxmox-token")
    with native_mutation_guard(db.path, "box"):
        pass


def test_vm_replacement_during_guard_acquisition_precedes_provider_sources(
    db, make_config, runtime, resolve_counter, monkeypatch
):
    seed_vm(db, "box")
    before = db.get_vm("box")
    original_guard = native_mutation_guard

    @contextmanager
    def replace_vm(path, name, **kwargs):
        with original_guard(path, name, **kwargs) as guard:
            db.delete_workspace("ws-box")
            db.delete_vm("box")
            seed_vm(db, "box")
            yield guard

    monkeypatch.setattr("agentworks.harness_setup.locking.native_mutation_guard", replace_vm)
    with pytest.raises(StateError):
        reconcile_vm(db, make_config(), name="box", interaction=REFUSE)
    assert db.get_vm("box").vm_uuid != before.vm_uuid
    assert resolve_counter == [] and not runtime[2]


def test_pending_child_adoption_does_not_suppress_selected_vm_activation(db, make_config, runtime, monkeypatch):
    seed_vm(db, "box")
    seed_session(db, "legacy", "box")
    db._conn.execute("UPDATE sessions SET desired_runtime_state=NULL WHERE name='legacy'")
    db._conn.commit()
    runtime[0]["box"] = VMStatus.STOPPED
    monkeypatch.setattr(manager, "_session_secret_target", lambda *a, **k: pytest.fail("unused pending-adoption env"))
    with pytest.raises(StateError):
        reconcile_vm(db, make_config(), name="box", interaction=REFUSE)
    assert ("vm-start", "box") in runtime[2]
    assert db.get_session("legacy").desired_runtime_state is None


@pytest.mark.parametrize(
    "vm_target,console_target",
    [
        (DesiredRuntimeState.RUNNING, DesiredRuntimeState.STOPPED),
        (DesiredRuntimeState.STOPPED, DesiredRuntimeState.RUNNING),
    ],
)
def test_fleet_console_results_retain_console_target_independently_of_vm(
    db, make_config, runtime, monkeypatch, vm_target, console_target
):
    seed_vm(db, "box", target=vm_target)
    create_console_record(db, name="view", vm_name="box", session_specs=[], add_admin_shell=True)
    db.set_console_desired_runtime_state("view", console_target)
    monkeypatch.setattr(mc, "_console_runtime_presence", lambda *a: (ProbeStatus.ABSENT, ProbeStatus.ABSENT))
    monkeypatch.setattr(mc, "_teardown_console_tmux", lambda *a, **k: None)
    rendered = []
    monkeypatch.setattr("agentworks.runtime_state._service.render_results", lambda rows, **k: rendered.extend(rows))
    if vm_target is DesiredRuntimeState.STOPPED:
        with pytest.raises(StateError):
            reconcile_vm(db, make_config(), all_vms=True, interaction=REFUSE)
    else:
        reconcile_vm(db, make_config(), all_vms=True, interaction=REFUSE)
    result = next(row for row in rendered if row.kind == "console")
    assert result.target_known and result.desired_runtime_state is console_target


def test_busy_console_family_reports_unselected_target_as_unknown(db, make_config, runtime, monkeypatch):
    from agentworks.runtime_state import reconcile_console

    seed_vm(db, "box")
    create_console_record(db, name="view", vm_name="box", session_specs=[], add_admin_shell=True)
    rendered = []
    monkeypatch.setattr("agentworks.runtime_state._service.render_results", lambda rows, **k: rendered.extend(rows))
    with native_mutation_guard(db.path, "box"), pytest.raises(StateError):
        reconcile_console(db, make_config(), name="view", interaction=REFUSE)
    result = next(row for row in rendered if row.kind == "console")
    assert not result.target_known and result.outcome is Outcome.BLOCKED


def test_keyboard_interrupt_after_activation_prevents_deferred_provider_stops(db, make_config, runtime, monkeypatch):
    from agentworks.secrets import resolve as resolution

    seed_vm(db, "work")
    seed_session(db, "launch", "work", template="requires-token")
    seed_vm(db, "shutdown", target=DesiredRuntimeState.STOPPED)
    runtime[0]["work"] = VMStatus.STOPPED
    failure = KeyboardInterrupt()
    original = resolution.resolve_batch

    def interrupt(declarations, *args, **kwargs):
        if any(row.name == "workload-key" for row in declarations):
            raise failure
        return original(declarations, *args, **kwargs)

    monkeypatch.setattr(resolution, "resolve_batch", interrupt)
    rendered = []
    monkeypatch.setattr("agentworks.runtime_state._service.render_results", lambda rows, **k: rendered.extend(rows))
    with pytest.raises(KeyboardInterrupt) as caught:
        reconcile_vm(db, make_config(manifests=[required_template()]), all_vms=True, interaction=REFUSE)
    assert caught.value is failure
    assert ("vm-start", "work") in runtime[2]
    assert not any(action in {"session-start", "vm-stop"} for action, _ in runtime[2])
    assert any(row.kind == "vm" and row.name == "work" and row.outcome is Outcome.CHANGED for row in rendered)
    assert any(row.kind == "vm" and row.name == "shutdown" and row.outcome is Outcome.BLOCKED for row in rendered)
    with native_mutation_guard(db.path, "work"), native_mutation_guard(db.path, "shutdown"):
        pass
