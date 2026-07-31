"""Tests for Phase 6 eager-prompting at ``sessions.manager`` entry points.

Split out of the original ``test_secrets_eager_resolve.py`` (see
``_secrets_eager_support.py`` for the full background on FRD R4's
operator-facing guarantee). This file covers the session-lifecycle slice:
``create_session`` and ``restart_session`` must eager-resolve secrets
BEFORE any state mutation (session-create DB insert, session-kill), and
the read-only session commands (``attach``, ``list``, ``describe``) must
NOT eager-resolve at all, since they open no new shells and consume no
secrets per FRD R4/R5.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.errors import SecretUnavailableError

from ._secrets_eager_support import _seed_basic_db, _stub_build_registry, _stub_session_prep
from .conftest import stub_vm_gates

__all__ = ["_stub_build_registry"]


# ---------------------------------------------------------------------------
# session create
# ---------------------------------------------------------------------------


def test_session_create_eager_resolve_fires_before_db_insert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If resolve_for_command raises SecretUnavailableError, the session
    row must NOT be inserted (state mutation must come after eager
    resolution). The error propagates unchanged."""
    from agentworks.sessions import manager as session_manager

    db = _seed_basic_db(tmp_path)
    _stub_session_prep(monkeypatch)

    class _Tmpl:
        name = "default"
        harness = "shell"
        harness_config: dict[str, object] = {}
        env: dict[str, str] = {}

    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: _Tmpl())
    # _session_secret_target_pre_create reads config.vm_templates /
    # agent_templates which the SimpleNamespace below doesn't have; stub
    # it to an empty target so the root still registers its env seam.
    from tests.conftest import empty_secret_target

    monkeypatch.setattr(
        session_manager,
        "_session_secret_target_pre_create",
        lambda *a, **k: empty_secret_target(),
    )

    # The env chain resolves in the orchestrator's one boundary pass, so
    # an unresolvable secret surfaces from the resolver's resolve().
    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env-var",
        )

    monkeypatch.setattr("agentworks.secrets.resolver.Resolver.resolve", _explode)

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(SecretUnavailableError, match="api-key"):
        session_manager.create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
            template_name=None,
            agent_name=None,
            admin=True,
        )

    # State must be untouched.
    assert db.get_session("s1") is None
    db.close()


def test_session_create_calls_resolve_with_session_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_session registers a single SecretTarget (the one returned
    by ``_session_secret_target_pre_create``) on the operation's
    resolver, joining the one boundary resolve. Verifies the glue that
    turns a session command into a candidate set."""
    from agentworks.secrets.resolver import Resolver as _RealResolver
    from agentworks.sessions import manager as session_manager
    from tests.conftest import empty_secret_target

    db = _seed_basic_db(tmp_path)
    _stub_session_prep(monkeypatch)

    class _Tmpl:
        name = "default"
        harness = "shell"
        harness_config: dict[str, object] = {}
        env: dict[str, str] = {}

    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: _Tmpl())

    sentinel_target = empty_secret_target(label="sentinel")
    monkeypatch.setattr(
        session_manager,
        "_session_secret_target_pre_create",
        lambda *a, **k: sentinel_target,
    )

    class _Sentinel(Exception):
        """Raised from the resolve spy so we can stop the test before the
        long-running SSH-driven part of create_session runs."""

    calls: list[list[object]] = []
    real_register = _RealResolver.register_targets

    def _register_spy(self: _RealResolver, targets: Any) -> None:
        calls.append(list(targets))
        real_register(self, targets)

    monkeypatch.setattr(_RealResolver, "register_targets", _register_spy)
    monkeypatch.setattr(_RealResolver, "resolve", lambda self: (_ for _ in ()).throw(_Sentinel()))

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(_Sentinel):
        session_manager.create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
            template_name=None,
            agent_name=None,
            admin=True,
        )

    assert len(calls) == 1, f"expected exactly one target registration, got {len(calls)}"
    assert calls[0] == [sentinel_target], "session target list should contain one target"
    db.close()


# ---------------------------------------------------------------------------
# session restart
# ---------------------------------------------------------------------------


def test_session_restart_broken_no_force_bails_before_eager_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A BROKEN session restarted without --force must raise BrokenStateError
    BEFORE eager-resolve runs. The operator gets a clean error without
    being asked for credentials they would have discarded."""
    from agentworks.db import SessionMode, SessionStatus
    from agentworks.errors import BrokenStateError
    from agentworks.sessions import manager as session_manager

    db = _seed_basic_db(tmp_path)
    _stub_session_prep(monkeypatch)

    # A non-NULL socket_path keeps this session on the new per-session-socket
    # model; with socket_path=None, restart_session treats the row as a
    # legacy migration target and skips the gates these tests exist to pin.
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, pid, socket_path) "
        "VALUES ('s1', 'ws1', 'default', ?, 9999, '/tmp/sock')",
        (SessionMode.ADMIN.value,),
    )
    db._conn.commit()

    monkeypatch.setattr(
        session_manager,
        "_ensure_pid",
        lambda session, **kwargs: session,
    )
    monkeypatch.setattr(session_manager, "check_session_status", lambda *a, **k: SessionStatus.BROKEN)
    monkeypatch.setattr(
        session_manager,
        "_build_session_target",
        lambda *a, **k: SimpleNamespace(run=lambda *a, **k: None),
    )

    resolve_calls: list[object] = []

    def _track_resolve(*args: object, **kwargs: object) -> dict[str, str]:
        resolve_calls.append(args)
        return {}

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _track_resolve)

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(BrokenStateError, match="broken"):
        session_manager.restart_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            force=False,
            yes=True,
        )

    assert resolve_calls == [], (
        "BROKEN + no --force must bail BEFORE eager-resolve so the "
        "operator isn't asked for credentials they would discard"
    )
    db.close()


