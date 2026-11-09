"""Ordered setup dispatch with synchronous instance-state acknowledgements."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Literal, cast

from pydantic import ValidationError

from agentworks import output
from agentworks.artifacts.application import ArtifactDeferral
from agentworks.artifacts.model import ArtifactInputs
from agentworks.artifacts.publication import ROOT_FILE_DIRECTORIES, publish_artifacts, validate_application
from agentworks.artifacts.reporting import report_applied, report_deferrals
from agentworks.artifacts.state import read_captures, write_capture
from agentworks.capabilities.harness_integration import ensure_harness_integration_enabled, harness_integration_for
from agentworks.capabilities.harness_integration.setup import (
    SetupInvocation,
    UserSetupInvocation,
    VMSetupInvocation,
    WorkspaceSetupInvocation,
)
from agentworks.errors import AgentworksError, ConfigError, ConnectivityError, ExternalError, StateError
from agentworks.execution.requests import Command
from agentworks.harness_setup.locking import native_mutation_guard
from agentworks.harness_setup.model import NativeClaim, NativeSetupState, SetupRecord
from agentworks.harness_setup.outcomes import (
    SetupFailure,
    sanitize_setup_diagnostic,
    sanitize_setup_error,
)
from agentworks.harness_setup.state import read_native_setup, replace_setup_record, write_native_setup

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.artifacts.application import OwnedArtifactFile
    from agentworks.db import Database, VMRow
    from agentworks.execution.access import VMAccess
    from agentworks.harness_setup.inputs import SetupInputs
    from agentworks.harness_setup.locking import NativeMutationGuard
    from agentworks.resources.registry import Registry


class _SetupInfrastructureFailure(Exception):
    """Keep callback infrastructure failures outside the facet recovery boundary."""

    def __init__(self, original: Exception) -> None:
        self.original = original
        super().__init__()


def _sanitize(invocation: SetupInvocation, text: str) -> str:
    """Redact diagnostics before they enter persisted state or the failure list."""
    return sanitize_setup_diagnostic(text, redactions=invocation.redactions)


def destination_id(vm: VMRow, access: VMAccess, *, location: str | None = None, username: str | None = None) -> str:
    """Fingerprint actual native placement and its owning VM generation.

    Native filesystem identity distinguishes a recreated user home or workspace.
    This contains no env or resolved secret values and performs only probes.
    """

    def observe(argv: tuple[str, ...]) -> str:
        try:
            result = access.run(Command(argv, deadline=15))
            status = result.require_completed(access.vm_name)
        except AgentworksError as error:
            sanitize_setup_error(error)
            raise
        value = result.stdout.decode("utf-8", "replace").strip() if status == 0 else ""
        if not value or "\n" in value:
            raise StateError(
                "native setup could not establish the destination identity",
                hint="Check the VM identity file and the destination directory, then retry this operation.",
            )
        return value

    machine = observe(("cat", "/etc/machine-id"))
    directory = None
    if location is not None:
        directory = observe(("stat", "-Lc", "%d:%i:%u", "--", location))
    payload = (vm.name, vm.site, vm.created_at, vm.platform_metadata, machine, username, location, directory)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def run_setup(
    db: Database,
    registry: Registry,
    inputs: SetupInputs,
    invocation: SetupInvocation,
    *,
    operation: str,
    failures: list[SetupFailure],
    buffered: bool = False,
    held: NativeMutationGuard | None = None,
    log_warning: Callable[[str], None] | None = None,
) -> NativeSetupState:
    """Run desired integration activations, then retire removed activations in prior order.

    ``log_warning`` writes to the operation log, which records each failed
    integration's reason.

    A fresh owner's caller buffers and commits this result with its owner row.
    It must already have refused native residue and own failed-create rollback.
    Existing owners checkpoint confirmed ownership changes before another can run.
    Recoverable integration failures retain that prefix and join the command's
    failure collection; shared infrastructure failures and cancellation abort.
    """
    from agentworks.artifacts.routing import RoutingResult, inactive_destination, setup_artifacts

    expected = {"vm": VMSetupInvocation, "user": UserSetupInvocation, "workspace": WorkspaceSetupInvocation}
    if not isinstance(invocation, expected[inputs.facet]):
        raise StateError("native setup invocation does not match its facet")
    facet = cast('Literal["vm", "user", "workspace"]', inputs.facet)
    remediation = (
        f"Inspect workspace '{inputs.name}' before deciding whether to recreate it; no workspace reinit is available."
        if inputs.component == "workspace"
        else f"Run 'agw {'agent' if inputs.component == 'agent' else 'vm'} reinit {inputs.name}'."
    )
    with native_mutation_guard(db.path, invocation.vm.name, held=held):
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

        state = read_native_setup(db, inputs.kind, inputs.name)
        previous_capture = read_captures(db, inputs.kind, inputs.name).get(inputs.component)
        local_origins = {
            item.origin_identity
            for capture in (previous_capture, inputs.artifact_snapshot)
            if capture is not None
            for item in capture.inputs.items()
        }
        if inputs.artifact_snapshot is not None and not buffered:
            write_capture(db, inputs.kind, inputs.name, inputs.component, inputs.artifact_snapshot, operation=operation)
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
            invocation.access,
            location=location,
            username=invocation.username if isinstance(invocation, UserSetupInvocation) else None,
        )
        desired = inputs.activations

        def persist(value: NativeSetupState) -> None:
            if not buffered:
                write_native_setup(db, inputs.kind, inputs.name, value, operation=operation)

        def checkpoint_state(value: NativeSetupState) -> None:
            try:
                persist(value)
            except Exception as error:
                raise _SetupInfrastructureFailure(error) from error

        for name in (*desired, *(name for name in prior if name not in desired)):
            previous = prior.get(name)
            block = desired.get(name)
            if (
                block is None
                and previous is not None
                and not previous.claims
                and not previous.artifact_files
                and not previous.pending_cleanup
                and not previous.incomplete_reasons
            ):
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
                if not integration.implements_setup(facet) and not previous.claims and not previous.artifact_files:
                    state = NativeSetupState(
                        records=tuple(
                            record
                            for record in state.records
                            if (record.component, record.integration) != (inputs.component, name)
                        )
                    )
                    persist(state)
                    continue
                declaration = previous.declaration
            else:
                integration = bound[name]
                declaration = inputs.declaration(name, block)

            routing = (
                RoutingResult(ArtifactInputs())
                if block is None
                else setup_artifacts(db, registry, inputs, invocation.vm, name)
            )
            artifacts = routing.inputs

            current = SetupRecord(
                component=inputs.component,
                integration=name,
                destination_id=destination,
                declaration=declaration,
                artifact_inputs=None if previous is None else previous.artifact_inputs,
                deferred=() if previous is None else previous.deferred,
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
                checkpoint_state(state)

            def checkpoint_files(files: tuple[OwnedArtifactFile, ...]) -> None:
                nonlocal current, state
                current = current.model_copy(update={"artifact_files": files})
                state = replace_setup_record(state, current)
                checkpoint_state(state)

            def report_incomplete(reason: str, *, integration_name: str = name) -> None:
                nonlocal current, state
                if not isinstance(reason, str) or not reason.strip():
                    raise StateError("integration returned an invalid incomplete-setup reason")
                try:
                    reason = _sanitize(invocation, reason) or "Integration reported incomplete setup."
                except Exception as error:
                    raise _SetupInfrastructureFailure(error) from error
                if reason not in current.incomplete_reasons:
                    current = current.model_copy(update={"incomplete_reasons": (*current.incomplete_reasons, reason)})
                    state = replace_setup_record(state, current)
                    checkpoint_state(state)
                try:
                    output.warn(
                        f"Native setup for {inputs.component}/{inputs.name} via {integration_name}: "
                        f"{reason.rstrip('.')}. "
                        f"Resolve the reported conflict before retrying. {remediation}"
                    )
                except Exception as error:
                    raise _SetupInfrastructureFailure(error) from error

            scoped_secrets = {ref.name: invocation.secrets[ref.name] for ref in integration.config_secret_refs()}
            call = replace(
                invocation,
                prior=previous,
                checkpoint=checkpoint,
                report_incomplete=report_incomplete,
                secrets=scoped_secrets,
                artifacts=artifacts,
            )
            try:
                if isinstance(call, VMSetupInvocation):
                    application = integration.vm_init(call)
                elif isinstance(call, UserSetupInvocation):
                    application = integration.user_init(call)
                elif isinstance(call, WorkspaceSetupInvocation):
                    application = integration.workspace_init(call)
                application = validate_application(application, artifacts, inputs.facet, integration=name)
                roots = ROOT_FILE_DIRECTORIES if location is None else (location,)
                publication = publish_artifacts(
                    call.files,
                    application.files,
                    current.artifact_files,
                    checkpoint_files,
                    roots=roots,
                    group=call.linux_group if isinstance(call, WorkspaceSetupInvocation) else "",
                    root=isinstance(call, VMSetupInvocation),
                    retain_origins=frozenset(
                        origin
                        for file in current.artifact_files
                        for origin in file.origins
                        if origin not in local_origins
                    )
                    if routing.retain_previous
                    else frozenset(),
                )
            except _SetupInfrastructureFailure as error:
                raise error.original from None
            except (ConfigError, StateError, ExternalError, ConnectivityError) as error:
                reason = _sanitize(invocation, str(error)).strip() or type(error).__name__
                hint = _sanitize(invocation, f"{error.hint} {remediation}" if error.hint else remediation)
                current = current.model_copy(
                    update={
                        "complete": False,
                        "pending_cleanup": block is None,
                        "incomplete_reasons": (*current.incomplete_reasons, reason),
                    }
                )
                state = replace_setup_record(state, current)
                persist(state)
                failures.append(SetupFailure(inputs.kind, inputs.name, facet, name, reason, hint))
                if log_warning is not None:
                    log_warning(
                        f"Setup failed for {inputs.kind}/{inputs.name} facet={facet} integration={name}: {reason}"
                    )
                continue
            owned = publication.files
            artifact_inputs = tuple(item.identity for item in artifacts.items())
            current_ids = set(artifact_inputs)
            deferred_by_id: dict[str, ArtifactDeferral] = {}
            if routing.retain_previous and previous is not None:
                retained_ids = routing.unresolved_routes or frozenset(item.input_id for item in previous.deferred)
                deferred_by_id.update(
                    (item.input_id, item)
                    for item in previous.deferred
                    if item.input_id in retained_ids and item.input_id not in current_ids
                )
            # These IDs belong to recorded upstream routes, not to readable
            # integration inputs. Core forwards their metadata only after the
            # integration's known-input application has been validated.
            if routing.unresolved_routes:
                unresolved_reason = _sanitize(
                    invocation,
                    " ".join(routing.unavailable_reasons)
                    or "The recorded upstream input has no readable captured content; repair its owning setup.",
                )
                deferred_by_id.update(
                    (
                        identity,
                        ArtifactDeferral(
                            input_id=identity,
                            destination=inactive_destination(inputs.facet),
                            reason=unresolved_reason,
                        ),
                    )
                    for identity in sorted(routing.unresolved_routes)
                )
            deferred_by_id.update((item.input_id, item) for item in application.deferred)
            deferred = tuple(deferred_by_id.values())
            artifact_inputs = tuple(dict.fromkeys((*artifact_inputs, *deferred_by_id)))
            current = current.model_copy(
                update={
                    "artifact_files": owned,
                    # Acknowledge the known inputs actually supplied to this
                    # invocation. Unknown incoming routing is inspected lazily;
                    # it is not a permanent failure of this owner's local setup.
                    "artifact_inputs": artifact_inputs,
                    "deferred": deferred,
                    "skipped": publication.skipped,
                }
            )
            if block is None:
                if current.claims or current.artifact_files or current.incomplete_reasons:
                    current = current.model_copy(update={"pending_cleanup": True})
                    state = replace_setup_record(state, current)
                    output.warn(f"Native cleanup for {name} remains pending; read diagnostics before retrying setup.")
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
                current = current.model_copy(
                    update={
                        "complete": not pending_files and not current.incomplete_reasons and not publication.skipped,
                        "pending_cleanup": pending_files,
                    }
                )
                state = replace_setup_record(state, current)
            persist(state)
            if block is not None and not pending_files:
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
