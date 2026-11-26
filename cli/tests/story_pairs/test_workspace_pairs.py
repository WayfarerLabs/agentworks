"""Story pairs around workspace changes: a start in another workspace runs beside them; one in theirs is refused."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.db import PID_STOPPED, DesiredRuntimeState, ProvisioningStatus, SessionMode
from agentworks.errors import StateError
from agentworks.native_locks import NativeLockBusyError
from agentworks.runtime_state import _families as families
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager
from agentworks.workspaces import manager as workspace_manager
from tests.runtime_state.test_composition import runtime as runtime
from tests.story_pairs._pairs import Pause, labelled
from tests.vm_access_support import command_line, guest_result, is_command_check, route_vm_access_to_guest
from tests.workspaces._lifecycle_support import Guest, RehomeGuest, reachable, seed, seed_workspace, starts
from tests.workspaces._lifecycle_support import make_config as make_config
from tests.workspaces._lifecycle_support import stub_ssh_identity as stub_ssh_identity

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import Database
    from agentworks.execution.results import Result
    from tests.vm_access_support import Call

REFUSE = TtyInteractionPolicy.REFUSE
START = ("agw", "session", "start")


def _seed_stopped_session(db: Database, name: str, workspace: str) -> None:
    socket = f"/run/agentworks/admin-tmux-sockets/admin/{name}.sock"
    db.insert_session(
        name,
        workspace,
        "default",
        SessionMode.ADMIN,
        socket_path=socket,
        desired_runtime_state=DesiredRuntimeState.STOPPED,
    )
    db.update_session_runtime(name, socket_path=socket, pid=PID_STOPPED, boot_id=None, tmux_server_start_ticks=None)
    # A session start needs the VM provisioned, and its admin named as the socket path does.
    db.update_vm_provisioning_status("box", ProvisioningStatus.COMPLETE)
    db._conn.execute("UPDATE vms SET admin_username = 'admin' WHERE name = 'box'")
    db._conn.commit()


def _route(monkeypatch: pytest.MonkeyPatch, guest: Guest) -> None:
    """Route every access to ``guest``, answering a session start's own reads as a healthy VM would."""

    def answer(call: Call) -> Result | None:
        line = command_line(call.request)
        if is_command_check(call.request):
            return None
        if line is not None and "AGW_RUNTIME_STARTUP=" in line:
            return guest_result(stdout="AGW_RUNTIME_STARTUP=off\n")
        return guest.answer(call)

    route_vm_access_to_guest(monkeypatch, answer)


class _PausingAt:
    """``guest``, pausing the first operation the first time it runs a command ``matches`` names."""

    def __init__(self, guest: Guest, matches: Callable[[object], bool], pause: Pause) -> None:
        self._guest = guest
        self._matches = matches
        self._pause = pause

    def answer(self, call: Call) -> Result:
        if self._matches(call.request):
            self._pause.reached()
        return self._guest.answer(call)


def _start(config: object, name: str) -> Callable[[Database], object]:
    return lambda other: manager.start_session(other, config, name=name, interaction=REFUSE)  # type: ignore[arg-type]


def test_a_session_start_in_another_workspace_runs_beside_a_workspace_delete(
    db: Database, make_config, runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config()
    seed(db)
    seed_workspace(db, vm_name="box", name="other")
    _seed_stopped_session(db, "beside", "other")
    reachable(monkeypatch, True)
    pause = Pause(db.path, START, _start(config, "beside"))
    _route(monkeypatch, _PausingAt(Guest(), starts("rm", "-rf", "--", "/srv/ws1"), pause))  # type: ignore[arg-type]

    with labelled(("agw", "workspace", "delete")):
        workspace_manager.delete_workspace(db, config, "ws1", yes=True, interaction=REFUSE)

    assert pause.result().succeeded, pause.result().error
    assert db.get_workspace("ws1") is None
    assert ("session-start", "beside") in runtime[2]


def test_a_session_start_in_a_workspace_being_rehomed_is_refused_naming_the_rehome(
    db: Database, make_config, runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config()
    seed(db)
    _seed_stopped_session(db, "inside", "ws1")
    reachable(monkeypatch, True)
    refusals: list[NativeLockBusyError] = []
    from agentworks.native_locks import probe_locks as probe

    def recording_probe(*args: object, **kwargs: object) -> None:
        try:
            probe(*args, **kwargs)  # type: ignore[arg-type]
        except NativeLockBusyError as busy:
            refusals.append(busy)
            raise

    monkeypatch.setattr(families, "probe_locks", recording_probe)
    pause = Pause(db.path, START, _start(config, "inside"))
    _route(monkeypatch, _PausingAt(RehomeGuest(), starts("rsync"), pause))  # type: ignore[arg-type]

    rehome = ("agw", "workspace", "rehome")
    with labelled(rehome):
        workspace_manager.rehome_workspace(db, config, "ws1", target_path="/dst/ws1", yes=True, interaction=REFUSE)

    error = pause.result().error
    assert isinstance(error, StateError) and error.entity_name == "inside"
    [refused] = refusals
    assert refused.entity_kind == "workspace"
    assert refused.holder is not None
    assert refused.holder.command == rehome
    assert refused.holder.entity == refused.entity.key
    assert refused.holder.held_for >= 0
    assert ("session-start", "inside") not in runtime[2]
    workspace = db.get_workspace("ws1")
    assert workspace is not None and workspace.workspace_path == "/dst/ws1"
