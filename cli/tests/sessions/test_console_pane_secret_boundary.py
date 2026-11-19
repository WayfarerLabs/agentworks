"""A console command's pane secrets resolve in its one boundary pass, before it changes anything.

``console create`` registers every pane's targets before the activation gate,
so a refused target leaves a stopped VM stopped, and resolves once the VM is
active, before its first tmux request or database write. ``restore-session`` first observes the
window read-only and refuses what it cannot repair, then resolves only the
targets of the panes it will open, before its first tmux change; with nothing
missing it resolves nothing (operator ruling of 2026-11-18). ``console start``
and ``restart`` resolve their build's targets on the same resolver before any
tmux change. Each resolves at most once, and none calls ``resolve_for_command``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.errors import NotFoundError, StateError
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
    from tests.conftest import _FakeResult, _ScriptedGuest

CON = "aw-console-con"
PANE = SecretTarget(vm={}, label="console-pane:pin")


class _Boundary:
    """What happened, in order: tmux lines, target registrations and the resolve, with the database's change count."""

    def __init__(self, db: Database, guest: _ScriptedGuest | None, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[tuple[str, object]] = []
        self.changes_at_resolve: int | None = None
        register, resolve = Resolver.register_targets, Resolver.resolve

        def registering(resolver: Resolver, targets: object, **kwargs: object) -> None:
            self.events.append(("register", tuple(targets)))  # type: ignore[arg-type]
            register(resolver, targets, **kwargs)  # type: ignore[arg-type]

        def resolving(resolver: Resolver) -> None:
            if self.changes_at_resolve is None:
                self.changes_at_resolve = db._conn.total_changes
            self.events.append(("resolve", None))
            resolve(resolver)

        monkeypatch.setattr(Resolver, "register_targets", registering)
        monkeypatch.setattr(Resolver, "resolve", resolving)
        monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: pytest.fail("late resolve"))
        if guest is not None:
            dispatch = guest.dispatch

            def recording(command: str) -> _FakeResult:
                if command.startswith("tmux"):
                    self.events.append(("tmux", command))
                return dispatch(command)

            monkeypatch.setattr(guest, "dispatch", recording)

    def before_resolve(self) -> list[tuple[str, object]]:
        first = next(index for index, (kind, _) in enumerate(self.events) if kind == "resolve")
        return self.events[:first]


def _registered(boundary: _Boundary) -> list[object]:
    return [targets for kind, targets in boundary.before_resolve() if kind == "register"]


def _resolves(boundary: _Boundary) -> int:
    return [kind for kind, _ in boundary.events].count("resolve")


