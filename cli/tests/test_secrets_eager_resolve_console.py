"""Tests for Phase 6 eager-prompting at ``sessions.multi_console`` entry
points.

Split out of the original ``test_secrets_eager_resolve.py`` (see
``_secrets_eager_support.py`` for the full background on FRD R4's
operator-facing guarantee). This file covers the console slice:
``add_shell``, ``attach_console`` (both the first-attach build path and
plain-attach-to-an-existing-session path), ``add_sessions``, and
``restore_session`` must eager-resolve secrets BEFORE any pane-opening
mutation (tmux build, DB write) -- but only on the branches that actually
open new shells. Branches that merely join or wrap existing tmux state
must NOT eager-resolve, per FRD R4/R5.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentworks.errors import SecretUnavailableError

from ._secrets_eager_support import _seed_basic_db, _stub_build_registry
from .conftest import stub_vm_gates

__all__ = ["_stub_build_registry"]


# ---------------------------------------------------------------------------
# console add-shell
# ---------------------------------------------------------------------------


def test_console_add_shell_eager_resolve_fires_before_db_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_shell must call resolve_for_command BEFORE update_console_shells.
    A failed eager-resolve leaves the console's shell list unchanged."""
    from agentworks.sessions import multi_console

    db = _seed_basic_db(tmp_path)

    # Seed: a session + a console + a console-session membership.
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode) VALUES ('s1', 'ws1', 'default', 'admin')"
    )
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES ('c1', 'vm1')")
    db._conn.execute(
        "INSERT INTO console_sessions (console_name, session_name, shells, position) VALUES ('c1', 's1', '[]', 0)"
    )
    db._conn.commit()

    # Stub the secret-target builder so we don't need a real Config.
    monkeypatch.setattr(multi_console, "_pane_secret_target", lambda *a, **k: object())

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env-var",
        )

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    config = SimpleNamespace()

    with pytest.raises(SecretUnavailableError, match="api-key"):
        multi_console.add_shell(
            db,
            config,  # type: ignore[arg-type]
            console_name="c1",
            session_name="s1",
            cwd=None,
            admin=False,
        )

    # The shells list must still be the original empty list -- no DB write.
    cs = db.get_console_session("c1", "s1")
    assert cs is not None
    assert cs.shells == [], "eager-resolve must precede update_console_shells"
    db.close()


