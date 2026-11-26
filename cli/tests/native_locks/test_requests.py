"""Building requests, probing them, and reusing a held set for nested operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.errors import StateConflictError, StateError
from agentworks.native_locks import (
    Entity,
    EntityKind,
    LockMode,
    LockRequest,
    NativeLockBusyError,
    console_entity,
    grant_all_entity,
    grant_entity,
    native_locks,
    path_entity,
    probe_locks,
    vm_entity,
    workspace_entity,
)
from agentworks.native_locks import _sets as sets_module

from ._lock_support import VM, account, labelled, records, session, workspace

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.windows


# -- Requests -------------------------------------------------------------


def test_request_holds_every_ancestor_shared_in_lock_order() -> None:
    request = LockRequest(exclusive=(session(),), shared=(workspace("other"),))
    assert [(entity.key, mode) for entity, mode in request.modes().items()] == [
        (f"vm/{VM}", LockMode.SHARED),
        ("workspace/other", LockMode.SHARED),
        ("workspace/w", LockMode.SHARED),
        ("account/a", LockMode.SHARED),
        ("session/s", LockMode.EXCLUSIVE),
    ]


def test_path_entities_hold_every_prefix_but_not_the_root() -> None:
    request = LockRequest(exclusive=(path_entity(VM, "/data/b/new"),))
    assert [(entity.key, mode) for entity, mode in request.modes().items()] == [
        (f"vm/{VM}", LockMode.SHARED),
        ("path/data", LockMode.SHARED),
        ("path/data/b", LockMode.SHARED),
        ("path/data/b/new", LockMode.EXCLUSIVE),
    ]


def test_vm_wide_sets_are_keyed_by_their_vm() -> None:
    assert grant_all_entity(VM).key == f"grant-all/{VM}"
    assert grant_all_entity(VM) != grant_all_entity("other")


def test_one_key_with_different_parents_is_refused() -> None:
    first_read = session(workspace_name="w1")
    second_read = session(workspace_name="w2")
    with pytest.raises(StateConflictError) as caught:
        LockRequest(exclusive=(first_read,), shared=(second_read,))
    assert (caught.value.entity_kind, caught.value.entity_name) == ("session", "s")


def test_console_members_compare_as_a_set() -> None:
    members = (session("s1"), session("s2"))
    LockRequest(exclusive=(console_entity(VM, "c", members=members), console_entity(VM, "c", members=members[::-1])))


@pytest.mark.parametrize(
    "build",
    [
        lambda: LockRequest(),
        lambda: LockRequest(exclusive=(workspace_entity("one", "w"), workspace_entity("two", "w"))),
    ],
    ids=["empty", "two-vms"],
)
def test_request_needs_entities_on_one_vm(build: object) -> None:
    with pytest.raises(ValueError):
        build()  # type: ignore[operator]


@pytest.mark.parametrize("path", ["/", "relative", "/data/", "/data//b", "/data/../b", "/data/./b", ""])
def test_path_entity_needs_a_normalized_path_below_the_root(path: str) -> None:
    with pytest.raises(ValueError):
        path_entity(VM, path)


@pytest.mark.parametrize(
    ("kind", "name", "parents"),
    [
        (EntityKind.VM, VM, (vm_entity(VM),)),
        (EntityKind.VM, "other", ()),
        (EntityKind.WORKSPACE, "w", ()),
        (EntityKind.WORKSPACE, "w", (vm_entity("other"),)),
        (EntityKind.SESSION, "s", (workspace(), workspace("x"))),
        (EntityKind.SESSION, "s", (account(), workspace())),
        (EntityKind.GRANT, "w+b", (workspace(), account())),
        (EntityKind.PATH, "/data/b", (vm_entity(VM),)),
        (EntityKind.PATH, "/data", (path_entity(VM, "/srv"),)),
        (EntityKind.CONSOLE, "c", (vm_entity(VM), workspace())),
        (EntityKind.GRANT_ALL, "x", (vm_entity(VM),)),
    ],
)
def test_entities_enforce_their_parents(kind: EntityKind, name: str, parents: tuple[Entity, ...]) -> None:
    with pytest.raises(ValueError):
        Entity(VM, kind, name, parents)


# -- The probe ------------------------------------------------------------


def test_conflicting_probe_names_the_holder_and_writes_no_record(database: Path) -> None:
    with labelled("workspace", "delete"), native_locks(database, LockRequest(exclusive=(workspace(),))):
        held_records = records(database)
        with pytest.raises(NativeLockBusyError) as caught:
            probe_locks(database, LockRequest(exclusive=(session(),)))
        assert records(database) == held_records
    assert caught.value.holder is not None
    assert (caught.value.holder.command, caught.value.holder.entity) == (("agw", "workspace", "delete"), "workspace/w")


def test_free_probe_leaves_nothing_held(database: Path) -> None:
    request = LockRequest(exclusive=(vm_entity(VM), workspace(), account(), session()))
    probe_locks(database, request)
    assert records(database) == []
    with native_locks(database, request):
        pass


def test_probe_leaves_path_entities_to_the_acquisition(database: Path) -> None:
    request = LockRequest(exclusive=(workspace(), path_entity(VM, "/data/w")))
    with native_locks(database, LockRequest(exclusive=(path_entity(VM, "/data"),))):
        probe_locks(database, request)
        with pytest.raises(NativeLockBusyError) as caught, native_locks(database, request):
            pass
    assert (caught.value.entity_kind, caught.value.entity_name) == ("path", "/data")


# -- Nested reuse ---------------------------------------------------------


def test_require_acquires_nothing(database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = LockRequest(exclusive=(session(),))
    with native_locks(database, request) as held:
        monkeypatch.setattr(sets_module, "open_lock_file", lambda path: pytest.fail("require opened a lock file"))
        held.require(request, database_path=database)
        held.require(LockRequest(shared=(session(),)), database_path=database)


@pytest.mark.parametrize(
    "nested",
    [
        LockRequest(exclusive=(workspace("other"),)),
        LockRequest(exclusive=(workspace(),)),
        LockRequest(exclusive=(workspace_entity("second", "w"),)),
    ],
    ids=["uncovered", "weaker", "other-vm"],
)
def test_require_refuses_what_the_set_does_not_cover(database: Path, nested: LockRequest) -> None:
    with native_locks(database, LockRequest(exclusive=(session(),))) as held, pytest.raises(StateError):
        held.require(nested, database_path=database)


def test_require_refuses_another_database(database: Path, tmp_path: Path) -> None:
    request = LockRequest(exclusive=(session(),))
    with native_locks(database, request) as held, pytest.raises(StateError):
        held.require(request, database_path=tmp_path / "other.db")


def test_require_refuses_a_released_set(database: Path) -> None:
    request = LockRequest(exclusive=(session(),))
    with native_locks(database, request) as held:
        pass
    with pytest.raises(StateError):
        held.require(request, database_path=database)


def test_vm_exclusive_covers_everything_on_its_vm(database: Path) -> None:
    with native_locks(database, LockRequest(exclusive=(vm_entity(VM),))) as held:
        held.require(LockRequest(exclusive=(account("admin"),)), database_path=database)
        held.require(LockRequest(exclusive=(session(), path_entity(VM, "/data/w"))), database_path=database)


def test_an_exclusive_parent_does_not_cover_a_grant_without_its_other_parent(database: Path) -> None:
    grant = LockRequest(exclusive=(grant_entity(workspace(), account()),))
    with native_locks(database, LockRequest(exclusive=(account(),))) as held, pytest.raises(StateError):
        held.require(grant, database_path=database)
    with native_locks(database, LockRequest(exclusive=(account(), grant_entity(workspace(), account())))) as held:
        held.require(grant, database_path=database)


def test_a_console_whose_members_changed_still_matches_by_key(database: Path) -> None:
    before = console_entity(VM, "c", members=(session("s1"), session("s2")))
    after = console_entity(VM, "c", members=(session("s1"),))
    with native_locks(database, LockRequest(exclusive=(before,))) as held:
        held.require(LockRequest(exclusive=(after,)), database_path=database)
