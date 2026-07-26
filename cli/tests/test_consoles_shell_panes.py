"""Tests for `add_shell`/`delete_console` live-tmux effects and `_split_shell_pane`.

Split out of `test_consoles.py` (see `.claude/rules/code-style.md` on file-size
targets). Covers sudo/`--preserve-env` behavior of `_split_shell_pane`
(including the `_seed_agent_session_console` local helper and the `_PROBE`
constant, both used only by tests in this module) and pane-tagging
(`@agentworks-shell-index`) tests. Shared seed helpers and stub Config classes
live in `tests/_consoles_support.py`.
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database
from agentworks.sessions import multi_console
from agentworks.sessions.multi_console import add_shell, create_console, delete_console
from agentworks.sessions.multi_console_layout import SHELL_INDEX_OPTION
from agentworks.vms.initializer import AGENTWORKS_SUDOERS_ENV_KEEP_PATTERNS
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests.conftest import _FakeResult, _FakeTarget

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput


def test_add_shell_live_sync_splits_pane_and_tiles(db: Database, fake_target: _FakeTarget) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    add_shell(db, _StubConfig(), console_name="con", session_name="a", cwd="src", admin=True)

    splits = [c for c in fake_target.commands if "split-window -t aw-console-con:a" in c]
    assert len(splits) == 1
    # Pane cwd reflects the relative path joined under the workspace root.
    assert "/home/me/vm1/src" in splits[0]
    layouts = [c for c in fake_target.commands if "select-layout -t aw-console-con:a tiled" in c]
    assert len(layouts) == 1


def test_delete_console_live_kills_tmux_session(db: Database, fake_target: _FakeTarget) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    delete_console(db, _StubConfig(), name="con", yes=True)

    kill_session = [c for c in fake_target.commands if "kill-session -t aw-console-con" in c]
    assert len(kill_session) == 1
    assert db.get_console("con") is None


def test_split_shell_pane_agent_branch_uses_sudo(db: Database, fake_target: _FakeTarget) -> None:
    """Agent-user shells bootstrap via `sudo --login -u <user> bash -c '...'`;
    admin-user shells skip the sudo wrapper since the console is already admin."""
    # Build an agent + agent-mode session manually so we can exercise the
    # session_user != admin_user branch of _split_shell_pane.
    _seed_vm(db, with_tailscale=True)
    db._conn.execute(
        "INSERT INTO agents (name, vm_name, linux_user) VALUES ('bot', 'vm1', 'bot-user')",
    )
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, agent_name, socket_path) "
        "VALUES ('s', 'ws-vm1', 'default', 'agent', 'bot', '/tmp/s.sock')",
    )
    db._conn.commit()
    create_console(db, name="con", vm_name="vm1", session_specs=["s"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    add_shell(db, _StubConfig(), console_name="con", session_name="s")  # agent, workspace root

    splits = [c for c in fake_target.commands if "split-window -t aw-console-con:s" in c]
    assert len(splits) == 1
    # `--preserve-env=<keys>` sits between --login and -u (see the dedicated
    # preserve-env test); assert the sudo wrapper and target user separately.
    assert "sudo --login" in splits[0]
    assert "-u bot-user" in splits[0]
    assert 'exec "$SHELL" -l' in splits[0]


def test_split_shell_pane_admin_branch_no_sudo(db: Database, fake_target: _FakeTarget) -> None:
    """Admin shell on an admin-mode session: no sudo, just cd + login shell."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    add_shell(db, _StubConfig(), console_name="con", session_name="a")

    splits = [c for c in fake_target.commands if "split-window -t aw-console-con:a" in c]
    assert len(splits) == 1
    assert "sudo --login" not in splits[0]
    assert 'exec "$SHELL" -l' in splits[0]
    # No sudo crossing, so no --preserve-env needed (the -e vars survive
    # into the login shell directly).
    assert "--preserve-env" not in splits[0]