def test_console_add_shell_promotes_admin_for_admin_mode_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_shell against an admin-mode session must promote the pane's
    is_admin_pane to True even when the operator passed ``admin=False``,
    matching ``_split_shell_pane``'s ``use_admin = shell.admin or
    session_user == admin_user`` logic. Without the promotion, the
    helper's ``agent_name is None`` branch would silently skip eager-
    resolve while ``_resolve_pane_env`` at split time prompts for
    admin-scope secrets late."""
    from agentworks.sessions import multi_console

    db = _seed_basic_db(tmp_path)
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode) VALUES ('s1', 'ws1', 'default', 'admin')"
    )
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES ('c1', 'vm1')")
    db._conn.execute(
        "INSERT INTO console_sessions (console_name, session_name, shells, position) VALUES ('c1', 's1', '[]', 0)"
    )
    db._conn.commit()

    captured: dict[str, object] = {}

    def _spy_target(
        db: object,
        registry: object,
        *,
        vm: object,
        session: object,
        is_admin_pane: bool,
    ) -> object:
        captured["is_admin_pane"] = is_admin_pane
        return object()

    monkeypatch.setattr(multi_console, "_pane_secret_target", _spy_target)
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: {})

    config = SimpleNamespace()

    multi_console.add_shell(
        db,
        config,  # type: ignore[arg-type]
        console_name="c1",
        session_name="s1",
        cwd=None,
        admin=False,  # operator did NOT pass --admin
    )

    assert captured["is_admin_pane"] is True, (
        "admin-mode session + add_shell without --admin should promote to admin pane"
    )
    db.close()


# ---------------------------------------------------------------------------
# console attach / build
# ---------------------------------------------------------------------------


def test_attach_console_build_path_eager_resolves_before_tmux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """attach_console's first-attach build path opens new shells
    (admin shell + per-session shell panes). resolve_for_command must
    fire BEFORE _build_console_tmux issues any tmux command. A failed
    eager-resolve leaves no tmux state created."""
    from agentworks.sessions import multi_console

    db = _seed_basic_db(tmp_path)
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES ('c1', 'vm1')")
    db._conn.commit()

    stub_vm_gates(monkeypatch)

    @contextlib.contextmanager
    def _fake_prepare(*a: object, **k: object):  # noqa: ANN202
        # The orchestrated helper yields (vm, target) inside the gate's
        # held-active span.
        yield (
            SimpleNamespace(name="vm1", admin_username="admin"),
            SimpleNamespace(run=lambda *a, **k: None),
        )

    monkeypatch.setattr(multi_console, "_prepare_vm_target_for_attach", _fake_prepare)
    monkeypatch.setattr(
        multi_console,
        "_console_tmux_exists",
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        multi_console,
        "_console_build_secret_targets",
        lambda *a, **k: [object()],
    )

    build_called: list[bool] = []
    monkeypatch.setattr(
        multi_console,
        "_build_console_tmux",
        lambda *a, **k: build_called.append(True),
    )

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env-var",
        )

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    config = SimpleNamespace(
        named_console=SimpleNamespace(tmux_layout="aw-session-vertical"),
    )

    monkeypatch.delenv("TMUX", raising=False)
    with pytest.raises(SecretUnavailableError, match="api-key"):
        multi_console.attach_console(db, config, name="c1")  # type: ignore[arg-type]

    assert build_called == [], "eager-resolve must fire before _build_console_tmux; build ran anyway"
    db.close()


def test_console_build_secret_targets_excludes_session_attach_panes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_console_build_secret_targets enumerates only the panes that
    OPEN NEW SHELLS: the admin shell (when set) and each configured
    helper shell pane. Session-attach windows are deliberately
    excluded per FRD R4 (they join existing tmux servers; SetEnv on
    the SSH connection doesn't flow into the existing server's panes).

    Pins enumeration shape (admin_shell present, helper shells one-per-
    config, no session-attach pane). The per-pane scope-selection
    contract (admin promotion, scope mix) is covered by sibling tests
    on _pane_secret_target via add_shell."""
    from agentworks.sessions import multi_console

    db = _seed_basic_db(tmp_path)
    # Seed: console with admin_shell=True + one session with two shells.
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode) VALUES ('s1', 'ws1', 'default', 'admin')"
    )
    db._conn.execute("INSERT INTO consoles (name, vm_name, admin_shell) VALUES ('c1', 'vm1', 1)")
    # Two shells: one --admin, one not. Admin-mode session promotes the
    # non-admin shell to admin via use_admin = ... or session_user ==
    # admin_user.
    db._conn.execute(
        "INSERT INTO console_sessions (console_name, session_name, shells, position) "
        'VALUES (\'c1\', \'s1\', \'[{"cwd":null,"admin":true},{"cwd":null,"admin":false}]\', 0)'
    )
    db._conn.commit()

    sentinel_pane = object()
    sentinel_admin = object()
    monkeypatch.setattr(
        multi_console,
        "_pane_secret_target",
        lambda *a, **k: sentinel_pane,
    )
    monkeypatch.setattr(
        multi_console,
        "_admin_only_secret_target",
        lambda *a, **k: sentinel_admin,
    )

    vm = db.get_vm("vm1")
    console = db.get_console("c1")
    assert vm is not None
    assert console is not None
    from tests.conftest import _StubRegistry

    targets = multi_console._console_build_secret_targets(
        db,
        _StubRegistry(SimpleNamespace()),
        console=console,
        vm=vm,  # type: ignore[arg-type]
    )

    # Expected: 1 admin-shell + 2 shell panes (one per configured shell).
    # No session-attach pane.
    assert len(targets) == 3
    assert targets[0] is sentinel_admin
    assert targets[1] is sentinel_pane
    assert targets[2] is sentinel_pane
    db.close()


