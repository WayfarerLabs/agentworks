"""Tests for tmux build orchestration via `attach_console` (FakeTarget-mocked).

Split out of `test_consoles.py` (see `.claude/rules/code-style.md` on file-size
targets). Covers the initial build, placeholder handling, the admin-shell
window, build/attach/recreate announcements, position-order iteration, and
missing-session/missing-agent warnings. Shared seed helpers and stub Config
classes live in `tests/_consoles_support.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.db import Database
from agentworks.sessions.multi_console import add_sessions, create_console, describe_console, remove_sessions
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests.conftest import _FakeResult, _FakeTarget

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput

# -- Tmux orchestration (FakeTarget-mocked) --------------------------------


def test_attach_loop_wrapper_format() -> None:
    """Lock the wrapper shape: entry banner + forever attach loop with
    exit-status notice on session-end."""
    from agentworks.sessions.multi_console import _attach_loop_wrapper

    wrapper = _attach_loop_wrapper("backend", None)
    # Unset TMUX so console -> session nesting is allowed.
    assert "unset TMUX" in wrapper
    # Forever loop with no break/timeout, no "press enter to close" prompt.
    assert "while true" in wrapper
    assert "break" not in wrapper
    assert "Press enter" not in wrapper
    # Entry banner names the session.
    assert "Waiting for session backend to come up" in wrapper
    # Exit notice distinguishes clean vs non-zero attach status; the post-exit
    # banner tells the user we're waiting for a restart so the pane isn't silent.
    assert "Session backend exited cleanly" in wrapper
    assert "exited (status $rc)" in wrapper
    assert "Waiting for session to restart" in wrapper
    # Silent poll with 2s back-off.
    assert "sleep 2" in wrapper
    assert "sleep 1" not in wrapper

    # Socketed wrapper threads -S through both has-session and attach.
    wrapper_sock = _attach_loop_wrapper("a", "/tmp/a.sock")
    assert "tmux -S /tmp/a.sock has-session" in wrapper_sock
    assert "tmux -S /tmp/a.sock attach" in wrapper_sock


def test_attach_console_builds_initial_tmux(db: Database, fake_target: _FakeTarget) -> None:
    """First attach: kill any existing session, create with a placeholder
    window, add one new-window per member in DB order, then drop the
    placeholder. Two shells on the first member -> two split-windows + tiled."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha", "beta"])
    create_console(db, name="con", vm_name="vm1", session_specs=["alpha+2", "beta"])

    # Simulate console not existing yet so build path runs.
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    # list-windows must report real windows so the placeholder gets killed.
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(
        returncode=0, stdout="_PLACEHOLDER\nalpha\nbeta\n"
    )

    attach_console(db, _StubConfig(), name="con", allow_nesting=True)

    cmds = fake_target.commands
    assert any("has-session -t aw-console-con" in c for c in cmds)
    assert any("kill-session -t aw-console-con" in c for c in cmds)
    assert any("new-session -d -s aw-console-con -n _PLACEHOLDER" in c for c in cmds)
    # No admin-shell window: named consoles only contain the curated sessions.
    assert not any("--admin--" in c for c in cmds)
    assert not any("admin-shell" in c for c in cmds)
    new_window_indexes = [i for i, c in enumerate(cmds) if "new-window -t aw-console-con" in c]
    assert len(new_window_indexes) == 2, cmds
    assert "alpha" in cmds[new_window_indexes[0]]
    assert "beta" in cmds[new_window_indexes[1]]
    split_cmds = [c for c in cmds if "split-window -t aw-console-con" in c]
    assert len(split_cmds) == 2, cmds  # two shells on alpha, none on beta
    assert any("select-layout -t aw-console-con:alpha tiled" in c for c in cmds)
    # Placeholder gets killed once real windows are in.
    assert any("kill-window -t aw-console-con:_PLACEHOLDER" in c for c in cmds)


def test_attach_console_placeholder_name_cannot_collide_with_session(db: Database, fake_target: _FakeTarget) -> None:
    """A user-created session literally named 'placeholder' must not be
    accidentally killed when we drop the build placeholder. The placeholder
    uses '--' (forbidden by validate_name) so collisions are impossible."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["placeholder", "real"])
    create_console(db, name="con", vm_name="vm1", session_specs=["placeholder", "real"])
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(
        returncode=0, stdout="_PLACEHOLDER\nplaceholder\nreal\n"
    )

    attach_console(db, _StubConfig(), name="con", allow_nesting=True)

    kill_windows = [c for c in fake_target.commands if "kill-window" in c]
    # We kill exactly the build placeholder, never the user's window.
    assert any("_PLACEHOLDER" in c for c in kill_windows)
    assert not any(
        "kill-window -t aw-console-con:placeholder" in c and "_PLACEHOLDER" not in c for c in fake_target.commands
    )


def test_attach_console_warns_when_list_windows_fails(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """If list-windows fails (SSH drop etc.), the user gets a warning so the
    persisting placeholder isn't a silent surprise."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    create_console(db, name="con", vm_name="vm1", session_specs=["alpha"])
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(returncode=255, stderr="transport failure")

    attach_console(db, _StubConfig(), name="con", allow_nesting=True)

    assert any("could not list windows" in w for w in captured_output.warnings)
    # No kill-window for the placeholder since we couldn't confirm cleanup.
    assert not any("kill-window -t aw-console-con:_PLACEHOLDER" in c for c in fake_target.commands)


