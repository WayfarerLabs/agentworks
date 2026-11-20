"""How workspace commands report a request that failed, and what they leave behind, over the recording access.

The orchestration fixtures are the lifecycle tests' own: a real config and
registry, and a guest double that answers the admin's typed requests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.db import InitStatus
from agentworks.errors import AlreadyExistsError, ExternalError
from agentworks.execution.errors import CommandFailed, ExecutionIncomplete, PathRefusal, PathRefused
from agentworks.execution.requests import Command, PythonProgram
from agentworks.execution.results import Result, Unknown, UnknownReason
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.workspaces import manager as workspace_manager
from agentworks.workspaces.backends.vm import default_workspace_path
from tests.workspaces._lifecycle_support import (
    FailingGuest,
    Guest,
    RehomeGuest,
    argv,
    reachable,
    route,
    seed,
    seed_vm_only,
    starts,
)
from tests.workspaces._lifecycle_support import make_config as make_config
from tests.workspaces._lifecycle_support import stub_ssh_identity as stub_ssh_identity

if TYPE_CHECKING:
    from agentworks.db import Database
    from tests.conftest import CapturedOutput
    from tests.vm_access_support import Call


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
    seed(db)
    reachable(monkeypatch, True)


# -- Failure detail and the operation log -------------------------------------------------------


def test_a_failed_repair_step_warns_with_the_guests_stderr_and_repair_keeps_a_log(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,  # noqa: ANN001
) -> None:
    _ready(db, monkeypatch)
    guest = FailingGuest({("which", "setfacl"): "", ("apt-get", "install"): "E: fixture apt failure"})
    recorder = route(monkeypatch, guest)

    workspace_manager.repair_workspace(db, make_config(), "ws1", interaction=TtyInteractionPolicy.REFUSE)

    assert len([w for w in captured_output.warnings if "E: fixture apt failure" in w]) == 1
    assert recorder.logs and all(log is not None for log in recorder.logs)


def test_a_failed_rehome_names_its_log_and_where_the_workspace_now_is(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    _ready(db, monkeypatch)
    guest = RehomeGuest(fails=(("chmod", "2770", "/dst/ws1"),))
    route(monkeypatch, guest)

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
    seed_vm_only(db)
    reachable(monkeypatch, True)
    route(monkeypatch, FailingGuest({("chmod", "2770"): "chmod: fixture failure"}))

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
    route(monkeypatch, FailingGuest({("tar", "xzf"): "tar: fixture truncated archive"}))

    with pytest.raises(CommandFailed) as caught:
        _copy(db, make_config())

    assert caught.value.hint is not None and "workspace-copy.log" in caught.value.hint
    assert db.get_workspace("ws2") is None


@pytest.mark.parametrize("side", ["source", "destination"])
def test_a_copy_that_fails_outside_a_request_names_the_log_of_the_side_that_failed(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    side: str,  # noqa: ANN001
) -> None:
    _ready(db, monkeypatch)
    guest = FailingGuest()
    recorder = route(monkeypatch, guest)
    failing = "czf" if side == "source" else "xzf"

    def answer(call: Call) -> Result | None:
        request = call.request
        if isinstance(request, Command) and failing in request.argv:
            raise OSError(5, "Input/output error")
        return guest.answer(call)

    recorder.answer = answer

    with pytest.raises(ExternalError) as caught:
        _copy(db, make_config())

    log = "workspace-copy-source" if side == "source" else "workspace-copy.log"
    assert caught.value.hint is not None and log in caught.value.hint
    assert isinstance(caught.value.__cause__, OSError)
    assert db.get_workspace("ws2") is None


def test_a_source_read_that_does_not_complete_names_its_log(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    _ready(db, monkeypatch)
    guest = FailingGuest()
    recorder = route(monkeypatch, guest)
    lost = ExecutionIncomplete("box", Result(Unknown(UnknownReason.CONNECTION)))

    def answer(call: Call) -> Result | None:
        request = call.request
        if isinstance(request, Command) and "czf" in request.argv:
            raise lost
        return guest.answer(call)

    recorder.answer = answer

    with pytest.raises(ExecutionIncomplete) as caught:
        _copy(db, make_config())

    assert caught.value is lost and caught.value.hint is not None
    assert "workspace-copy-source" in caught.value.hint


# -- What copy leaves behind ----------------------------------------------------------------------


def test_copy_refuses_an_occupied_destination_before_extracting(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    _ready(db, monkeypatch)
    guest = Guest()  # every probe succeeds, so the destination is occupied
    route(monkeypatch, guest)

    with pytest.raises(AlreadyExistsError):
        _copy(db, make_config())

    assert not any(
        starts(*prefix)(request) for request in guest.requests for prefix in (("tar", "xzf"), ("mkdir",), ("chown",))
    )
    assert db.get_workspace("ws2") is None


def test_copy_refused_at_its_tmuxinator_config_records_no_workspace(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    _ready(db, monkeypatch)
    guest = FailingGuest()
    recorder = route(monkeypatch, guest)

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
    guest = FailingGuest()
    route(monkeypatch, guest)
    config = make_config()

    _copy(db, config)

    path = default_workspace_path(config, "ws2")
    extracted = guest.index(starts("tar", "xzf"))
    assert ("chmod", "2770", path) in [argv(request) for request in guest.requests[extracted:]]


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
    seed_vm_only(db)
    db.update_vm_init_status("box", InitStatus.COMPLETE)
    reachable(monkeypatch, True)
    created = route(monkeypatch, FailingGuest())
    workspace_manager.create_workspace(db, config, name="ws1", vm_name="box", interaction=TtyInteractionPolicy.REFUSE)
    copied = route(monkeypatch, FailingGuest())
    _copy(db, config)
    rehomed = route(monkeypatch, RehomeGuest())
    workspace_manager.rehome_workspace(
        db, config, "ws1", target_path="/dst/ws1", yes=True, interaction=TtyInteractionPolicy.REFUSE
    )

    assert [argv[1] for argv in _links(created)] == ["-s"]
    assert [argv[1] for argv in _links(copied)] == ["-sf"]
    assert [argv[1] for argv in _links(rehomed)] == ["-sf"]


# -- Repair's bounded output ----------------------------------------------------------------------


def _over_the_limit(guest: Guest, needle: str):  # noqa: ANN202
    def answer(call: Call) -> Result | None:
        request = call.request
        if isinstance(request, Command) and needle in request.argv:
            return Result(Unknown(UnknownReason.OUTPUT_LIMIT))
        return guest.answer(call)

    return answer


@pytest.mark.parametrize("needle", ["chown", "getfacl"])
def test_repair_output_over_its_limit_is_never_read_as_ok_or_fixed(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
    needle: str,
) -> None:
    """Each repair step reports one OK or Fixed detail, or one warning; the limited step's is a warning."""
    _ready(db, monkeypatch)
    guest = FailingGuest()
    recorder = route(monkeypatch, guest)
    workspace_manager.repair_workspace(db, make_config(), "ws1", interaction=TtyInteractionPolicy.REFUSE)
    details, warnings = len(captured_output.detail), len(captured_output.warnings)

    recorder.answer = _over_the_limit(guest, needle)
    workspace_manager.repair_workspace(db, make_config(), "ws1", interaction=TtyInteractionPolicy.REFUSE)

    assert len(captured_output.detail) - details == details - 1
    assert len(captured_output.warnings) - warnings == warnings + 1
