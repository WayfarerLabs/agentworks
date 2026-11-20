"""The workspace guest scripts take paths as arguments, run by a real shell with a recording ``chmod``."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from agentworks.workspaces.guest import open_parent_traversal, path_occupied

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.execution.requests import Script

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="runs the scripts under /bin/sh")

SHELLS = [
    pytest.param(name, marks=pytest.mark.skipif(shutil.which(name) is None, reason=f"{name} is unavailable"))
    for name in ("dash", "bash", "zsh")
]


def run(request: Script, tmp_path: Path, shell: str = "/bin/sh") -> subprocess.CompletedProcess[str]:
    """Run ``request``'s source in ``shell``, with ``chmod`` recording its arguments instead of acting."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    chmod = bin_dir / "chmod"
    chmod.write_text(f"#!/bin/sh\nprintf '%s\\0' \"$@\" >> {tmp_path / 'chmod.log'}\n")
    chmod.chmod(0o755)
    environment = {"PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin"}
    return subprocess.run(
        [shutil.which(shell) or shell, "-c", request.source],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def chmodded(tmp_path: Path) -> list[str]:
    """The directory each recorded ``chmod -c a+x --`` named, exactly as it named it."""
    arguments = (tmp_path / "chmod.log").read_bytes().decode().split("\0")[:-1]
    assert len(arguments) % 4 == 0
    calls = [arguments[index : index + 4] for index in range(0, len(arguments), 4)]
    assert all(call[:3] == ["-c", "a+x", "--"] for call in calls)
    return [call[3] for call in calls]


def test_parent_traversal_walks_only_the_ancestors_of_a_path_with_spaces_and_metacharacters(tmp_path: Path) -> None:
    path = "/srv/my ws;touch injected/$(touch substituted)"

    completed = run(open_parent_traversal(path), tmp_path)

    assert completed.returncode == 0
    assert chmodded(tmp_path) == ["/srv/my ws;touch injected", "/srv"]
    assert not (tmp_path / "injected").exists() and not (tmp_path / "substituted").exists()


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("newlines", ["\n", "\n\n"], ids=("one-lf", "two-lfs"))
def test_parent_traversal_names_an_ancestor_ending_in_newlines_by_its_exact_bytes(
    tmp_path: Path, shell: str, newlines: str
) -> None:
    """A name captured from a command's output would lose its trailing newlines and name another directory."""
    path = f"/srv/ancestor{newlines}/ws"

    completed = run(open_parent_traversal(path), tmp_path, shell)

    assert completed.returncode == 0, completed.stderr
    assert chmodded(tmp_path) == [f"/srv/ancestor{newlines}", "/srv"]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    ("path", "ancestors"),
    [
        ("/ws", []),
        ("//ws", []),
        ("///ws", []),
        ("//srv/ws", ["/srv"]),
        ("/srv/ws/", ["/srv"]),
        ("/srv//ws", ["/srv/", "/srv"]),
    ],
    ids=(
        "under-root",
        "two-leading-slashes",
        "three-leading-slashes",
        "leading-slashes",
        "trailing-slash",
        "doubled-slash",
    ),
)
def test_parent_traversal_stops_below_the_root(tmp_path: Path, shell: str, path: str, ancestors: list[str]) -> None:
    completed = run(open_parent_traversal(path), tmp_path, shell)

    assert completed.returncode == 0, completed.stderr
    assert (chmodded(tmp_path) if (tmp_path / "chmod.log").exists() else []) == ancestors


@pytest.mark.parametrize("path", [".", "relative/ws", "ws"])
def test_parent_traversal_of_a_relative_path_ends_without_changing_anything(tmp_path: Path, path: str) -> None:
    completed = run(open_parent_traversal(path), tmp_path)

    assert completed.returncode == 0
    assert not (tmp_path / "chmod.log").exists()


@pytest.mark.parametrize("kind", ["absent", "directory", "file", "dangling-link"])
def test_path_occupied_sees_anything_at_a_path_with_spaces(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "my ws;x"
    if kind == "directory":
        path.mkdir()
    elif kind == "file":
        path.write_text("")
    elif kind == "dangling-link":
        path.symlink_to(tmp_path / "missing")

    completed = run(path_occupied(str(path)), tmp_path)

    assert completed.returncode == (1 if kind == "absent" else 0)


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("path", ["/a/../b", "/srv/ws/..", "/../ws"])
def test_parent_traversal_of_a_path_with_a_dotdot_component_refuses_before_any_change(
    tmp_path: Path, shell: str, path: str
) -> None:
    completed = run(open_parent_traversal(path), tmp_path, shell)

    assert completed.returncode == 2
    assert not (tmp_path / "chmod.log").exists()
