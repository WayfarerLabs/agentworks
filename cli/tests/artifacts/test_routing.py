"""Actual-owner routing from immutable captures and persisted native results."""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from agentworks.agents.template import AgentTemplate
from agentworks.artifacts.application import ArtifactDeferral, OwnedArtifactFile
from agentworks.artifacts.bundle import ArtifactBundle
from agentworks.artifacts.declarations import ArtifactsConfig, HintArtifactSpec
from agentworks.artifacts.model import ArtifactGroup, ArtifactInputs, ArtifactOwner
from agentworks.artifacts.routing import (
    deferred_inputs,
    inspect_owner_artifacts,
    session_artifacts,
    setup_artifacts,
)
from agentworks.artifacts.state import CapturedArtifacts, capture_owner, write_capture
from agentworks.db import Database, VMRow, WorkspaceRow
from agentworks.env.entry import EnvEntry
from agentworks.errors import StateError
from agentworks.harness_setup.inputs import SetupInputs
from agentworks.harness_setup.model import NativeClaim, NativeSetupState, SetupRecord
from agentworks.harness_setup.state import read_native_setup, replace_setup_record, write_native_setup
from agentworks.origin import Origin
from agentworks.resources.registry import Registry
from agentworks.schema import CapabilityConfig
from agentworks.secrets.orchestration import SecretTarget
from agentworks.vms.admin import AdminConfig
from agentworks.vms.template import VMTemplate
from agentworks.workspaces.template import WorkspaceTemplate
from tests.artifacts._fixtures import group


@dataclass
class Graph:
    db: Database
    registry: Registry
    vm: VMRow
    workspace: WorkspaceRow
    owners: dict[str, SetupInputs]
    captures: dict[str, CapturedArtifacts]

    def save(
        self,
        owner: str,
        *,
        inherited: ArtifactGroup | None = None,
        routes: dict[str, str] | None = None,
        complete: bool = True,
        files: tuple[OwnedArtifactFile, ...] = (),
    ) -> SetupRecord:
        inputs = self.owners[owner]
        prepared = ArtifactInputs(
            local=self.captures[owner].inputs, deferred={} if inherited is None else {inherited.owner: inherited}
        )
        record = SetupRecord(
            component=inputs.component,
            integration="shell",
            destination_id="d" * 64,
            declaration=inputs.declaration("shell", CapabilityConfig()),
            complete=complete,
            artifact_inputs=tuple(item.identity for item in prepared.items()),
            artifact_files=files,
            deferred=tuple(
                ArtifactDeferral(input_id=item.identity, destination=routes[item.origin.entry], reason="later")
                for item in prepared.items()
                if routes is not None and item.origin.entry in routes
            ),
        )
        state = replace_setup_record(read_native_setup(self.db, inputs.kind, inputs.name), record)
        write_native_setup(self.db, inputs.kind, inputs.name, state, operation="fixture")
        return record

    def route(self, *, user: str | None = "agent"):
        return session_artifacts(
            self.db,
            self.registry,
            self.vm,
            self.workspace,
            user,
            "shell",
            ArtifactGroup(ArtifactOwner("session", "session", "s1")),
        )


def graph(db: Database, *, active: tuple[str, ...] = (), counts: dict[str, int] | None = None) -> Graph:
    registry = Registry.empty()
    origin = Origin.built_in(source="routing-fixture")
    vm = db.insert_vm("vm", "site", "vm", template="vm", admin_template="admin")
    workspace = db.insert_workspace("workspace", "/work/project", "vm", "project", template="workspace")
    db.insert_agent("agent", "vm", "worker", template="agent")
    owners = {}
    captures = {}
    for component in ("vm", "admin", "agent", "workspace"):
        bundle_name = f"{component}-bundle"
        entries = {
            f"{component}-{index}": HintArtifactSpec(text=f"{component} {index}")
            for index in range((counts or {}).get(component, 1))
        }
        registry.add("artifact-bundle", bundle_name, ArtifactBundle(name=bundle_name, hints=entries), origin)
        config = ArtifactsConfig(bundles=[bundle_name])
        activations = {"shell": CapabilityConfig.model_validate({})} if component in active else {}
        model = {"vm": VMTemplate, "admin": AdminConfig, "agent": AgentTemplate, "workspace": WorkspaceTemplate}[
            component
        ]
        template = model(name=component, artifacts=config, harness_integrations=activations)
        registry.add(f"{component}-template" if component != "admin" else "admin-template", component, template, origin)
        kind = "vm" if component in ("vm", "admin") else component
        name = "vm" if component in ("vm", "admin") else component
        target = SecretTarget(
            vm={},
            admin={} if component == "admin" else None,
            agent={} if component == "agent" else None,
            workspace={} if component == "workspace" else None,
        )
        inputs = SetupInputs(kind, name, component, activations, target, artifacts=config)
        snapshot = capture_owner(registry, kind, name, component, config)
        write_capture(db, kind, name, component, snapshot, operation="fixture")
        owners[component] = inputs
        captures[component] = snapshot
    return Graph(db, registry, vm, workspace, owners, captures)


