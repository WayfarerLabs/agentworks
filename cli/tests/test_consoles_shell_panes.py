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
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import multi_console
from agentworks.sessions.multi_console import add_shell, delete_console
from agentworks.sessions.multi_console_layout import SHELL_INDEX_OPTION, _reorder_shell_panes
from agentworks.vms.initializer import AGENTWORKS_SUDOERS_ENV_KEEP_PATTERNS
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests._tmux_model import TmuxModel
from tests.conftest import _FakeResult, _FakeTarget
from tests.console_helpers import create_console_record as create_console

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.conftest import CapturedOutput

# tmux session name for console "con"; see tmux_session_name.
CON = "aw-console-con"


def test_add_shell_live_sync_splits_pane_and_tiles(db: Database, fake_target: _FakeTarget) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    add_shell(
        db,
        _StubConfig(),
        console_name="con",
        session_name="a",
        cwd="src",
        admin=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    splits = [c for c in fake_target.commands if "split-window -t =aw-console-con:a" in c]
    assert len(splits) == 1
    # Pane cwd reflects the relative path joined under the workspace root.
    assert "/home/me/vm1/src" in splits[0]
    layouts = [c for c in fake_target.commands if "select-layout -t =aw-console-con:a tiled" in c]
    assert len(layouts) == 1


def test_delete_console_live_kills_tmux_session(db: Database, fake_target: _FakeTarget) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    delete_console(db, _StubConfig(), name="con", yes=True)

    kill_session = [c for c in fake_target.commands if "kill-session -t =aw-console-con" in c]
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
    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    add_shell(
        db, _StubConfig(), console_name="con", session_name="s", interaction=TtyInteractionPolicy.REFUSE
    )  # agent, workspace root

    splits = [c for c in fake_target.commands if "split-window -t =aw-console-con:s" in c]
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
    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    add_shell(db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE)

    splits = [c for c in fake_target.commands if "split-window -t =aw-console-con:a" in c]
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
    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    add_shell(db, _StubConfig(), console_name="con", session_name="s", interaction=TtyInteractionPolicy.REFUSE)

    splits = [c for c in fake_target.commands if "split-window -t =aw-console-con:s" in c]
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
    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    add_shell(db, _StubConfig(), console_name="con", session_name="s", interaction=TtyInteractionPolicy.REFUSE)

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
    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    # Simulate the VM refusing --preserve-env (no setenv fragment), with sudo's
    # own refusal text on stderr.
    fake_target.responses[_PROBE] = _FakeResult(
        returncode=1,
        stderr=("sudo: sorry, you are not allowed to set the following environment variables: AWPROBE"),
    )
    add_shell(db, _StubConfig(), console_name="con", session_name="s", interaction=TtyInteractionPolicy.REFUSE)

    splits = [c for c in fake_target.commands if "split-window -t =aw-console-con:s" in c]
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
    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    add_shell(db, _StubConfig(), console_name="con", session_name="s", interaction=TtyInteractionPolicy.REFUSE)

    splits = [c for c in fake_target.commands if "split-window -t =aw-console-con:s" in c]
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
    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    add_shell(db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE)

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
    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    add_shell(db, _StubConfig(), console_name="con", session_name="s", interaction=TtyInteractionPolicy.REFUSE)

    splits = [c for c in fake_target.commands if "split-window -t =aw-console-con:s" in c]
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
    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    # Simulate tmux split-window -P emitting a pane id.
    fake_target.responses["split-window -t =aw-console-con:a"] = _FakeResult(stdout="%7\n")

    add_shell(db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE)

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
    repair this window without `console restart`."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    # Default _FakeResult has empty stdout, so no pane_id to tag.

    add_shell(db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE)

    set_options = [c for c in fake_target.commands if "set-option -p" in c]
    assert set_options == []
    # The recovery hint includes the actual console name so it can be
    # copy/pasted verbatim.
    assert any(
        "couldn't capture its id" in w and "untagged" in w and "restart con" in w for w in captured_output.warnings
    )


# -- Shell-pane ordering (issue #246 part a) -------------------------------


def test_reorder_shell_panes_puts_tagged_shells_into_config_order(
    console_target_factory: Callable[..., _FakeTarget],
) -> None:
    """Direct proof of the corrective helper on the exact shape the add-shell
    bug produces: a window whose shell panes sit out of config order (tag 1
    above tag 0, as a `split-window` after the session pane leaves them).
    `_reorder_shell_panes` swaps them back so pane index N+1 carries config tag
    N (session pane stays lowest and untagged)."""
    model = TmuxModel()
    # Session pane (untagged) then shells in the wrong order: tag 1 at index 1,
    # tag 0 at index 2. This is what real tmux leaves after add-shell splits the
    # new shell in directly below the active session pane.
    model.seed_session(CON, "a", pane_tags=(None, 1, 0))
    target = console_target_factory(model)

    _reorder_shell_panes(target, CON, "a", 2)

    rows = model.pane_rows(CON, "a")
    assert rows is not None
    assert [(pidx, tag) for _pid, pidx, tag in rows] == [(0, None), (1, 0), (2, 1)]


def test_add_shell_reorders_shell_panes_into_config_order(
    db: Database,
    console_target_factory: Callable[..., _FakeTarget],
) -> None:
    """Regression for issue #246 (a): add-shell must leave the window's shell
    panes in tagged config order.

    `_split_shell_pane` splits the window's active pane, which after an attach
    is the session pane, so a new shell lands directly below it, above the
    existing shells. (The stateful model appends splits at the tail, a
    documented fidelity boundary, so we start from a window already carrying the
    out-of-order shape a prior split left, tags [1, 0], and prove add-shell's
    reorder step settles the whole window, including the freshly split pane,
    back into order [0, 1, 2].) Without the reorder call the window would stay
    out of order."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    # Two configured shells already; the new one becomes config index 2.
    create_console(db, name="con", vm_name="vm1", session_specs=["a+2"])

    model = TmuxModel()
    # Live window is out of config order (tag 1 above tag 0), the state a
    # previous unfixed add-shell leaves behind.
    model.seed_session(CON, "a", pane_tags=(None, 1, 0))
    # Installs the model-backed transport seam (side effect); no handle needed.
    console_target_factory(model)

    add_shell(db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE)

    rows = model.pane_rows(CON, "a")
    assert rows is not None
    # Session pane lowest and untagged; all three shells in config order.
    assert [(pidx, tag) for _pid, pidx, tag in rows] == [(0, None), (1, 0), (2, 1), (3, 2)]


def test_split_shell_pane_warns_when_set_option_fails(db: Database, fake_target: _FakeTarget) -> None:
    """If tmux split-window succeeded and emitted a pane id but the subsequent
    set-option fails (tmux version/flags mismatch, target gone, etc.), the
    pane is live but untagged. _split_shell_pane must surface this so the
    operator gets a loud signal instead of restore-session breaking later."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["split-window -t =aw-console-con:a"] = _FakeResult(stdout="%7\n")
    # set-option fails non-zero.
    fake_target.responses["set-option -p"] = _FakeResult(returncode=1, stderr="bad target")

    add_shell(db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE)