def _seed_agent_session_console(db: Database) -> None:
    """VM + agent session 's' + console 'con', the agent-pane fixture shape."""
    _seed_vm(db, with_tailscale=True)
    db._conn.execute(
        "INSERT INTO agents (name, vm_name, linux_user) VALUES ('bot', 'vm1', 'bot-user')",
    )
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, agent_name, socket_path) "
        "VALUES ('s', 'ws-vm1', 'default', 'agent', 'bot', '/tmp/s.sock')",
    )
    db._conn.commit()
    create_console(db, name="con", vm_name="vm1", session_specs=["s"])


# Substring identifying the capability probe among the captured commands.
_PROBE = f"sudo -n --preserve-env={multi_console._SUDO_PRESERVE_PROBE_VAR}"


def test_split_shell_pane_agent_branch_preserves_composed_env_across_sudo(
    db: Database, fake_target: _FakeTarget
) -> None:
    """The agent pane sudo's to the agent user, which resets the env. The
    composed keys (which tmux set via -e) are named on `sudo --preserve-env`
    so they survive the crossing; only the names appear, not the values.
    Permitted VM-side by the `Defaults:<admin> setenv` sudoers fragment (the
    probe reports it present here: _FakeTarget defaults to rc=0)."""
    _seed_agent_session_console(db)

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    add_shell(db, _StubConfig(), console_name="con", session_name="s")

    splits = [c for c in fake_target.commands if "split-window -t aw-console-con:s" in c]
    assert len(splits) == 1
    # The composed workspace-identity key is both set via -e and named on
    # --preserve-env so it crosses the sudo boundary.
    assert " -e AGENTWORKS_WORKSPACE=ws-vm1" in splits[0]
    assert "--preserve-env=" in splits[0]
    preserve_arg = splits[0].split("--preserve-env=", 1)[1].split(" ", 1)[0]
    assert "AGENTWORKS_WORKSPACE" in preserve_arg
    # Values are carried by the -e channel, not embedded in the preserve
    # list (only names appear there).
    assert "ws-vm1" not in preserve_arg


def test_sudo_preserve_probe_uses_a_name_no_env_keep_pattern_covers() -> None:
    """The probe must isolate the `setenv` grant, so it names a var that no
    env_keep pattern matches; a covered name would pass validation on a VM
    with no setenv fragment and report a capability that isn't there. Pinned
    against the deployed fragment's own pattern list, so widening that list
    fails here rather than silently blunting the probe."""
    probe_var = multi_console._SUDO_PRESERVE_PROBE_VAR
    for pattern in AGENTWORKS_SUDOERS_ENV_KEEP_PATTERNS:
        assert not fnmatch.fnmatchcase(probe_var, pattern), (
            f"probe var {probe_var!r} is covered by env_keep pattern "
            f"{pattern!r}; it would survive sudo without the setenv fragment "
            f"and the probe would report a capability the VM lacks"
        )


def test_sudo_preserve_probe_command_shape(db: Database, fake_target: _FakeTarget) -> None:
    """The probe sets the var it asks sudo to preserve (it cannot rely on the
    composed env having reached this process: on non-SSH transports it has
    not), and goes through `env` rather than a `VAR=val cmd` prefix because
    this string runs under the admin's configurable login shell, which need
    not be POSIX."""
    probe_var = multi_console._SUDO_PRESERVE_PROBE_VAR
    _seed_agent_session_console(db)
    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    add_shell(db, _StubConfig(), console_name="con", session_name="s")

    probes = [c for c in fake_target.commands if _PROBE in c]
    assert len(probes) == 1
    assert probes[0] == (f"env {probe_var}=1 sudo -n --preserve-env={probe_var} -u bot-user true")


