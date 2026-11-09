"""Session completion carries the actual recorded application status."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentworks.db import SessionStatus
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager

from .test_create_start_restart_orchestrated import _create_stubs, _restart_fixture
from .test_create_start_restart_orchestrated import _stub_build_registry as _stub_build_registry


@pytest.mark.parametrize("operation", ["create_session", "start_session", "restart_session"])
def test_completion_uses_recorded_session_application_state(tmp_path, monkeypatch, captured_output, operation):
    from agentworks.artifacts import session as artifacts
    from agentworks.harness_setup import outcomes

    if operation == "create_session":
        db = _create_stubs(tmp_path, monkeypatch, [])
        db.insert_agent("a1", "vm1", "agt-a1")
        options = {"workspace": "ws1", "agent": "a1"}
    else:
        db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
        options = {}
    prepare = artifacts.prepare_session_artifacts
    suffix = outcomes.setup_completion_suffix
    token = uuid4().hex
    observed = []

    def prepare_unavailable(*args, **kwargs):
        return replace(prepare(*args, **kwargs), unavailable_reasons=(uuid4().hex,))

    def completion(state):
        observed.append(state)
        return token if suffix(state) else ""

    monkeypatch.setattr(artifacts, "prepare_session_artifacts", prepare_unavailable)
    monkeypatch.setattr(outcomes, "setup_completion_suffix", completion)
    try:
        getattr(manager, operation)(
            db,
            SimpleNamespace(session=SimpleNamespace(history_limit=1)),
            name="s1",
            interaction=TtyInteractionPolicy.REFUSE,
            **options,
        )
        assert len(observed) == 1
        assert observed[0] is not None and any(not record.complete for record in observed[0].records)
        assert any(token in message for _role, _level, message in captured_output.lines)
        assert db.get_session("s1") is not None
    finally:
        db.close()
