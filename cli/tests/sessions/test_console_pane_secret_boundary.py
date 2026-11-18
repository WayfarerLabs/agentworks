"""A console command's pane secrets resolve in its one boundary pass, before it changes anything.

``console create`` and ``restore-session`` register every pane's targets on the
boundary resolver before the activation gate and resolve once the VM is active,
before their first tmux request or database write; ``restore-session`` takes
a target for every configured shell, whatever turns out to be missing (operator
ruling of 2026-11-12). ``console start`` and ``restart`` resolve their build's
targets on the same resolver before any tmux change. Each resolves once, and
none calls ``resolve_for_command``.
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

    from agentworks.db import Database
    from tests.conftest import _FakeResult, _FakeTarget

CON = "aw-console-con"
PANE = SecretTarget(vm={}, label="console-pane:pin")


class _Boundary:
    """What happened, in order: tmux lines, target registrations and the resolve, with the database's change count."""

    def __init__(self, db: Database, target: _FakeTarget | None, monkeypatch: pytest.MonkeyPatch) -> None:
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
        if target is not None:
            dispatch = target.dispatch

            def recording(command: str) -> _FakeResult:
                if command.startswith("tmux"):
                    self.events.append(("tmux", command))
                return dispatch(command)

            monkeypatch.setattr(target, "dispatch", recording)

    def before_resolve(self) -> list[tuple[str, object]]:
        first = next(index for index, (kind, _) in enumerate(self.events) if kind == "resolve")
        return self.events[:first]


def _registered(boundary: _Boundary) -> list[object]:
    return [targets for kind, targets in boundary.before_resolve() if kind == "register"]


def _resolves(boundary: _Boundary) -> int:
    return [kind for kind, _ in boundary.events].count("resolve")


def test_create_resolves_its_panes_before_any_tmux_request_or_database_write(
    db: Database, console_target_factory: Callable[..., _FakeTarget], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    target = console_target_factory(TmuxModel())
    monkeypatch.setattr(mc, "_console_build_secret_targets", lambda *a, **k: [PANE])
    before = db._conn.total_changes
    boundary = _Boundary(db, target, monkeypatch)

    mc.create_console(
        db, _StubConfig(), name="con", vm_name="vm1", session_specs=["a+1"], interaction=TtyInteractionPolicy.REFUSE
    )

    assert _registered(boundary) == [(PANE,)]
    assert _resolves(boundary) == 1
    assert not [event for event in boundary.before_resolve() if event[0] == "tmux"]
    assert boundary.changes_at_resolve == before
    assert db.get_console("con") is not None


@pytest.mark.parametrize("window_missing", [True, False])
def test_restore_resolves_every_configured_shell_before_any_tmux_request(
    db: Database,
    console_target_factory: Callable[..., _FakeTarget],
    monkeypatch: pytest.MonkeyPatch,
    window_missing: bool,
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["a", "b+3"])
    model = TmuxModel()
    model.seed_session(CON, "a")
    if not window_missing:
        model.seed_window(CON, "b", pane_tags=(None, 0, 2))
    target = console_target_factory(model)
    shells = [SecretTarget(vm={}, label=f"console-pane:{index}") for index in range(3)]
    unasked = iter(shells)
    monkeypatch.setattr(mc, "_pane_secret_target", lambda *a, **k: next(unasked))
    before = db._conn.total_changes
    boundary = _Boundary(db, target, monkeypatch)

    mc.restore_session(db, _StubConfig(), console_name="con", session_name="b", interaction=TtyInteractionPolicy.REFUSE)

    # A target for every configured shell, though only index 1 (or the whole window) was missing.
    assert _registered(boundary) == [tuple(shells)]
    assert _resolves(boundary) == 1
    assert not [event for event in boundary.before_resolve() if event[0] == "tmux"]
    assert boundary.changes_at_resolve == before
    assert [tag for _pid, _pidx, tag in model.pane_rows(CON, "b") or []] == [None, 0, 1, 2]


@pytest.mark.parametrize("operation", ["start", "restart"])
def test_start_and_restart_resolve_their_build_before_changing_tmux(
    db: Database,
    console_target_factory: Callable[..., _FakeTarget],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["a+1"])
    model = TmuxModel()
    if operation == "restart":
        model.seed_session(CON, "a")
    target = console_target_factory(model)
    monkeypatch.setattr(mc, "_console_build_secret_targets", lambda *a, **k: [PANE])
    boundary = _Boundary(db, target, monkeypatch)

    run = mc.start_console if operation == "start" else mc.restart_console
    run(db, _StubConfig(), name="con", interaction=TtyInteractionPolicy.REFUSE)

    assert _registered(boundary) == [(PANE,)]
    assert _resolves(boundary) == 1
    # Only the read-only presence probes that choose the action come first.
    early = [command for kind, command in boundary.before_resolve() if kind == "tmux"]
    assert early
    assert all("has-session" in str(command) for command in early)
    assert model.has_session(CON)


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