def test_split_shell_pane_agent_branch_warns_and_falls_back_when_setenv_missing(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """A VM without the `Defaults:<admin> setenv` fragment makes sudo refuse
    the whole --preserve-env command (it rejects the env_add vars outside
    env_keep and aborts) rather than dropping the vars, so asking for the flag
    anyway would kill the pane on spawn. On a failed probe we drop the flag,
    keeping the env_keep-only pane, and warn at the operator's surface rather
    than only inside the pane."""
    _seed_agent_session_console(db)
    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    # Simulate the VM refusing --preserve-env (no setenv fragment), with sudo's
    # own refusal text on stderr.
    fake_target.responses[_PROBE] = _FakeResult(
        returncode=1,
        stderr=("sudo: sorry, you are not allowed to set the following environment variables: AWPROBE"),
    )
    add_shell(db, _StubConfig(), console_name="con", session_name="s")

    splits = [c for c in fake_target.commands if "split-window -t aw-console-con:s" in c]
    assert len(splits) == 1
    # The pane still comes up, just without the flag sudo would have refused.
    assert "--preserve-env" not in splits[0]
    assert "exec sudo --login -u bot-user" in splits[0]
    # The -e vars still ride the tmux channel.
    assert " -e AGENTWORKS_WORKSPACE=ws-vm1" in splits[0]
    # The warning names the requirement and the recovery, and quotes sudo
    # rather than diagnosing a cause the probe cannot establish.
    warning = next(w for w in captured_output.warnings if "will not reach" in w)
    assert "Defaults:admin setenv" in warning
    assert "51-agentworks-console-setenv" in warning
    assert "agw vm reinit vm1" in warning
    assert "not allowed to set the following environment variables" in warning


def test_split_shell_pane_preserve_probe_warns_once_per_console_build(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """Building a console splits a pane per shell per session, all asking the
    same VM the same question. The memo keeps that to one probe and one
    warning per agent user, so a miss does not bury the attach output."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    db._conn.execute(
        "INSERT INTO agents (name, vm_name, linux_user) VALUES ('bot', 'vm1', 'bot-user')",
    )
    for name in ("s1", "s2"):
        db._conn.execute(
            "INSERT INTO sessions (name, workspace_name, template, mode, agent_name, "
            "socket_path) VALUES (?, 'ws-vm1', 'default', 'agent', 'bot', ?)",
            (name, f"/tmp/{name}.sock"),
        )
    db._conn.commit()
    # Two sessions, two agent shells each: four agent panes off the one VM.
    create_console(db, name="con", vm_name="vm1", session_specs=["s1+2", "s2+2"])

    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(returncode=0, stdout="_PLACEHOLDER\ns1\ns2\n")
    fake_target.responses[_PROBE] = _FakeResult(returncode=1)
    fake_target.commands.clear()
    attach_console(db, _StubConfig(), name="con", allow_nesting=True)

    splits = [c for c in fake_target.commands if "split-window -t aw-console-con" in c]
    assert len(splits) == 4
    assert not any("--preserve-env" in c for c in splits)
    # One probe and one warning for the whole build, not one per pane.
    assert len([c for c in fake_target.commands if _PROBE in c]) == 1
    assert len([w for w in captured_output.warnings if "will not reach" in w]) == 1


def test_split_shell_pane_agent_branch_no_probe_without_composed_env(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """With no composed env there is nothing to preserve, so there is nothing
    to ask sudo about: no probe, no warning, no empty `--preserve-env=`."""
    _seed_agent_session_console(db)
    monkeypatch.setattr(multi_console, "_resolve_pane_env", lambda *a, **k: {})

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    add_shell(db, _StubConfig(), console_name="con", session_name="s")

    splits = [c for c in fake_target.commands if "split-window -t aw-console-con:s" in c]
    assert len(splits) == 1
    assert "--preserve-env" not in splits[0]
    assert not [c for c in fake_target.commands if _PROBE in c]
    assert "exec sudo --login -u bot-user" in splits[0]
    assert not [w for w in captured_output.warnings if "will not reach this pane" in w]


def test_split_shell_pane_admin_branch_never_probes(db: Database, fake_target: _FakeTarget) -> None:
    """The admin pane never sudo's, so there is no boundary to preserve across
    and no reason to spend a probe on every split."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    add_shell(db, _StubConfig(), console_name="con", session_name="a")

    assert not [c for c in fake_target.commands if _PROBE in c]


def test_split_shell_pane_emits_workspace_identity_only(db: Database, fake_target: _FakeTarget) -> None:
    """``tmux split-window -e KEY=VAL`` flags on a console add-shell agent
    pane carry the workspace dynamic-identity vars only. The pane is a
    sidecar shell rooted in the session's workspace -- it's not part of
    the session, so it doesn't see AGENTWORKS_SESSION[_KIND]. The agent's
    own AGENTWORKS_AGENT is per-user-static and reaches the pane via the
    agent's on-disk ``~/.agentworks-profile.sh`` (login-shell sourcing),
    not via SetEnv. Tests the post-static/dynamic-split contract."""
    _seed_vm(db, with_tailscale=True)
    db._conn.execute(
        "INSERT INTO agents (name, vm_name, linux_user) VALUES ('bot', 'vm1', 'bot-user')",
    )
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, agent_name, socket_path) "
        "VALUES ('s', 'ws-vm1', 'default', 'agent', 'bot', '/tmp/s.sock')",
    )
    db._conn.commit()
    create_console(db, name="con", vm_name="vm1", session_specs=["s"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    add_shell(db, _StubConfig(), console_name="con", session_name="s")

    splits = [c for c in fake_target.commands if "split-window -t aw-console-con:s" in c]
    assert len(splits) == 1
    # Workspace dynamic identity reaches the pane.
    assert " -e AGENTWORKS_WORKSPACE=ws-vm1" in splits[0]
    assert " -e AGENTWORKS_WORKSPACE_DIR=" in splits[0]
    # Session dynamic identity does NOT (add-shell panes are sidecar
    # shells, not part of the session itself).
    assert "AGENTWORKS_SESSION" not in splits[0]
    # Agent static identity does NOT come via SetEnv (it's in the agent's
    # per-user profile fragment).
    assert "AGENTWORKS_AGENT" not in splits[0]


# -- Pane tagging ----------------------------------------------------------


def test_split_shell_pane_tags_new_pane_with_config_index(db: Database, fake_target: _FakeTarget) -> None:
    """After split-window emits the new pane id, _split_shell_pane sets
    @agentworks-shell-index so restore-session can identify which configured
    shell a given live pane corresponds to."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    # Simulate tmux split-window -P emitting a pane id.
    fake_target.responses["split-window -t aw-console-con:a"] = _FakeResult(stdout="%7\n")

    add_shell(db, _StubConfig(), console_name="con", session_name="a")

    set_options = [c for c in fake_target.commands if "set-option -p" in c and SHELL_INDEX_OPTION in c]
    assert len(set_options) == 1
    # The first shell added is config index 0 (cs.shells was empty).
    assert f"-t %7 {SHELL_INDEX_OPTION} 0" in set_options[0]


def test_split_shell_pane_warns_when_split_returns_no_pane_id(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """If split-window's stdout is empty (older tmux / weird transport), the
    tag step is skipped and the operator gets a warning that the pane is
    untagged. The pane is still live; restore-session just won't be able to
    repair this window without `attach --recreate`."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    # Default _FakeResult has empty stdout, so no pane_id to tag.

    add_shell(db, _StubConfig(), console_name="con", session_name="a")

    set_options = [c for c in fake_target.commands if "set-option -p" in c]
    assert set_options == []
    # The recovery hint includes the actual console name so it can be
    # copy/pasted verbatim.
    assert any(
        "couldn't capture its id" in w and "untagged" in w and "attach con --recreate" in w
        for w in captured_output.warnings
    )


def test_split_shell_pane_warns_when_set_option_fails(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """If tmux split-window succeeded and emitted a pane id but the subsequent
    set-option fails (tmux version/flags mismatch, target gone, etc.), the
    pane is live but untagged. _split_shell_pane must surface this so the
    operator gets a loud signal instead of restore-session breaking later."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["split-window -t aw-console-con:a"] = _FakeResult(stdout="%7\n")
    # set-option fails non-zero.
    fake_target.responses["set-option -p"] = _FakeResult(returncode=1, stderr="bad target")

    add_shell(db, _StubConfig(), console_name="con", session_name="a")

    assert any("tagging failed" in w and "attach con --recreate" in w for w in captured_output.warnings)