def test_inactive_diamond_passes_each_origin_once_without_records(db):
    fixture = graph(db)
    result = fixture.route()
    assert [item.origin.component for item in result.inputs.items()] == ["vm", "agent", "workspace"]
    assert result.active_facets == ()
    assert not result.ancestor_files
    assert not read_native_setup(db, "vm", "vm").records
    admin = fixture.route(user=None)
    assert [item.origin.component for item in admin.inputs.items()] == ["vm", "admin", "workspace"]


@pytest.mark.parametrize("user", ["agent", "admin"])
def test_inactive_vm_inputs_are_handled_by_actual_active_user(db, user):
    fixture = graph(db, active=(user,))
    prepared = setup_artifacts(db, fixture.registry, fixture.owners[user], fixture.vm, "shell")
    assert prepared.local == fixture.captures[user].inputs
    assert prepared.deferred == {fixture.captures["vm"].inputs.owner: fixture.captures["vm"].inputs}
    fixture.save(user, inherited=fixture.captures["vm"].inputs)
    result = fixture.route(user=None if user == "admin" else user)
    assert [item.origin.component for item in result.inputs.items()] == ["workspace"]
    assert result.active_facets == ("user",)
    assert not any(record.component == "vm" for record in read_native_setup(db, "vm", "vm").records)


def test_inactive_vm_routes_only_to_user_and_workspace_keeps_local_inputs(db):
    fixture = graph(db)
    vm = inspect_owner_artifacts(db, fixture.registry, fixture.owners["vm"], "shell")
    assert deferred_inputs(vm, "user") == {fixture.captures["vm"].inputs.owner: fixture.captures["vm"].inputs}
    assert deferred_inputs(vm, "workspace") == {}
    assert deferred_inputs(vm, "session") == {}
    assert setup_artifacts(db, fixture.registry, fixture.owners["workspace"], fixture.vm, "shell") == ArtifactInputs(
        local=fixture.captures["workspace"].inputs
    )


def test_newly_inherited_vm_inputs_require_active_user_setup(db):
    fixture = graph(db, active=("agent",))
    previous = fixture.save("agent")
    inherited = fixture.captures["vm"].inputs
    view = inspect_owner_artifacts(
        db, fixture.registry, fixture.owners["agent"], "shell", inherited={inherited.owner: inherited}
    )
    assert view.status == "stale"
    with pytest.raises(StateError):
        fixture.route()
    assert read_native_setup(db, "agent", "agent").records == (previous,)
    fixture.save("agent", inherited=inherited)
    assert [item.origin.component for item in fixture.route().inputs.items()] == ["workspace"]


@pytest.mark.parametrize("component", ["agent", "workspace"])
def test_inactive_branch_passes_inherited_and_local_inputs_to_session(db, component):
    fixture = graph(db, active=("vm",))
    owner = fixture.owners[component]
    fixture.save("vm", routes={"vm-0": owner.facet})
    inherited = fixture.captures["vm"].inputs
    view = inspect_owner_artifacts(db, fixture.registry, owner, "shell", inherited={inherited.owner: inherited})
    assert deferred_inputs(view, "session") == {
        inherited.owner: inherited,
        fixture.captures[component].inputs.owner: fixture.captures[component].inputs,
    }
    assert deferred_inputs(view, "user") == {}
    assert deferred_inputs(view, "workspace") == {}
    result = fixture.route()
    assert [item.origin.component for item in result.inputs.items()] == ["vm", "agent", "workspace"]
    assert len({item.identity for item in result.inputs.items()}) == sum(1 for _ in result.inputs.items())


