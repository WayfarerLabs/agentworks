"""Session preparation, private publication, and lifecycle-owned cleanup."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from agentworks.artifacts.application import ArtifactApplication, OwnedArtifactFile, SessionArtifactContext
from agentworks.artifacts.publication import publish_artifacts, validate_application
from agentworks.artifacts.reporting import report_application
from agentworks.artifacts.state import CapturedArtifacts, capture_owner, write_capture
from agentworks.errors import StateError
from agentworks.harness_setup.inputs import SetupInputs
from agentworks.harness_setup.model import NativeSetupState, SetupRecord
from agentworks.harness_setup.state import read_native_setup, replace_setup_record, write_native_setup
from agentworks.native_files import native_path
from agentworks.schema import CapabilityConfig

if TYPE_CHECKING:
    from agentworks.db import Database, SessionRow, VMRow, WorkspaceRow
    from agentworks.resources.registry import Registry
    from agentworks.secrets.orchestration import SecretTarget
    from agentworks.sessions.templates import ResolvedSessionTemplate
    from agentworks.transports import Transport


@dataclass(frozen=True)
class PreparedSessionArtifacts:
    context: SessionArtifactContext
    capture: CapturedArtifacts
    setup_inputs: SetupInputs


def prepare_session_artifacts(
    db: Database,
    registry: Registry,
    *,
    name: str,
    template: ResolvedSessionTemplate,
    vm: VMRow,
    workspace: WorkspaceRow,
    agent_name: str | None,
    runner: Transport,
    environment: dict[str, str],
    secret_target: SecretTarget,
    linux_user: str,
    session_uuid: str | None = None,
) -> PreparedSessionArtifacts:
    """Capture local inputs and validate ancestor results before runtime teardown."""
    from agentworks.artifacts.routing import session_artifacts
    from agentworks.harness_setup.readiness import _applicable_evidence

    capture = capture_owner(registry, "session", name, "session", template.artifacts)
    routed = session_artifacts(db, registry, vm, workspace, agent_name, template.harness_integration, capture.inputs)
    for facet in routed.active_facets:
        if facet == "session":
            continue
        evidence = _applicable_evidence(
            db,
            registry,
            template.harness_integration,
            facet,
            vm=vm,
            workspace=workspace,
            agent_name=agent_name,
            runner=runner,
        )
        if not evidence.current:
            raise StateError("ancestor artifact placement is not current", hint=evidence.remediation)
    # The actual launch user's home is an identity, never a workspace-derived path.
    home = f"/home/{linux_user}"
    prior = read_native_setup(db, "session", name)
    if routed.inputs or routed.ancestor_files or any(item.artifact_files for item in prior.records):
        result = runner.run('printf "%s" "$HOME"', check=False)
        if not result.ok:
            raise StateError("could not determine the session artifact home")
        home = native_path(result.stdout.strip())
    identity = session_uuid or str(uuid4())
    run_id = str(uuid4())
    context = SessionArtifactContext(
        inputs=routed.inputs,
        home=home,
        directory=f"{home}/.agentworks-artifacts/session/{identity}/{run_id}",
        session_uuid=identity,
        run_id=run_id,
        ancestor_files=routed.ancestor_files,
        environment=dict(environment),
    )
    setup_inputs = SetupInputs(
        "session",
        name,
        "session",
        {template.harness_integration: CapabilityConfig.model_validate(template.harness_integration_config)},
        secret_target,
        template.artifacts,
        capture,
    )
    return PreparedSessionArtifacts(context, capture, setup_inputs)


def validate_session_application(
    application: object, context: SessionArtifactContext, *, integration: str
) -> ArtifactApplication:
    result = validate_application(application, context.inputs, "session", integration=integration)
    if result.artifacts_dir is not None and result.artifacts_dir != context.directory:
        raise StateError("session artifact directory must identify its private run directory")
    ancestors = {item.path.casefold() for item in context.ancestor_files}
    if any(item.path.casefold() in ancestors for item in result.files):
        raise StateError("session artifacts conflict with an ancestor artifact destination")
    if any(not item.path.startswith(context.directory + "/") for item in result.files):
        raise StateError("session artifact publication must use its private run directory")
    report_application(
        context.inputs, integration=integration, owner="session facet", deferred=result.deferred, terminal=True
    )
    return result


def stage_session_artifacts(
    db: Database,
    name: str,
    integration: str,
    runner: Transport,
    prepared: PreparedSessionArtifacts,
    application: ArtifactApplication,
) -> None:
    """Publish the prospective run while retaining files the old runtime can use."""
    context = prepared.context
    state = read_native_setup(db, "session", name)
    if not context.inputs and not application.files and not state.records:
        return
    previous = next((item for item in state.records if item.integration == integration), None)
    retained = () if previous is None else previous.artifact_files
    current = SetupRecord(
        component="session",
        integration=integration,
        destination_id=hashlib.sha256(context.directory.encode()).hexdigest(),
        declaration=prepared.setup_inputs.declaration(integration, prepared.setup_inputs.activations[integration]),
        artifact_inputs=tuple(item.identity for item in context.inputs.items()),
        artifact_files=retained,
        deferred=application.deferred,
    )

    def checkpoint(files: tuple[OwnedArtifactFile, ...]) -> None:
        nonlocal state, current
        current = current.model_copy(update={"artifact_files": (*retained, *files)})
        state = replace_setup_record(state, current)
        write_native_setup(db, "session", name, state, operation="session-prepare")

    checkpoint(())
    published = publish_artifacts(runner, application.files, (), checkpoint, roots=(context.directory,))
    report_application(
        context.inputs,
        integration=integration,
        owner=f"session '{name}'",
        files=published.files,
        skipped=published.skipped,
    )


def commit_session_artifacts(
    db: Database,
    name: str,
    integration: str,
    runner: Transport,
    prepared: PreparedSessionArtifacts,
) -> None:
    """After old runtime teardown, retire obsolete runs and record the new inputs."""
    context = prepared.context
    state = read_native_setup(db, "session", name)
    for record in tuple(state.records):
        keep = tuple(item for item in record.artifact_files if item.path.startswith(context.directory + "/"))
        obsolete = tuple(item for item in record.artifact_files if item not in keep)

        def checkpoint(
            files: tuple[OwnedArtifactFile, ...],
            record: SetupRecord = record,
            keep: tuple[OwnedArtifactFile, ...] = keep,
        ) -> None:
            nonlocal state
            updated = record.model_copy(update={"artifact_files": (*keep, *files)})
            state = replace_setup_record(state, updated)
            write_native_setup(db, "session", name, state, operation="session-start")

        remaining = publish_artifacts(
            runner,
            (),
            obsolete,
            checkpoint,
            roots=(f"{context.home}/.agentworks-artifacts/session/{context.session_uuid}",),
        )
        updated = record.model_copy(
            update={
                "artifact_files": (*keep, *remaining.files),
                "complete": record.integration == integration,
                "pending_cleanup": record.integration != integration and bool(remaining.files),
            }
        )
        state = replace_setup_record(state, updated)
    state = NativeSetupState(
        records=tuple(item for item in state.records if item.integration == integration or item.artifact_files)
    )
    if state.records:
        write_native_setup(db, "session", name, state, operation="session-start")
    if prepared.setup_inputs.artifacts.bundles or state.records:
        write_capture(db, "session", name, "session", prepared.capture, operation="session-start")
    db.set_session_run_id(name, context.run_id)


def cleanup_session_artifacts(db: Database, session: SessionRow, runner: Transport) -> None:
    """Remove known private files before dropping their ownership evidence."""
    state = read_native_setup(db, "session", session.name)
    if not any(item.artifact_files for item in state.records):
        return
    home = native_path(runner.run('printf "%s" "$HOME"').stdout.strip())
    for record in tuple(state.records):

        def checkpoint(files: tuple[OwnedArtifactFile, ...], record: SetupRecord = record) -> None:
            nonlocal state
            state = replace_setup_record(state, record.model_copy(update={"artifact_files": files}))
            write_native_setup(db, "session", session.name, state, operation="session-delete")

        remaining = publish_artifacts(
            runner,
            (),
            record.artifact_files,
            checkpoint,
            roots=(f"{home}/.agentworks-artifacts/session/{session.session_uuid}",),
        )
        if remaining.files:
            raise StateError("modified session artifacts remain; inspect them before deleting their ownership record")
