"""Tests for `add_shell`/`delete_console` live-tmux effects and `_split_shell_pane`.

Split out of `test_consoles.py` (see `.claude/rules/code-style.md` on file-size
targets). Covers the terminal handover of `_split_shell_pane` (each pane parks
its shell's request on its own identity's access and runs the handover's
command, an agent's behind `sudo -n -u`), including the
`_seed_agent_session_console` local helper used only by tests in this module,
and pane-tagging (`@agentworks-shell-index`) tests. Shared seed helpers and stub
Config classes live in `tests/_consoles_support.py`.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database, SessionMode
from agentworks.errors import StateError
from agentworks.execution.access import Admin, Agent
from agentworks.execution.errors import RequestRefused
from agentworks.execution.requests import Command, LoginShell
from agentworks.execution.results import Result, Unknown, UnknownReason
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions.multi_console import add_shell, delete_console
from agentworks.sessions.multi_console_layout import SHELL_INDEX_OPTION, _reorder_shell_panes
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests._tmux_model import TmuxModel
from tests.conftest import _FakeGuest, _FakeResult
from tests.console_helpers import create_console_record as create_console
from tests.vm_access_support import PARK_TOKEN, Call, command_line

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.conftest import CapturedOutput

# tmux session name for console "con"; see tmux_session_name.
CON = "aw-console-con"
# Where the agent session's server listens: the bot agent's socket directory.
AGENT_SOCKET = "/run/agentworks/agent-tmux-sockets/bot-user/s.sock"
BOT = Agent("bot", "bot-user")


def _runs(calls: list[Call]) -> list[Call]:
    return [call for call in calls if call.operation == "run"]


def _pane_command(split: str) -> list[str]:
    """The pane's own command, the last word of a recorded ``split-window`` line."""
    return shlex.split(shlex.split(split)[-1])


