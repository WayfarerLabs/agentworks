"""The guest lookup walk names what the kernel consults to reach each stored workspace path."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.errors import StateError
from agentworks.execution.requests import PythonProgram
from agentworks.execution.results import Completed, Result
from agentworks.native_locks.path_walk import LINK_LIMIT, PathWalk, walk_paths
from tests.vm_access_support import Admin, Call, RecordingVMAccess

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the walk runs on a Linux guest")


def run_locally(call: Call) -> Result:
    """Run the walk's program on this machine, as the test's own user (elevation is not applied)."""
    request = call.request
    assert isinstance(request, PythonProgram) and request.elevated
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", request.source, *request.args], capture_output=True, timeout=60, check=False
    )
    return Result(Completed(completed.returncode), completed.stdout, completed.stderr)


class Guest:
    """A directory tree standing in for a VM's root: test paths are spelled from ``base`` as if it were ``/``."""

    def __init__(self, base: Path):
        self.base = str(base.resolve())
        self.access = RecordingVMAccess([], Admin())
        self.access.answer = run_locally

    def path(self, name: str) -> Path:
        return Path(self.base + name)

    def mkdir(self, *names: str) -> None:
        for name in names:
            self.path(name).mkdir(parents=True, exist_ok=True)

    def link(self, name: str, target: str) -> None:
        """``name`` as a link to ``target``, an absolute target spelled from ``base``."""
        self.path(name).parent.mkdir(parents=True, exist_ok=True)
        self.path(name).symlink_to(self.base + target if target.startswith("/") else target)

    def walk(self, *names: str, unlink_only: bool = False) -> list[PathWalk]:
        """The walks of ``names``, every name spelled from ``base``, leaving out ``base``'s own ancestors."""
        walks = walk_paths(self.access, [self.base + name for name in names], unlink_only=unlink_only)
        return [
            PathWalk(
                walk.path.removeprefix(self.base),
                tuple(self._local(item) for item in walk.directories if item.startswith(self.base + "/")),
                tuple(self._local(item) for item in walk.links),
                self._local(walk.final),
            )
            for walk in walks
        ]

    def _local(self, name: str) -> str:
        assert name.startswith(self.base + "/"), name
        return name.removeprefix(self.base)


@pytest.fixture
def guest(tmp_path: Path) -> Guest:
    return Guest(tmp_path)


def test_links_in_the_middle_and_after_a_link_record_every_directory_and_followed_link(guest):
    guest.mkdir("/physical/a", "/f", "/p/q", "/home/admin", "/home/srv/ws1")
    guest.link("/front/link", "/middle/bridge")
    guest.link("/middle/bridge", "/physical")
    guest.link("/c/l1", "/e")
    guest.link("/e/d/l2", "/f")
    guest.link("/data/x", "/p/q")
    assert guest.walk("/front/link/a", "/c/l1/d/l2/a", "/data/x/../y", "/home/admin/../srv/ws1") == [
        PathWalk("/front/link/a", ("/front", "/middle", "/physical"), ("/front/link", "/middle/bridge"), "/physical/a"),
        PathWalk("/c/l1/d/l2/a", ("/c", "/e", "/e/d", "/f"), ("/c/l1", "/e/d/l2"), "/f/a"),
        PathWalk("/data/x/../y", ("/data", "/p", "/p/q"), ("/data/x",), "/p/y"),
        PathWalk("/home/admin/../srv/ws1", ("/home", "/home/admin", "/home/srv"), (), "/home/srv/ws1"),
    ]


def test_a_relative_link_resolves_from_its_own_directory(guest):
    guest.mkdir("/data/real")
    guest.link("/data/alias", "real")
    assert guest.walk("/data/alias/ws") == [
        PathWalk("/data/alias/ws", ("/data", "/data/real"), ("/data/alias",), "/data/real/ws")
    ]


def test_a_missing_name_ends_the_physical_part_and_joins_the_rest_lexically(guest):
    guest.mkdir("/m", "/n")
    guest.link("/m/link", "/n")
    assert guest.walk("/m/new/deeper", "/m/link/new/x") == [
        PathWalk("/m/new/deeper", ("/m",), (), "/m/new/deeper"),
        PathWalk("/m/link/new/x", ("/m", "/n"), ("/m/link",), "/n/new/x"),
    ]


def test_link_expansions_past_the_kernel_limit_refuse(guest):
    guest.mkdir("/target")
    # l0 -> l1 -> ... -> l40 -> /target: from l1 the walk expands exactly the limit, from l0 one more.
    for index in range(LINK_LIMIT):
        guest.link(f"/chain/l{index}", f"/chain/l{index + 1}")
    guest.link(f"/chain/l{LINK_LIMIT}", "/target")
    (within,) = guest.walk("/chain/l1/ws")
    assert len(within.links) == LINK_LIMIT and within.final == "/target/ws"
    with pytest.raises(StateError, match="/chain/l0/ws"):
        guest.walk("/chain/l1/ws", "/chain/l0/ws")


def test_unlink_only_names_a_dangling_or_cyclic_final_link_without_reading_it(guest):
    guest.link("/w/gone", "/nowhere")
    guest.link("/w/loop", "/w/loop")
    assert guest.walk("/w/gone", "/w/loop", unlink_only=True) == [
        PathWalk("/w/gone", ("/w",), (), "/w/gone"),
        PathWalk("/w/loop", ("/w",), (), "/w/loop"),
    ]
    with pytest.raises(StateError, match="/w/loop"):
        guest.walk("/w/loop")


def test_unlink_only_follows_links_before_the_last_name_and_walks_a_final_dotdot_in_full(guest):
    guest.mkdir("/physical/child", "/p/q")
    guest.link("/front/link", "/physical")
    guest.link("/data/x", "/p/q")
    (removed,) = guest.walk("/front/link", unlink_only=True)
    (followed,) = guest.walk("/front/link/child")
    assert removed == PathWalk("/front/link", ("/front",), (), "/front/link")
    assert removed.final in followed.links
    assert guest.walk("/data/x/..", "/data/x/inner", unlink_only=True) == [
        PathWalk("/data/x/..", ("/data", "/p", "/p/q"), ("/data/x",), "/p"),
        PathWalk("/data/x/inner", ("/data", "/p", "/p/q"), ("/data/x",), "/p/q/inner"),
    ]


@pytest.mark.parametrize("name", ["", "relative", "/a//b", "/a/./b", "/a/../b", "/a/"])
@pytest.mark.parametrize("kind", ["D", "L", "F"])
def test_a_walk_reporting_a_name_not_in_normal_form_refuses_as_malformed(kind: str, name: str) -> None:
    access = RecordingVMAccess([], Admin())
    final = "" if kind == "F" else "F/a/b\0"
    access.answer = lambda call: Result(Completed(0), f"{kind}{name}\0{final}".encode())
    with pytest.raises(StateError) as refused:
        walk_paths(access, ["/a/b"])
    assert type(refused.value) is StateError
