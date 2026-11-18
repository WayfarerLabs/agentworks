"""Named-console attach behavior after explicit lifecycle cutover."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database
from agentworks.execution.access import Admin
from agentworks.execution.requests import TerminalCommand
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions.manager import SessionTmuxServer
from agentworks.sessions.multi_console import _attach_loop_wrapper, attach_console
from agentworks.sessions.tmux import SOCKET_GUARD_REFUSED
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests._tmux_model import TmuxModel
from tests.console_helpers import create_console_record

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.conftest import _FakeTarget


def test_attach_loop_uses_exact_session_targets() -> None:
    wrapper = _attach_loop_wrapper("backend", SessionTmuxServer("/tmp/backend.sock", None))
    assert "tmux -S /tmp/backend.sock has-session -t '=backend'" in wrapper
    assert "tmux -S /tmp/backend.sock attach -t '=backend'" in wrapper


_STUB_SUDO = """#!/bin/sh
case "$*" in
*has-session*)
    polls=$(cat "$STUB_DIR/polls" 2>/dev/null || echo 0)
    polls=$((polls + 1))
    echo "$polls" > "$STUB_DIR/polls"
    if [ "$polls" -le "$STUB_DOWN_POLLS" ]; then exit "$STUB_DOWN_STATUS"; fi
    exit 0 ;;
*attach*)
    kill -TERM "$PPID"
    exit 0 ;;
esac
exit 99
"""


def _console_window_output(tmp_path: Path, *, down_status: int) -> list[str]:
    """Run an agent session's window loop under a real ``/bin/sh`` until its first attach.

    A ``sudo`` stand-in answers the first four ``has-session`` polls with
    ``down_status``, then reports the session up; its attach ends the loop.
    """
    stubs = tmp_path / f"stubs-{down_status}"
    stubs.mkdir()
    for name, body in (("sudo", _STUB_SUDO), ("sleep", "#!/bin/sh\n"), ("clear", "#!/bin/sh\n")):
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    wrapper = _attach_loop_wrapper(
        "s", SessionTmuxServer("/run/agentworks/agent-tmux-sockets/bot-user/s.sock", "bot-user")
    )
    environment = {
        "PATH": f"{stubs}:{os.defpath}",
        "STUB_DIR": str(stubs),
        "STUB_DOWN_POLLS": "4",
        "STUB_DOWN_STATUS": str(down_status),
    }
    completed = subprocess.run(
        ["/bin/sh", "-c", wrapper], env=environment, capture_output=True, text=True, timeout=30, check=False
    )
    assert (stubs / "polls").read_text().strip() == "5"
    return completed.stdout.splitlines()


@pytest.mark.skipif(sys.platform != "linux", reason="runs the window loop under a Linux /bin/sh")
def test_a_console_window_says_once_why_the_guard_refuses_its_session(tmp_path: Path) -> None:
    """Four refused polls add one line to what four polls of an absent server print, then the window attaches."""
    absent = _console_window_output(tmp_path, down_status=1)
    refused = _console_window_output(tmp_path, down_status=SOCKET_GUARD_REFUSED)
    assert len(refused) == len(absent) + 1
    assert [line for line in refused if line not in absent] == [refused[0]]


def test_attach_joins_existing_canonical_runtime_without_building(
    db: Database,
    console_target_factory: Callable[[TmuxModel], _FakeTarget],
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["alpha"])
    model = TmuxModel()
    model.seed_session("aw-console-con", "alpha")
    target = console_target_factory(model)

    assert (
        attach_console(
            db,
            _StubConfig(),
            name="con",
            allow_nesting=True,
            interaction=TtyInteractionPolicy.REFUSE,
        )
        == 0
    )
    # The admin's terminal runs tmux's own argv, with no shell in between.
    assert [(call.identity, call.request) for call in target.attaches()] == [
        (Admin(), TerminalCommand(("tmux", "attach", "-t", "=aw-console-con")))
    ]
    assert not any("new-session" in command for command in target.commands)
