"""Private MCP files follow prospective session staging and run fences."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agentworks.artifacts.bundle import ArtifactBundle
from agentworks.artifacts.declarations import MCPServerArtifactSpec
from agentworks.artifacts.mcp import MCPStdioDefinition
from agentworks.errors import StateError
from agentworks.harness_setup.state import read_native_setup
from tests.artifacts.test_session_lifecycle import lifecycle as lifecycle

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="local Linux guest filesystem execution")


def select_claude_mcp(lifecycle, monkeypatch) -> None:
    login = lifecycle.target.root / "login-shell"
    login.write_text(
        login.read_text().replace('[ "$1" = "-lc" ] || exit 9', 'case "$1" in -lc|-lic) ;; *) exit 9 ;; esac')
    )
    lifecycle.template.harness_integration = "claude-code"
    lifecycle.template.harness_integration_config = {"enabled_workarounds": ["session-mcp-config"]}
    lifecycle.config.artifact_bundles["team"] = ArtifactBundle(
        name="team",
        mcp_servers={
            "inventory": MCPServerArtifactSpec(
                definition=MCPStdioDefinition(type="stdio", command="inventory-server", env={"TOKEN": "${TOKEN}"})
            )
        },
    )
    monkeypatch.setattr("agentworks.plugins.claude.harness_integration.require_commands", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "agentworks.artifacts.native.probe.probe_native",
        lambda *args, **kwargs: str(lifecycle.target.home / ".claude"),
    )


def test_failed_private_mcp_stage_preserves_previous_runtime_and_run(lifecycle, monkeypatch):
    from agentworks.native_files import NativeFiles

    lifecycle.restart()
    previous = lifecycle.db.get_session("s1")
    old_files = lifecycle.files()
    select_claude_mcp(lifecycle, monkeypatch)
    original = NativeFiles.publish

    def reject_mcp(self, path, *args, **kwargs):
        if path.endswith("/mcp.json"):
            raise StateError("fixture private MCP publication interrupted")
        return original(self, path, *args, **kwargs)

    monkeypatch.setattr(NativeFiles, "publish", reject_mcp)
    lifecycle.events.clear()
    with pytest.raises(StateError):
        lifecycle.restart()
    current = lifecycle.db.get_session("s1")
    assert current is not None and previous is not None
    assert (current.session_uuid, current.run_id, current.pid) == (previous.session_uuid, previous.run_id, previous.pid)
    assert all(path.exists() for path in old_files)
    assert "kill" not in lifecycle.events and "tmux_create" not in lifecycle.events


def test_private_mcp_is_staged_before_teardown_and_committed_to_new_run_after(lifecycle, monkeypatch):
    lifecycle.restart()
    previous = lifecycle.db.get_session("s1")
    old_files = lifecycle.files()
    select_claude_mcp(lifecycle, monkeypatch)
    observed = []

    def teardown(*args, **kwargs):
        current = lifecycle.db.get_session("s1")
        assert current is not None and previous is not None and current.run_id == previous.run_id
        assert all(path.exists() for path in old_files)
        staged = [path for path in lifecycle.files() if path.name == "mcp.json"]
        assert len(staged) == 1 and staged[0].exists()
        server = next(iter(json.loads(staged[0].read_bytes())["mcpServers"].values()))
        assert server["env"] == {"TOKEN": "${TOKEN}"}
        observed.append(staged[0])

    monkeypatch.setattr("agentworks.sessions.manager._teardown_session", teardown)
    lifecycle.restart()
    current = lifecycle.db.get_session("s1")
    assert current is not None and previous is not None
    assert current.session_uuid == previous.session_uuid and current.run_id != previous.run_id
    assert len(observed) == 1 and observed[0].is_file() and current.run_id in str(observed[0])
    assert all(not path.exists() for path in old_files)
    assert all(record.complete for record in read_native_setup(lifecycle.db, "session", "s1").records)


def test_post_teardown_launch_failure_keeps_new_mcp_run_for_retry(lifecycle, monkeypatch):
    lifecycle.restart()
    previous = lifecycle.db.get_session("s1")
    select_claude_mcp(lifecycle, monkeypatch)

    def refuse_launch(*args, **kwargs):
        raise StateError("fixture workload launch interrupted")

    monkeypatch.setattr("agentworks.sessions.tmux.create_session", refuse_launch)
    lifecycle.events.clear()
    with pytest.raises(StateError):
        lifecycle.restart()
    current = lifecycle.db.get_session("s1")
    assert current is not None and previous is not None and current.run_id != previous.run_id
    files = [Path(path) for path in lifecycle.files() if path.name == "mcp.json"]
    assert len(files) == 1 and files[0].is_file() and current.run_id in str(files[0])
    assert "kill" in lifecycle.events
