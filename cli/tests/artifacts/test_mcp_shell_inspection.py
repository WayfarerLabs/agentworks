"""Shell publication and offline MCP metadata never imply a server connection."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks import output
from agentworks.artifacts.application import ArtifactDeferral
from agentworks.artifacts.bundle import ArtifactBundle
from agentworks.artifacts.declarations import ArtifactsConfig
from agentworks.artifacts.inspection import inspect_artifacts, inspection_data, render_artifacts
from agentworks.artifacts.mcp import content_from_definition, parse_definition
from agentworks.artifacts.model import ArtifactInput, ArtifactOrigin, ArtifactProvenance
from agentworks.artifacts.native.shell import filesystem_artifacts
from agentworks.artifacts.reporting import report_applied, report_deferrals
from agentworks.artifacts.state import capture_owner, write_capture
from agentworks.capabilities.base import RunContext
from agentworks.capabilities.harness_integration import HarnessStart
from agentworks.capabilities.harness_integration.shell import ShellIntegration
from agentworks.harness_setup.inputs import SetupInputs
from agentworks.harness_setup.model import NativeClaim, NativeSetupState, SetupRecord
from agentworks.harness_setup.state import write_native_setup
from agentworks.origin import Origin
from agentworks.schema import CapabilityConfig
from agentworks.secrets.orchestration import SecretTarget
from agentworks.vms.template import VMTemplate
from tests.artifacts._fixtures import received
from tests.artifacts.test_native_delivery import context
from tests.conftest import registry_with_shell

if TYPE_CHECKING:
    from agentworks.artifacts.state import CapturedArtifacts
    from agentworks.db import Database
    from agentworks.resources.registry import Registry

STDIO = {"type": "stdio", "command": "unavailable-test-command", "args": ["private-arg"], "env": {"TOKEN": "${TOKEN}"}}
HTTP = {"type": "http", "url": "https://private-endpoint.invalid/mcp", "headers": {"X-Token": "private-header"}}


def mcp(name: str = "inventory", *, owner: str = "worker", definition: object = STDIO) -> ArtifactInput:
    return ArtifactInput(
        content_from_definition(name, parse_definition(definition)),
        ArtifactProvenance(),
        ArtifactOrigin("session", "session", owner, bundle="tools", entry=name),
    )


@pytest.mark.parametrize(
    "facet,root",
    [
        ("vm", "/opt/agentworks/artifacts"),
        ("user", "/home/worker/.agentworks-artifacts/user"),
        ("workspace", "/work/project/.agentworks-artifacts"),
        ("session", "/home/worker/.agentworks-artifacts/session/u/r"),
    ],
)
def test_shell_publishes_canonical_mcp_json_at_every_facet_without_execution(facet, root, monkeypatch):
    first = mcp()
    second = mcp("endpoint", definition=HTTP)

    def forbid(*args, **kwargs):
        raise AssertionError("Shell publication must not execute or connect to MCP servers")

    monkeypatch.setattr(subprocess, "Popen", forbid)
    monkeypatch.setenv("TOKEN", "must-remain-unresolved")
    if facet == "session":
        prepared = replace(context(first, second), directory=root)
        integration = ShellIntegration(
            "shell",
            {},
            session_name="run",
            vm_name="box",
            workspace_name="workspace",
            workspace_path="/work/project",
            target=None,
            admin=True,
            state={},
            artifact_context=prepared,
        )
        result = integration.start(RunContext())
        assert isinstance(result, HarnessStart)
        application = result.artifacts
        assert application.artifacts_dir == root
    else:
        integration = ShellIntegration.for_setup(owner_kind="vm", owner_name="box", facet=facet, config={})
        invocation = SimpleNamespace(artifacts=received(first, second), home="/home/worker", root="/work/project")
        application = getattr(integration, f"{facet}_init")(invocation)
        assert application.artifacts_dir is None
    assert not application.deferred
    assert len(application.files) == 3
    index = json.loads(next(file.data for file in application.files if file.path == f"{root}/index.json"))
    entries = index["groups"][0]["mcp_servers"]
    for item in (first, second):
        relative = f"scopes/session/worker/mcp_servers/{item.content.name}.json"
        file = next(file for file in application.files if file.path == f"{root}/{relative}")
        assert json.loads(file.data) == item.content.metadata
        assert file.data.endswith(b"\n") and b"\r" not in file.data
        assert file.origins == (item.origin_identity,)
        assert entries[item.content.name]["files"] == [relative]
        assert entries[item.content.name]["origin"]["resource_name"] == item.origin.resource_name
        assert not file.executable
    assert b"must-remain-unresolved" not in b"".join(file.data for file in application.files)


def test_shell_keeps_same_named_mcp_servers_from_different_owners_separate():
    first, second = mcp(owner="outer"), mcp(owner="inner", definition=HTTP)
    application = filesystem_artifacts(received(first, second), "/private", session=True)
    definitions = [file for file in application.files if file.path.endswith("/inventory.json")]
    assert len({file.path for file in definitions}) == 2
    assert {file.origins for file in definitions} == {(first.origin_identity,), (second.origin_identity,)}
    assert [json.loads(file.data) for file in definitions] == [first.content.metadata, second.content.metadata]


@pytest.mark.parametrize("count,visible", [(1, 1), (3, 3), (4, 2)])
@pytest.mark.parametrize("deferred", [False, True])
def test_mcp_progress_bounds_names_and_omits_connection_values(monkeypatch, count, visible, deferred):
    items = tuple(mcp(f"server-{index}") for index in range(count))
    messages: list[str] = []
    monkeypatch.setattr(output, "info", messages.append)
    monkeypatch.setattr(output, "warn", messages.append)
    if deferred:
        report_deferrals(
            received(*items),
            integration="native",
            owner="vm",
            deferred=tuple(
                ArtifactDeferral(input_id=item.identity, destination="session", reason="test-reason") for item in items
            ),
        )
    else:
        report_applied(received(*items), integration="shell", owner="user")
    line = messages[0]
    assert f" {count} " in line
    assert all((item.content.name in line) == (index < visible) for index, item in enumerate(items))
    assert "private-arg" not in line and "unavailable-test-command" not in line and "${TOKEN}" not in line
    assert ("..." in line) == (count > 3)


def recorded_mcp(db: Database) -> tuple[Registry, CapturedArtifacts, SetupRecord]:
    registry = registry_with_shell()
    origin = Origin.built_in(source="mcp-inspection-fixture")
    bundle = ArtifactBundle.model_validate(
        {
            "name": "tools",
            "mcp_servers": {"inventory": {"definition": STDIO}, "endpoint": {"definition": HTTP}},
        }
    )
    registry.add("artifact-bundle", bundle.name, bundle, origin)
    config = ArtifactsConfig(bundles=[bundle.name])
    template = VMTemplate(name="vm", artifacts=config, harness_integrations={"shell": CapabilityConfig()})
    registry.add("vm-template", template.name, template, origin)
    db.insert_vm("vm", "local", "vm", template="vm")
    snapshot = capture_owner(registry, "vm", "vm", "vm", config)
    write_capture(db, "vm", "vm", "vm", snapshot, operation="fixture")
    owner = SetupInputs("vm", "vm", "vm", template.harness_integrations, SecretTarget(vm={}), config)
    record = SetupRecord(
        component="vm",
        integration="shell",
        destination_id="d" * 64,
        declaration=owner.declaration("shell", CapabilityConfig()),
        complete=True,
        artifact_inputs=tuple(item.identity for item in snapshot.inputs.items()),
        claims=tuple(
            NativeClaim(
                role="artifact-mcp-server",
                identifier=item.content.name,
                destination="/home/user/config.json",
                source=item.origin_identity,
                sha256="a" * 64,
                strategy="private-claim-extra",
            )
            for item in snapshot.inputs.items()
        )
        + (NativeClaim(role="plugin", identifier="private-plugin", destination="https://private-plugin.invalid"),),
    )
    write_native_setup(db, "vm", "vm", NativeSetupState(records=(record,)), operation="fixture")
    return registry, snapshot, record


def test_inspection_projects_only_registration_metadata_without_io_or_secret_resolution(db, monkeypatch, capsys):
    registry, snapshot, record = recorded_mcp(db)
    before = db.instance_state.get_applied_slices("vm", "vm")

    def forbid(*args, **kwargs):
        raise AssertionError("inspection must not acquire, execute native inventory, or resolve secrets")

    monkeypatch.setattr("agentworks.package_sources.PackageCapture.capture", forbid)
    monkeypatch.setattr("agentworks.artifacts.capture.capture_artifacts", forbid)
    monkeypatch.setattr("agentworks.secrets.orchestration.resolve_for_command", forbid)
    monkeypatch.setattr(subprocess, "Popen", forbid)
    result = inspect_artifacts(db, registry, vm_name="vm")
    owner = result.owners[0]
    assert [item.native_name for item in owner.artifacts] == [item.content.name for item in snapshot.inputs.items()]
    placements = owner.integrations[0].placements
    assert len(placements) == 2
    assert [(item.path, item.native_identity, item.origins) for item in placements] == [
        (claim.destination, claim.identifier, (claim.source,)) for claim in record.claims[:2]
    ]
    render_artifacts(result)
    encoded = json.dumps(inspection_data(result)) + capsys.readouterr().out
    for value in (
        HTTP["url"],
        "private-header",
        "private-arg",
        "unavailable-test-command",
        "${TOKEN}",
        "private-plugin",
        "private-claim-extra",
    ):
        assert value not in encoded
    assert db.instance_state.get_applied_slices("vm", "vm") == before


@pytest.mark.parametrize(
    "case", ["missing-source", "bad-origin", "bad-identity", "url-destination", "traversal", "withdrawn"]
)
def test_claim_projection_omits_malformed_metadata_but_retains_withdrawn_registration(db, case):
    registry, _, record = recorded_mcp(db)
    claim = record.claims[0]
    changes = {
        "missing-source": {"source": None},
        "bad-origin": {"source": "private-value"},
        "bad-identity": {"identifier": "https://private-value.invalid"},
        "url-destination": {"destination": "https://private-value.invalid"},
        "traversal": {"destination": "/home/user/../private-value"},
        "withdrawn": {"source": "f" * 64, "identifier": "agentworks-withdrawn-" + "f" * 20},
    }
    changed = record.model_copy(update={"claims": (claim.model_copy(update=changes[case]),)})
    write_native_setup(db, "vm", "vm", NativeSetupState(records=(changed,)), operation="fixture")
    result = inspect_artifacts(db, registry, vm_name="vm")
    placements = result.owners[0].integrations[0].placements
    assert len(placements) == (1 if case == "withdrawn" else 0)
    assert "private-value" not in json.dumps(inspection_data(result))
    if placements:
        assert placements[0].origins == ("f" * 64,)
