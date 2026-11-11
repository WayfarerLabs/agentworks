"""Preparation preserves healthy views and defers every tmux mutation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import agentworks.bootstrap as bootstrap
from agentworks.db import DesiredRuntimeState
from agentworks.errors import StateError, UserAbort, ValidationError
from agentworks.harness_setup.locking import NativeSetupBusyError, native_mutation_guard
from agentworks.sessions import multi_console as mc
from agentworks.sessions.tmux import ProbeStatus
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests.console_helpers import create_console_record

if TYPE_CHECKING:
    from agentworks.db import ConsoleRow, Database, SessionRow, VMRow


def _console(db: Database) -> ConsoleRow:
    row = db.get_console("con")
    assert row is not None
    return row


def _vm(db: Database) -> VMRow:
    row = db.get_vm("vm1")
    assert row is not None
    return row


def _session(db: Database) -> SessionRow:
    row = db.get_session("alpha")
    assert row is not None
    return row


@pytest.mark.parametrize(
    ("canonical", "staging", "action", "collect"),
    [
        (ProbeStatus.PRESENT, ProbeStatus.ABSENT, mc.ConsoleAction.UNCHANGED, False),
        (ProbeStatus.PRESENT, ProbeStatus.PRESENT, mc.ConsoleAction.CLEANUP_STAGING, False),
        (ProbeStatus.ABSENT, ProbeStatus.PRESENT, mc.ConsoleAction.BUILD, True),
        (ProbeStatus.ABSENT, ProbeStatus.ABSENT, mc.ConsoleAction.BUILD, True),
    ],
)
def test_prepare_classifies_without_mutation(db: Database, monkeypatch, canonical, staging, action, collect):
    _seed_vm(db)
    _seed_sessions(db, ["alpha"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["alpha"])
    registry = bootstrap.load_request_registry(_StubConfig(), live_database=db)
    calls = []
    monkeypatch.setattr(mc, "_console_runtime_presence", lambda *a: (canonical, staging))
    monkeypatch.setattr(mc, "_console_build_secret_targets", lambda *a, **kw: calls.append("collect") or [])
    target = object()
    with mc.guarded_console_selection(db, "con") as (console, guard):
        prepared = mc.prepare_console_reconcile(
            db,
            registry,
            console=console,
            vm=_vm(db),
            target=target,
            held_native_guard=guard,
        )
        assert prepared.action is action
        assert calls == (["collect"] if collect else [])


@pytest.mark.parametrize(
    "canonical,staging",
    [
        (ProbeStatus.UNKNOWN, ProbeStatus.ABSENT),
        (ProbeStatus.PRESENT, ProbeStatus.UNKNOWN),
    ],
)
def test_unknown_never_collects_or_changes_runtime(db: Database, monkeypatch, canonical, staging):
    _seed_vm(db)
    create_console_record(db, name="con", vm_name="vm1", session_specs=[], add_admin_shell=True)
    registry = bootstrap.load_request_registry(_StubConfig(), live_database=db)
    monkeypatch.setattr(mc, "_console_runtime_presence", lambda *a: (canonical, staging))
    monkeypatch.setattr(mc, "_console_build_secret_targets", lambda *a, **kw: pytest.fail("collected secrets"))
    with mc.guarded_console_selection(db, "con") as (console, guard), pytest.raises(StateError):
        mc.prepare_console_reconcile(
            db,
            registry,
            console=console,
            vm=_vm(db),
            target=object(),
            held_native_guard=guard,
        )


def test_cleanup_only_preserves_canonical_and_requires_live_guard(db: Database, monkeypatch):
    _seed_vm(db)
    create_console_record(db, name="con", vm_name="vm1", session_specs=[], add_admin_shell=True)
    registry = bootstrap.load_request_registry(_StubConfig(), live_database=db)
    monkeypatch.setattr(mc, "_console_runtime_presence", lambda *a: (ProbeStatus.PRESENT, ProbeStatus.PRESENT))
    import agentworks.sessions.multi_console.attach as attach

    changed = []
    monkeypatch.setattr(attach, "_teardown_console_staging", lambda *a: changed.append("staging"))
    monkeypatch.setattr(mc, "_teardown_console_tmux", lambda *a: pytest.fail("canonical teardown"))
    monkeypatch.setattr(mc, "_build_console_tmux", lambda *a, **kw: pytest.fail("built canonical"))
    with mc.guarded_console_selection(db, "con") as (console, guard):
        prepared = mc.prepare_console_reconcile(
            db,
            registry,
            console=console,
            vm=_vm(db),
            target=object(),
            held_native_guard=guard,
        )
        assert not changed
        assert mc.execute_console_reconcile(prepared, values={})
        assert changed == ["staging"]
    with pytest.raises(StateError):
        mc.execute_console_reconcile(prepared, values={})


def test_intent_is_local_kind_strict_and_preserves_members(db: Database):
    _seed_vm(db)
    _seed_sessions(db, ["alpha"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["alpha"])
    session = _session(db)
    mc.set_console_desired_runtime_state(db, name="con", state="stopped")
    assert _console(db).desired_runtime_state is DesiredRuntimeState.STOPPED
    assert _session(db) == session
    with pytest.raises(ValidationError):
        mc.set_console_desired_runtime_state(db, name="con", state="on-demand")
    assert _console(db).desired_runtime_state is DesiredRuntimeState.STOPPED


def test_delete_consent_holds_family_guard_and_nested_reuses_it(db: Database, monkeypatch):
    _seed_vm(db)
    create_console_record(db, name="con", vm_name="vm1", session_specs=[], add_admin_shell=True)
    from agentworks import output

    def confirm(_prompt):
        with pytest.raises(NativeSetupBusyError), native_mutation_guard(db.path, "vm1"):
            pass
        return True

    monkeypatch.setattr(output, "confirm", confirm)
    monkeypatch.setattr(mc, "_live_target", lambda *a: None)
    with native_mutation_guard(db.path, "vm1") as guard:
        mc.delete_console(db, _StubConfig(), name="con", held_native_guard=guard)
    assert db.get_console("con") is None


def test_delete_cancellation_propagates_and_retains_definition(db: Database, monkeypatch):
    _seed_vm(db)
    create_console_record(db, name="con", vm_name="vm1", session_specs=[], add_admin_shell=True)
    monkeypatch.setattr(mc, "_live_target", lambda *a: (_ for _ in ()).throw(UserAbort("cancelled")))
    with pytest.raises(UserAbort):
        mc.delete_console(db, _StubConfig(), name="con", yes=True)
    assert db.get_console("con") is not None


def test_admin_only_env_is_literal_delivered_and_redacted(db: Database, tmp_path, monkeypatch):
    import shlex
    from uuid import uuid4

    from agentworks.ssh import SSHResult
    from tests._tmux_model import TmuxModel
    from tests.conftest import _FakeTarget

    _seed_vm(db)
    create_console_record(db, name="con", vm_name="vm1", session_specs=[], add_admin_shell=True)
    registry = bootstrap.load_request_registry(_StubConfig(), live_database=db)
    marker = uuid4().hex
    value = f"prefix'{marker}' $(touch ignored) suffix"
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    monkeypatch.setattr(mc, "_resolve_admin_env", lambda *a, **kw: {"TOKEN": kw["values"]["token"]})

    class Target(_FakeTarget):
        def run(self, command, **kwargs):
            result = super().run(command, **kwargs)
            if self.logger:
                self.logger.log_command(command, SSHResult(result.returncode, result.stdout, result.stderr))
            if command.startswith("tmux new-session"):
                assert kwargs["env"] == {"TOKEN": value}
                assert "TOKEN=" + value in shlex.split(command)
            return result

    target = Target(model=TmuxModel())
    with mc.guarded_console_selection(db, "con") as (console, guard):
        prepared = mc.prepare_console_reconcile(
            db,
            registry,
            console=console,
            vm=_vm(db),
            target=target,
            held_native_guard=guard,
        )
        assert mc.execute_console_reconcile(prepared, values={"token": value})
        assert mc.observe_prepared_console(prepared).value == "running"
    logs = list(tmp_path.glob("**/*.log"))
    assert logs
    assert all(marker not in log.read_text() for log in logs)
    assert target.logger is None


def test_failed_build_cleanup_cannot_mask_cancellation(db: Database, tmp_path, monkeypatch):
    import agentworks.sessions.multi_console.attach as attach
    from tests.conftest import _FakeTarget

    _seed_vm(db)
    create_console_record(db, name="con", vm_name="vm1", session_specs=[], add_admin_shell=True)
    registry = bootstrap.load_request_registry(_StubConfig(), live_database=db)
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    monkeypatch.setattr("agentworks.ssh.SSHLogger.close", lambda *a: (_ for _ in ()).throw(StateError("log failed")))
    monkeypatch.setattr(attach, "_tmux_session_presence", lambda *a: ProbeStatus.ABSENT)
    monkeypatch.setattr(mc, "_resolve_admin_env", lambda *a, **kw: {})
    cleanup = []

    def teardown(*a):
        cleanup.append(True)
        if len(cleanup) > 1:
            raise StateError("cleanup failed")

    class Target(_FakeTarget):
        def run(self, *a, **kw):
            raise UserAbort("cancelled")

    monkeypatch.setattr(attach, "_teardown_console_tmux", teardown)
    with pytest.raises(UserAbort):
        mc._build_console_tmux(
            Target(),
            db,
            registry,
            _console(db),
            _vm(db),
            values={},
            layout="tiled",
        )
    assert len(cleanup) == 2


def test_stop_off_parent_accepts_without_transport_activation_or_hold(db: Database, monkeypatch):
    from types import SimpleNamespace

    import agentworks.sessions.multi_console.lifecycle as lifecycle
    from agentworks.db import VMStatus
    from agentworks.secrets.policy import TtyInteractionPolicy

    _seed_vm(db)
    create_console_record(db, name="con", vm_name="vm1", session_specs=[], add_admin_shell=True)
    calls = []
    node = SimpleNamespace(hold_active=lambda: pytest.fail("opened a VM hold"))
    context = SimpleNamespace(node=node, observed_status=VMStatus.STOPPED, clear_values=lambda: calls.append("clear"))
    monkeypatch.setattr(lifecycle, "preflight_console_vm", lambda *a, **kw: node)
    monkeypatch.setattr("agentworks.vms.manager.runtime.prepare_vm_runtime", lambda *a, **kw: context)
    monkeypatch.setattr("agentworks.vms.manager.runtime.observe_vm_runtime", lambda *a, **kw: VMStatus.STOPPED)
    monkeypatch.setattr("agentworks.vms.manager.runtime.activate_vm_runtime", lambda *a, **kw: pytest.fail("activated"))
    monkeypatch.setattr("agentworks.transports.transport", lambda *a, **kw: pytest.fail("constructed transport"))
    mc.stop_console(db, _StubConfig(), name="con", interaction=TtyInteractionPolicy.REFUSE)
    assert _console(db).desired_runtime_state is DesiredRuntimeState.STOPPED
    assert calls == ["clear"]


@pytest.mark.parametrize("error_type", [UserAbort, StateError])
def test_start_acceptance_survives_infrastructure_failure(db: Database, fake_target, monkeypatch, error_type):
    import agentworks.sessions.multi_console.lifecycle as lifecycle
    from agentworks.secrets.policy import TtyInteractionPolicy

    _seed_vm(db, with_tailscale=True)
    create_console_record(db, name="con", vm_name="vm1", session_specs=[], add_admin_shell=True)
    mc.set_console_desired_runtime_state(db, name="con", state="stopped")
    monkeypatch.setattr(lifecycle, "preflight_console_vm", lambda *a, **kw: object())

    def fail(*a, **kw):
        assert _console(db).desired_runtime_state is DesiredRuntimeState.RUNNING
        raise error_type("failed")

    monkeypatch.setattr("agentworks.vms.manager.runtime.prepare_vm_runtime", fail)
    with pytest.raises(error_type):
        mc.start_console(db, _StubConfig(), name="con", interaction=TtyInteractionPolicy.REFUSE)
    assert _console(db).desired_runtime_state is DesiredRuntimeState.RUNNING


def test_start_below_stopped_parent_records_child_running_without_activation(db: Database, fake_target, monkeypatch):
    import agentworks.sessions.multi_console.lifecycle as lifecycle
    from agentworks.secrets.policy import TtyInteractionPolicy

    _seed_vm(db, with_tailscale=True)
    create_console_record(db, name="con", vm_name="vm1", session_specs=[], add_admin_shell=True)
    mc.set_console_desired_runtime_state(db, name="con", state="stopped")
    vm = _vm(db)
    db.set_vm_desired_runtime_state("vm1", DesiredRuntimeState.STOPPED, expected_vm_uuid=vm.vm_uuid)
    monkeypatch.setattr(lifecycle, "preflight_console_vm", lambda *a, **kw: object())
    monkeypatch.setattr(
        "agentworks.vms.manager.runtime.prepare_vm_runtime", lambda *a, **kw: pytest.fail("prepared VM")
    )
    with pytest.raises(StateError):
        mc.start_console(db, _StubConfig(), name="con", interaction=TtyInteractionPolicy.REFUSE)
    assert _console(db).desired_runtime_state is DesiredRuntimeState.RUNNING
    assert _vm(db).desired_runtime_state is DesiredRuntimeState.STOPPED


def test_known_preflight_refusal_preserves_console_target(db: Database, fake_target, monkeypatch):
    import agentworks.sessions.multi_console.lifecycle as lifecycle
    from agentworks.secrets.policy import TtyInteractionPolicy

    _seed_vm(db, with_tailscale=True)
    create_console_record(db, name="con", vm_name="vm1", session_specs=[], add_admin_shell=True)
    mc.set_console_desired_runtime_state(db, name="con", state="stopped")
    before = _console(db)

    def refuse(*a, **kw):
        raise StateError("known policy conflict")

    monkeypatch.setattr(lifecycle, "preflight_console_vm", refuse)
    with pytest.raises(StateError):
        mc.start_console(db, _StubConfig(), name="con", interaction=TtyInteractionPolicy.REFUSE)
    assert _console(db) == before


def test_focused_selection_refuses_reparent_before_guarded_reread(db: Database, monkeypatch):
    from contextlib import contextmanager

    _seed_vm(db)
    _seed_vm(db, "vm2")
    create_console_record(db, name="con", vm_name="vm1", session_specs=[], add_admin_shell=True)

    @contextmanager
    def changing_parent(path, vm_name, *, held=None):
        assert vm_name == "vm1"
        db._conn.execute("UPDATE consoles SET vm_name = 'vm2' WHERE name = 'con'")
        db._conn.commit()
        yield object()

    monkeypatch.setattr("agentworks.harness_setup.locking.native_mutation_guard", changing_parent)
    with pytest.raises(StateError):
        mc.set_console_desired_runtime_state(db, name="con", state="stopped")
    assert _console(db).desired_runtime_state is DesiredRuntimeState.RUNNING


@pytest.mark.parametrize("power_state", ["stopped", "unknown"])
def test_stop_guest_disappearance_requires_conclusive_provider_off(db: Database, monkeypatch, power_state):
    from types import SimpleNamespace

    import agentworks.sessions.multi_console.lifecycle as lifecycle
    from agentworks.db import VMStatus
    from agentworks.errors import ConnectivityError
    from agentworks.secrets.policy import TtyInteractionPolicy

    _seed_vm(db, with_tailscale=True)
    create_console_record(db, name="con", vm_name="vm1", session_specs=[], add_admin_shell=True)
    node = SimpleNamespace(hold_active=lambda: pytest.fail("opened hold"))
    context = SimpleNamespace(node=node, observed_status=VMStatus.RUNNING, clear_values=lambda: None)
    monkeypatch.setattr(lifecycle, "preflight_console_vm", lambda *a, **kw: node)
    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", lambda *a, **kw: None)
    monkeypatch.setattr("agentworks.vms.manager.runtime.prepare_vm_runtime", lambda *a, **kw: context)
    monkeypatch.setattr("agentworks.vms.manager.runtime.observe_vm_runtime", lambda *a: VMStatus(power_state))
    monkeypatch.setattr("agentworks.transports.transport", lambda *a, **kw: object())

    def disconnected(*a):
        raise ConnectivityError("guest disappeared")

    monkeypatch.setattr(mc, "_console_runtime_presence", disconnected)
    if power_state == "stopped":
        mc.stop_console(db, _StubConfig(), name="con", interaction=TtyInteractionPolicy.REFUSE)
    else:
        with pytest.raises(ConnectivityError):
            mc.stop_console(db, _StubConfig(), name="con", interaction=TtyInteractionPolicy.REFUSE)
    assert _console(db).desired_runtime_state is DesiredRuntimeState.STOPPED


def test_noop_start_preserves_canonical_panes_and_independent_member_intent(
    db: Database, console_target_factory, monkeypatch
):
    from copy import deepcopy

    from agentworks.secrets.policy import TtyInteractionPolicy
    from tests._tmux_model import TmuxModel

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    session = _session(db)
    db.set_session_desired_runtime_state(
        "alpha", DesiredRuntimeState.STOPPED, expected_session_uuid=session.session_uuid
    )
    session = _session(db)
    create_console_record(db, name="con", vm_name="vm1", session_specs=["alpha"])
    mc.set_console_desired_runtime_state(db, name="con", state="stopped")
    model = TmuxModel()
    model.seed_session("aw-console-con", "alpha")
    console_target_factory(model)
    before = deepcopy(model._sessions)
    monkeypatch.setattr(mc, "_console_build_secret_targets", lambda *a, **kw: pytest.fail("collected pane secrets"))
    mc.start_console(db, _StubConfig(), name="con", interaction=TtyInteractionPolicy.REFUSE)
    assert model._sessions == before
    assert _console(db).desired_runtime_state is DesiredRuntimeState.RUNNING
    assert _session(db) == session


@pytest.mark.parametrize("parent_target", [DesiredRuntimeState.STOPPED, DesiredRuntimeState.RUNNING])
def test_missing_host_record_refuses_console_start_before_accepting_intent(
    db: Database, make_config, monkeypatch, parent_target
):
    from agentworks.execution import trust
    from agentworks.execution.errors import TrustMissing
    from agentworks.plugins.proxmox.platform import ProxmoxPlatform
    from agentworks.secrets.policy import TtyInteractionPolicy
    from tests.runtime_state.test_composition import seed_vm

    seed_vm(db, "vm1", target=parent_target)
    _seed_sessions(db, ["alpha"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["alpha"])
    db.set_console_desired_runtime_state("con", DesiredRuntimeState.STOPPED)
    trust.remove("vm1")
    before = (_vm(db), _console(db), _session(db), db.list_console_sessions("con"), db._conn.total_changes)

    def forbidden(*args, **kwargs):
        pytest.fail("locally refused start reached a resolver, provider or guest")

    monkeypatch.setattr("agentworks.secrets.resolver.Resolver.__init__", forbidden)
    monkeypatch.setattr("agentworks.transports.transport", forbidden)
    for name in ("status", "start", "stop", "vm_active"):
        monkeypatch.setattr(ProxmoxPlatform, name, forbidden)

    with pytest.raises(TrustMissing):
        mc.start_console(db, make_config(), name="con", interaction=TtyInteractionPolicy.REFUSE)

    assert (_vm(db), _console(db), _session(db), db.list_console_sessions("con"), db._conn.total_changes) == before
    with native_mutation_guard(db.path, "vm1"):
        pass
