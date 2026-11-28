"""Workspace trees locked as the guest's lookup walk names them: which tree operations refuse each other."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.errors import StateConflictError
from agentworks.native_locks import LockMode, LockRequest, NativeLockBusyError, native_locks, workspace_entity
from agentworks.native_locks.path_walk import PathWalk
from agentworks.workspaces import locks
from agentworks.workspaces.locks import TreeLocks, with_trees

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.windows

VM = "dev"


def _walk(final: str, *, directories: tuple[str, ...] = (), links: tuple[str, ...] = (), path: str = "") -> PathWalk:
    return PathWalk(path or final, directories, links, final)


def _tree(*walks: PathWalk, unlink_only: bool = False) -> TreeLocks:
    return TreeLocks(VM, tuple(walk.path for walk in walks), unlink_only, walks)


def _plain(path: str) -> PathWalk:
    """The walk of a path with no links: every proper prefix is a directory it looks a name up in."""
    parts = path.strip("/").split("/")
    return _walk(path, directories=tuple("/" + "/".join(parts[:index]) for index in range(1, len(parts))))


def _rehome(workspace: str, source: str, target: str) -> LockRequest:
    return with_trees(LockRequest(exclusive=(workspace_entity(VM, workspace),)), _tree(_plain(source), _plain(target)))


def _refuse_each_other(database: Path, first: LockRequest, second: LockRequest) -> None:
    for held, asked in ((first, second), (second, first)):
        with native_locks(database, held), pytest.raises(NativeLockBusyError), native_locks(database, asked):
            pass


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


def test_a_rehome_into_a_tree_and_a_rehome_of_that_tree_refuse_each_other(database: Path) -> None:
    _refuse_each_other(database, _rehome("a", "/srv/a", "/data/b/new"), _rehome("b", "/data/b", "/srv/b"))


def test_a_rehome_of_a_tree_and_a_rehome_inside_it_refuse_each_other(database: Path) -> None:
    _refuse_each_other(database, _rehome("t", "/tree", "/copy"), _rehome("c", "/tree/before", "/tree/after"))


def test_creates_of_disjoint_paths_run_together(database: Path) -> None:
    first = with_trees(LockRequest(exclusive=(workspace_entity(VM, "a"),)), _tree(_plain("/data/a")))
    second = with_trees(LockRequest(exclusive=(workspace_entity(VM, "b"),)), _tree(_plain("/data/b")))
    with native_locks(database, first), native_locks(database, second):
        pass


def test_every_directory_and_link_a_walk_follows_is_held(database: Path) -> None:
    # /front/link -> /middle/bridge -> /physical, stored source /front/link/a.
    through_links = _walk(
        "/physical/a",
        directories=("/front", "/middle", "/physical"),
        links=("/front/link", "/middle/bridge"),
        path="/front/link/a",
    )
    request = with_trees(LockRequest(exclusive=(workspace_entity(VM, "a"),)), _tree(through_links))
    rehome_of_middle = _rehome("m", "/middle", "/elsewhere")
    delete_of_link = with_trees(
        LockRequest(exclusive=(workspace_entity(VM, "l"),)),
        _tree(_walk("/front/link", directories=("/front",)), unlink_only=True),
    )
    _refuse_each_other(database, request, rehome_of_middle)
    _refuse_each_other(database, request, delete_of_link)


def test_a_delete_of_a_looping_link_locks_only_its_entry(database: Path) -> None:
    loop = _tree(_walk("/w/loop", directories=("/w",)), unlink_only=True)
    delete = with_trees(LockRequest(exclusive=(workspace_entity(VM, "loop"),)), loop)
    keys = {entity.key: mode for entity, mode in delete.modes().items()}
    assert keys["path/w/loop"] is LockMode.EXCLUSIVE
    assert keys["path/w"] is LockMode.SHARED
    # A sibling tree under the same directory proceeds beside it.
    with native_locks(database, delete), native_locks(database, _rehome("s", "/w/sibling", "/srv/s")):
        pass


def test_a_walk_that_changes_before_the_set_is_held_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    before = _tree(_plain("/data/ws"))
    moved = TreeLocks(VM, before.paths, False, (_walk("/other/ws", directories=("/other",), path="/data/ws"),))
    monkeypatch.setattr(locks, "walk_trees", lambda vm, config, paths, *, unlink_only=False: moved)
    with pytest.raises(StateConflictError):
        locks.require_trees_unchanged(type("VMRow", (), {"name": VM})(), object(), before)  # type: ignore[arg-type]
