"""The vectors for detached work, DT1 to DT4: the four behaviors the sessions LLD's Tests section names.

They join :mod:`vectors`' list, so the same lanes run them: the real
``Detached`` client sends the real guest program through the real ``VMAccess``
and agw-launch, elevated, so each launch outlives the request that started it.
A fresh launch never matches an earlier one (DT1), a launch outlives its
connection and is observed and downloaded from later ones (DT2), interrupt
stops the payload's whole process group (DT3), and the base is never
world-writable (DT4). Each asserts observable behavior only.

On L and S the program's base is under the worker's temporary directory
(``conftest.py``), so root writes nothing of the runner's own
``/var/lib``; V uses the VM's real base and discards every launch it makes.
"""

from __future__ import annotations

import contextlib
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.execution import detached_program
from agentworks.execution.detached import State
from agentworks.execution.errors import DetachedRefused
from agentworks.execution.requests import Command, ToFile
from agentworks.execution.results import Completed
from tests.execution.conformance.lanes import needs_sudo

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agentworks.execution.detached import Detached, Launch, Observation
    from tests.execution.conformance.lanes import Lane

REQUEST_DEADLINE = 60
SETTLE_SECONDS = 20
"""How long a vector waits for a payload that is ending, or for its processes to start."""


def payload(source: str, **env: str) -> Command:
    return Command(("sh", "-c", source), env=env, elevated=True)


def as_root(lane: Lane, *argv: str) -> tuple[int, str]:
    result = lane.run(Command(argv, elevated=True, deadline=REQUEST_DEADLINE))
    assert isinstance(result.completion, Completed), result.completion
    return result.completion.status, result.stdout.decode()


def work_path(launch: Launch, name: str) -> str:
    return f"{detached_program.BASE}/{launch.id}/work/{name}"


@contextlib.contextmanager
def launches(lane: Lane) -> Iterator[tuple[Detached, list[Launch]]]:
    """The lane's ``Detached`` and a list of launches, each interrupted and discarded on the way out."""
    needs_sudo(lane)
    detached = lane.access.detached()
    made: list[Launch] = []
    try:
        yield detached, made
    finally:
        for launch in made:
            detached.interrupt(launch)
            detached.discard(launch)


def wait_until(detached: Detached, launch: Launch, done: Callable[[Observation], bool]) -> Observation:
    deadline = time.monotonic() + SETTLE_SECONDS
    while not done(observed := detached.observe(launch)):
        assert time.monotonic() < deadline, f"launch {launch.id} stayed {observed.state}"
        time.sleep(0.1)
    return observed


def ended(observed: Observation) -> bool:
    return observed.state is not State.RUNNING


# -- DT1: a fresh launch never matches an earlier one ------------------------------------------


def a_fresh_launch_never_matches_an_earlier_one(lane: Lane) -> None:
    command = 'printf "%s\\n" "$MARK"; exit "$STATUS"'
    with launches(lane) as (detached, made):
        first, second = detached.new_launch(), detached.new_launch()
        made += [first, second]
        detached.launch(first, payload(command, MARK="first", STATUS="3"))
        detached.launch(second, payload(command, MARK="second", STATUS="4"))

        observed = [wait_until(detached, launch, ended) for launch in (first, second)]

        assert first.id != second.id
        assert [(o.state, o.status, o.output_tail) for o in observed] == [
            (State.EXITED, 3, b"first\n"),
            (State.EXITED, 4, b"second\n"),
        ]
        with pytest.raises(DetachedRefused):
            detached.launch(first, payload(command, MARK="third", STATUS="5"))
        again = detached.observe(first)
        assert (again.state, again.status, again.output_tail) == (State.EXITED, 3, b"first\n")


# -- DT2: a launch outlives its connection -----------------------------------------------------


def a_launch_outlives_its_connection_and_later_ones_observe_and_download_it(lane: Lane) -> None:
    """``launch`` returns while the payload waits; later requests, each its own connection, see it through.

    The payload waits for a file only a later request creates, so it is
    certainly still running once the connection that launched it has closed.
    """
    content = bytes(range(256)) + b"\r\n\x00end"
    with launches(lane) as (detached, made), tempfile.TemporaryDirectory() as local:
        launch = detached.new_launch()
        made.append(launch)
        detached.launch(
            launch,
            payload(
                'while [ ! -e go ]; do sleep 0.1; done; printf "$CONTENT" > result; echo finished',
                CONTENT="".join(f"\\{byte:03o}" for byte in content),
            ),
        )

        assert detached.observe(launch).state is State.RUNNING
        assert as_root(lane, "touch", "--", work_path(launch, "go"))[0] == 0
        observed = wait_until(detached, launch, ended)
        download = Path(local) / "result"
        detached.download(launch, "result", ToFile(download))

        assert (observed.state, observed.status, observed.output_tail) == (State.EXITED, 0, b"finished\n")
        assert observed.work == {"go": 0, "result": len(content)}
        assert download.read_bytes() == content


# -- DT3: interrupt stops the work -------------------------------------------------------------


def interrupt_stops_the_payloads_whole_process_group(lane: Lane) -> None:
    with launches(lane) as (detached, made):
        launch = detached.new_launch()
        made.append(launch)
        detached.launch(launch, payload("sleep 600 & sleep 600"))
        deadline = time.monotonic() + SETTLE_SECONDS
        while True:  # the payload's pid, once its supervisor has recorded it, and the shell's two sleeps
            status, pid = as_root(lane, "cat", "--", f"{detached_program.BASE}/{launch.id}/process")
            if status == 0 and pid.strip() and len(as_root(lane, "pgrep", "-g", pid.strip())[1].split()) == 3:
                break
            assert time.monotonic() < deadline, "the payload's processes did not start"
            time.sleep(0.1)

        observed = detached.interrupt(launch)

        assert (observed.state, observed.status) == (State.EXITED, 128 + 15)
        assert as_root(lane, "pgrep", "-g", pid.strip()) == (1, "")


# -- DT4: the detached base is never world-writable --------------------------------------------


def the_detached_base_is_never_world_writable(lane: Lane) -> None:
    """Root owns the base, its parent, a launch's directory and its ``work/``, and none is writable by others."""
    with launches(lane) as (detached, made):
        launch = detached.new_launch()
        made.append(launch)
        detached.launch(launch, payload("true"))
        directory = f"{detached_program.BASE}/{launch.id}"
        root = as_root(lane, "id", "-u")[1].strip()

        paths = (str(Path(detached_program.BASE).parent), detached_program.BASE, directory, f"{directory}/work")
        status, listing = as_root(lane, "stat", "-c", "%u %a", "--", *paths)

        assert status == 0
        assert listing.splitlines() == [f"{root} 755", f"{root} 700", f"{root} 700", f"{root} 700"]


# (name, lanes, check), which ``vectors.build`` turns into its ``Vector`` entries.
ENTRIES: list[tuple[str, str, Callable[..., None]]] = [
    ("DT1-fresh-launch-never-matches-an-earlier-one", "LSV", a_fresh_launch_never_matches_an_earlier_one),
    (
        "DT2-launch-outlives-its-connection",
        "LSV",
        a_launch_outlives_its_connection_and_later_ones_observe_and_download_it,
    ),
    ("DT3-interrupt-stops-the-process-group", "LSV", interrupt_stops_the_payloads_whole_process_group),
    ("DT4-detached-base-never-world-writable", "LSV", the_detached_base_is_never_world_writable),
]
