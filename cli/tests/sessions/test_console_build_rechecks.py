"""``console create`` and ``restore-session`` resolve with nothing locked, then refuse what changed before the lock."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from agentworks.errors import StateConflictError
from agentworks.native_locks import NativeLockBusyError, native_locks
from agentworks.native_locks.rows import vm_exclusive
from agentworks.secrets import SecretTarget
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.secrets.resolver import Resolver
from agentworks.sessions import multi_console as mc
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests._tmux_model import TmuxModel
from tests.console_helpers import create_console_record

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import ConsoleSessionRow, Database
    from tests.conftest import _ScriptedGuest

CON = "aw-console-con"
PANE = SecretTarget(vm={}, label="console-pane:pin")


def _vm_unlocked(db: Database) -> bool:
    try:
        with native_locks(db.path, vm_exclusive("vm1")):
            return True
    except NativeLockBusyError:
        return False


def _resolving(db: Database, monkeypatch: pytest.MonkeyPatch, then: Callable[[], None] | None = None) -> list[bool]:
    """Record, at each boundary resolve, whether the lock was free; ``then`` runs after the first."""
    during: list[bool] = []
    resolve = Resolver.resolve

    def resolving(resolver: Resolver) -> None:
        during.append(_vm_unlocked(db))
        resolve(resolver)
        if then is not None and len(during) == 1:
            then()

    monkeypatch.setattr(Resolver, "resolve", resolving)
    return during


def _replace_session(db: Database, name: str) -> None:
    """Give the session row a new identity, keeping its console memberships."""
    db._conn.execute("UPDATE sessions SET session_uuid = ? WHERE name = ?", (str(uuid4()), name))
    db._conn.commit()


# -- console create ------------------------------------------------------------


def _create(db: Database) -> None:
    mc.create_console(
        db, _StubConfig(), name="con", vm_name="vm1", session_specs=["a+1"], interaction=TtyInteractionPolicy.REFUSE
    )


def test_create_resolves_with_nothing_locked(
    db: Database, console_guest_factory: Callable[..., _ScriptedGuest], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    console_guest_factory(TmuxModel())
    monkeypatch.setattr(mc, "_console_build_secret_targets", lambda *a, **k: [PANE])
    during = _resolving(db, monkeypatch)

    _create(db)

    assert during == [True]
    assert db.get_console("con") is not None


@pytest.mark.parametrize(
    "change",
    [
        lambda db: create_console_record(db, name="con", vm_name="vm1", session_specs=["a"]),
        lambda db: _replace_session(db, "a"),
        lambda db: db.update_vm_tailscale("vm1", "100.64.0.250"),
    ],
    ids=["console-created", "member-session-replaced", "vm-changed"],
)
def test_create_refuses_what_changed_before_its_lock(
    db: Database,
    console_guest_factory: Callable[..., _ScriptedGuest],
    monkeypatch: pytest.MonkeyPatch,
    change: Callable[[Database], None],
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    guest = console_guest_factory(TmuxModel())
    monkeypatch.setattr(mc, "_console_build_secret_targets", lambda *a, **k: [PANE])
    _resolving(db, monkeypatch, then=lambda: change(db))
    monkeypatch.setattr(mc, "_teardown_console_tmux", lambda *a, **k: pytest.fail("changed tmux"))

    with pytest.raises(StateConflictError):
        _create(db)

    assert not any("new-session" in command for command in guest.commands)


# -- restore-session -----------------------------------------------------------


def _restorable(db: Database, monkeypatch: pytest.MonkeyPatch) -> TmuxModel:
    """Console ``con`` whose member ``b`` configures three shells, with the window missing the middle one."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["a", "b+3"])
    db.update_console_shells("con", "b", [{"cwd": f"s{index}", "admin": False} for index in range(3)])

    def targets(*_args: object, member: ConsoleSessionRow, **_kwargs: object) -> list[SecretTarget]:
        return [SecretTarget(vm={}, label=f"console-pane:{shell['cwd']}") for shell in member.shells]

    monkeypatch.setattr(mc, "_member_secret_targets", targets)
    model = TmuxModel()
    model.seed_session(CON, "a")
    model.seed_window(CON, "b", pane_tags=(None, 0, 2))
    return model


def _restore(db: Database) -> None:
    mc.restore_session(db, _StubConfig(), console_name="con", session_name="b", interaction=TtyInteractionPolicy.REFUSE)


def test_restore_resolves_with_nothing_locked(
    db: Database, console_guest_factory: Callable[..., _ScriptedGuest], monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _restorable(db, monkeypatch)
    console_guest_factory(model)
    during = _resolving(db, monkeypatch)

    _restore(db)

    assert during == [True]
    assert [tag for _pid, _pidx, tag in model.pane_rows(CON, "b") or []] == [None, 0, 1, 2]


@pytest.mark.parametrize(
    "change",
    [
        lambda db, model: db.update_console_shells("con", "b", [{"cwd": "elsewhere", "admin": False}]),
        lambda db, model: _replace_session(db, "b"),
        lambda db, model: db.update_vm_tailscale("vm1", "100.64.0.250"),
        lambda db, model: model.kill_window(CON, "b"),
    ],
    ids=["member-shells-changed", "session-replaced", "vm-changed", "window-changed"],
)
def test_restore_refuses_what_changed_before_its_lock(
    db: Database,
    console_guest_factory: Callable[..., _ScriptedGuest],
    monkeypatch: pytest.MonkeyPatch,
    change: Callable[[Database, TmuxModel], object],
) -> None:
    model = _restorable(db, monkeypatch)
    console_guest_factory(model)
    _resolving(db, monkeypatch, then=lambda: change(db, model))
    monkeypatch.setattr("agentworks.sessions.multi_console.restore._apply_restore", lambda *a, **k: pytest.fail("ran"))

    with pytest.raises(StateConflictError):
        _restore(db)
