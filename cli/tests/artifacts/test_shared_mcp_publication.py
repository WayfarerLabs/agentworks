"""Shared native documents receive exclusions without whole-file ownership."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from unittest.mock import Mock

import pytest

from agentworks.artifacts.application import ArtifactApplication, ArtifactFile
from agentworks.artifacts.git_excludes import GIT_EXCLUDE_ROLE, reconcile_workspace_excludes
from agentworks.artifacts.model import (
    ArtifactContent,
    ArtifactInput,
    ArtifactInputs,
    ArtifactOrigin,
    ArtifactProvenance,
    ArtifactType,
)
from agentworks.artifacts.native.mcp import MCP_CLAIM_ROLE
from agentworks.artifacts.publication import validate_application
from agentworks.artifacts.routing import RoutingResult
from agentworks.capabilities.harness_integration.setup import WorkspaceSetupInvocation
from agentworks.errors import StateError
from agentworks.harness_setup.dispatch import run_setup
from agentworks.harness_setup.inputs import SetupInputs
from agentworks.harness_setup.model import NativeClaim
from agentworks.harness_setup.outcomes import SetupFailure
from agentworks.plugins import Plugin, seated_plugin
from agentworks.schema import CapabilityConfig
from agentworks.secrets.orchestration import SecretTarget
from tests.artifacts._fixtures import received
from tests.native_setup_fixtures import LocalFixtureTransport
from tests.plugins._fixtures import ConformingHarnessIntegration


@pytest.mark.parametrize("facet", ["vm", "user", "session"])
def test_shared_paths_are_workspace_only(facet):
    application = ArtifactApplication(shared_paths=("/workspace/.mcp.json",))
    with pytest.raises(StateError):
        validate_application(application, ArtifactInputs(), facet, integration="fixture")


@pytest.mark.parametrize("paths", [["/workspace/.mcp.json"], "/workspace/.mcp.json", None])
def test_shared_paths_require_a_tuple(paths):
    with pytest.raises(StateError):
        validate_application(
            ArtifactApplication(shared_paths=paths), ArtifactInputs(), "workspace", integration="fixture"
        )


@pytest.mark.parametrize(
    "path", [None, 3, "", "/", "relative", "//double", "/work/../outside", "/work/./file", "/work/file/", "/work/a\nb"]
)
def test_shared_paths_require_normalized_absolute_strings(path):
    with pytest.raises(StateError):
        validate_application(
            ArtifactApplication(shared_paths=(path,)), ArtifactInputs(), "workspace", integration="fixture"
        )


@pytest.mark.parametrize("duplicate", ["/workspace/.mcp.json", "/WORKSPACE/.MCP.JSON"])
def test_shared_paths_reject_duplicate_casefolded_destinations(duplicate):
    with pytest.raises(StateError):
        validate_application(
            ArtifactApplication(shared_paths=("/workspace/.mcp.json", duplicate)),
            ArtifactInputs(),
            "workspace",
            integration="fixture",
        )


@pytest.mark.parametrize("shared", ["/workspace/.mcp.json", "/WORKSPACE/.MCP.JSON"])
def test_shared_document_cannot_also_be_an_owned_artifact_file(shared):
    item = ArtifactInput(
        ArtifactContent(ArtifactType.RULE, "fixture"),
        ArtifactProvenance(),
        ArtifactOrigin("workspace", "workspace", "fixture", entry="fixture"),
    )
    file = ArtifactFile("/workspace/.mcp.json", b"{}", (item.origin_identity,))
    with pytest.raises(StateError):
        validate_application(
            ArtifactApplication(files=(file,), shared_paths=(shared,)),
            received(item),
            "workspace",
            integration="fixture",
        )


def test_shared_document_needs_no_whole_file_or_input_origin():
    application = ArtifactApplication(shared_paths=("/workspace/.mcp.json", "/workspace/.codex/config.toml"))
    assert validate_application(application, ArtifactInputs(), "workspace", integration="fixture") is application
    assert application.files == ()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux guest filesystem and local Git")
@pytest.mark.parametrize("tracked", [False, True])
def test_dispatch_excludes_shared_mcp_document_without_owning_or_changing_it(
    db, tmp_path, monkeypatch, captured_output, tracked
):
    target = LocalFixtureTransport(tmp_path / "guest")
    workspace = target.home / "project"

    def git(*args: object, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *map(str, args)], env=target.environment, capture_output=True, text=True, check=check
        )

    git("init", "-q", workspace)
    native = workspace / ".mcp.json"
    content = b'{"mcpServers":{"fixture":{"command":"fixture"}},"operator":true}\n'
    native.write_bytes(content)
    before = native.stat()
    if tracked:
        git("-C", workspace, "add", "--", ".mcp.json")
    exclude = workspace / ".git/info/exclude"
    original_exclude = exclude.read_bytes()
    vm = db.insert_vm("vm", site="fixture", hostname="vm")
    inputs = SetupInputs(
        kind="workspace",
        name="fixture",
        component="workspace",
        activations={"shared-mcp-fixture": CapabilityConfig.model_validate({})},
        target=SecretTarget(vm={}),
    )
    invocation = WorkspaceSetupInvocation(
        vm=vm,
        runner=target,
        prior=None,
        checkpoint=Mock(),
        workspace_name="fixture",
        root=str(workspace),
        linux_group="",
    )
    monkeypatch.setattr("agentworks.harness_setup.dispatch.destination_id", lambda *args, **kwargs: "a" * 64)
    monkeypatch.setattr("agentworks.harness_setup.dispatch.ensure_harness_integration_enabled", lambda *args: None)
    monkeypatch.setattr("agentworks.artifacts.routing.setup_artifacts", lambda *args: RoutingResult(ArtifactInputs()))

    class SharedMCP(ConformingHarnessIntegration):
        name = "shared-mcp-fixture"
        description = "Shared native document fixture"

        def workspace_init(self, call: WorkspaceSetupInvocation) -> ArtifactApplication:
            claims = tuple(claim for claim in (call.prior.claims if call.prior else ()) if claim.role != MCP_CLAIM_ROLE)
            if not self.retiring:
                claims += (
                    NativeClaim(
                        role=MCP_CLAIM_ROLE,
                        identifier="fixture",
                        destination=str(native),
                        source="e" * 64,
                        sha256="f" * 64,
                    ),
                )
            call.checkpoint(claims)
            return ArtifactApplication(shared_paths=() if self.retiring else (str(native),))

    with seated_plugin(Plugin(name="shared-mcp-plugin", capabilities={"harness-integration": (SharedMCP,)})):
        failures: list[SetupFailure] = []
        state = run_setup(db, Mock(), inputs, invocation, operation="workspace-create", failures=failures)
        assert failures == []
        (record,) = state.records
        assert record.complete and record.artifact_files == ()
        assert any(claim.role == MCP_CLAIM_ROLE for claim in record.claims)
        assert any(claim.role == GIT_EXCLUDE_ROLE for claim in record.claims) is not tracked
        assert (git("-C", workspace, "check-ignore", "--", ".mcp.json", check=False).returncode == 0) is not tracked
        if tracked:
            assert captured_output.warnings
            assert git("-C", workspace, "ls-files", "--error-unmatch", "--", ".mcp.json").stdout
        settled = exclude.read_bytes()
        run_setup(db, Mock(), inputs, invocation, operation="workspace-create", failures=failures)
        assert exclude.read_bytes() == settled
        retired = run_setup(
            db, Mock(), replace(inputs, activations={}), invocation, operation="workspace-delete", failures=failures
        )
        assert failures == [] and retired.records == ()
    assert native.read_bytes() == content
    assert native.stat().st_ino == before.st_ino and native.stat().st_mtime_ns == before.st_mtime_ns
    assert exclude.read_bytes() == original_exclude
    assert git("-C", workspace, "check-ignore", "--", ".mcp.json", check=False).returncode == 1
    if tracked:
        assert git("-C", workspace, "ls-files", "--error-unmatch", "--", ".mcp.json").stdout
    assert not list((target.root / "tmp").iterdir())


def test_exclusion_metadata_cannot_escape_owning_workspace():
    target = Mock()
    with pytest.raises(StateError):
        reconcile_workspace_excludes(
            target,
            "/workspace",
            "fixture",
            (),
            (),
            Mock(),
            group="",
            report_incomplete=Mock(),
            shared_paths=("/workspace-neighbor/.mcp.json",),
        )
    assert target.method_calls == []