def test_create_resolves_its_panes_before_any_tmux_request_or_database_write(
    db: Database, console_guest_factory: Callable[..., _ScriptedGuest], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    guest = console_guest_factory(TmuxModel())
    monkeypatch.setattr(mc, "_console_build_secret_targets", lambda *a, **k: [PANE])
    before = db._conn.total_changes
    boundary = _Boundary(db, guest, monkeypatch)

    mc.create_console(
        db, _StubConfig(), name="con", vm_name="vm1", session_specs=["a+1"], interaction=TtyInteractionPolicy.REFUSE
    )

    assert _registered(boundary) == [(PANE,)]
    assert _resolves(boundary) == 1
    assert not [event for event in boundary.before_resolve() if event[0] == "tmux"]
    assert boundary.changes_at_resolve == before
    assert db.get_console("con") is not None


@pytest.mark.parametrize("refused_at", ["building", "registering"])
def test_create_refuses_a_pane_target_before_it_starts_the_vm(
    db: Database,
    console_guest_factory: Callable[..., _ScriptedGuest],
    monkeypatch: pytest.MonkeyPatch,
    refused_at: str,
) -> None:
    """``console create`` knows its panes up front, so a refused target stops it before the activation gate."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    guest = console_guest_factory(TmuxModel())
    refusal = StateError("the pane target is refused")

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise refusal

    if refused_at == "building":
        monkeypatch.setattr(mc, "_console_build_secret_targets", refuse)
    else:
        monkeypatch.setattr(mc, "_console_build_secret_targets", lambda *a, **k: [PANE])
        monkeypatch.setattr(Resolver, "register_targets", refuse)
    for step in ("prepare_vm_runtime", "activate_vm_runtime"):
        monkeypatch.setattr(f"agentworks.vms.manager.runtime.{step}", lambda *a, **k: pytest.fail("started the VM"))
    monkeypatch.setattr(Resolver, "resolve", lambda self: pytest.fail("resolved"))
    before = db._conn.total_changes

    with pytest.raises(StateError) as raised:
        mc.create_console(
            db, _StubConfig(), name="con", vm_name="vm1", session_specs=["a+1"], interaction=TtyInteractionPolicy.REFUSE
        )

    assert raised.value is refusal
    assert guest.commands == []
    assert db._conn.total_changes == before


_READS = ("has-session", "list-windows", "list-panes")


def _restorable(db: Database, monkeypatch: pytest.MonkeyPatch) -> list[SecretTarget]:
    """Console ``con`` with members ``a`` and ``b``, ``b`` configuring three shells told apart by their
    directories; each shell's target is labeled with its directory, and the targets are returned in
    configured order."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["a", "b+3"])
    db.update_console_shells("con", "b", [{"cwd": f"s{index}", "admin": False} for index in range(3)])

    def targets(*_args: object, member: ConsoleSessionRow, **_kwargs: object) -> list[SecretTarget]:
        return [SecretTarget(vm={}, label=f"console-pane:{shell['cwd']}") for shell in member.shells]

    monkeypatch.setattr(mc, "_member_secret_targets", targets)
    return [SecretTarget(vm={}, label=f"console-pane:s{index}") for index in range(3)]


def _restore(db: Database) -> None:
    mc.restore_session(db, _StubConfig(), console_name="con", session_name="b", interaction=TtyInteractionPolicy.REFUSE)


@pytest.mark.parametrize("window_missing", [True, False])
def test_restore_resolves_only_the_panes_it_opens_before_any_tmux_change(
    db: Database,
    console_guest_factory: Callable[..., _ScriptedGuest],
    monkeypatch: pytest.MonkeyPatch,
    window_missing: bool,
) -> None:
    shells = _restorable(db, monkeypatch)
    model = TmuxModel()
    model.seed_session(CON, "a")
    if not window_missing:
        model.seed_window(CON, "b", pane_tags=(None, 0, 2))
    guest = console_guest_factory(model)
    before = db._conn.total_changes
    boundary = _Boundary(db, guest, monkeypatch)

    _restore(db)

    # The whole window's shells when it is gone; otherwise only index 1, the one with no live pane.
    assert _registered(boundary) == [tuple(shells) if window_missing else (shells[1],)]
    assert _resolves(boundary) == 1
    early = [str(command) for kind, command in boundary.before_resolve() if kind == "tmux"]
    assert early
    assert all(any(read in command for read in _READS) for command in early)
    assert boundary.changes_at_resolve == before
    assert [tag for _pid, _pidx, tag in model.pane_rows(CON, "b") or []] == [None, 0, 1, 2]


def test_restore_with_nothing_missing_resolves_nothing(
    db: Database, console_guest_factory: Callable[..., _ScriptedGuest], monkeypatch: pytest.MonkeyPatch
) -> None:
    _restorable(db, monkeypatch)
    model = TmuxModel()
    model.seed_session(CON, "a")
    model.seed_window(CON, "b", pane_tags=(None, 0, 1, 2))
    guest = console_guest_factory(model)
    monkeypatch.setattr(mc, "_member_secret_targets", lambda *a, **k: pytest.fail("built pane targets"))
    boundary = _Boundary(db, guest, monkeypatch)

    _restore(db)

    assert [kind for kind, _ in boundary.events if kind != "tmux"] == []


@pytest.mark.parametrize(
    "window",
    [
        pytest.param(lambda model: model.seed_window(CON, "b", pane_tags=(None, 0, 0)), id="duplicate-tags"),
        pytest.param(lambda model: model.seed_window(CON, "b", pane_tags=(0, 1)), id="attach-pane-killed"),
        pytest.param(None, id="presence-unknown"),
    ],
)
def test_restore_refuses_what_it_cannot_repair_before_resolving_anything(
    db: Database,
    console_guest_factory: Callable[..., _ScriptedGuest],
    monkeypatch: pytest.MonkeyPatch,
    window: Callable[[TmuxModel], object] | None,
) -> None:
    from agentworks.sessions.tmux import ProbeStatus

    _restorable(db, monkeypatch)
    model = TmuxModel()
    model.seed_session(CON, "a")
    if window is None:
        monkeypatch.setattr(mc, "_console_tmux_presence", lambda *a, **k: ProbeStatus.UNKNOWN)
    else:
        window(model)
    guest = console_guest_factory(model)
    monkeypatch.setattr(mc, "_member_secret_targets", lambda *a, **k: pytest.fail("built pane targets"))
    before = db._conn.total_changes
    boundary = _Boundary(db, guest, monkeypatch)

    with pytest.raises(StateError):
        _restore(db)

    assert [kind for kind, _ in boundary.events if kind != "tmux"] == []
    assert all(any(read in str(command) for read in _READS) for _kind, command in boundary.events)
    assert db._conn.total_changes == before


@pytest.mark.parametrize("operation", ["start", "restart"])
def test_start_and_restart_resolve_their_build_before_changing_tmux(
    db: Database,
    console_guest_factory: Callable[..., _ScriptedGuest],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["a+1"])
    model = TmuxModel()
    if operation == "restart":
        model.seed_session(CON, "a")
    guest = console_guest_factory(model)
    monkeypatch.setattr(mc, "_console_build_secret_targets", lambda *a, **k: [PANE])
    boundary = _Boundary(db, guest, monkeypatch)

    run = mc.start_console if operation == "start" else mc.restart_console
    run(db, _StubConfig(), name="con", interaction=TtyInteractionPolicy.REFUSE)

    assert _registered(boundary) == [(PANE,)]
    assert _resolves(boundary) == 1
    # Only the read-only presence probes that choose the action come first.
    early = [command for kind, command in boundary.before_resolve() if kind == "tmux"]
    assert early
    assert all("has-session" in str(command) for command in early)
    assert model.has_session(CON)


def _misbind_socket(db: Database, session_name: str) -> None:
    """Give ``session_name`` a stored socket in the right directory under another session's name."""
    db._conn.execute(
        "UPDATE sessions SET socket_path = ? WHERE name = ?",
        ("/run/agentworks/admin-tmux-sockets/admin/other.sock", session_name),
    )
    db._conn.commit()


@pytest.mark.parametrize("operation", ["start", "restart"])
def test_start_and_restart_refuse_a_member_socket_before_changing_anything(
    db: Database,
    console_guest_factory: Callable[..., _ScriptedGuest],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """A build would skip the window only after tearing down or staging, so the stored socket is checked first."""
    from agentworks.db import DesiredRuntimeState

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["a+1", "b"])
    _misbind_socket(db, "b")
    model = TmuxModel()
    if operation == "restart":
        model.seed_session(CON, "a", pane_tags=(None, 0))
        model.seed_window(CON, "b")
    else:
        db.set_console_desired_runtime_state("con", DesiredRuntimeState.STOPPED)
    guest = console_guest_factory(model)
    monkeypatch.setattr(mc, "_console_build_secret_targets", lambda *a, **k: pytest.fail("built pane targets"))
    for step in ("prepare_vm_runtime", "activate_vm_runtime"):
        monkeypatch.setattr(f"agentworks.vms.manager.runtime.{step}", lambda *a, **k: pytest.fail("started the VM"))
    before = db._conn.total_changes
    boundary = _Boundary(db, guest, monkeypatch)

    run = mc.start_console if operation == "start" else mc.restart_console
    with pytest.raises(StateError):
        run(db, _StubConfig(), name="con", interaction=TtyInteractionPolicy.REFUSE)

    assert boundary.events == []
    assert guest.commands == []
    assert db._conn.total_changes == before
    if operation == "restart":
        assert model.window_names(CON) == ["a", "b"]
        assert [tag for _pid, _pidx, tag in model.pane_rows(CON, "a") or []] == [None, 0]


def test_restore_of_a_missing_window_refuses_its_member_socket_before_resolving_anything(
    db: Database, console_guest_factory: Callable[..., _ScriptedGuest], monkeypatch: pytest.MonkeyPatch
) -> None:
    _restorable(db, monkeypatch)
    _misbind_socket(db, "b")
    model = TmuxModel()
    model.seed_session(CON, "a")
    guest = console_guest_factory(model)
    monkeypatch.setattr(mc, "_member_secret_targets", lambda *a, **k: pytest.fail("built pane targets"))
    before = db._conn.total_changes
    boundary = _Boundary(db, guest, monkeypatch)

    with pytest.raises(StateError):
        _restore(db)

    assert [kind for kind, _ in boundary.events if kind != "tmux"] == []
    assert all(any(read in str(command) for read in _READS) for _kind, command in boundary.events)
    assert db._conn.total_changes == before
    assert model.window_names(CON) == ["a"]


def test_a_vm_without_an_address_resolves_nothing_and_dispatches_nothing(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.vm_access_support import install_access_recorder

    _seed_vm(db, with_tailscale=False)
    _seed_sessions(db, ["a"])
    recorder = install_access_recorder(monkeypatch)
    monkeypatch.setattr(mc, "_console_build_secret_targets", lambda *a, **k: pytest.fail("built pane targets"))
    boundary = _Boundary(db, None, monkeypatch)

    with pytest.raises(StateError) as raised:
        mc.create_console(
            db, _StubConfig(), name="con", vm_name="vm1", session_specs=["a+1"], interaction=TtyInteractionPolicy.REFUSE
        )

    assert (raised.value.entity_kind, raised.value.entity_name) == ("vm", "vm1")
    assert boundary.events == []
    assert recorder.calls == []
    assert db.get_console("con") is None


def test_a_console_not_found_resolves_nothing_and_dispatches_nothing(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.vm_access_support import install_access_recorder

    _seed_vm(db, with_tailscale=True)
    recorder = install_access_recorder(monkeypatch)
    boundary = _Boundary(db, None, monkeypatch)

    with pytest.raises(NotFoundError):
        mc.restore_session(
            db, _StubConfig(), console_name="nope", session_name="a", interaction=TtyInteractionPolicy.REFUSE
        )

    assert boundary.events == []
    assert recorder.calls == []