def test_diamond_routes_restore_original_vm_declaration_order(db):
    fixture = graph(db, active=("vm",), counts={"vm": 3})
    fixture.save("vm", routes={"vm-0": "user", "vm-1": "session", "vm-2": "workspace"})
    result = fixture.route()
    assert [item.origin.entry for item in result.inputs.items()] == ["vm-0", "vm-1", "vm-2", "agent-0", "workspace-0"]
    assert len({item.identity for item in result.inputs.items()}) == sum(1 for _ in result.inputs.items())


def test_each_branch_handles_only_its_prepared_inputs(db):
    fixture = graph(db, active=("vm", "agent", "workspace"), counts={"vm": 3})
    fixture.save("vm", routes={"vm-0": "user", "vm-1": "session", "vm-2": "workspace"})
    vm_items = fixture.captures["vm"].inputs
    fixture.save("agent", inherited=group(vm_items.hints["vm-0"]))
    fixture.save("workspace", inherited=group(vm_items.hints["vm-2"]), routes={"workspace-0": "session"})
    result = fixture.route()
    assert [item.origin.entry for item in result.inputs.items()] == ["vm-1", "workspace-0"]
    assert result.active_facets == ("vm", "user", "workspace")


def test_setup_routes_only_requested_vm_branch_and_uses_prospective_capture(db):
    fixture = graph(db, active=("vm",), counts={"vm": 2})
    fixture.save("vm", routes={"vm-0": "workspace", "vm-1": "user"})
    owner = replace(fixture.owners["agent"], artifact_snapshot=fixture.captures["agent"])
    result = setup_artifacts(db, fixture.registry, owner, fixture.vm, "shell")
    assert [item.origin.entry for item in result.items()] == ["vm-1", "agent-0"]
    project = setup_artifacts(db, fixture.registry, fixture.owners["workspace"], fixture.vm, "shell")
    assert [item.origin.entry for item in project.items()] == ["vm-0", "workspace-0"]


@pytest.mark.parametrize("condition", ["missing", "incomplete", "stale", "legacy"])
def test_active_results_must_be_current_and_complete(db, condition):
    fixture = graph(db, active=("agent",))
    if condition != "missing":
        record = fixture.save("agent", complete=condition != "incomplete")
        if condition in ("stale", "legacy"):
            changed = record.model_copy(update={"artifact_inputs": () if condition == "stale" else None})
            write_native_setup(db, "agent", "agent", NativeSetupState(records=(changed,)), operation="fixture")
    with pytest.raises(StateError):
        fixture.route()
    view = inspect_owner_artifacts(db, fixture.registry, fixture.owners["agent"], "shell")
    assert view.status == ("missing" if condition == "legacy" else condition)
    assert deferred_inputs(view, "session") is None


def test_owner_declaration_change_is_not_hidden_by_old_capture(db):
    fixture = graph(db)
    owner = replace(fixture.owners["agent"], artifacts=ArtifactsConfig())
    view = inspect_owner_artifacts(db, fixture.registry, owner, "shell")
    assert view.status == "stale"
    assert view.captured is not None and view.captured.inputs
    assert view.prepared is None
    with pytest.raises(StateError):
        setup_artifacts(db, fixture.registry, owner, fixture.vm, "shell")


def test_missing_capture_is_unknown_and_empty_legacy_owner_is_empty(db):
    fixture = graph(db)
    db.insert_agent("other", "vm", "other")
    owner = replace(fixture.owners["agent"], name="other")
    view = inspect_owner_artifacts(db, fixture.registry, owner, "shell")
    assert view.status == "missing" and view.captured is None
    empty = inspect_owner_artifacts(db, fixture.registry, replace(owner, artifacts=ArtifactsConfig()), "shell")
    assert empty.status == "inactive" and not empty.prepared


def test_removed_activation_with_owned_artifacts_requires_retirement(db):
    fixture = graph(db)
    item = tuple(fixture.captures["agent"].inputs.items())[0]
    file = OwnedArtifactFile(path="/home/worker/.agents/rule.md", sha256="a" * 64, origins=(item.origin_identity,))
    fixture.save("agent", files=(file,))
    view = inspect_owner_artifacts(db, fixture.registry, fixture.owners["agent"], "shell")
    assert view.status == "retirement"
    with pytest.raises(StateError):
        fixture.route()


def test_unrelated_plugin_claims_do_not_block_passthrough(db):
    fixture = graph(db)
    record = fixture.save("agent")
    plugin = NativeClaim(role="plugin", identifier="one", destination="native")
    changed = record.model_copy(update={"claims": (plugin,), "complete": False, "pending_cleanup": True})
    write_native_setup(db, "agent", "agent", NativeSetupState(records=(changed,)), operation="fixture")
    assert sum(1 for _ in fixture.route().inputs.items()) == 3


