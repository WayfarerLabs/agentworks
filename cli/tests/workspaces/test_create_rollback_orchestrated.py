"""``workspace create``'s rollback: a failed create leaves no directory and no group behind.

After ``create_vm_workspace`` returns (issue #249), realize's rollback removes
both; inside it (issue #262), the backend's own cleanup does, and an interrupt
during the clone still lands that cleanup and keeps the original interrupt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.errors import ExternalError
from agentworks.execution.errors import CommandFailed
from agentworks.execution.results import Result
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.workspaces import manager as workspace_manager
from agentworks.workspaces.guest import ensure_group
from tests.conftest import ManifestDoc
from tests.workspaces._lifecycle_support import (
    Guest,
    argv,
    is_occupancy_probe,
    reachable,
    route,
    seed_vm_only,
    starts,
)
from tests.workspaces._lifecycle_support import make_config as make_config
from tests.workspaces._lifecycle_support import stub_ssh_identity as stub_ssh_identity

if TYPE_CHECKING:
    from agentworks.db import Database
    from tests.conftest import CapturedOutput

REMOVE_GROUP = ("/usr/sbin/groupdel", "ws-ws1")
CLONE = starts("git", "clone")


def _removes_the_directory(request: object) -> bool:
    """The workspace's directory, or the clone attempt beside it that a failed clone leaves."""
    return starts("rm", "-rf")(request) and "/ws1" in argv(request)[-1]


def _boom_after_create(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a failure AFTER ``create_vm_workspace`` returns: the VS Code
    stub step raises, so ``workspace_path`` is already set and the
    rollback's on-VM teardown (directory + fresh group) fires."""

    def _boom(*args: object, **kwargs: object) -> str:
        raise RuntimeError("stub exploded")

    monkeypatch.setattr("agentworks.workspaces.backends.vm.generate_vscode_workspace", _boom)


def _create(db: Database, config: object) -> None:
    workspace_manager.create_workspace(
        db,
        config,  # type: ignore[arg-type]
        name="ws1",
        vm_name="box",
        interaction=TtyInteractionPolicy.REFUSE,
    )


def test_create_rollback_removes_the_fresh_workspace_group(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """A create that fails after ``create_vm_workspace`` returns rolls back
    the on-VM state through ``delete_vm_workspace``, which also removes the
    group create just made, after the directory, mirroring the delete path.
    The free path lets create proceed."""
    fake = Guest(fails=(is_occupancy_probe,))
    route(monkeypatch, fake)
    seed_vm_only(db)
    reachable(monkeypatch, True)
    _boom_after_create(monkeypatch)

    with pytest.raises(ExternalError, match="creating workspace: stub exploded"):
        _create(db, make_config())

    assert fake.index(starts(*REMOVE_GROUP)) > fake.index(_removes_the_directory)
    assert db.get_workspace("ws1") is None


def test_create_rollback_tolerates_a_group_removal_failure(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """A group removal that fails during create rollback does not mask the
    original create error, nor turn the rollback itself into a warning."""
    fake = Guest(fails=(is_occupancy_probe, REMOVE_GROUP))
    route(monkeypatch, fake)
    seed_vm_only(db)
    reachable(monkeypatch, True)
    _boom_after_create(monkeypatch)

    with pytest.raises(ExternalError, match="creating workspace: stub exploded"):
        _create(db, make_config())

    assert REMOVE_GROUP in fake.argvs()
    assert db.get_workspace("ws1") is None
    assert not any("rollback during workspace create" in w for w in captured_output.warnings)


def test_create_rollback_on_clone_failure_leaves_no_residue(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """A create that fails INSIDE ``create_vm_workspace`` (the git clone,
    after the group and directory were already made) leaves no residue:
    ``create_vm_workspace`` cleans exactly what it created (issue #262), once,
    since realize's rollback runs only when it returns. The clone's error
    type propagates with the workspace's identity, the directory and then
    the group are removed, and no row survives."""
    fake = Guest(fails=(is_occupancy_probe, CLONE))
    route(monkeypatch, fake)
    config = make_config(
        manifests=[ManifestDoc("workspace-template", "default", {"repo": "https://example.com/repo.git"})]
    )
    seed_vm_only(db)
    reachable(monkeypatch, True)

    with pytest.raises(CommandFailed) as caught:
        _create(db, config)

    assert caught.value.entity_kind == "workspace" and caught.value.entity_name == "ws1"
    assert fake.argvs().count(REMOVE_GROUP) == 1
    assert fake.index(starts(*REMOVE_GROUP)) > fake.index(_removes_the_directory)
    assert db.get_workspace("ws1") is None
    # The teardown succeeded, so its own failure warning must not fire, and
    # realize's post-return rollback never engaged.
    assert not captured_output.warnings


@pytest.mark.parametrize("cleanup_interrupt", [False, True])
def test_cancel_during_clone_cleans_directory_and_group_and_preserves_original_interrupt(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    cleanup_interrupt: bool,
) -> None:
    interrupt = KeyboardInterrupt("fixture clone cancellation")
    resources: set[str] = set()

    class _Guest(Guest):
        def respond(self, request: object) -> Result | None:
            if CLONE(request):
                assert resources == {"directory", "group"}
                raise interrupt
            if request == ensure_group("ws-ws1"):
                resources.add("group")
            elif starts("mkdir")(request):
                resources.add("directory")
            elif starts("rm", "-rf")(request):
                resources.discard("directory")
            elif starts(*REMOVE_GROUP)(request):
                resources.discard("group")
                if cleanup_interrupt:
                    raise KeyboardInterrupt("fixture second interruption during cleanup")
            return super().respond(request)

    fake = _Guest(fails=(is_occupancy_probe,))
    route(monkeypatch, fake)
    config = make_config(
        manifests=[ManifestDoc("workspace-template", "default", {"repo": "https://example.com/repo.git"})]
    )
    seed_vm_only(db)
    reachable(monkeypatch, True)

    with pytest.raises(KeyboardInterrupt) as caught:
        _create(db, config)

    assert caught.value is interrupt
    assert not resources and db.get_workspace("ws1") is None
    directory = [index for index, request in enumerate(fake.requests) if starts("rm", "-rf")(request)]
    group = [index for index, request in enumerate(fake.requests) if starts(*REMOVE_GROUP)(request)]
    assert len(directory) == len(group) == 1 and directory[0] < group[0]
