"""Guard-free preview isolates concurrent, authorized resource deletion."""

import pytest

from agentworks.db import DesiredRuntimeState, SessionStatus
from agentworks.errors import StateError
from agentworks.runtime_state import reconcile_vm
from agentworks.runtime_state._models import Outcome
from agentworks.sessions import manager
from tests.runtime_state.test_composition import REFUSE, seed_session, seed_vm
from tests.runtime_state.test_composition import runtime as runtime


@pytest.mark.parametrize("race_window", ["candidate", "reader"])
def test_preview_workspace_deletion_during_grouped_observation_isolated(
    db, make_config, runtime, monkeypatch, race_window
):
    from agentworks.db import Database
    from agentworks.sessions.manager._status import observe_session_statuses
    from agentworks.workspaces.manager import delete_workspace

    seed_vm(db, "bad")
    seed_session(db, "bad-child", "bad", target=DesiredRuntimeState.STOPPED)
    seed_vm(db, "good")
    seed_session(db, "good-child", "good", target=DesiredRuntimeState.STOPPED)
    rendered = []
    deleted: list[bool] = []
    config = make_config()
    monkeypatch.setattr("agentworks.vms.manager._is_tailscale_reachable", lambda *a, **k: True)
    monkeypatch.setattr(manager, "_teardown_session", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.workspaces.backends.vm.delete_vm_workspace", lambda *a, **k: None)

    def concurrent_delete() -> None:
        if deleted:
            return
        other = Database(db.path)
        try:
            delete_workspace(other, config, "ws-bad", force=True, yes=True, interaction=REFUSE)
        finally:
            other.close()
        deleted.append(True)

    if race_window == "candidate":
        from agentworks.runtime_state._prepare import session_vm_name

        def parent(invocation, session):
            if session.name == "bad-child":
                concurrent_delete()
            return session_vm_name(invocation, session)

        monkeypatch.setattr("agentworks.runtime_state._preview.session_vm_name", parent)

    def observe(rows, **kwargs):
        if race_window == "reader":
            concurrent_delete()
        return observe_session_statuses(rows, **kwargs)

    monkeypatch.setattr(manager, "observe_session_statuses", observe)
    monkeypatch.setattr(
        "agentworks.sessions.manager._status.batch_check_status",
        lambda rows, **k: {row.name: SessionStatus.STOPPED for row in rows},
    )
    monkeypatch.setattr("agentworks.runtime_state._preview.render_results", lambda rows, **k: rendered.extend(rows))
    with pytest.raises(StateError):
        reconcile_vm(db, config, all_vms=True, dry_run=True, interaction=REFUSE)
    results = {(row.kind, row.name): row for row in rendered}
    assert deleted
    assert results[("session", "bad-child")].outcome is Outcome.BLOCKED
    assert results[("session", "good-child")].outcome is Outcome.UNCHANGED
    assert results[("vm", "good")].outcome is Outcome.UNCHANGED
    assert db.get_workspace("ws-bad") is None and db.get_session("bad-child") is None
    assert db.get_session("good-child") is not None
    assert all(action == "hold" and name == "bad" for action, name in runtime[2])


def test_preview_console_deleted_after_route_selection_keeps_independent_results(db, make_config, runtime, monkeypatch):
    from agentworks.db import Database
    from agentworks.sessions import multi_console as consoles
    from tests.console_helpers import create_console_record

    seed_vm(db, "bad")
    seed_vm(db, "good")
    create_console_record(db, name="gone", vm_name="bad", session_specs=[], add_admin_shell=True)
    original_get = db.get_console
    config = make_config()
    deleted: list[bool] = []
    rendered = []
    monkeypatch.setattr(consoles, "_live_target", lambda *a, **k: None)

    def get_console(name):
        if name == "gone" and not deleted:
            other = Database(db.path)
            try:
                consoles.delete_console(other, config, name=name, yes=True)
            finally:
                other.close()
            deleted.append(True)
        return original_get(name)

    monkeypatch.setattr(db, "get_console", get_console)
    monkeypatch.setattr("agentworks.runtime_state._preview.render_results", lambda rows, **k: rendered.extend(rows))
    with pytest.raises(StateError):
        reconcile_vm(db, config, all_vms=True, dry_run=True, interaction=REFUSE)
    results = {(row.kind, row.name): row for row in rendered}
    gone = results[("console", "gone")]
    assert deleted and gone.outcome is Outcome.BLOCKED
    assert not gone.target_known and gone.desired_runtime_state is None
    assert results[("vm", "good")].outcome is Outcome.UNCHANGED
    assert not runtime[2]
