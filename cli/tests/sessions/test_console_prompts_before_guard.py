"""Console edits ask their questions and resolve their secrets before the VM's guard, then re-read under it."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from agentworks import output
from agentworks.errors import StateConflictError
from agentworks.harness_setup.locking import NativeSetupBusyError, native_mutation_guard
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import multi_console as mc
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests.console_helpers import create_console_record

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import Database


def _guard_free(db: Database) -> bool:
    try:
        with native_mutation_guard(db.path, "vm1"):
            return True
    except NativeSetupBusyError:
        return False


def _resolving(db: Database, monkeypatch: pytest.MonkeyPatch, during: list[bool], then: object = None) -> None:
    """Give the edit's panes a secret target and record, as it resolves, whether the guard was free."""

    def resolve(*args: object, **kwargs: object) -> dict[str, str]:
        during.append(_guard_free(db))
        if callable(then):
            then()
        return {}

    monkeypatch.setattr(mc, "_pane_secret_target", lambda *a, **k: object())
    monkeypatch.setattr(mc, "_member_secret_targets", lambda *a, **k: [object()])
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", resolve)


def test_remove_sessions_offers_the_empty_console_once_its_guard_is_released(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["a"])
    asked: list[bool] = []
    monkeypatch.setattr(output, "is_interactive", lambda: True)
    monkeypatch.setattr(output, "confirm", lambda *a, **k: asked.append(_guard_free(db)) or True)

    mc.remove_sessions(db, _StubConfig(), console_name="con", session_names=["a"])

    assert asked == [True]
    assert db.get_console("con") is None


def test_remove_sessions_keeps_a_console_that_gains_a_member_while_its_offer_waits(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["a"])

    def answer(*args: object, **kwargs: object) -> bool:
        db.add_console_session("con", "b", [])
        return True

    monkeypatch.setattr(output, "is_interactive", lambda: True)
    monkeypatch.setattr(output, "confirm", answer)

    mc.remove_sessions(db, _StubConfig(), console_name="con", session_names=["a"])

    assert [m.session_name for m in db.list_console_sessions("con")] == ["b"]


def test_add_shell_resolves_unguarded_and_appends_to_the_list_read_under_the_guard(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shell another command appended while this one resolved is kept."""
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["a"])
    during: list[bool] = []
    _resolving(
        db,
        monkeypatch,
        during,
        then=lambda: db.update_console_shells("con", "a", [{"cwd": "other", "admin": False}]),
    )

    mc.add_shell(
        db, _StubConfig(), console_name="con", session_name="a", cwd="src", interaction=TtyInteractionPolicy.REFUSE
    )

    assert during == [True]
    member = db.get_console_session("con", "a")
    assert member is not None
    assert member.shells == [{"cwd": "other", "admin": False}, {"cwd": "src", "admin": False}]


def test_add_sessions_resolves_unguarded_and_keeps_a_member_added_before_the_guard(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positions come from the member list read under the guard, so a concurrent addition is kept."""
    _seed_vm(db)
    _seed_sessions(db, ["a", "b", "c"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["a"])
    during: list[bool] = []
    _resolving(db, monkeypatch, during, then=lambda: db.add_console_session("con", "c", []))

    mc.add_sessions(
        db, _StubConfig(), console_name="con", session_specs=["b+1"], interaction=TtyInteractionPolicy.REFUSE
    )

    assert during == [True]
    assert [m.session_name for m in db.list_console_sessions("con")] == ["a", "c", "b"]


def test_add_sessions_refuses_a_console_replaced_before_the_guard(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["a"])
    during: list[bool] = []

    def replace_the_console() -> None:
        db.delete_console("con")
        create_console_record(db, name="con", vm_name="vm1", session_specs=["a"], add_admin_shell=True)

    _resolving(db, monkeypatch, during, then=replace_the_console)

    with pytest.raises(StateConflictError):
        mc.add_sessions(
            db, _StubConfig(), console_name="con", session_specs=["b+1"], interaction=TtyInteractionPolicy.REFUSE
        )

    assert [m.session_name for m in db.list_console_sessions("con")] == ["a"]


def _replace_session(db: Database, name: str) -> None:
    """Give the session row a new identity, keeping its console memberships."""
    db._conn.execute("UPDATE sessions SET session_uuid = ? WHERE name = ?", (str(uuid4()), name))
    db._conn.commit()


@pytest.mark.parametrize(
    "change", [lambda db: db.delete_session("b"), lambda db: _replace_session(db, "b")], ids=["deleted", "replaced"]
)
def test_add_sessions_refuses_a_session_changed_before_the_guard(
    db: Database, monkeypatch: pytest.MonkeyPatch, change: Callable[[Database], None]
) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["a"])
    _resolving(db, monkeypatch, [], then=lambda: change(db))

    with pytest.raises(StateConflictError):
        mc.add_sessions(
            db, _StubConfig(), console_name="con", session_specs=["b+1"], interaction=TtyInteractionPolicy.REFUSE
        )

    assert [m.session_name for m in db.list_console_sessions("con")] == ["a"]


def test_add_shell_refuses_a_session_replaced_before_the_guard(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["a"])
    _resolving(db, monkeypatch, [], then=lambda: _replace_session(db, "a"))

    with pytest.raises(StateConflictError):
        mc.add_shell(
            db, _StubConfig(), console_name="con", session_name="a", cwd="src", interaction=TtyInteractionPolicy.REFUSE
        )

    member = db.get_console_session("con", "a")
    assert member is not None and member.shells == []