def test_unavailable_parent_route_does_not_manufacture_current_empty_input(db):
    fixture = graph(db, active=("agent",))
    fixture.save("agent")
    view = inspect_owner_artifacts(db, fixture.registry, fixture.owners["agent"], "shell", inherited=None)
    assert view.status == "unavailable" and view.prepared is None


def test_workspace_only_vm_change_does_not_stale_user_branch(db):
    fixture = graph(db, active=("vm", "agent"), counts={"vm": 2})
    fixture.save("vm", routes={"vm-0": "user", "vm-1": "workspace"})
    first, second = fixture.captures["vm"].inputs.items()
    fixture.save("agent", inherited=group(first))
    changed = replace(second, content=replace(second.content, text="changed workspace-only input"))
    previous = fixture.captures["vm"]
    current = replace(previous, inputs=group(first, changed))
    fixture.captures["vm"] = current
    write_capture(db, "vm", "vm", "vm", current, operation="fixture")
    fixture.save("vm", routes={"vm-0": "user", "vm-1": "workspace"})
    result = fixture.route()
    assert [item.origin.entry for item in result.inputs.items()] == ["vm-1", "workspace-0"]
    assert tuple(result.inputs.items())[0].content.text == "changed workspace-only input"


def test_config_and_env_freshness_still_apply(db):
    fixture = graph(db, active=("agent",))
    fixture.save("agent")
    changed = replace(fixture.owners["agent"], target=SecretTarget(vm={}, agent={"A": EnvEntry.model_validate("new")}))
    view = inspect_owner_artifacts(db, fixture.registry, changed, "shell")
    assert view.status == "stale"


@pytest.mark.parametrize("active_vm", [False, True])
def test_independent_consumers_do_not_discharge_other_users(db, active_vm):
    fixture = graph(db, active=("vm", "agent") if active_vm else ("agent",))
    if active_vm:
        fixture.save("vm", routes={"vm-0": "user"})
    fixture.save("agent", inherited=fixture.captures["vm"].inputs)
    first = fixture.route()
    assert [item.origin.component for item in first.inputs.items()] == ["workspace"]
    db.insert_agent("other", "vm", "other", template="agent")
    other = capture_owner(fixture.registry, "agent", "other", "agent", fixture.owners["agent"].artifacts)
    write_capture(db, "agent", "other", "agent", other, operation="fixture")
    assert (
        tuple(other.inputs.items())[0].origin_identity
        != tuple(fixture.captures["agent"].inputs.items())[0].origin_identity
    )
    with pytest.raises(StateError):
        fixture.route(user="other")
    fixture.owners["other"] = replace(fixture.owners["agent"], name="other")
    fixture.captures["other"] = other
    fixture.save("other", inherited=fixture.captures["vm"].inputs)
    assert fixture.route(user="other").inputs == first.inputs
    assert fixture.route() == first


def test_actual_lineage_conflicts_are_rejected(db):
    fixture = graph(db)
    other_vm = db.insert_vm("other", "site", "other")
    db.insert_agent("foreign", other_vm.name, "foreign", template="agent")
    with pytest.raises(StateError):
        fixture.route(user="foreign")
    with pytest.raises(StateError):
        session_artifacts(
            db,
            fixture.registry,
            other_vm,
            fixture.workspace,
            None,
            "shell",
            ArtifactGroup(ArtifactOwner("session", "session", "s1")),
        )


def test_duplicate_input_paths_are_refused(db):
    fixture = graph(db)
    with pytest.raises(ValueError):
        inspect_owner_artifacts(
            db,
            fixture.registry,
            fixture.owners["agent"],
            "shell",
            inherited={fixture.captures["agent"].inputs.owner: fixture.captures["agent"].inputs},
        )


def test_illegal_persisted_route_is_not_reused(db):
    fixture = graph(db, active=("agent",))
    fixture.save("agent", routes={"agent-0": "workspace"})
    view = inspect_owner_artifacts(db, fixture.registry, fixture.owners["agent"], "shell")
    assert view.status == "unavailable"
    with pytest.raises(StateError):
        fixture.route()