def test_create_console_with_admin_shell_persists_flag(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console(
        db,
        name="con",
        vm_name="vm1",
        session_specs=["a"],
        add_admin_shell=True,
    )
    console = db.get_console("con")
    assert console is not None
    assert console.admin_shell is True


def test_create_console_admin_shell_only_allowed(db: Database) -> None:
    """A console with admin_shell=True and no sessions is allowed (top-level shell only)."""
    _seed_vm(db)
    create_console(
        db,
        name="shell-only",
        vm_name="vm1",
        session_specs=[],
        add_admin_shell=True,
    )
    console = db.get_console("shell-only")
    assert console is not None
    assert console.admin_shell is True
    assert db.list_console_sessions("shell-only") == []


def test_attach_console_builds_admin_shell_window_without_placeholder(db: Database, fake_target: _FakeTarget) -> None:
    """When admin_shell is set, window 0 is the admin-shell -- no placeholder."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    create_console(
        db,
        name="con",
        vm_name="vm1",
        session_specs=["alpha"],
        add_admin_shell=True,
    )
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)

    attach_console(db, _StubConfig(), name="con", allow_nesting=True)

    cmds = fake_target.commands
    new_sessions = [c for c in cmds if "new-session -d -s aw-console-con" in c]
    assert len(new_sessions) == 1
    # Window 0 is the --admin-- window, running `exec $SHELL -l` directly --
    # the prior `sudo su --login <admin>` wrapper was a no-op user-switch
    # (post FRD R1 the SSH user IS the admin user) and got dropped by the
    # env-and-secrets SDD.
    assert "-n --admin--" in new_sessions[0]
    assert "exec $SHELL -l" in new_sessions[0]
    assert "sudo" not in new_sessions[0]
    assert not any("_PLACEHOLDER" in c for c in cmds)
    assert not any("list-windows" in c for c in cmds)
    new_windows = [c for c in cmds if "new-window -t aw-console-con" in c]
    assert len(new_windows) == 1 and "alpha" in new_windows[0]


def test_describe_console_shows_admin_shell_state(db: Database, captured_output: CapturedOutput) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console(db, name="plain", vm_name="vm1", session_specs=["a"])
    create_console(db, name="with-shell", vm_name="vm1", session_specs=["a"], add_admin_shell=True)

    captured_output.info.clear()
    describe_console(db, name="plain")
    assert any("Admin shell: no" in m for m in captured_output.info)

    captured_output.info.clear()
    describe_console(db, name="with-shell")
    assert any("Admin shell: yes" in m for m in captured_output.info)


def test_attach_console_keeps_placeholder_when_all_members_fail(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """If every new-window call fails, the placeholder stays so the tmux
    session survives for the user to investigate."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    create_console(db, name="con", vm_name="vm1", session_specs=["alpha"])
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    # new-window for alpha fails -> only placeholder ends up in list-windows.
    fake_target.responses["new-window -t aw-console-con"] = _FakeResult(returncode=1, stderr="simulated failure")
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(returncode=0, stdout="_PLACEHOLDER\n")

    attach_console(db, _StubConfig(), name="con", allow_nesting=True)

    assert not any("kill-window -t aw-console-con:_PLACEHOLDER" in c for c in fake_target.commands)
    assert any("placeholder kept" in w for w in captured_output.warnings)


def test_attach_console_announces_build_path(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """First attach prints a 'Building...' status."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(returncode=0, stdout="_PLACEHOLDER\na\n")

    captured_output.info.clear()
    attach_console(db, _StubConfig(), name="con", allow_nesting=True)
    assert any("Building console 'con' on first attach" in m for m in captured_output.info)


def test_attach_console_announces_attach_path(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """Subsequent attach (tmux already running) prints an 'Attaching...' status,
    not a build status."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)

    captured_output.info.clear()
    attach_console(db, _StubConfig(), name="con", allow_nesting=True)
    assert any("Attaching to running console 'con'" in m for m in captured_output.info)
    assert not any("Building" in m or "Rebuilding" in m for m in captured_output.info)


def test_attach_console_announces_recreate_path(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """--recreate against an alive console prints a 'Rebuilding...' status."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(returncode=0, stdout="_PLACEHOLDER\na\n")

    captured_output.info.clear()
    attach_console(db, _StubConfig(), name="con", recreate=True, allow_nesting=True)
    assert any("Rebuilding console 'con' (--recreate)" in m for m in captured_output.info)


def test_attach_console_reuses_existing_tmux(db: Database, fake_target: _FakeTarget) -> None:
    """Subsequent attach: console exists -> no rebuild commands fire."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    create_console(db, name="con", vm_name="vm1", session_specs=["alpha"])
    # Console exists.
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)

    attach_console(db, _StubConfig(), name="con", allow_nesting=True)

    cmds = fake_target.commands
    assert not any("new-session" in c for c in cmds)
    assert not any("new-window" in c for c in cmds)


def test_attach_console_recreate_rebuilds_even_if_alive(db: Database, fake_target: _FakeTarget) -> None:
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    create_console(db, name="con", vm_name="vm1", session_specs=["alpha"])
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(returncode=0, stdout="_PLACEHOLDER\nalpha\n")

    attach_console(db, _StubConfig(), name="con", recreate=True, allow_nesting=True)

    cmds = fake_target.commands
    assert any("kill-session -t aw-console-con" in c for c in cmds)
    assert any("new-session -d -s aw-console-con" in c for c in cmds)


def test_attach_console_iterates_in_position_order(db: Database, fake_target: _FakeTarget) -> None:
    """Even when DB positions have gaps (after a remove), iteration uses
    ORDER BY position ASC, not insertion order or row order."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b", "c", "d"])
    # Force live-sync to short-circuit so the mutations below don't issue
    # spurious tmux commands that pollute the attach assertion.
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b", "c"])
    remove_sessions(db, _StubConfig(), console_name="con", session_names=["b"])
    add_sessions(db, _StubConfig(), console_name="con", session_specs=["d"])
    # positions are now a=0, c=2, d=3.

    fake_target.commands.clear()
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(
        returncode=0, stdout="_PLACEHOLDER\na\nc\nd\n"
    )
    attach_console(db, _StubConfig(), name="con", allow_nesting=True)

    new_windows = [c for c in fake_target.commands if "new-window -t aw-console-con" in c]
    names = [c.split("-n ")[1].split()[0] for c in new_windows]
    assert names == ["a", "c", "d"]


def test_attach_console_skips_missing_session_with_warning(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """Cascade should keep this from happening normally, but if a member row
    survives without its session, we warn and continue."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha", "ghost"])
    create_console(db, name="con", vm_name="vm1", session_specs=["alpha", "ghost"])
    # Delete the session directly so console_sessions still has a 'ghost' row.
    # ON DELETE CASCADE would normally clear it; bypass via raw SQL to simulate
    # an inconsistency.
    db._conn.execute("PRAGMA foreign_keys = OFF")
    db._conn.execute("DELETE FROM sessions WHERE name = 'ghost'")
    db._conn.execute("PRAGMA foreign_keys = ON")
    db._conn.commit()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(returncode=0, stdout="_PLACEHOLDER\nalpha\n")

    attach_console(db, _StubConfig(), name="con", allow_nesting=True)

    assert any("ghost" in w and "no longer exists" in w for w in captured_output.warnings)
    new_windows = [c for c in fake_target.commands if "new-window -t aw-console-con" in c]
    # Only the surviving session gets a window.
    assert len(new_windows) == 1
    assert "alpha" in new_windows[0]


