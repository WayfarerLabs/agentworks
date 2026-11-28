"""Console lists are rewritten from what the console's final set shows, so a concurrent edit is kept or refuses."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.errors import StateConflictError
from agentworks.native_locks import NativeLockBusyError, native_locks
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import multi_console as mc
from agentworks.sessions.multi_console._helpers import console_request
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests._tmux_model import TmuxModel
from tests.console_helpers import create_console_record

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import Database
    from tests.conftest import _ScriptedGuest

REFUSE = TtyInteractionPolicy.REFUSE


def _shells(db: Database, session: str) -> list[object]:
    member = db.get_console_session("con", session)
    assert member is not None
    return [shell["cwd"] for shell in member.shells]


def test_two_concurrent_add_shells_on_one_member_keep_both_shells(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["a"])
    monkeypatch.setattr(mc, "_pane_secret_target", lambda *a, **k: object())
    second: list[str] = []

    def resolve(*args: object, **kwargs: object) -> dict[str, str]:
        # The first add-shell has read the member and resolves its pane's secret unlocked;
        # the second add-shell runs to completion meanwhile.
        if not second:
            second.append("ran")
            mc.add_shell(db, _StubConfig(), console_name="con", session_name="a", cwd="second", interaction=REFUSE)
        return {}

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", resolve)

    mc.add_shell(db, _StubConfig(), console_name="con", session_name="a", cwd="first", interaction=REFUSE)

    assert _shells(db, "a") == ["second", "first"]


def test_an_add_shell_while_another_holds_the_console_refuses_and_a_retry_keeps_both(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["a"])
    console = db.get_console("con")
    assert console is not None
    with native_locks(db.path, console_request(db, console)):
        with pytest.raises(NativeLockBusyError):
            mc.add_shell(db, _StubConfig(), console_name="con", session_name="a", cwd="held", interaction=REFUSE)
        db.update_console_shells("con", "a", [{"cwd": "holder", "admin": False}])
    mc.add_shell(db, _StubConfig(), console_name="con", session_name="a", cwd="retry", interaction=REFUSE)
    assert _shells(db, "a") == ["holder", "retry"]


@pytest.mark.parametrize("changed", [True, False], ids=["plan-changed", "plan-unchanged"])
def test_a_restore_whose_pane_plan_changed_before_its_final_set_refuses(
    db: Database, console_guest_factory: Callable[..., _ScriptedGuest], monkeypatch: pytest.MonkeyPatch, changed: bool
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["a+2"])
    model = TmuxModel()
    # The session pane and shell 0 are live; restore plans to open shell 1 and resolves its secrets.
    model.seed_session("aw-console-con", "a", pane_tags=(None, 0))
    guest = console_guest_factory(model)
    from agentworks.secrets.resolver import Resolver

    original_resolve = Resolver.resolve
    resolved: list[int] = []

    def resolve_then_the_plan_changes(self: Resolver) -> None:
        original_resolve(self)
        resolved.append(1)
        if not changed:
            return
        # Another console edit, done before restore takes the console, gives the member a third shell
        # whose pane secret this restore never resolved.
        db.update_console_shells(
            "con", "a", [{"cwd": None, "admin": False}, {"cwd": None, "admin": False}, {"cwd": "new", "admin": True}]
        )

    monkeypatch.setattr(Resolver, "resolve", resolve_then_the_plan_changes)
    guest.commands.clear()

    if not changed:
        # The same restore with its plan intact opens the pane it planned.
        mc.restore_session(db, _StubConfig(), console_name="con", session_name="a", interaction=REFUSE)
        rows = model.pane_rows("aw-console-con", "a")
        assert rows is not None and [tag for _pid, _pidx, tag in rows] == [None, 0, 1]
        return
    with pytest.raises(StateConflictError):
        mc.restore_session(db, _StubConfig(), console_name="con", session_name="a", interaction=REFUSE)

    assert resolved == [1]
    assert not any("split-window" in command for command in guest.commands)
    rows = model.pane_rows("aw-console-con", "a")
    assert rows is not None and [tag for _pid, _pidx, tag in rows] == [None, 0]


def test_a_member_added_as_the_console_is_locked_refuses_the_edit(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks import native_locks

    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["a"])
    lock = native_locks.native_locks

    def another_edit_adds_a_member_then_lock(*args: object, **kwargs: object) -> object:
        # Between building the console's request and holding it, another edit adds a member.
        if db.get_console_session("con", "b") is None:
            db.add_console_session("con", "b", [])
        return lock(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(native_locks, "native_locks", another_edit_adds_a_member_then_lock)

    with pytest.raises(StateConflictError):
        mc.add_shell(db, _StubConfig(), console_name="con", session_name="a", cwd="first", interaction=REFUSE)

    assert _shells(db, "a") == []
