"""Guest discovery exposes applicability without workstation acquisition paths."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agentworks.artifacts.model import ArtifactProvenance, ArtifactType
from agentworks.artifacts.native.shell import shell_artifacts
from tests.artifacts._fixtures import received
from tests.artifacts.test_native_delivery import artifact


@pytest.mark.parametrize("artifact_type", list(ArtifactType))
def test_guest_index_omits_acquisition_path_without_losing_origin_or_revision(artifact_type):
    source = "file::/private/workstation/agent-content"
    revision = "a" * 40
    item = replace(artifact(artifact_type), provenance=ArtifactProvenance(source=source, commit=revision))
    application = shell_artifacts(received(item), "/opt/agentworks/artifacts")
    index_file = next(file for file in application.files if file.path.endswith("/index.json"))
    index = json.loads(index_file.data)
    entry = index["groups"][0][artifact_type.map_name][item.content.name]
    assert "source" not in entry
    assert source.encode() not in index_file.data
    assert entry["commit"] == revision
    assert entry["origin"]["resource_name"] == item.origin.resource_name
    assert entry["files"]
    assert item.provenance.source == source