def test_attach_console_skips_window_when_agent_missing(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """If an agent-mode session's agent row is gone (FK violation under
    foreign_keys=OFF, or post-migration inconsistency), _session_linux_user
    raises NotFoundError. _add_session_window catches it, warns, and continues
    instead of aborting the whole console build."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    # Create an agent-mode session pointing at an agent row, then delete the
    # agent row directly to simulate the inconsistency.
    db._conn.execute("INSERT INTO agents (name, vm_name, linux_user) VALUES ('bot', 'vm1', 'bot-user')")
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, agent_name, socket_path) "
        "VALUES ('s', 'ws-vm1', 'default', 'agent', 'bot', '/tmp/s.sock')"
    )
    db._conn.commit()
    create_console(db, name="con", vm_name="vm1", session_specs=["s+1"])
    db._conn.execute("PRAGMA foreign_keys = OFF")
    db._conn.execute("DELETE FROM agents WHERE name = 'bot'")
    db._conn.execute("PRAGMA foreign_keys = ON")
    db._conn.commit()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(returncode=0, stdout="_PLACEHOLDER\ns\n")

    attach_console(db, _StubConfig(), name="con", allow_nesting=True)

    assert any("agent for session 's' is missing" in w for w in captured_output.warnings)
    # The window itself was created (new-window happened before the agent check);
    # only the split-window calls for the shell panes are skipped.
    new_windows = [c for c in fake_target.commands if "new-window -t aw-console-con" in c]
    assert len(new_windows) == 1
    splits = [c for c in fake_target.commands if "split-window -t aw-console-con:s" in c]
    assert splits == []
