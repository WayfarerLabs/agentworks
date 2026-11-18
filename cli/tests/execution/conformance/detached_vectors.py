"""The vectors for detached work over the real wire, DT1 and DT2.

They join :mod:`vectors`' list, so the lanes they name run them: the real
``Detached`` client sends the real guest program through the real ``VMAccess``
and agw-launch, elevated, so a launch outlives the request that started it.
A launch outlives its connection and is observed, completed and downloaded by
later ones (DT1, S and V), and the VM's base is never writable by anyone but
root (DT2, V). Launch identity, interrupt and the launch directory's own modes
are the guest program's unit tests (``test_detached_program.py``), which need
no sudo.

On S the program's base is under the worker's temporary directory
(``conftest.py``'s ``private_detached_base``), so root writes nothing of the
runner's own ``/var/lib``; V uses the VM's real base and discards every
launch it makes.
"""

from __future__ import annotations

import contextlib
import stat
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks.execution import detached_program
from agentworks.execution.detached import State
from agentworks.execution.requests import Command, ToFile
from agentworks.execution.results import Completed
from tests.execution.conformance.lanes import needs_sudo

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agentworks.execution.detached import Detached, Launch
    from tests.execution.conformance.lanes import Lane

REQUEST_DEADLINE = 60
SETTLE_SECONDS = 20
"""How long a vector waits for a payload that is ending."""


def payload(source: str, **env: str) -> Command:
    return Command(("sh", "-c", source), env=env, elevated=True)


def as_root(lane: Lane, *argv: str) -> tuple[int, str]:
    result = lane.run(Command(argv, elevated=True, deadline=REQUEST_DEADLINE))
    assert isinstance(result.completion, Completed), result.completion
    return result.completion.status, result.stdout.decode()


@contextlib.contextmanager
def one_launch(lane: Lane) -> Iterator[tuple[Detached, Launch]]:
    """The lane's ``Detached`` and a fresh launch, interrupted and then discarded on the way out, whatever happened."""
    needs_sudo(lane)
    detached = lane.access.detached()
    launch = detached.new_launch()
    try:
        yield detached, launch
    finally:
        try:
            detached.interrupt(launch)
        finally:
            detached.discard(launch)


# -- DT1: a launch outlives its connection -----------------------------------------------------


def a_launch_outlives_its_connection_and_later_ones_observe_and_download_it(lane: Lane) -> None:
    """``launch`` returns while the payload waits; later requests, each its own connection, see it through.

    The payload waits for a file only a later request creates, so it is
    certainly still running once the connection that launched it has closed.
    Its environment reaches it over the wire: the content it writes is in it.
    """
    content = bytes(range(256)) + b"\r\n\x00end"
    with one_launch(lane) as (detached, launch), tempfile.TemporaryDirectory() as local:
        detached.launch(
            launch,
            payload(
                'while [ ! -e go ]; do sleep 0.1; done; printf "$CONTENT" > result; echo finished',
                CONTENT="".join(f"\\{byte:03o}" for byte in content),
            ),
        )

        assert detached.observe(launch).state is State.RUNNING
        assert as_root(lane, "touch", "--", f"{detached_program.BASE}/{launch.id}/work/go")[0] == 0
        deadline = time.monotonic() + SETTLE_SECONDS
        while (observed := detached.observe(launch)).state is State.RUNNING:
            assert time.monotonic() < deadline, f"launch {launch.id} did not end"
            time.sleep(0.1)
        download = Path(local) / "result"
        detached.download(launch, "result", ToFile(download))

        assert (observed.state, observed.status, observed.output_tail) == (State.EXITED, 0, b"finished\n")
        assert observed.work == {"go": 0, "result": len(content)}
        assert download.read_bytes() == content


# -- DT2: the VM's detached base is never writable by anyone but root ---------------------------


def the_detached_base_is_writable_by_root_alone(lane: Lane) -> None:
    """Root owns the base and its parent, and neither is group- or world-writable.

    The parent comes from the VM's bootstrap rather than from the program, so
    this asserts the invariant, not the exact modes; the launch directory's
    own modes are a unit test.
    """
    with one_launch(lane) as (detached, launch):
        detached.launch(launch, payload("true"))
        base = detached_program.BASE

        status, listing = as_root(lane, "stat", "-c", "%u %a", "--", str(Path(base).parent), base)

        assert status == 0
        assert len(listing.splitlines()) == 2
        for line in listing.splitlines():
            owner, mode = line.split()
            assert owner == "0"
            assert int(mode, 8) & (stat.S_IWGRP | stat.S_IWOTH) == 0, line


# (name, lanes, check), which ``vectors.build`` turns into its ``Vector`` entries.
ENTRIES: list[tuple[str, str, Callable[..., None]]] = [
    (
        "DT1-launch-outlives-its-connection",
        "SV",
        a_launch_outlives_its_connection_and_later_ones_observe_and_download_it,
    ),
    ("DT2-detached-base-writable-by-root-alone", "V", the_detached_base_is_writable_by_root_alone),
]
