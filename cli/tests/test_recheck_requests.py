"""A command's held lock set against the one its rows build after acquisition: any other entity or mode refuses."""

from __future__ import annotations

import pytest

from agentworks.errors import StateConflictError
from agentworks.native_locks import (
    LockRequest,
    account_entity,
    console_entity,
    session_entity,
    vm_entity,
    workspace_entity,
)
from agentworks.recheck import require_request_unchanged

pytestmark = pytest.mark.windows

WORKSPACE = workspace_entity("box", "ws")
ACCOUNT = account_entity("box", "admin")


def _session(name: str):  # noqa: ANN202
    return session_entity(name, workspace=WORKSPACE, account=ACCOUNT)


def test_the_same_entities_in_the_same_modes_pass() -> None:
    request = LockRequest(exclusive=(_session("a"),), shared=(vm_entity("box"),))
    require_request_unchanged("session", "a", request, LockRequest(exclusive=(_session("a"),)))


@pytest.mark.parametrize(
    "rebuilt",
    [
        LockRequest(exclusive=(_session("a"), _session("b"))),
        LockRequest(exclusive=(WORKSPACE,)),
        LockRequest(shared=(_session("a"),)),
    ],
    ids=["entity-added", "entity-replaced", "mode-weaker"],
)
def test_another_entity_or_mode_refuses(rebuilt: LockRequest) -> None:
    with pytest.raises(StateConflictError) as refused:
        require_request_unchanged("session", "a", LockRequest(exclusive=(_session("a"),)), rebuilt)
    assert (refused.value.entity_kind, refused.value.entity_name) == ("session", "a")


def test_a_console_whose_members_changed_refuses() -> None:
    locked = console_entity("box", "c", members=[_session("a")])
    current = console_entity("box", "c", members=[_session("a"), _session("b")])
    with pytest.raises(StateConflictError):
        require_request_unchanged("console", "c", LockRequest(exclusive=(locked,)), LockRequest(exclusive=(current,)))
