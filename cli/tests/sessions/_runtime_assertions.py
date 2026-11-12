"""Assertions for accepted requests that preserve existing runtime evidence."""

from dataclasses import replace

from agentworks.db import Database, DesiredRuntimeState, SessionRow


def assert_runtime_preserved(db: Database, before: SessionRow | None, *, operation: str) -> None:
    assert before is not None
    current = db.get_session(before.name)
    assert current is not None
    target = DesiredRuntimeState.STOPPED if operation == "stop_session" else before.desired_runtime_state
    assert current.desired_runtime_state is target
    if target is before.desired_runtime_state:
        assert current == before
    else:
        assert (
            replace(current, desired_runtime_state=before.desired_runtime_state, updated_at=before.updated_at) == before
        )
