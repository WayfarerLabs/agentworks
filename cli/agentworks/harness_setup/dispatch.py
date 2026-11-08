"""Ordered setup dispatch with synchronous instance-state acknowledgements."""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import replace
from typing import TYPE_CHECKING, Literal, cast

from pydantic import ValidationError

from agentworks import output
from agentworks.artifacts.application import ArtifactDeferral
from agentworks.artifacts.model import ArtifactInputs
from agentworks.artifacts.publication import publish_artifacts, validate_application
from agentworks.artifacts.reporting import report_applied, report_deferrals
from agentworks.artifacts.state import write_capture
from agentworks.capabilities.harness_integration import ensure_harness_integration_enabled, harness_integration_for
from agentworks.capabilities.harness_integration.setup import (
    SetupInvocation,
    UserSetupInvocation,
    VMSetupInvocation,
    WorkspaceSetupInvocation,
)
from agentworks.errors import ConfigError, StateError
from agentworks.harness_setup.locking import native_mutation_guard
from agentworks.harness_setup.model import NativeClaim, NativeSetupState, SetupRecord
from agentworks.harness_setup.state import read_native_setup, replace_setup_record, write_native_setup
from agentworks.native_files import ROOT_FILE_DIRECTORIES

if TYPE_CHECKING:
    from agentworks.artifacts.application import OwnedArtifactFile
    from agentworks.db import Database, VMRow
    from agentworks.harness_setup.inputs import SetupInputs
    from agentworks.harness_setup.locking import NativeMutationGuard
    from agentworks.resources.registry import Registry
    from agentworks.transports import Transport


