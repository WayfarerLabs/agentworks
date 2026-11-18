"""I84 over the admin's access: a teardown kills once, repeats nothing uncertain, and an unknown removes nothing.

The reachable case runs against a real tmux server (the ``native`` fixture),
each request recorded on its way through the access.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.errors import AgentworksError
from agentworks.execution.requests import Script
from agentworks.execution.results import Result, Unknown, UnknownReason
from agentworks.sessions import tmux
from agentworks.sessions.manager._teardown import DedicatedTeardownPlan, execute_dedicated_teardown
from tests.conftest import current_user_name
from tests.vm_access_support import Admin, RecordingVMAccess

from .test_native_startup import _NATIVE_CAPTURE, NativeGuest, launch
from .test_native_startup import native as native

SESSION_UUID = "11111111-1111-4111-8111-111111111111"


def _plan(native: NativeGuest, access: RecordingVMAccess, *, force: bool = False) -> DedicatedTeardownPlan:
    fingerprint = _NATIVE_CAPTURE(run_command=native.run, socket_path=native.socket).fingerprint
    assert fingerprint is not None
    return DedicatedTeardownPlan(
        session_name="s1",
        session_uuid=SESSION_UUID,
        run_id=None,
        vm_name="box",
        socket_path=native.socket,
        owner=current_user_name(),
        stored_pid=fingerprint.pid,
        stored_boot_id=fingerprint.boot_id,
        stored_start_ticks=fingerprint.start_ticks,
        access=access,
        switch_user=False,
        deadline=10,
        force=force,
    )


def _sources(access: RecordingVMAccess) -> list[str]:
    return [request.source for request in access.requests() if isinstance(request, Script)]


def test_a_teardown_kills_the_matching_server_once_and_repeats_only_its_absence_poll(native: NativeGuest) -> None:
    launch(native, "sleep 30")
    access = native.access()

    execute_dedicated_teardown(_plan(native, access))

    sources = _sources(access)
    kills = [index for index, source in enumerate(sources) if "kill-server" in source]
    assert len(kills) == 1
    before_kill, after_kill = sources[: kills[0]], sources[kills[0] + 1 :]
    # Before the kill only the fingerprint's own paired pid and start-time
    # reads repeat, once each; after it, only the bounded absence poll
    # repeats, and it mutates nothing.
    paired = {source: count for source, count in Counter(before_kill).items() if count > 1}
    assert set(paired.values()) == {2}
    assert all("#{pid}" in source or "/stat" in source for source in paired)
    repeated = [source for source, count in Counter(after_kill).items() if count > 1]
    assert not any("kill" in source or "rm -f" in source for source in repeated)
    assert all(request.deadline == 10 for request in access.requests())
    assert native.native("list-sessions").returncode != 0
    assert not Path(native.socket).exists()


@pytest.mark.parametrize("force", [False, True])
def test_a_teardown_whose_probe_does_not_complete_removes_and_signals_nothing(native: NativeGuest, force: bool) -> None:
    launch(native, "sleep 30")
    probed = native.access()
    plan = _plan(native, probed, force=force)
    unknown = RecordingVMAccess([], Admin())
    unknown.default_outcome = Result(Unknown(UnknownReason.DEADLINE))

    with pytest.raises(AgentworksError):
        execute_dedicated_teardown(replace(plan, access=unknown))

    # The first request does not complete; nothing follows it.
    assert len(_sources(unknown)) == 1
    assert native.native("has-session -t '=s1'").ok
    assert tmux.ProbeStatus.PRESENT is _NATIVE_CAPTURE(run_command=native.run, socket_path=native.socket).status
