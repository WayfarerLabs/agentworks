"""Tests for tmuxinator config generation and the agent socket path."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import yaml

from agentworks.db import SessionRow
from agentworks.sessions.tmux import (
    AGENT_SOCKET_ROOT,
    agent_socket_path,
    as_socket_owner_line,
    exact_tmux_target,
    tmux_cmd,
)
from agentworks.workspaces.tmuxinator import GENERATED_HEADER, console_session_name, generate_config
from tests.vm_access_support import Admin, RecordingVMAccess


def _pane(config: str, window: str) -> str:
    """The one pane command of ``window`` in a generated config, as tmuxinator reads it."""
    windows = yaml.safe_load(config)["windows"]
    [pane] = next(entry[window] for entry in windows if window in entry)["panes"]
    assert isinstance(pane, str)
    return pane


def test_generate_config_no_sessions() -> None:
    config = generate_config("myproject", "/home/agentworks/workspaces/myproject")
    assert config.startswith(GENERATED_HEADER)
    assert f"name: {console_session_name('myproject')}" in config
    assert "root: /home/agentworks/workspaces/myproject" in config
    assert '  - "--admin--":' in config
    # no session windows
    assert "while tmux has-session" not in config


def test_generate_config_with_sessions() -> None:
    sessions = [
        SessionRow(
            name="ws-1-build",
            workspace_name="ws-1",
            template="default",
            mode="admin",
            created_at="",
            updated_at="",
        ),
        SessionRow(
            name="ws-1-test",
            workspace_name="ws-1",
            template="default",
            mode="admin",
            created_at="",
            updated_at="",
        ),
    ]
    config = generate_config("ws-1", "/home/agentworks/workspaces/ws-1", sessions=sessions)

    assert '  - "--admin--":' in config
    assert '  - "ws-1-build":' in config
    assert '  - "ws-1-test":' in config


@pytest.mark.skipif(
    shutil.which("tmux") is None or shutil.which("zsh") is None,
    reason="tmux and zsh are required",
)
def test_generated_wrapper_does_not_match_a_session_name_prefix() -> None:
    # Keep the owned socket below the native Unix path-length limit.
    with TemporaryDirectory(prefix="agw-r1-tmux-prefix-", dir="/tmp") as directory:
        root = Path(directory)
        socket_path = str(root / "tmux.sock")
        subprocess.run(
            ["tmux", "-S", socket_path, "new-session", "-d", "-s", "foobar", "sleep 30"],
            check=True,
        )
        try:
            session = SessionRow(
                name="foo",
                workspace_name="ws",
                template="default",
                mode="admin",
                created_at="",
                updated_at="",
                socket_path=socket_path,
            )
            wrapper = _pane(generate_config("ws", "/tmp/ws", sessions=[session]), "foo")
            probe = wrapper.removeprefix("unset TMUX; while ").split(" 2>/dev/null;", maxsplit=1)[0]
            result = subprocess.run(["zsh", "-f", "-c", probe], check=False)
            assert result.returncode == 1
        finally:
            subprocess.run(["tmux", "-S", socket_path, "kill-server"], check=False)
    assert not root.exists()


def test_generate_config_admin_window_first() -> None:
    """Admin shell window should always be first."""
    sessions = [
        SessionRow(
            name="ws-alpha",
            workspace_name="ws",
            template="default",
            mode="admin",
            created_at="",
            updated_at="",
        ),
    ]
    config = generate_config("ws", "/tmp/ws", sessions=sessions)
    lines = config.splitlines()

    admin_idx = next(i for i, line in enumerate(lines) if '- "--admin--":' in line)
    session_idx = next(i for i, line in enumerate(lines) if '"ws-alpha":' in line)
    assert admin_idx < session_idx


def test_agent_socket_path() -> None:
    path = agent_socket_path("agt--alice", "myws-dev")
    assert path == f"{AGENT_SOCKET_ROOT}/agt--alice/myws-dev.sock"


def test_an_agent_sessions_window_reaches_its_server_only_as_the_agent() -> None:
    """The admin runs the config, so an agent's window elevates only to switch to the agent behind the guard."""
    sock = "/run/agentworks/agent-tmux-sockets/agt--alice/ws-dev.sock"
    sessions = [
        SessionRow(
            name="ws-dev",
            workspace_name="ws",
            template="default",
            mode="agent",
            created_at="",
            updated_at="",
            agent_name="alice",
            socket_path=sock,
        ),
    ]
    pane = _pane(generate_config("ws", "/tmp/ws", sessions=sessions, agent_owners={"ws-dev": "agt--alice"}), "ws-dev")

    switched = [
        "sudo -n /bin/sh -c "
        + shlex.quote(
            as_socket_owner_line(
                tmux_cmd(f"{verb} -t {exact_tmux_target('ws-dev')}", sock),
                socket_path=sock,
                owner="agt--alice",
                switch_user=True,
                terminal=terminal,
            )
        )
        # The attach is an interactive client, so it runs on a relay terminal of its own.
        for verb, terminal in (("has-session", False), ("attach", True))
    ]
    assert all(command in pane for command in switched)
    remainder = pane
    for command in switched:
        remainder = remainder.replace(command, "")
    assert "tmux" not in remainder