def test_attach_console_existing_tmux_session_skips_eager_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the tmux session already exists (plain attach, not first-
    attach build), FRD R4 says no secrets are consumed. The wiring
    must reflect that: resolve_for_command is NOT called."""
    from agentworks.sessions import multi_console

    db = _seed_basic_db(tmp_path)
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES ('c1', 'vm1')")
    db._conn.commit()

    stub_vm_gates(monkeypatch)

    @contextlib.contextmanager
    def _fake_prepare(*a: object, **k: object):  # noqa: ANN202
        yield (
            SimpleNamespace(name="vm1", admin_username="admin"),
            SimpleNamespace(
                run=lambda *a, **k: None,
                interactive=lambda *a, **k: 0,
            ),
        )

    monkeypatch.setattr(multi_console, "_prepare_vm_target_for_attach", _fake_prepare)
    monkeypatch.setattr(
        multi_console,
        "_console_tmux_exists",
        lambda *a, **k: True,
    )

    resolve_called: list[bool] = []

    def _track_resolve(*args: object, **kwargs: object) -> dict[str, str]:
        resolve_called.append(True)
        return {}

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _track_resolve)

    config = SimpleNamespace(
        named_console=SimpleNamespace(tmux_layout="aw-session-vertical"),
    )

    monkeypatch.delenv("TMUX", raising=False)
    multi_console.attach_console(db, config, name="c1")  # type: ignore[arg-type]

    assert resolve_called == [], (
        "plain attach (existing tmux session) must NOT eager-resolve "
        "per FRD R4: it joins existing shells, consumes no secrets"
    )
    db.close()


# ---------------------------------------------------------------------------
# console add-sessions / restore-session
# ---------------------------------------------------------------------------


def test_console_add_sessions_does_not_eager_resolve_live_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``console add-sessions`` with a live tmux session: wraps existing
    sessions into new console windows via tmux new-window + attach. No
    new agent shells are opened; per FRD R4/R5 no secrets consumed."""
    from agentworks.sessions import multi_console

    db = _seed_basic_db(tmp_path)
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode) VALUES ('s1', 'ws1', 'default', 'admin')"
    )
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES ('c1', 'vm1')")
    db._conn.commit()

    resolve_called: list[bool] = []

    def _track_resolve(*args: object, **kwargs: object) -> dict[str, str]:
        resolve_called.append(True)
        return {}

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _track_resolve)
    # Live branch: simulate a live tmux session so _add_session_window runs.
    fake_vm = SimpleNamespace(name="vm1", admin_username="admin", tailscale_host="x")
    fake_target = SimpleNamespace(
        run=lambda *a, **k: SimpleNamespace(ok=True, returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        multi_console,
        "_live_target",
        lambda *a, **k: (fake_vm, fake_target),
    )
    monkeypatch.setattr(
        multi_console,
        "_console_tmux_exists",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        multi_console,
        "_add_session_window",
        lambda *a, **k: None,
    )

    config = SimpleNamespace(
        named_console=SimpleNamespace(tmux_layout="aw-session-vertical"),
    )
    multi_console.add_sessions(
        db,
        config,
        console_name="c1",
        session_specs=["s1"],  # type: ignore[arg-type]
    )

    assert resolve_called == [], "console add-sessions even on the live branch must not eager-resolve"
    db.close()


def test_console_add_sessions_does_not_eager_resolve_db_only_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``console add-sessions`` DB-only branch (no live tmux): just
    inserts console_sessions rows. Trivially no secrets."""
    from agentworks.sessions import multi_console

    db = _seed_basic_db(tmp_path)
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode) VALUES ('s1', 'ws1', 'default', 'admin')"
    )
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES ('c1', 'vm1')")
    db._conn.commit()

    resolve_called: list[bool] = []

    def _track_resolve(*args: object, **kwargs: object) -> dict[str, str]:
        resolve_called.append(True)
        return {}

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _track_resolve)
    monkeypatch.setattr(multi_console, "_live_target", lambda *a, **k: None)

    config = SimpleNamespace()
    multi_console.add_sessions(
        db,
        config,
        console_name="c1",
        session_specs=["s1"],  # type: ignore[arg-type]
    )

    assert resolve_called == [], "console add-sessions DB-only branch must not eager-resolve"
    db.close()


def test_console_add_sessions_with_shells_eager_resolves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``console add-sessions s1+2`` requests 2 new shell panes per
    session. Per FRD R4 those panes consume secrets at open time, so
    eager-resolve must fire BEFORE the DB write that records the new
    shells. Regression test for the PR-review finding that the +N path
    silently opened shells with no eager-resolve."""
    from agentworks.sessions import multi_console

    db = _seed_basic_db(tmp_path)
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode) VALUES ('s1', 'ws1', 'default', 'admin')"
    )
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES ('c1', 'vm1')")
    db._conn.commit()

    monkeypatch.setattr(
        multi_console,
        "_pane_secret_target",
        lambda *a, **k: object(),
    )

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env-var",
        )

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    config = SimpleNamespace(
        named_console=SimpleNamespace(tmux_layout="aw-session-vertical"),
    )

    with pytest.raises(SecretUnavailableError, match="api-key"):
        multi_console.add_sessions(
            db,
            config,  # type: ignore[arg-type]
            console_name="c1",
            session_specs=["s1+2"],
        )

    # DB write must not have happened.
    assert db.get_console_session("c1", "s1") is None, (
        "eager-resolve must fire BEFORE the console_sessions DB insert when any spec requests shells"
    )
    db.close()


