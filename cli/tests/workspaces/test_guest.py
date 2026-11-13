"""The workspace guest scripts take paths as arguments, run by a real ``/bin/sh`` with a recording ``chmod``."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from agentworks.workspaces.guest import open_parent_traversal, path_occupied

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.execution.requests import Script

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="runs the scripts under /bin/sh")


def run(request: Script, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run ``request``'s source as the guest would, with ``chmod`` recording its arguments instead of acting."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    chmod = bin_dir / "chmod"
    chmod.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {tmp_path / "chmod.log"}\n')
    chmod.chmod(0o755)
    environment = {"PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin"}
    return subprocess.run(
        ["/bin/sh", "-c", request.source],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_parent_traversal_walks_only_the_ancestors_of_a_path_with_spaces_and_metacharacters(tmp_path: Path) -> None:
    path = "/srv/my ws;touch injected/$(touch substituted)"

    completed = run(open_parent_traversal(path), tmp_path)

    assert completed.returncode == 0
    assert (tmp_path / "chmod.log").read_text().splitlines() == [
        "-c a+x /srv/my ws;touch injected",
        "-c a+x /srv",
    ]
    assert not (tmp_path / "injected").exists() and not (tmp_path / "substituted").exists()


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