def test_session_restart_eager_resolve_fires_before_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """restart_session must call resolve_for_command BEFORE _kill_session.
    A failed eager-resolve leaves the running session untouched."""
    from agentworks.db import SessionMode, SessionStatus
    from agentworks.sessions import manager as session_manager

    db = _seed_basic_db(tmp_path)
    _stub_session_prep(monkeypatch)

    # A non-NULL socket_path keeps this session on the new per-session-socket
    # model; with socket_path=None, restart_session treats the row as a
    # legacy migration target and skips the gates these tests exist to pin.
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, pid, socket_path) "
        "VALUES ('s1', 'ws1', 'default', ?, 9999, '/tmp/sock')",
        (SessionMode.ADMIN.value,),
    )
    db._conn.commit()

    # Status probes -> OK so the restart path would try to kill.
    monkeypatch.setattr(
        session_manager,
        "_ensure_pid",
        lambda session, **kwargs: session,
    )
    monkeypatch.setattr(session_manager, "check_session_status", lambda *a, **k: SessionStatus.OK)
    monkeypatch.setattr(
        session_manager,
        "_build_session_target",
        lambda *a, **k: SimpleNamespace(run=lambda *a, **k: None),
    )

    class _Tmpl:
        name = "default"
        harness = "shell"
        harness_config: dict[str, object] = {}
        env: dict[str, str] = {}

    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: _Tmpl())
    monkeypatch.setattr(session_manager, "_session_secret_target", lambda *a, **k: object())

    kill_calls: list[str] = []

    def _track_kill(name: str, **kwargs: object) -> bool:
        kill_calls.append(name)
        return True

    monkeypatch.setattr(session_manager, "_kill_session", _track_kill)

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env-var",
        )

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(SecretUnavailableError, match="api-key"):
        session_manager.restart_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            yes=True,
        )

    assert kill_calls == [], "eager-resolve must precede kill; kill ran anyway"
    db.close()


# ---------------------------------------------------------------------------
# FRD R4 / R5 no-shell-opening surface: these commands MUST NOT eager-resolve
# ---------------------------------------------------------------------------