def test_session_projection_shares_canonical_diamond_order(db):
    from agentworks.artifacts.routing import session_inherited_inputs
    from agentworks.db import SessionMode

    fixture = graph(db, active=("vm",), counts={"vm": 3})
    fixture.save("vm", routes={"vm-0": "user", "vm-1": "session", "vm-2": "workspace"})
    vm = inspect_owner_artifacts(db, fixture.registry, fixture.owners["vm"], "shell")
    user = inspect_owner_artifacts(
        db, fixture.registry, fixture.owners["agent"], "shell", inherited=deferred_inputs(vm, "user")
    )
    workspace = inspect_owner_artifacts(
        db, fixture.registry, fixture.owners["workspace"], "shell", inherited=deferred_inputs(vm, "workspace")
    )
    inherited = session_inherited_inputs(vm, user, workspace)
    assert inherited == fixture.route().inputs.deferred
    db.insert_session(
        "session",
        "workspace",
        "session",
        SessionMode.AGENT,
        agent_name="agent",
        socket_path="/run/user/1000/session.socket",
    )
    local = capture_owner(fixture.registry, "session", "session", "session", fixture.owners["agent"].artifacts)
    write_capture(db, "session", "session", "session", local, operation="fixture")
    inputs = SetupInputs(
        "session",
        "session",
        "session",
        {"shell": CapabilityConfig.model_validate({})},
        SecretTarget(vm={}, agent={}, workspace={}, session={}),
        artifacts=fixture.owners["agent"].artifacts,
    )
    assert inherited is not None
    record = SetupRecord(
        component="session",
        integration="shell",
        destination_id="d" * 64,
        declaration=inputs.declaration("shell", CapabilityConfig()),
        complete=True,
        artifact_inputs=tuple(item.identity for item in ArtifactInputs(local=local.inputs, deferred=inherited).items()),
    )
    write_native_setup(db, "session", "session", NativeSetupState(records=(record,)), operation="fixture")
    result = inspect_owner_artifacts(db, fixture.registry, inputs, "shell", inherited=inherited)
    assert result.status == "current"
    assert result.prepared == ArtifactInputs(local=local.inputs, deferred=inherited)
    changed = replace(
        inputs, target=SecretTarget(vm={}, agent={}, workspace={}, session={"NEW": EnvEntry.model_validate("value")})
    )
    assert inspect_owner_artifacts(db, fixture.registry, changed, "shell", inherited=inherited).status == "stale"


def test_capture_projection_and_routing_do_not_mutate_or_acquire(db, monkeypatch):
    from agentworks.package_sources import PackageCapture

    fixture = graph(db)

    def forbidden(*args, **kwargs):
        raise AssertionError("read-only inspection attempted mutation or source acquisition")

    monkeypatch.setattr(PackageCapture, "capture", forbidden)
    monkeypatch.setattr(type(db.instance_state), "replace_applied_slices", forbidden)
    result = fixture.route()
    assert sum(1 for _ in result.inputs.items()) == 3
    view = inspect_owner_artifacts(db, fixture.registry, fixture.owners["agent"], "shell")
    assert view.status == "inactive"


def test_artifact_free_active_facets_retain_existing_plugin_readiness_semantics(db):
    fixture = graph(db, active=("vm", "agent", "workspace"), counts={"vm": 0, "agent": 0, "workspace": 0})
    result = fixture.route()
    assert not result.inputs
    assert result.active_facets == ()


def test_upstream_owned_files_survive_handled_payload_elision(db):
    fixture = graph(db, active=("agent",))
    item = tuple(fixture.captures["agent"].inputs.items())[0]
    file = OwnedArtifactFile(
        path="/home/worker/.agents/skills/review/SKILL.md",
        sha256="a" * 64,
        origins=(item.origin_identity,),
        native_identity="skill:review",
    )
    fixture.save("agent", inherited=fixture.captures["vm"].inputs, files=(file,))
    result = fixture.route()
    assert item.identity not in {value.identity for value in result.inputs.items()}
    assert result.ancestor_files == (file,)
    assert result.active_facets == ("user",)


def test_missing_parent_route_remains_unknown_at_session_join(db):
    from agentworks.artifacts.routing import session_inherited_inputs

    fixture = graph(db, active=("vm",))
    vm = inspect_owner_artifacts(db, fixture.registry, fixture.owners["vm"], "shell")
    user = inspect_owner_artifacts(db, fixture.registry, fixture.owners["agent"], "shell", inherited=None)
    workspace = inspect_owner_artifacts(db, fixture.registry, fixture.owners["workspace"], "shell", inherited=None)
    assert session_inherited_inputs(vm, user, workspace) is None


