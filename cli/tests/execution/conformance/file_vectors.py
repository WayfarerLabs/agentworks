"""The vectors for file operations, F6 and F7 in the files LLD.

They join :mod:`vectors`' list, so the same lanes run them: the VM-wide lock
under the image's ``protected_regular`` (F6), and elevated file operations with
an agent's refusal before dispatch (F7). Each asserts observable behavior only.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.execution import file_program
from agentworks.execution.access import Agent, VMAccess
from agentworks.execution.errors import PermissionDenied
from agentworks.execution.requests import Command
from agentworks.execution.results import Completed
from tests.execution.conformance.lanes import needs_sudo

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.execution.conformance.lanes import Lane

REQUEST_DEADLINE = 60


def _owner(lane: Lane, path: str, *, elevated: bool = False) -> str:
    result = lane.run(Command(("stat", "-c", "%u", "--", path), elevated=elevated, deadline=REQUEST_DEADLINE))
    assert result.completion == Completed(0)
    return result.stdout.decode().strip()


def _remove_lock(lane: Lane) -> None:
    removed = lane.run(Command(("rm", "-f", "--", file_program.LOCK_PATH), elevated=True, deadline=REQUEST_DEADLINE))
    assert removed.completion == Completed(0)


# -- F6: the lock under the image's protected_regular --------------------------------------


def lock_is_shared_across_identities(lane: Lane) -> None:
    """The admin and root each create the lock, and the other then takes it and writes.

    The live lane reaches the VM as its admin only, so root stands in for the
    second identity; protected_regular applies to any account other than the
    file's owner, so the pair exercises the same rule an agent would.
    """
    needs_sudo(lane)
    files = lane.access.files(deadline=REQUEST_DEADLINE)
    directory = lane.make_dir()
    try:
        for creator, taker in ((False, True), (True, False)):
            _remove_lock(lane)
            files.write_if(
                f"{directory}/made-{creator}", b"lock", expected=None, mode=0o644, parent_mode=0o755, elevated=creator
            )
            assert _owner(lane, file_program.LOCK_PATH) == ("0" if creator else _owner(lane, directory))
            for writer in (taker, creator):
                path = f"{directory}/written-{creator}-{writer}"
                files.write_if(path, b"content", expected=None, mode=0o644, parent_mode=0o755, elevated=writer)
                assert files.read(path, elevated=True) == b"content"
    finally:
        lane.run(Command(("rm", "-rf", "--", directory), elevated=True, deadline=REQUEST_DEADLINE))


# -- F7: elevated file operations ----------------------------------------------------------


def elevated_write_and_read_of_a_root_owned_file(lane: Lane) -> None:
    needs_sudo(lane)
    files = lane.access.files(deadline=REQUEST_DEADLINE)
    directory = lane.make_dir()
    path = f"{directory}/root/file-{uuid.uuid4().hex[:8]}"
    try:
        files.write_if(path, b"root content", expected=None, mode=0o600, parent_mode=0o700, elevated=True)

        # The parent is root's 0700 directory, which the admin cannot search.
        assert _owner(lane, path, elevated=True) == "0"
        assert files.read(path, elevated=True) == b"root content"
    finally:
        lane.run(Command(("rm", "-rf", "--", f"{directory}/root"), elevated=True, deadline=REQUEST_DEADLINE))


def agent_elevated_write_is_refused_before_dispatch(lane: Lane) -> None:
    sent: list[object] = []
    agent = VMAccess(
        vm_name=lane.access.vm_name,
        host="127.0.0.1",
        user="agent",
        identity=Agent("agent", "agent"),
        identity_file=Path("/nonexistent/key"),
        log=lambda request, result: sent.append(request),
    )

    with pytest.raises(PermissionDenied):
        agent.files().write_if("/etc/agentworks-f7", b"x", expected=None, mode=0o644, parent_mode=0o755, elevated=True)

    assert sent == []


# (name, lanes, check), which ``vectors.build`` turns into its ``Vector`` entries.
ENTRIES: list[tuple[str, str, Callable[..., None]]] = [
    ("F6-lock-shared-across-identities", "V", lock_is_shared_across_identities),
    ("F7-elevated-file-operations", "SV", elevated_write_and_read_of_a_root_owned_file),
    ("F7-agent-elevated-file-operation-refused", "SV", agent_elevated_write_is_refused_before_dispatch),
]