def test_session_attach_does_not_eager_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``session attach`` joins an existing tmux session via SSH; the
    existing session retains its create-time env (FRD R5 "Attach
    inherits create-time env"). Eager-resolve must NOT fire."""
    from agentworks.db import SessionMode, SessionStatus
    from agentworks.sessions import manager as session_manager

    db = _seed_basic_db(tmp_path)
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, pid) VALUES ('s1', 'ws1', 'default', ?, 1234)",
        (SessionMode.ADMIN.value,),
    )
    db._conn.commit()

    resolve_called: list[bool] = []

    def _track_resolve(*args: object, **kwargs: object) -> dict[str, str]:
        resolve_called.append(True)
        return {}

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _track_resolve)

    # Stub out the SSH probe + transport so we don't need a real VM.
    fake_target = SimpleNamespace(
        run=lambda *a, **k: SimpleNamespace(ok=True, returncode=0, stdout="", stderr=""),
        interactive=lambda *a, **k: 0,
    )
    ws_row = db.get_workspace("ws1")
    vm_row = db.get_vm("vm1")
    assert ws_row is not None and vm_row is not None
    stub_vm_gates(monkeypatch)

    # _prepare_vm is a gate-span context manager now; the stub yields
    # the same 5-tuple shape inside a trivial span.
    @contextlib.contextmanager
    def _fake_prepare_vm(*a: object, **k: object):  # noqa: ANN202
        yield (ws_row, vm_row, fake_target.run, None, fake_target)

    monkeypatch.setattr(session_manager, "_prepare_vm", _fake_prepare_vm)
    monkeypatch.setattr(
        session_manager,
        "_ensure_pid",
        lambda session, **kwargs: session,
    )
    monkeypatch.setattr(
        session_manager,
        "check_session_status",
        lambda *a, **k: SessionStatus.OK,
    )

    config = SimpleNamespace(operator=SimpleNamespace(ssh_private_key=None))
    # attach_session returns interactive()'s exit code (0, stubbed) and
    # does not sys.exit; the CLI owns the process exit.
    assert session_manager.attach_session(db, config, name="s1") == 0  # type: ignore[arg-type]

    assert resolve_called == [], "session attach joins existing shell; must not eager-resolve"
    db.close()


def test_session_list_does_not_eager_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``session list`` reads the DB only; per FRD R4/R5 it opens no new
    shells and consumes no secrets. A spy on resolve_for_command must
    never fire. Seeded with a session row so the meaty body (not the
    empty-list short-circuit) is exercised."""
    from agentworks.db import SessionMode
    from agentworks.sessions import manager as session_manager

    db = _seed_basic_db(tmp_path)
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode) VALUES ('s1', 'ws1', 'default', ?)",
        (SessionMode.ADMIN.value,),
    )
    db._conn.commit()

    resolve_called: list[bool] = []

    def _track_resolve(*args: object, **kwargs: object) -> dict[str, str]:
        resolve_called.append(True)
        return {}

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _track_resolve)

    config = SimpleNamespace()
    session_manager.list_sessions(
        db,
        config,  # type: ignore[arg-type]
        no_status=True,  # avoid SSH liveness probes
    )

    assert resolve_called == [], "session list reads DB only; must not eager-resolve secrets"
    db.close()


def test_session_describe_does_not_eager_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``session describe`` reads DB + best-effort liveness; per FRD R4/R5
    it opens no new shells and consumes no secrets."""
    from agentworks.db import SessionMode, SessionStatus
    from agentworks.sessions import manager as session_manager

    db = _seed_basic_db(tmp_path)
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode) VALUES ('s1', 'ws1', 'default', ?)",
        (SessionMode.ADMIN.value,),
    )
    db._conn.commit()

    resolve_called: list[bool] = []

    def _track_resolve(*args: object, **kwargs: object) -> dict[str, str]:
        resolve_called.append(True)
        return {}

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _track_resolve)
    # describe_session enters _prepare_vm's gate span, which probes SSH
    # connectivity. Stub it (a context manager yielding the 5-tuple)
    # and the downstream status helpers; the contract under test is
    # whether resolve_for_command fires, not the probe path.
    ws_row = db.get_workspace("ws1")
    vm_row = db.get_vm("vm1")
    assert ws_row is not None and vm_row is not None
    fake_target = SimpleNamespace(
        run=lambda *a, **k: SimpleNamespace(ok=True, returncode=0, stdout="", stderr=""),
    )
    stub_vm_gates(monkeypatch)

    @contextlib.contextmanager
    def _fake_prepare_vm(*a: object, **k: object):  # noqa: ANN202
        yield (ws_row, vm_row, fake_target.run, None, fake_target)

    monkeypatch.setattr(session_manager, "_prepare_vm", _fake_prepare_vm)
    monkeypatch.setattr(
        session_manager,
        "_ensure_pid",
        lambda session, **kwargs: session,
    )
    monkeypatch.setattr(
        session_manager,
        "check_session_status",
        lambda *a, **k: SessionStatus.UNKNOWN,
    )

    config = SimpleNamespace(operator=SimpleNamespace(ssh_private_key=None))
    # describe_session has `name` as a keyword-only arg.
    session_manager.describe_session(
        db,
        config,
        name="s1",  # type: ignore[arg-type]
    )

    assert resolve_called == [], "session describe must not eager-resolve secrets"
    db.close()