def test_removed_bundle_declaration_remains_inspectable(db):
    fixture = graph(db)
    owner = replace(fixture.owners["agent"], artifacts=ArtifactsConfig(bundles=["removed"]))
    view = inspect_owner_artifacts(db, fixture.registry, owner, "shell")
    assert view.status == "unavailable"
    assert view.captured is not None and view.captured.inputs
    assert view.prepared is None


def test_provenance_only_capture_refresh_uses_current_winner_with_retained_handling(db):
    from agentworks.artifacts.inspection import _artifact_metadata

    fixture = graph(db, active=("vm",))
    previous = fixture.captures["vm"]
    item = next(previous.inputs.items())
    record = fixture.save("vm", routes={item.content.name: "session"})
    fixture.registry.add(
        "artifact-bundle",
        "replacement",
        ArtifactBundle(name="replacement", hints={item.content.name: HintArtifactSpec(text=item.content.text)}),
        Origin.built_in(source="replacement-fixture"),
    )
    config = ArtifactsConfig(bundles=["replacement"])
    fixture.owners["vm"] = replace(fixture.owners["vm"], artifacts=config)
    refreshed = capture_owner(fixture.registry, "vm", "vm", "vm", config)
    newer = next(refreshed.inputs.items())
    assert newer.identity == item.identity
    write_capture(db, "vm", "vm", "vm", refreshed, operation="refresh")
    view = inspect_owner_artifacts(db, fixture.registry, fixture.owners["vm"], "shell")
    assert view.status == "current" and view.record == record
    assert view.prepared is not None and next(view.prepared.items()) == newer
    rows = _artifact_metadata(fixture.registry, config, view)
    assert rows[0].bundle == "replacement" and rows[0].declared
    assert read_native_setup(db, "vm", "vm").records == (record,)


def test_database_round_trip_retains_map_order_and_current_routing(db):
    from agentworks.artifacts.state import read_captures

    fixture = graph(db, active=("vm",))
    fixture.registry.add(
        "artifact-bundle",
        "ordered",
        ArtifactBundle(
            name="ordered", hints={"zebra": HintArtifactSpec(text="zebra"), "alpha": HintArtifactSpec(text="alpha")}
        ),
        Origin.built_in(source="ordered-fixture"),
    )
    config = ArtifactsConfig(bundles=["ordered"])
    fixture.owners["vm"] = replace(fixture.owners["vm"], artifacts=config)
    capture = capture_owner(fixture.registry, "vm", "vm", "vm", config)
    fixture.captures["vm"] = capture
    write_capture(db, "vm", "vm", "vm", capture, operation="setup")
    record = fixture.save("vm", routes={"zebra": "session", "alpha": "session"})
    loaded = read_captures(db, "vm", "vm")["vm"]
    assert list(loaded.inputs.hints) == ["zebra", "alpha"]
    view = inspect_owner_artifacts(db, fixture.registry, fixture.owners["vm"], "shell")
    assert view.status == "current" and view.record == record
    assert view.prepared is not None
    assert [item.content.name for item in view.prepared.items()] == ["zebra", "alpha"]
    deferred = deferred_inputs(view, "session")
    assert deferred is not None
    assert list(deferred[capture.inputs.owner].hints) == ["zebra", "alpha"]


def test_skipped_reapplication_keeps_cleanup_evidence_without_ancestor_delivery(db):
    from agentworks.artifacts.application import ArtifactSkip

    fixture = graph(db, active=("agent",))
    item = tuple(fixture.captures["agent"].inputs.items())[0]
    prior = OwnedArtifactFile(
        path="/home/worker/AGENTS.md",
        sha256="a" * 64,
        origins=(item.origin_identity,),
        generated_section=True,
    )
    record = fixture.save("agent", inherited=fixture.captures["vm"].inputs, files=(prior,))
    skipped = ArtifactSkip(path=prior.path, origins=prior.origins, reason="partial section")
    state = replace_setup_record(
        read_native_setup(db, "agent", "agent"), record.model_copy(update={"skipped": (skipped,)})
    )
    write_native_setup(db, "agent", "agent", state, operation="fixture")
    result = fixture.route()
    assert result.ancestor_files == ()
    assert item.identity not in {value.identity for value in result.inputs.items()}
    assert read_native_setup(db, "agent", "agent").records[0].artifact_files == (prior,)