def test_add_shell_live_sync_splits_pane_and_tiles(db: Database, fake_guest: _FakeGuest) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_guest.commands.clear()
    fake_guest.responses["has-session -t '=aw-console-con'"] = _FakeResult(returncode=0)
    add_shell(
        db,
        _StubConfig(),
        console_name="con",
        session_name="a",
        cwd="src",
        admin=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    splits = [c for c in fake_guest.commands if "split-window -t '=aw-console-con':a" in c]
    assert len(splits) == 1
    # The pane's directory, the relative path joined under the workspace root, is in its parked request.
    [park] = fake_guest.parks()
    assert park.request.cwd == "/home/me/vm1/src"
    layouts = [c for c in fake_guest.commands if "select-layout -t '=aw-console-con':a tiled" in c]
    assert len(layouts) == 1


def test_delete_console_live_kills_tmux_session(db: Database, fake_guest: _FakeGuest) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_guest.commands.clear()
    delete_console(db, _StubConfig(), name="con", yes=True)

    kill_session = [c for c in fake_guest.commands if "kill-session -t '=aw-console-con'" in c]
    assert len(kill_session) == 1
    assert db.get_console("con") is None


def _seed_agent_session_console(db: Database) -> None:
    """VM + agent session 's' + console 'con', the agent-pane fixture shape."""
    _seed_vm(db, with_tailscale=True)
    db._conn.execute(
        "INSERT INTO agents (name, vm_name, linux_user) VALUES ('bot', 'vm1', 'bot-user')",
    )
    db.insert_session(
        "s",
        "ws-vm1",
        "default",
        SessionMode.AGENT,
        agent_name="bot",
        socket_path=AGENT_SOCKET,
    )
    db._conn.commit()
    create_console(db, name="con", vm_name="vm1", session_specs=["s"])


def test_an_agent_pane_parks_as_the_agent_and_switches_to_it(db: Database, fake_guest: _FakeGuest) -> None:
    """The agent's own connection parks the pane's shell, and the admin's tmux runs the handover behind `sudo -n -u`."""
    _seed_agent_session_console(db)

    fake_guest.responses["has-session -t '=aw-console-con'"] = _FakeResult(returncode=0)
    add_shell(db, _StubConfig(), console_name="con", session_name="s", interaction=TtyInteractionPolicy.REFUSE)

    [park] = fake_guest.parks()
    assert park.identity == BOT
    assert isinstance(park.request, LoginShell)
    assert park.request.cwd == "/home/me/vm1"
    [split] = [c for c in fake_guest.commands if "split-window -t '=aw-console-con':s" in c]
    switched = _pane_command(split)
    assert switched[:5] == ["sudo", "-n", "-u", "bot-user", "--"]
    assert PARK_TOKEN in switched[-1]
    # The split itself runs as the admin, the console's tmux server's owner.
    assert fake_guest.recorder is not None
    splits = [call for call in _runs(fake_guest.recorder.calls) if "split-window" in str(command_line(call.request))]
    assert [call.identity for call in splits] == [Admin()]


def test_an_admin_pane_parks_as_the_admin_with_no_switch(db: Database, fake_guest: _FakeGuest) -> None:
    """Admin shell on an admin-mode session: the admin parks and the pane runs the handover itself."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_guest.responses["has-session -t '=aw-console-con'"] = _FakeResult(returncode=0)
    add_shell(db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE)

    [park] = fake_guest.parks()
    assert park.identity == Admin()
    [split] = [c for c in fake_guest.commands if "split-window -t '=aw-console-con':a" in c]
    assert "sudo" not in _pane_command(split)


def test_an_agent_pane_checks_the_agents_access_before_anything_changes(db: Database, fake_guest: _FakeGuest) -> None:
    """On a running console, an agent that predates the direct-SSH rollout is refused before the shell is recorded."""
    _seed_agent_session_console(db)
    fake_guest.responses["has-session -t '=aw-console-con'"] = _FakeResult(returncode=0)
    assert fake_guest.recorder is not None
    agent = fake_guest.recorder.access(BOT)
    agent.answer = lambda _call: Result(Unknown(UnknownReason.AUTH_FAILED))  # ssh as the agent is refused

    with pytest.raises(StateError) as raised:
        add_shell(db, _StubConfig(), console_name="con", session_name="s", interaction=TtyInteractionPolicy.REFUSE)

    assert (raised.value.entity_kind, raised.value.entity_name) == ("agent", "bot")
    assert raised.value.hint
    member = db.get_console_session("con", "s")
    assert member is not None and member.shells == []
    assert fake_guest.parks() == []
    assert agent.requests() == [Command(("true",), deadline=30)]


def test_a_stopped_console_records_an_agent_pane_without_checking_the_agent(
    db: Database, fake_guest: _FakeGuest
) -> None:
    """With no live tmux there is no pane to park, so the shell is recorded and the agent is not asked."""
    _seed_agent_session_console(db)
    fake_guest.responses["has-session -t '=aw-console-con'"] = _FakeResult(
        returncode=1, stderr="can't find session: aw-console-con"
    )
    assert fake_guest.recorder is not None
    agent = fake_guest.recorder.access(BOT)

    add_shell(db, _StubConfig(), console_name="con", session_name="s", interaction=TtyInteractionPolicy.REFUSE)

    member = db.get_console_session("con", "s")
    assert member is not None and len(member.shells) == 1
    assert agent.requests() == []
    assert fake_guest.parks() == []


def test_a_console_whose_tmux_cannot_be_read_records_the_shell_then_fails(db: Database, fake_guest: _FakeGuest) -> None:
    """A probe that cannot tell checks no agent: the shell is recorded, then the live sync refuses to guess."""
    _seed_agent_session_console(db)
    fake_guest.responses["has-session -t '=aw-console-con'"] = _FakeResult(returncode=1, stderr="lost server")
    assert fake_guest.recorder is not None
    agent = fake_guest.recorder.access(BOT)

    with pytest.raises(StateError) as raised:
        add_shell(db, _StubConfig(), console_name="con", session_name="s", interaction=TtyInteractionPolicy.REFUSE)

    assert (raised.value.entity_kind, raised.value.entity_name) == ("console", "con")
    member = db.get_console_session("con", "s")
    assert member is not None and len(member.shells) == 1
    assert agent.requests() == []
    assert fake_guest.parks() == []


def test_a_pane_whose_directory_is_gone_opens_in_the_home_directory(
    db: Database, fake_guest: _FakeGuest, captured_output: CapturedOutput
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    fake_guest.responses["has-session -t '=aw-console-con'"] = _FakeResult(returncode=0)
    fake_guest.responses["split-window -t '=aw-console-con':a"] = _FakeResult(stdout="%7\n")
    fake_guest.admin().park_outcomes.append(RequestRefused("vm1", "cwd", Result(Unknown(UnknownReason.CONNECTION))))

    add_shell(
        db, _StubConfig(), console_name="con", session_name="a", cwd="gone", interaction=TtyInteractionPolicy.REFUSE
    )

    assert [park.request.cwd for park in fake_guest.parks()] == ["/home/me/vm1/gone", None]
    assert any("/home/me/vm1/gone" in warning for warning in captured_output.warnings)
    assert [c for c in fake_guest.commands if "split-window" in c]
    assert any(f"-t %7 {SHELL_INDEX_OPTION} 0" in c for c in fake_guest.commands)


def test_a_refused_split_lets_the_parked_request_go(
    db: Database, fake_guest: _FakeGuest, captured_output: CapturedOutput
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    fake_guest.responses["has-session -t '=aw-console-con'"] = _FakeResult(returncode=0)
    fake_guest.responses["split-window -t '=aw-console-con':a"] = _FakeResult(returncode=1, stderr="no space")

    add_shell(db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE)

    # The split ran inside the parking block, which left by exception, so the request went at once.
    [park] = fake_guest.parks()
    assert "raised" in park.options
    assert not [c for c in fake_guest.commands if "set-option -p" in c]
    # The one warning carries tmux's own reason.
    [warning] = captured_output.warnings
    assert "no space" in warning


def test_a_pane_that_never_collects_its_request_is_a_failed_pane(
    db: Database, fake_guest: _FakeGuest, captured_output: CapturedOutput
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    fake_guest.responses["has-session -t '=aw-console-con'"] = _FakeResult(returncode=0)
    fake_guest.responses["split-window -t '=aw-console-con':a"] = _FakeResult(stdout="%7\n")
    fake_guest.admin().park_collected = False

    add_shell(db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE)

    # The pane started no shell, so it is killed rather than left untagged, and the failure is warned.
    assert not [c for c in fake_guest.commands if "set-option -p" in c]
    assert [c for c in fake_guest.commands if "kill-pane" in c] == ["tmux kill-pane -t %7"]
    assert len(captured_output.warnings) == 1


def test_a_panes_environment_is_parked_never_on_a_command_line(db: Database, fake_guest: _FakeGuest) -> None:
    """The pane's composed environment, workspace identity only, is in its parked request and nowhere else.

    An add-shell pane is a sidecar shell rooted in the session's workspace,
    not part of the session, so it has no AGENTWORKS_SESSION; the agent's own
    AGENTWORKS_AGENT reaches it from the agent's profile fragment.
    """
    _seed_agent_session_console(db)
    fake_guest.responses["has-session -t '=aw-console-con'"] = _FakeResult(returncode=0)

    add_shell(db, _StubConfig(), console_name="con", session_name="s", interaction=TtyInteractionPolicy.REFUSE)

    [park] = fake_guest.parks()
    env = dict(park.request.env)
    assert env["AGENTWORKS_WORKSPACE"] == "ws-vm1"
    assert "AGENTWORKS_WORKSPACE_DIR" in env
    assert not [key for key in env if key.startswith(("AGENTWORKS_SESSION", "AGENTWORKS_AGENT"))]
    assert fake_guest.recorder is not None
    assert not [call for call in _runs(fake_guest.recorder.calls) if call.request.env]
    assert not [c for c in fake_guest.commands if "ws-vm1" in c and "split-window" in c]


# -- Pane tagging ----------------------------------------------------------


def test_split_shell_pane_tags_new_pane_with_config_index(db: Database, fake_guest: _FakeGuest) -> None:
    """After split-window emits the new pane id, _split_shell_pane sets
    @agentworks-shell-index so restore-session can identify which configured
    shell a given live pane corresponds to."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_guest.commands.clear()
    fake_guest.responses["has-session -t '=aw-console-con'"] = _FakeResult(returncode=0)
    # Simulate tmux split-window -P emitting a pane id.
    fake_guest.responses["split-window -t '=aw-console-con':a"] = _FakeResult(stdout="%7\n")

    add_shell(db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE)

    set_options = [c for c in fake_guest.commands if "set-option -p" in c and SHELL_INDEX_OPTION in c]
    assert len(set_options) == 1
    # The first shell added is config index 0 (cs.shells was empty).
    assert f"-t %7 {SHELL_INDEX_OPTION} 0" in set_options[0]


def test_split_shell_pane_warns_when_split_returns_no_pane_id(
    db: Database, fake_guest: _FakeGuest, captured_output: CapturedOutput
) -> None:
    """If split-window's stdout is empty (older tmux / weird transport), the
    tag step is skipped and the operator gets a warning that the pane is
    untagged. The pane is still live; restore-session just won't be able to
    repair this window without `console restart`."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_guest.commands.clear()
    fake_guest.responses["has-session -t '=aw-console-con'"] = _FakeResult(returncode=0)
    # Default _FakeResult has empty stdout, so no pane_id to tag.

    add_shell(db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE)

    set_options = [c for c in fake_guest.commands if "set-option -p" in c]
    assert set_options == []
    # The recovery hint includes the actual console name so it can be
    # copy/pasted verbatim.
    assert any(
        "couldn't capture its id" in w and "untagged" in w and "restart con" in w for w in captured_output.warnings
    )


# -- Shell-pane ordering (issue #246 part a) -------------------------------


def test_reorder_shell_panes_puts_tagged_shells_into_config_order(
    console_guest_factory: Callable[..., _FakeGuest],
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
    target = console_guest_factory(model)

    _reorder_shell_panes(target.run_command(), CON, "a", 2)

    rows = model.pane_rows(CON, "a")
    assert rows is not None
    assert [(pidx, tag) for _pid, pidx, tag in rows] == [(0, None), (1, 0), (2, 1)]


def test_add_shell_reorders_shell_panes_into_config_order(
    db: Database,
    console_guest_factory: Callable[..., _FakeGuest],
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
    console_guest_factory(model)

    add_shell(db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE)

    rows = model.pane_rows(CON, "a")
    assert rows is not None
    # Session pane lowest and untagged; all three shells in config order.
    assert [(pidx, tag) for _pid, pidx, tag in rows] == [(0, None), (1, 0), (2, 1), (3, 2)]


@pytest.mark.parametrize("failure", ["refused", "incomplete"])
def test_split_shell_pane_warns_when_set_option_fails(
    db: Database,
    fake_guest: _FakeGuest,
    captured_output: CapturedOutput,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """If tmux split-window succeeded and emitted a pane id but the subsequent
    set-option fails (tmux refuses it, or the request does not complete), the
    pane is live but untagged. _split_shell_pane must surface this so the
    operator gets a loud signal instead of restore-session breaking later."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_guest.commands.clear()
    fake_guest.responses["has-session -t '=aw-console-con'"] = _FakeResult(returncode=0)
    fake_guest.responses["split-window -t '=aw-console-con':a"] = _FakeResult(stdout="%7\n")
    if failure == "refused":
        fake_guest.responses["set-option -p"] = _FakeResult(returncode=1, stderr="bad target")
    else:
        answer = fake_guest.answer

        def lost(call: Call) -> Result | None:
            line = command_line(call.request)
            return Result(Unknown(UnknownReason.CONNECTION)) if line and "set-option -p" in line else answer(call)

        monkeypatch.setattr(fake_guest, "answer", lost)

    add_shell(db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE)

    [warning] = captured_output.warnings
    if failure == "refused":
        assert "bad target" in warning  # tmux's own reason reaches the operator
    member = db.get_console_session("con", "a")
    assert member is not None and len(member.shells) == 1