def destination_id(vm: VMRow, runner: Transport, *, location: str | None = None, username: str | None = None) -> str:
    """Fingerprint actual native placement and its owning VM generation.

    Native filesystem identity distinguishes a recreated user home or workspace.
    This contains no env or resolved secret values and performs only probes.
    """
    machine = runner.run("cat /etc/machine-id", timeout=15).stdout.strip()
    if not machine:
        raise StateError("native setup could not establish the VM identity")
    directory = None
    if location is not None:
        directory = runner.run(f"stat -Lc '%d:%i:%u' -- {shlex.quote(location)}", timeout=15).stdout.strip()
        if not directory:
            raise StateError("native setup could not establish the destination identity")
    payload = (vm.name, vm.site, vm.created_at, vm.platform_metadata, machine, username, location, directory)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def run_setup(
    db: Database,
    registry: Registry,
    inputs: SetupInputs,
    invocation: SetupInvocation,
    *,
    operation: str,
    buffered: bool = False,
    held: NativeMutationGuard | None = None,
) -> NativeSetupState:
    """Run desired integration activations, then retire removed activations in prior order.

    A fresh owner's caller buffers and commits this result with its owner row.
    It must already have refused native residue and own failed-create rollback.
    Existing owners checkpoint confirmed ownership changes before another can run.
    """
    from agentworks.artifacts.routing import inactive_destination, setup_artifacts

    expected = {"vm": VMSetupInvocation, "user": UserSetupInvocation, "workspace": WorkspaceSetupInvocation}
    if not isinstance(invocation, expected[inputs.facet]):
        raise StateError("native setup invocation does not match its facet")
    facet = cast('Literal["vm", "user", "workspace"]', inputs.facet)
    with native_mutation_guard(db.path, invocation.vm.name, held=held):
        if inputs.artifact_snapshot is not None and not buffered:
            write_capture(db, inputs.kind, inputs.name, inputs.component, inputs.artifact_snapshot, operation=operation)
        state = read_native_setup(db, inputs.kind, inputs.name)
        prior = {record.integration: record for record in state.records if record.component == inputs.component}
        fallback: tuple[ArtifactDeferral, ...] = ()
        if not inputs.activations and inputs.artifact_snapshot is not None and inputs.artifact_snapshot.inputs:
            fallback = tuple(
                ArtifactDeferral(
                    input_id=item.identity,
                    destination=inactive_destination(inputs.facet),
                    reason="no activated harness integrations",
                )
                for item in inputs.artifact_snapshot.inputs.items()
            )
        if not inputs.activations and not prior:
            _report_fallback(inputs, fallback)
            return state
        location = (
            invocation.home
            if isinstance(invocation, UserSetupInvocation)
            else invocation.root
            if isinstance(invocation, WorkspaceSetupInvocation)
            else None
        )
        destination = destination_id(
            invocation.vm,
            invocation.runner,
            location=location,
            username=invocation.username if isinstance(invocation, UserSetupInvocation) else None,
        )
        desired = inputs.activations
        # Validate and bind the complete activation map before its first mutation.
        bound = {}
        for name, selected in inputs.activations.items():
            ensure_harness_integration_enabled(registry, name)
            implementation = harness_integration_for(name)
            bound[name] = implementation.for_setup(
                owner_kind=inputs.kind, owner_name=inputs.name, facet=facet, config=selected.config
            )

        for integration in bound.values():
            if any(ref.name not in invocation.secrets for ref in integration.config_secret_refs()):
                raise StateError("native setup received an unresolved config secret")

        def persist(value: NativeSetupState) -> None:
            if not buffered:
                write_native_setup(db, inputs.kind, inputs.name, value, operation=operation)

        for name in (*desired, *(name for name in prior if name not in desired)):
            previous = prior.get(name)
            block = desired.get(name)
            if block is None and previous is not None and not previous.claims and not previous.artifact_files:
                state = NativeSetupState(
                    records=tuple(
                        record
                        for record in state.records
                        if (record.component, record.integration) != (inputs.component, name)
                    )
                )
                persist(state)
                continue
            if block is None:
                assert previous is not None
                try:
                    ensure_harness_integration_enabled(registry, name)
                    integration = harness_integration_for(name).for_setup(
                        owner_kind=inputs.kind, owner_name=inputs.name, facet=facet, config=None
                    )
                except (ConfigError, StateError):
                    pending = previous.model_copy(update={"complete": False, "pending_cleanup": True})
                    state = replace_setup_record(state, pending)
                    persist(state)
                    output.warn(f"Native cleanup for {name} remains pending; enable its integration and retry setup.")
                    continue
                declaration = previous.declaration
            else:
                integration = bound[name]
                declaration = inputs.declaration(name, block)

            artifacts = (
                ArtifactInputs() if block is None else setup_artifacts(db, registry, inputs, invocation.vm, name)
            )

            current = SetupRecord(
                component=inputs.component,
                integration=name,
                destination_id=destination,
                declaration=declaration,
                artifact_inputs=tuple(item.identity for item in artifacts.items()),
                claims=() if previous is None else previous.claims,
                artifact_files=()
                if previous is None or previous.destination_id != destination
                else previous.artifact_files,
            )
            state = replace_setup_record(state, current)
            persist(state)

            def checkpoint(claims: tuple[NativeClaim, ...]) -> None:
                nonlocal current, state
                # Plugin output crosses the registered capability boundary.
                try:
                    current = SetupRecord.model_validate({**current.model_dump(), "claims": claims})
                except ValidationError:
                    raise StateError("integration returned malformed native claim metadata") from None
                state = replace_setup_record(state, current)
                persist(state)

            def checkpoint_files(files: tuple[OwnedArtifactFile, ...]) -> None:
                nonlocal current, state
                current = current.model_copy(update={"artifact_files": files})
                state = replace_setup_record(state, current)
                persist(state)

            scoped_secrets = {ref.name: invocation.secrets[ref.name] for ref in integration.config_secret_refs()}
            call = replace(
                invocation, prior=previous, checkpoint=checkpoint, secrets=scoped_secrets, artifacts=artifacts
            )
            if isinstance(call, VMSetupInvocation):
                application = integration.vm_init(call)
            elif isinstance(call, UserSetupInvocation):
                application = integration.user_init(call)
            elif isinstance(call, WorkspaceSetupInvocation):
                application = integration.workspace_init(call)
            else:
                raise StateError("unknown native setup invocation")
            application = validate_application(application, artifacts, inputs.facet, integration=name)
            roots = ROOT_FILE_DIRECTORIES if location is None else (location,)
            publication = publish_artifacts(
                call.runner,
                application.files,
                current.artifact_files,
                checkpoint_files,
                roots=roots,
                group=call.linux_group if isinstance(call, WorkspaceSetupInvocation) else "",
                root=isinstance(call, VMSetupInvocation),
            )
            owned = publication.files
            current = current.model_copy(
                update={"artifact_files": owned, "deferred": application.deferred, "skipped": publication.skipped}
            )
            if block is None:
                if current.claims or current.artifact_files:
                    current = current.model_copy(update={"pending_cleanup": True})
                    state = replace_setup_record(state, current)
                    output.warn(f"Native cleanup for {name} retains outstanding ownership; retry its owning setup.")
                else:
                    state = NativeSetupState(
                        records=tuple(
                            record
                            for record in state.records
                            if (record.component, record.integration) != (inputs.component, name)
                        )
                    )
            else:
                settled_paths = {file.path for file in application.files} | {item.path for item in publication.skipped}
                pending_files = any(item.path not in settled_paths for item in owned)
                current = current.model_copy(update={"complete": not pending_files, "pending_cleanup": pending_files})
                state = replace_setup_record(state, current)
            persist(state)
            if block is not None and current.complete:
                report_applied(
                    artifacts,
                    integration=name,
                    owner=f"{inputs.component}/{inputs.name}",
                    deferred=application.deferred,
                    skipped=publication.skipped,
                )
                report_deferrals(
                    artifacts,
                    integration=name,
                    owner=f"{inputs.component}/{inputs.name}",
                    deferred=application.deferred,
                )
        if fallback and not any(
            record.pending_cleanup for record in state.records if record.component == inputs.component
        ):
            _report_fallback(inputs, fallback)
        return state


def _report_fallback(inputs: SetupInputs, deferred: tuple[ArtifactDeferral, ...]) -> None:
    if deferred and inputs.artifact_snapshot is not None:
        report_deferrals(
            ArtifactInputs(local=inputs.artifact_snapshot.inputs),
            integration="core fallback",
            owner=f"{inputs.component}/{inputs.name}",
            deferred=deferred,
        )
