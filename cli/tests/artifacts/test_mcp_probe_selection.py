"""MCP carriers do not inherit unrelated skill and persona discovery checks."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from agentworks.artifacts.application import ArtifactApplication, ArtifactDeferral
from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.native.common import NativeSessionArtifacts, prepare_session_delivery
from agentworks.transports import Transport
from tests.artifacts.test_native_delivery import artifact, context
from tests.artifacts.test_native_mcp import server


@pytest.mark.parametrize("legacy", ["absent", "deferred", "delivered"])
def test_only_delivered_legacy_artifacts_require_legacy_discovery_probe(legacy, monkeypatch):
    item = server()
    rule = artifact(ArtifactType.RULE)
    current = context(item) if legacy == "absent" else context(item, rule)
    probe = Mock(return_value="/home/alice/.codex")
    monkeypatch.setattr("agentworks.artifacts.native.probe.probe_native", probe)
    application = ArtifactApplication(
        deferred=(ArtifactDeferral(input_id=rule.identity, destination="session", reason="not selected"),)
        if legacy == "deferred"
        else ()
    )
    plan = NativeSessionArtifacts(application, ("-c", "mcp_servers.fixture={command='example'}"))
    result, root = prepare_session_delivery(
        Mock(spec=Transport),
        tool="codex",
        integration="codex",
        context=current,
        render=lambda _: plan,
        workspace="/workspace",
    )
    assert result == plan
    assert probe.call_count == (1 if legacy == "delivered" else 0)
    assert root == ("/home/alice/.codex" if legacy == "delivered" else None)
