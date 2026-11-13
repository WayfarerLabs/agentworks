"""How workspace commands report a request that failed, and what they leave behind, over the recording access.

The orchestration fixtures are the lifecycle tests' own: a real config and
registry, and an admin double that models the guest by command line.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.db import InitStatus
from agentworks.errors import AlreadyExistsError
from agentworks.execution.errors import CommandFailed, PathRefusal, PathRefused
from agentworks.execution.requests import Command, PythonProgram
from agentworks.execution.results import Result, Unknown, UnknownReason
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.workspaces import manager as workspace_manager
from agentworks.workspaces.backends.vm import default_workspace_path
from tests.workspaces.test_lifecycle_orchestrated import (
    _OCCUPIED_PROBE,
    _FakeAdminTarget,
    _reachable,
    _route,
    _seed,
    _seed_vm_only,
)
from tests.workspaces.test_lifecycle_orchestrated import _stub_ssh_identity as _stub_ssh_identity
from tests.workspaces.test_lifecycle_orchestrated import make_config as make_config

if TYPE_CHECKING:
    from agentworks.db import Database
    from tests.conftest import CapturedOutput
    from tests.vm_access_support import Call


class _Guest(_FakeAdminTarget):
    """The lifecycle double, with a copy destination that is free and chosen command lines that fail.

    ``failures`` maps a command line's prefix to the stderr its non-zero exit carries.
    """

    def __init__(self, failures: dict[str, str] | None = None) -> None:
        super().__init__()
        self.failures = failures or {}

    def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
        stderr = next((text for prefix, text in self.failures.items() if cmd.startswith(prefix)), None)
        if stderr is not None:
            self.commands.append(cmd)
            return SimpleNamespace(ok=False, returncode=1, stdout="", stderr=stderr)
        if cmd.startswith(_OCCUPIED_PROBE):
            self.commands.append(cmd)
            return SimpleNamespace(ok=False, returncode=1, stdout="", stderr="")
        return super().run(cmd, **kwargs)


class _RehomeGuest(_Guest):
    """The target directory exists once it is made."""

    made = False

    def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
        if cmd == "test -d /dst/ws1" and not self.made:
            self.commands.append(cmd)
            return SimpleNamespace(ok=False, returncode=1, stdout="", stderr="")
        self.made |= cmd == "mkdir -p -- /dst/ws1"
        return super().run(cmd, **kwargs)


def _copy(db: Database, config: object) -> None:
    workspace_manager.copy_workspace(
        db,
        config,  # type: ignore[arg-type]
        "ws1",
        dest_name="ws2",
        vm_name="box",
        interaction=TtyInteractionPolicy.REFUSE,
    )


def _ready(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(db)
    _reachable(monkeypatch, True)


# -- Failure detail and the operation log -------------------------------------------------------


def test_a_failed_repair_step_warns_with_the_guests_stderr_and_repair_keeps_a_log(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,  # noqa: ANN001
) -> None:
    _ready(db, monkeypatch)
    guest = _Guest({"which setfacl": "", "apt-get install": "E: fixture apt failure"})
    recorder = _route(monkeypatch, guest)

    workspace_manager.repair_workspace(db, make_config(), "ws1", interaction=TtyInteractionPolicy.REFUSE)

    [warning] = [w for w in captured_output.warnings if w.startswith("acl package check failed")]
    assert "E: fixture apt failure" in warning
    assert recorder.logs and all(log is not None for log in recorder.logs)


def test_a_failed_rehome_names_its_log_and_where_the_workspace_now_is(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    _ready(db, monkeypatch)
    guest = _RehomeGuest({"chmod 2770 /dst/ws1": "chmod: fixture failure"})
    _route(monkeypatch, guest)

    with pytest.raises(CommandFailed) as caught:
        workspace_manager.rehome_workspace(
            db, make_config(), "ws1", target_path="/dst/ws1", yes=True, interaction=TtyInteractionPolicy.REFUSE
        )

    assert caught.value.hint is not None
    assert "workspace-rehome" in caught.value.hint and "/srv/ws1" in caught.value.hint


def test_a_failed_create_names_its_log(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    _seed_vm_only(db)
    _reachable(monkeypatch, True)
    _route(monkeypatch, _Guest({_OCCUPIED_PROBE: "", "chmod 2770": "chmod: fixture failure"}))

    with pytest.raises(CommandFailed) as caught:
        workspace_manager.create_workspace(
            db, make_config(), name="ws1", vm_name="box", interaction=TtyInteractionPolicy.REFUSE
        )

    assert caught.value.hint is not None and "workspace-create" in caught.value.hint


def test_a_failed_extraction_names_the_destinations_log_and_records_no_workspace(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    _ready(db, monkeypatch)
    _route(monkeypatch, _Guest({"tar xzf": "tar: fixture truncated archive"}))

    with pytest.raises(CommandFailed) as caught:
        _copy(db, make_config())

    assert caught.value.hint is not None and "workspace-copy.log" in caught.value.hint
    assert db.get_workspace("ws2") is None


# -- What copy leaves behind ----------------------------------------------------------------------


def test_copy_refuses_an_occupied_destination_before_extracting(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    _ready(db, monkeypatch)
    guest = _FakeAdminTarget()  # every probe succeeds, so the destination is occupied
    _route(monkeypatch, guest)

    with pytest.raises(AlreadyExistsError):
        _copy(db, make_config())

    assert not any(command.startswith(("tar xzf", "mkdir", "chown")) for command in guest.commands)
    assert db.get_workspace("ws2") is None


def test_copy_refused_at_its_tmuxinator_config_records_no_workspace(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    _ready(db, monkeypatch)
    guest = _Guest()
    recorder = _route(monkeypatch, guest)

    def answer(call: Call) -> Result | None:
        request = call.request
        if isinstance(request, PythonProgram) and request.args[:1] == ("write",):
            raise PathRefused("box", "write", "/opt/agentworks/workspaces/ws2/.tmuxinator.yml", PathRefusal.UNSUITABLE)
        return guest.answer(call)

    recorder.answer = answer

    with pytest.raises(PathRefused):
        _copy(db, make_config())

    assert db.get_workspace("ws2") is None


def test_copy_restores_the_workspace_mode_after_extraction(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    _ready(db, monkeypatch)
    guest = _Guest()
    _route(monkeypatch, guest)
    config = make_config()

    _copy(db, config)

    path = default_workspace_path(config, "ws2")
    extracted = next(i for i, command in enumerate(guest.commands) if command.startswith("tar xzf"))
    assert f"chmod 2770 {path}" in guest.commands[extracted:]


# -- The tmuxinator link ---------------------------------------------------------------------------


def _links(recorder: object) -> list[tuple[str, ...]]:
    return [
        request.argv
        for request in recorder.requests("run")  # type: ignore[attr-defined]
        if isinstance(request, Command) and request.argv[0] == "ln"
    ]


def test_create_never_replaces_an_existing_link_and_copy_and_rehome_replace_theirs(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed_vm_only(db)
    db.update_vm_init_status("box", InitStatus.COMPLETE)
    _reachable(monkeypatch, True)
    created = _route(monkeypatch, _Guest({_OCCUPIED_PROBE: ""}))
    workspace_manager.create_workspace(db, config, name="ws1", vm_name="box", interaction=TtyInteractionPolicy.REFUSE)
    copied = _route(monkeypatch, _Guest())
    _copy(db, config)
    rehomed = _route(monkeypatch, _RehomeGuest())
    workspace_manager.rehome_workspace(
        db, config, "ws1", target_path="/dst/ws1", yes=True, interaction=TtyInteractionPolicy.REFUSE
    )

    assert [argv[1] for argv in _links(created)] == ["-s"]
    assert [argv[1] for argv in _links(copied)] == ["-sf"]
    assert [argv[1] for argv in _links(rehomed)] == ["-sf"]


# -- Repair's bounded output ----------------------------------------------------------------------


def _over_the_limit(guest: _Guest, needle: str):  # noqa: ANN202
    def answer(call: Call) -> Result | None:
        request = call.request
        if isinstance(request, Command) and needle in request.argv:
            return Result(Unknown(UnknownReason.OUTPUT_LIMIT))
        return guest.answer(call)

    return answer


@pytest.mark.parametrize(
    ("needle", "reported"),
    [("chown", "permission fix failed"), ("getfacl", "the before/after check could not run")],
)
def test_repair_output_over_its_limit_is_never_read_as_ok_or_fixed(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
    needle: str,
    reported: str,
) -> None:
    _ready(db, monkeypatch)
    guest = _Guest()
    _route(monkeypatch, guest).answer = _over_the_limit(guest, needle)

    workspace_manager.repair_workspace(db, make_config(), "ws1", interaction=TtyInteractionPolicy.REFUSE)

    step = "directory ownership" if needle == "chown" else "ACLs"
    assert any(reported in warning for warning in captured_output.warnings)
    assert not any(step in detail for detail in captured_output.detail if detail.startswith(("OK:", "Fixed:")))