def test_generate_config_admin_session_no_socket() -> None:
    """Admin-mode sessions should not have -S in wrapper commands."""
    sessions = [
        SessionRow(
            name="ws-build",
            workspace_name="ws",
            template="default",
            mode="admin",
            created_at="",
            updated_at="",
        ),
    ]
    config = generate_config("ws", "/tmp/ws", sessions=sessions)
    assert "-S " not in config


def test_the_generated_config_is_written_private_to_its_admin() -> None:
    import json

    from agentworks.execution.requests import PythonProgram
    from agentworks.workspaces.tmuxinator import write_config

    access = RecordingVMAccess([], Admin())

    write_config(access, "/srv/ws", "config\n")

    [request] = access.requests()
    assert isinstance(request, PythonProgram) and request.args[0] == "write"
    params = json.loads(request.args[1])
    assert (params["path"], params["mode"]) == ("/srv/ws/.tmuxinator.yml", 0o600)


@pytest.mark.parametrize("stored", ["/data/ws", "/data/ws/", "//data/ws", "/data//./ws/"])
def test_a_stored_workspace_path_names_its_config_in_one_spelling(stored: str) -> None:
    from agentworks.workspaces.tmuxinator import config_path

    assert config_path(stored) == "/data/ws/.tmuxinator.yml"


@pytest.mark.parametrize("stored", ["~/ws", "/data/link/../ws/", "/data/w\ts"])
def test_a_stored_path_the_file_operations_cannot_address_names_no_config(stored: str) -> None:
    """A relative path names no one directory, a ``..`` one names its directory only once the VM resolves it,
    and the file operations refuse a character that is not printable."""
    from agentworks.errors import ValidationError
    from agentworks.workspaces.tmuxinator import config_path

    with pytest.raises(ValidationError):
        config_path(stored)


@pytest.mark.parametrize(
    ("stored", "target", "removed"),
    [
        ("{root}/ws", "{root}/ws/.tmuxinator.yml", True),
        ("{root}/ws/", "{root}/ws/.tmuxinator.yml", True),
        ("{root}/home/../srv/ws", "{root}/home/../srv/ws/.tmuxinator.yml", True),
        ("{root}/home/../srv/ws", "{root}/srv/ws/.tmuxinator.yml", True),
        ("{root}/ws", "{root}/other/.tmuxinator.yml", False),
        ("{root}/ws", "ws/.tmuxinator.yml", False),
    ],
    ids=["same-spelling", "trailing-slash", "dotdot-as-written", "dotdot-collapsed", "another-workspace", "relative"],
)
def test_delete_removes_the_console_link_only_while_it_points_at_this_workspace(
    tmp_path: Path, stored: str, target: str, removed: bool
) -> None:
    """The guest script itself, run by this machine's ``/bin/sh`` with GNU ``realpath``."""
    from agentworks.workspaces.tmuxinator import config_link, remove_link_to

    home = tmp_path / "home" / "admin"
    link = Path(config_link(str(home), "w"))
    link.parent.mkdir(parents=True)
    link.symlink_to(target.format(root=tmp_path))

    request = remove_link_to(str(home), "w", stored.format(root=tmp_path))
    subprocess.run(["/bin/sh", "-c", request.source], check=True)

    assert link.is_symlink() != removed


@pytest.mark.parametrize("fake", ["exit 1", "exit 0"], ids=["fails", "prints-nothing"])
def test_delete_keeps_the_console_link_when_realpath_gives_no_path(tmp_path: Path, fake: str) -> None:
    """The guest script itself, with ``realpath`` shadowed on ``PATH``: two empty answers are no match."""
    from agentworks.workspaces.tmuxinator import config_link, remove_link_to

    home = tmp_path / "home" / "admin"
    link = Path(config_link(str(home), "w"))
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path / "other" / ".tmuxinator.yml")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "realpath").write_text(f"#!/bin/sh\n{fake}\n")
    (bin_dir / "realpath").chmod(0o755)

    request = remove_link_to(str(home), "w", str(tmp_path / "ws"))
    environment = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    subprocess.run(["/bin/sh", "-c", request.source], check=True, env=environment)

    assert link.is_symlink()


@pytest.mark.parametrize("entry", ["link", "dangling-link", "file"])
def test_delete_of_a_path_unknown_row_removes_its_console_link_and_nothing_else(tmp_path: Path, entry: str) -> None:
    """The guest script itself: the link goes whatever it points at, its target stays, and a file is not a link."""
    from agentworks.workspaces.tmuxinator import config_link, remove_link

    home = tmp_path / "home" / "admin"
    link = Path(config_link(str(home), "w"))
    link.parent.mkdir(parents=True)
    pointed = tmp_path / "ws" / ".tmuxinator.yml"
    if entry == "file":
        link.write_text("operator's own\n")
    else:
        if entry == "link":
            pointed.parent.mkdir()
            pointed.write_text("config\n")
        link.symlink_to(pointed)

    subprocess.run(["/bin/sh", "-c", remove_link(str(home), "w").source], check=True)

    assert (link.exists() or link.is_symlink()) == (entry == "file")
    assert pointed.exists() == (entry == "link")
