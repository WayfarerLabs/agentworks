"""Composed launch-env conflicts fail before replacing an owned runtime."""

from __future__ import annotations

from uuid import uuid4

import pytest

from agentworks.db import SessionStatus
from agentworks.env import EnvEntry, PlaintextEnvEntry
from agentworks.errors import AgentworksError
from agentworks.sessions import manager, tmux
from agentworks.sessions.manager import _env

from .test_create_start_restart_orchestrated import _create_stubs, _restart_fixture
from .test_create_start_restart_orchestrated import _stub_build_registry as _stub_build_registry
from .test_native_startup import _NATIVE_CAPTURE, launch
from .test_native_startup import native as native
from .test_private_startup_boundaries import record_native_runtime
from .test_round8_startup import bind_native_runtime
from .test_round9_runtime import invoke


def composed_workspace_env(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    entry = EnvEntry(PlaintextEnvEntry(value="operator-value"))
    monkeypatch.setattr(manager, "_resolve_session_env", _env._resolve_session_env)
    monkeypatch.setattr(
        _env,
        "_resolve_session_env_scopes",
        lambda *a, **k: _env._SessionEnvScopes(vm={}, workspace={key: entry}, admin=None, agent={}, session={}),
    )
    monkeypatch.setattr("agentworks.vms.sites.site_platform_name", lambda *a: "linux")


@pytest.mark.parametrize("key", ["_AGW_SESSION_UUID", "_AGW_RUN_ID", "AGENTWORKS_SESSION_UUID", "AGENTWORKS_RUN_ID"])
@pytest.mark.parametrize("operation", ["create_session", "restart_session"])
def test_manager_refuses_composed_reserved_env_before_staging_or_teardown(
    tmp_path, monkeypatch, native, key, operation
):
    if operation == "create_session":
        db = _create_stubs(tmp_path, monkeypatch, [])
        db.insert_agent("a1", "vm1", "agt-a1")
        options = {"workspace": "ws1", "agent": "a1"}
    else:
        db, _ = _restart_fixture(tmp_path, monkeypatch)
        options = {}
    bind_native_runtime(db, monkeypatch, native)
    observed = None
    if operation == "restart_session":
        observed = record_native_runtime(db, native)
        db.set_session_run_id("s1", str(uuid4()))
    before = db.get_session("s1")
    composed_workspace_env(monkeypatch, key)
    stages = []
    monkeypatch.setattr("agentworks.artifacts.session.stage_session_artifacts", lambda *a, **k: stages.append(True))
    monkeypatch.setattr(
        tmux, "create_session", lambda *a, **k: launch(native, "sleep 30", is_admin=False, launch_options=k)
    )
    first = len(native.calls)
    try:
        with pytest.raises(AgentworksError):
            invoke(db, operation, **options)
        assert db.get_session("s1") == before
        assert stages == []
        assert not any("kill-server" in command or "new-session" in command for command, _ in native.calls[first:])
        if observed is not None:
            assert _NATIVE_CAPTURE(target=native, socket_path=native.socket).fingerprint == observed
            assert native.native("has-session -t '=s1'").ok
    finally:
        db.close()


def test_manager_preserves_unreserved_private_prefix_env_in_native_launch(tmp_path, monkeypatch, native):
    db = _create_stubs(tmp_path, monkeypatch, [])
    db.insert_agent("a1", "vm1", "agt-a1")
    bind_native_runtime(db, monkeypatch, native)
    composed_workspace_env(monkeypatch, "_AGW_OPERATOR_VALUE")
    monkeypatch.setattr(
        tmux, "create_session", lambda *a, **k: launch(native, "sleep 30", is_admin=False, launch_options=k)
    )
    try:
        invoke(db, "create_session", workspace="ws1", agent="a1")
        row = db.get_session("s1")
        assert row is not None and manager.check_session_status(row, target=native) is SessionStatus.RUNNING
        assert native.native("show-environment -t '=s1' _AGW_OPERATOR_VALUE").stdout.strip() == (
            "_AGW_OPERATOR_VALUE=operator-value"
        )
    finally:
        db.close()