def test_console_add_sessions_without_shells_does_not_eager_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``console add-sessions s1 s2`` (no +N) opens no new shells in the
    DB-write path; it only registers DB rows. The live-attach wrappers
    join existing tmux servers without consuming secrets. Eager-resolve
    must NOT fire."""
    from agentworks.sessions import multi_console

    db = _seed_basic_db(tmp_path)
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode) VALUES ('s1', 'ws1', 'default', 'admin')"
    )
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES ('c1', 'vm1')")
    db._conn.commit()

    monkeypatch.setattr(multi_console, "_live_target", lambda *a, **k: None)

    resolve_called: list[bool] = []

    def _track_resolve(*args: object, **kwargs: object) -> dict[str, str]:
        resolve_called.append(True)
        return {}

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _track_resolve)

    config = SimpleNamespace()
    multi_console.add_sessions(
        db,
        config,  # type: ignore[arg-type]
        console_name="c1",
        session_specs=["s1"],
    )

    assert resolve_called == [], "add-sessions without +N must not eager-resolve; wrappers only join existing sessions"
    db.close()


def test_restore_session_window_missing_branch_eager_resolves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When restore_session's window-missing path rebuilds via
    _add_session_window, it opens new shells -- so eager-resolve must
    fire BEFORE the rebuild. Regression test for the PR-review finding
    that this branch bypassed the eager-resolve wired further down."""
    from agentworks.sessions import multi_console

    db = _seed_basic_db(tmp_path)
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode) VALUES ('s1', 'ws1', 'default', 'admin')"
    )
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES ('c1', 'vm1')")
    # Two configured shells so the rebuild would open new panes.
    db._conn.execute(
        "INSERT INTO console_sessions (console_name, session_name, shells, position) "
        "VALUES ('c1', 's1', "
        '\'[{"cwd":null,"admin":true},{"cwd":null,"admin":false}]\', 0)'
    )
    db._conn.commit()

    fake_vm = SimpleNamespace(name="vm1", admin_username="admin", tailscale_host="x")
    # `tmux list-windows` returns names NOT including 's1' (window missing).
    fake_target = SimpleNamespace(
        run=lambda *a, **k: SimpleNamespace(ok=True, returncode=0, stdout="other-window", stderr=""),
    )
    stub_vm_gates(monkeypatch)

    @contextlib.contextmanager
    def _fake_prepare(*a: object, **k: object):  # noqa: ANN202
        yield (fake_vm, fake_target)

    monkeypatch.setattr(multi_console, "_prepare_vm_target_for_attach", _fake_prepare)
    monkeypatch.setattr(
        multi_console,
        "_console_tmux_exists",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        multi_console,
        "_restore_session_secret_targets",
        lambda *a, **k: [object(), object()],
    )

    add_called: list[bool] = []
    monkeypatch.setattr(
        multi_console,
        "_add_session_window",
        lambda *a, **k: add_called.append(True),
    )

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env-var",
        )

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    config = SimpleNamespace(
        named_console=SimpleNamespace(tmux_layout="aw-session-vertical"),
    )

    with pytest.raises(SecretUnavailableError, match="api-key"):
        multi_console.restore_session(
            db,
            config,  # type: ignore[arg-type]
            console_name="c1",
            session_name="s1",
        )

    assert add_called == [], "eager-resolve must fire BEFORE _add_session_window in the window-missing rebuild branch"
    db.close()
