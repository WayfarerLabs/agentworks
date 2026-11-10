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
    from agentworks.db import Database


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
            vm=db.get_vm("vm1"),
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
            vm=db.get_vm("vm1"),
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
            vm=db.get_vm("vm1"),
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
    session = db.get_session("alpha")
    mc.set_console_desired_runtime_state(db, name="con", state="stopped")
    assert db.get_console("con").desired_runtime_state is DesiredRuntimeState.STOPPED
    assert db.get_session("alpha") == session
    with pytest.raises(ValidationError):
        mc.set_console_desired_runtime_state(db, name="con", state="on-demand")
    assert db.get_console("con").desired_runtime_state is DesiredRuntimeState.STOPPED


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
